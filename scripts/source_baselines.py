#!/usr/bin/env python3
"""Read-only exact-byte observations for explicitly mapped local asset roots."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Any
from urllib.parse import quote

from module_loading import load_source

_LEDGER = load_source("source_baselines_evidence_ledger", str(Path(__file__).with_name("evidence_ledger.py")))
_GIT_OUTPUT_LIMIT = 1024 * 1024
_GIT_BLOB_LIMIT = 64 * 1024 * 1024
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class SourceBaselineError(ValueError):
    pass


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _mapping_snapshot(path: Path) -> tuple[tuple[Path, tuple[int, int]], ...]:
    """Capture and validate every public ancestor that maps to the selected root."""
    absolute = Path(os.path.abspath(path))
    chain: list[Path] = []
    cursor = Path(absolute.anchor)
    chain.append(cursor)
    try:
        for part in absolute.parts[1:]:
            names = os.listdir(cursor)
            matches = [name for name in names if name.casefold() == part.casefold()]
            if matches and matches != [part]:
                raise SourceBaselineError("mapped root has a case-insensitive path collision")
            cursor = cursor / part
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SourceBaselineError("mapped source root traverses a symlink")
            chain.append(cursor)
        final = chain[-1].lstat()
        if not stat.S_ISDIR(final.st_mode):
            raise SourceBaselineError("mapped source root must be a directory")
        return tuple((entry, _directory_identity(entry.lstat())) for entry in chain)
    except SourceBaselineError:
        raise
    except OSError as exc:
        raise SourceBaselineError(f"cannot verify mapped source root: {exc}") from exc


def _absolute_root(root: Path) -> Path:
    requested = Path(root).expanduser()
    if not requested.is_absolute():
        raise SourceBaselineError("mapped source root must be an explicit absolute path")
    absolute = Path(os.path.abspath(requested))
    _mapping_snapshot(absolute)
    return absolute


def _assert_mapping(snapshot: tuple[tuple[Path, tuple[int, int]], ...]) -> None:
    current = _mapping_snapshot(snapshot[-1][0])
    if current != snapshot:
        raise SourceBaselineError("mapped source root or an ancestor changed during observation")


def _relative_locator(locator: str) -> PurePosixPath:
    if not isinstance(locator, str) or not locator or "\\" in locator:
        raise SourceBaselineError("source locator must be a canonical relative POSIX path")
    path = PurePosixPath(locator)
    if (path.is_absolute() or not path.parts or path.as_posix() != locator
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise SourceBaselineError("source locator must be a canonical relative POSIX path")
    if any(ord(char) < 32 or ord(char) == 127 for char in locator):
        raise SourceBaselineError("source locator contains control characters")
    return path


def _public_directory(path: Path, descriptor: int, before: tuple[int, int]) -> None:
    try:
        opened = os.fstat(descriptor)
        public = path.lstat()
    except OSError as exc:
        raise SourceBaselineError("source root or parent directory changed during observation") from exc
    if (stat.S_ISLNK(public.st_mode) or _directory_identity(opened) != before
            or _directory_identity(public) != before):
        raise SourceBaselineError("source root or parent directory changed during observation")


def _open_selected(root: Path, relative: PurePosixPath,
                   mapping: tuple[tuple[Path, tuple[int, int]], ...]) -> str:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    dir_flags = os.O_RDONLY | nofollow | directory
    file_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
    descriptors: list[int] = []
    dirs: list[tuple[Path, int, tuple[int, int]]] = []
    source_fd = -1
    try:
        fd = os.open(root, dir_flags)
        descriptors.append(fd)
        info = os.fstat(fd)
        dirs.append((root, fd, _directory_identity(info)))
        for part in relative.parts[:-1]:
            _assert_mapping(mapping)
            _public_directory(dirs[-1][0], fd, dirs[-1][2])
            names = os.listdir(fd)
            matching = [name for name in names if name.casefold() == part.casefold()]
            if matching and matching != [part]:
                raise SourceBaselineError("source locator has a case-insensitive path collision")
            fd = os.open(part, dir_flags, dir_fd=fd)
            descriptors.append(fd)
            directory_path = dirs[-1][0] / part
            info = os.fstat(fd)
            dirs.append((directory_path, fd, _directory_identity(info)))
        _assert_mapping(mapping)
        _public_directory(dirs[-1][0], fd, dirs[-1][2])
        names = os.listdir(fd)
        matching = [name for name in names if name.casefold() == relative.name.casefold()]
        if matching and matching != [relative.name]:
            raise SourceBaselineError("source locator has a case-insensitive path collision")
        try:
            before_name = os.stat(relative.name, dir_fd=fd, follow_symlinks=False)
        except OSError as exc:
            raise SourceBaselineError(f"cannot inspect selected source: {exc}") from exc
        if not stat.S_ISREG(before_name.st_mode):
            raise SourceBaselineError("selected source must be a regular file")
        try:
            source_fd = os.open(relative.name, file_flags, dir_fd=fd)
        except OSError as exc:
            raise SourceBaselineError(f"cannot open selected source: {exc}") from exc
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or _identity(before) != _identity(before_name):
            raise SourceBaselineError("selected source changed or is not a regular file")
        digest = hashlib.sha256()
        with os.fdopen(source_fd, "rb") as stream:
            source_fd = -1
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            opened_after = os.fstat(stream.fileno())
        after_name = os.stat(relative.name, dir_fd=fd, follow_symlinks=False)
        if (_identity(before) != _identity(opened_after) or _identity(before) != _identity(after_name)
                or before.st_size != opened_after.st_size or before.st_size != after_name.st_size):
            raise SourceBaselineError("selected source changed while being observed")
        _assert_mapping(mapping)
        for dir_path, dir_fd, identity in dirs:
            _public_directory(dir_path, dir_fd, identity)
        return "sha256:" + digest.hexdigest()
    except SourceBaselineError:
        raise
    except OSError as exc:
        raise SourceBaselineError(f"cannot safely inspect selected source: {exc}") from exc
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _git_run(root: Path, args: list[str], *, limit: int = _GIT_OUTPUT_LIMIT) -> tuple[int, bytes]:
    env = os.environ.copy()
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    process = subprocess.Popen(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=env)
    assert process.stdout is not None
    output = bytearray()
    try:
        while chunk := process.stdout.read(min(65536, limit + 1 - len(output))):
            output.extend(chunk)
            if len(output) > limit:
                process.kill()
                process.wait()
                raise SourceBaselineError("Git output exceeded the configured evidence limit")
        return process.wait(), bytes(output)
    finally:
        process.stdout.close()


def _repo_mapping(root: Path, mapping: tuple[tuple[Path, tuple[int, int]], ...]) -> tuple[Path, Path, str]:
    status, output = _git_run(root, ["rev-parse", "--show-toplevel"])
    if status:
        raise SourceBaselineError("Git worktree is unavailable")
    try:
        top = Path(os.fsdecode(output).strip())
        repo = Path(os.path.abspath(top))
        repo_mapping = _mapping_snapshot(repo)
        _assert_mapping(mapping)
        return top, repo, str(repo_mapping[-1][0])
    except OSError as exc:
        raise SourceBaselineError("Git worktree mapping is unavailable") from exc


def _git_context(root: Path, relative: PurePosixPath, content_hash: str,
                 mapping: tuple[tuple[Path, tuple[int, int]], ...]) -> dict[str, str]:
    unavailable = {"selectedBytes": "unknown", "limitation": "Git context unavailable"}
    try:
        top, repo, _ = _repo_mapping(root, mapping)
        head_status, head_data = _git_run(root, ["rev-parse", "--verify", "HEAD^{commit}"])
        if head_status:
            return {**unavailable, "limitation": "Git repository has no available HEAD commit"}
        head = os.fsdecode(head_data).strip()
        if not _COMMIT.fullmatch(head):
            return {**unavailable, "limitation": "Git returned an unsupported commit object id"}
        try:
            repo_relative = root.relative_to(repo).joinpath(*relative.parts).as_posix()
        except ValueError:
            return {**unavailable, "limitation": "mapped root is outside the Git worktree"}
        if _relative_locator(repo_relative).as_posix() != repo_relative:
            return {**unavailable, "limitation": "selected Git path is not portable"}
        context = {"head": head, "repositoryLocator": repo_relative}
        tree_status, tree_data = _git_run(repo, ["ls-tree", "-r", "--full-tree", "--name-only", "-z",
                                                  head, "--", f":(top,literal){repo_relative}"])
        if tree_status:
            return {**context, "selectedBytes": "unknown", "limitation": "recorded HEAD tree is unavailable"}
        tree_paths = tree_data.split(b"\0")
        if repo_relative.encode("utf-8") not in tree_paths:
            context["selectedBytes"] = "untracked"
            _assert_mapping(mapping)
            return context
        blob_status, blob = _git_run(repo, ["cat-file", "blob", f"{head}:{repo_relative}"], limit=_GIT_BLOB_LIMIT)
        if blob_status:
            return {**context, "selectedBytes": "unknown", "limitation": "selected blob unavailable at recorded HEAD"}
        context["selectedBytes"] = "equal" if hashlib.sha256(blob).hexdigest() == content_hash.removeprefix("sha256:") else "different"
        _assert_mapping(mapping)
        return context
    except SourceBaselineError as exc:
        return {**unavailable, "limitation": str(exc)}
    except (OSError, subprocess.SubprocessError):
        return unavailable


def _portable_source_locator(root_id: str, locator: str) -> str:
    return f"local:{root_id}/" + "/".join(quote(part, safe="-._~") for part in _relative_locator(locator).parts)


def observe_source(root_id: str, asset_id: str, root: Path, locator: str, observed_at: str,
                   *, git: bool = True) -> dict[str, Any]:
    """Build portable asset-linked records from exact bytes without returning content."""
    relative = _relative_locator(locator)
    absolute = _absolute_root(root)
    mapping = _mapping_snapshot(absolute)
    digest = _open_selected(absolute, relative, mapping)
    # Asset identity is caller-owned; this helper never derives it from path or bytes.
    portable_locator = _portable_source_locator(root_id, locator)
    source = _LEDGER.make_source_digest(portable_locator, digest)
    asset = _LEDGER.make_asset(asset_id, root_id, locator)
    context = _git_context(absolute, relative, digest, mapping) if git else None
    _assert_mapping(mapping)
    observation = _LEDGER.make_observation(asset, source, observed_at, git_context=context)
    return {"asset": asset, "source": source, "observation": observation}


def _ledger_records(vault: Path) -> dict[str, tuple[dict[str, Any], bytes]]:
    batch = load_source("source_baselines_batch", str(Path(__file__).with_name("wiki_batch.py")))
    records: dict[str, tuple[dict[str, Any], bytes]] = {}
    for name, raw in batch._ledger_files(vault).items():
        try:
            value = json.loads(raw)
            _LEDGER.validate_record(value, expected_id=name[:-5])
            if _LEDGER.record_path(value).rsplit("/", 1)[-1] != name:
                raise ValueError("record path does not match canonical filename")
        except (ValueError, TypeError) as exc:
            raise SourceBaselineError(f"invalid evidence ledger record {name}: {exc}") from exc
        records[value["id"]] = (value, raw)
    return records


def observation_bundle(vault: Path, root: Path, selection: dict[str, Any], *, allow_move: bool = False) -> dict[str, Any]:
    batch = load_source("source_baselines_batch", str(Path(__file__).with_name("wiki_batch.py")))
    if (not isinstance(selection, dict) or set(selection) != {"version", "rootId", "assetId", "locator", "observedAt"}
            or type(selection.get("version")) is not int or selection["version"] != 1):
        raise SourceBaselineError("selection must contain exactly version, rootId, assetId, locator, and observedAt")
    observed = observe_source(selection["rootId"], selection["assetId"], root,
                              selection["locator"], selection["observedAt"])
    records = _ledger_records(vault)
    asset = observed["asset"]
    prior_asset = records.get(asset["id"])
    if prior_asset:
        prior = prior_asset[0]
        if prior["rootId"] != asset["rootId"]:
            raise SourceBaselineError("asset root identity is immutable")
        moved = prior["locator"] != asset["locator"]
        prior_observation = records.get(observed["observation"]["id"])
        historical_retry = (moved and not allow_move and prior_observation is not None
                            and _LEDGER._observation_material(prior_observation[0])
                            == _LEDGER._observation_material(observed["observation"]))
        if moved and not allow_move and not historical_retry:
            raise SourceBaselineError("asset locator changed; explicit --allow-move and reviewed approval are required")
        if historical_retry:
            asset = prior
            observed["asset"] = prior
            observed["observation"] = prior_observation[0]
        else:
            asset = _LEDGER.merge_record(prior, {"locator": asset["locator"]})
            observed["asset"] = asset
            observed["observation"] = _LEDGER.make_observation(asset, observed["source"], selection["observedAt"],
                                                                  git_context=observed["observation"].get("gitContext"))
    operations = []
    for record in (asset, observed["source"], observed["observation"]):
        previous = records.get(record["id"])
        if previous is not None:
            old, raw = previous
            if record["kind"] == "asset":
                merged = _LEDGER.merge_record(old, {"locator": record["locator"]})
                if merged == old:
                    continue
                record = merged
            elif record["kind"] == "source":
                if old["locator"] != record["locator"] or old["contentHash"] != record["contentHash"]:
                    raise SourceBaselineError("source identity conflicts with its immutable locator or content")
                continue
            else:
                if _LEDGER._observation_material(old) != _LEDGER._observation_material(record):
                    raise SourceBaselineError("observation identity conflicts with immutable source evidence")
                continue
            expected = batch.sha(raw)
        else:
            expected = None
        operations.append(_LEDGER.operation(record))
        operations[-1]["expectedHash"] = expected
    batch.ensure_no_unfinished_ledger_publication(
        vault, {asset["id"], observed["source"]["id"], observed["observation"]["id"]})
    return {"version": 1, "operations": operations}


def baseline_bundle(vault: Path, selection: dict[str, Any]) -> dict[str, Any]:
    if (not isinstance(selection, dict) or set(selection) != {"version", "reconciliations"}
            or type(selection.get("version")) is not int or selection["version"] != 1):
        raise SourceBaselineError("selection must contain exactly version and reconciliations")
    entries = selection["reconciliations"]
    if not isinstance(entries, list) or not entries:
        raise SourceBaselineError("reconciliations must be a nonempty list")
    batch = load_source("source_baselines_batch", str(Path(__file__).with_name("wiki_batch.py")))
    records = _ledger_records(vault)
    seen: set[tuple[str, str]] = set()
    relevant_record_ids: set[str] = set()
    operations: list[dict[str, Any]] = []
    for selected in entries:
        fields = {"page", "observationId", "pageBatchId", "reconciledAt", "outcome"}
        if not isinstance(selected, dict) or set(selected) != fields:
            raise SourceBaselineError("each reconciliation must contain exactly page, observationId, pageBatchId, reconciledAt, and outcome")
        if (not isinstance(selected["page"], str) or not isinstance(selected["observationId"], str)
                or not isinstance(selected["pageBatchId"], str)
                or not isinstance(selected["reconciledAt"], str)
                or not isinstance(selected["outcome"], str)):
            raise SourceBaselineError("reconciliation fields must be strings")
        observation_pair = records.get(selected["observationId"])
        if observation_pair is None or observation_pair[0].get("kind") != "observation":
            raise SourceBaselineError("selected observation must already exist durably")
        observation = observation_pair[0]
        pair = (selected["page"], observation["assetId"])
        if pair in seen:
            raise SourceBaselineError("duplicate page/asset reconciliation pair")
        seen.add(pair)
        asset_pair = records.get(observation["assetId"])
        source_pair = records.get(observation["sourceId"])
        if asset_pair is None or source_pair is None:
            raise SourceBaselineError("selected observation dependencies are unavailable")
        journal = batch._load_journal(Path(vault).resolve(strict=True), selected["pageBatchId"])
        if journal["authority"] != "wiki-page" or journal["phase"] != "complete":
            raise SourceBaselineError("reconciliation requires a completed wiki-page batch")
        page_entry = next((item for item in journal["entries"] if item["path"] == selected["page"]), None)
        if page_entry is None:
            raise SourceBaselineError("page batch does not include selected reconciliation page")
        before, after = page_entry["beforeHash"], page_entry["afterHash"]
        if selected["outcome"] == "no-change" and before != after:
            raise SourceBaselineError("no-change reconciliation requires identical page-batch bytes")
        if selected["outcome"] == "updated" and before == after:
            raise SourceBaselineError("updated reconciliation requires changed page-batch bytes")
        batch._page_batch_evidence(Path(vault).resolve(strict=True), selected["pageBatchId"],
                                   selected["page"], after, selected["outcome"])
        if not ledger_timestamp(selected["reconciledAt"]):
            raise SourceBaselineError("reconciledAt must be a timezone-aware ISO timestamp")
        entry = {"observationId": observation["id"], "sourceId": source_pair[0]["id"],
                 "contentHash": observation["contentHash"], "pageHash": "sha256:" + after,
                 "pageBatchId": selected["pageBatchId"], "reconciledAt": selected["reconciledAt"],
                 "outcome": selected["outcome"]}
        baseline_id = _LEDGER._identity_id("baseline-", [selected["page"], asset_pair[0]["id"]])
        relevant_record_ids.update({baseline_id, observation["id"], observation["sourceId"], asset_pair[0]["id"]})
        existing = records.get(baseline_id)
        if existing:
            history = existing[0]["history"]
            if entry in history:
                # Only an unchanged retry must validate the latest historical page against current bytes.
                batch._validate_ledger_operations(
                    vault, [_LEDGER.operation(existing[0], existing[1])], publication=True)
                continue
            baseline = _LEDGER.merge_record(existing[0], {"history": history + [entry]})
            expected = batch.sha(existing[1])
        else:
            baseline = _LEDGER.make_baseline(selected["page"], asset_pair[0]["id"], [entry])
            expected = None
        operation = _LEDGER.operation(baseline)
        operation["expectedHash"] = expected
        operations.append(operation)
    batch.ensure_no_unfinished_ledger_publication(vault, relevant_record_ids)
    return {"version": 1, "operations": operations}


def ledger_timestamp(value: Any) -> bool:
    from datetime import datetime
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def history_availability(root: Path, recorded_commit: str | None) -> dict[str, str]:
    """Report a selected root's recorded commit relation to its current HEAD; never infer history."""
    absolute = _absolute_root(root)
    mapping = _mapping_snapshot(absolute)
    if recorded_commit is None:
        _assert_mapping(mapping)
        return {"status": "no-commit-recorded", "limitation": "no commit was recorded"}
    if not isinstance(recorded_commit, str) or not _COMMIT.fullmatch(recorded_commit):
        raise SourceBaselineError("recorded commit must be a full 40- or 64-character object id")
    try:
        top, _, _ = _repo_mapping(absolute, mapping)
        object_status, _ = _git_run(absolute, ["cat-file", "-e", f"{recorded_commit}^{{commit}}"])
        if object_status:
            return {"status": "unavailable", "limitation": "recorded commit object is missing or unavailable"}
        head_status, head_data = _git_run(absolute, ["rev-parse", "--verify", "HEAD^{commit}"])
        if head_status:
            return {"status": "unavailable", "limitation": "current Git HEAD is unavailable"}
        head = os.fsdecode(head_data).strip()
        ancestor_status, _ = _git_run(absolute, ["merge-base", "--is-ancestor", recorded_commit, head])
        _assert_mapping(mapping)
        if ancestor_status == 0:
            return {"status": "available-ancestor", "head": head}
        if ancestor_status == 1:
            shallow_status, shallow_data = _git_run(absolute, ["rev-parse", "--is-shallow-repository"])
            if shallow_status or shallow_data.strip() == b"true":
                return {"status": "unavailable", "head": head,
                        "limitation": "shallow history cannot establish complete commit ancestry"}
            return {"status": "retained-not-ancestor", "head": head,
                    "limitation": "recorded commit is retained but not an ancestor of current HEAD"}
        return {"status": "unavailable", "limitation": "Git could not determine commit ancestry"}
    except SourceBaselineError as exc:
        return {"status": "unavailable", "limitation": str(exc)}
    except (OSError, subprocess.SubprocessError):
        return {"status": "unavailable", "limitation": "Git history context unavailable"}
    finally:
        _assert_mapping(mapping)
