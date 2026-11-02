#!/usr/bin/env python3
"""Read-only detection of drift for explicitly selected local source assets."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import unicodedata
from typing import Any

from module_loading import load_source

BASELINES = load_source("source_drift_baselines", str(Path(__file__).with_name("source_baselines.py")))
LEDGER = load_source("source_drift_ledger", str(Path(__file__).with_name("evidence_ledger.py")))
READS = load_source("source_drift_reads", str(Path(__file__).with_name("retrieval_reads.py")))
_MIDDLEWARE = str(Path(__file__).parents[1] / "skills/wiki/scripts/okf_mw")
sys.path.insert(0, _MIDDLEWARE)
try:
    VALIDATOR = load_source("source_drift_validator", str(Path(_MIDDLEWARE) / "validate.py"))
finally:
    sys.path.remove(_MIDDLEWARE)

ROOT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SOURCE_ID = re.compile(r"source-[0-9a-f]{24}")
_LEGACY_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CLAIM_ID = re.compile(r"claim-[a-z0-9][a-z0-9._-]{0,121}")
_RESERVED = {"index.md", "log.md", "_plan.md"}
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class DriftInputError(ValueError):
    """An input path or portable identifier is invalid."""


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _reason(exc: BaseException) -> str:
    """Translate safe exception types/errno to portable state codes."""
    current: BaseException | None = exc
    chain: list[BaseException] = []
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    for item in chain:
        code = getattr(item, "errno", None)
        if code in (errno.EACCES, errno.EPERM):
            return "permission-denied"
        if code == errno.ENOENT:
            return "missing"
        if code in (errno.ELOOP, errno.ENOTDIR):
            return "unsafe-path"
        if isinstance(item, FileNotFoundError):
            return "missing"
        if isinstance(item, PermissionError):
            return "permission-denied"
    if isinstance(exc, BASELINES.SourceBaselineError):
        # These are fixed messages emitted by source_baselines' guarded reader.
        message = str(exc)
        if message == "selected source changed while being observed":
            return "unstable"
        if message in {"selected source must be a regular file", "selected source changed or is not a regular file",
                       "selected source has hardlink aliases", "source locator component is not NFC canonical",
                       "mapped source root traverses a symlink", "mapped source root must be a directory",
                       "source locator has a case-insensitive path collision"}:
            return "unsafe-path"
        if message == "mapped root has a case-insensitive path collision":
            return "path-collision"
        if message in {"source root or parent directory changed during observation",
                       "mapped source root or an ancestor changed during observation"}:
            return "unstable"
    if isinstance(exc, READS.SourceChanged):
        return "unstable"
    if isinstance(exc, ValueError) and str(exc) == "unsafe-page-file":
        return "unsafe-path"
    if isinstance(exc, READS.ReadError):
        return "unsafe-path"
    return "inspection-failed"


def _load(vault: Path) -> tuple[dict[str, dict[str, Any]], dict[str, bytes], list[dict[str, Any]]]:
    """Safely load portable ledger records; invalid records do not hide valid peers."""
    batch = load_source("source_drift_batch", str(Path(__file__).with_name("wiki_batch.py")))
    records: dict[str, dict[str, Any]] = {}
    try:
        raw_records = batch._ledger_files(vault, reject_hardlinks=True)
    except batch.BatchError as exc:
        code = _reason(exc)
        current: BaseException | None = exc
        has_os_error = False
        while current is not None:
            has_os_error = has_os_error or isinstance(current, OSError)
            current = current.__cause__ or current.__context__
        if code == "permission-denied":
            state, reason = "operational-error", "ledger-permission-denied"
        elif code == "unsafe-path" or not has_os_error:
            state, reason = "unsafe-unresolvable", "ledger-unsafe-path" if code == "unsafe-path" else "ledger-unsafe"
        else:
            state, reason = "operational-error", "ledger-" + code
        return records, {}, [{"state": state, "reason": reason}]
    except (OSError, ValueError) as exc:
        code = _reason(exc)
        return records, {}, [{"state": _state(code), "reason": "ledger-" + code}]
    issues: list[dict[str, Any]] = []
    for name, raw in sorted(raw_records.items()):
        try:
            record = json.loads(raw)
            LEDGER.validate_record(record, expected_id=Path(name).stem)
            if LEDGER.record_path(record).rsplit("/", 1)[-1] != name:
                raise ValueError("record path mismatch")
            records[record["id"]] = record
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            safe_name = name if re.fullmatch(r"[a-z0-9._-]{1,140}\.json", name) else "unrecognized-record"
            issues.append({"path": "wiki/meta/evidence/" + safe_name, "state": "invalid", "reason": "invalid-ledger-record"})
    return records, raw_records, issues


def _discover_pages(vault: Path) -> tuple[set[str], tuple[Any, ...], list[dict[str, Any]]]:
    """Enumerate visible eligible Markdown pages without following links."""
    root_chain = READS._open_root(Path(os.path.abspath(vault)))
    root_fd = root_chain[-1]
    opened: list[int] = []
    files: set[str] = set()
    directory_state: list[tuple[str, tuple[int, ...], tuple[str, ...]]] = []
    issues: list[dict[str, Any]] = []
    try:
        READS._check_siblings(root_fd, "wiki")
        wiki_fd = os.open("wiki", _DIR_FLAGS, dir_fd=root_fd)
        opened.append(wiki_fd)

        def walk(fd: int, prefix: tuple[str, ...]) -> None:
            rel_dir = "/".join(("wiki", *prefix))
            before = os.fstat(fd)
            try:
                names = tuple(sorted(os.listdir(fd)))
            except OSError as exc:
                names = ("<unavailable>",)
                issues.append({"path": rel_dir, "state": _state(_reason(exc)), "reason": "page-directory-unavailable"})
                directory_state.append((rel_dir, _stamp(before), names))
                return
            directory_state.append((rel_dir, _stamp(before), names))
            groups: dict[str, list[str]] = {}
            for name in names:
                groups.setdefault(unicodedata.normalize("NFC", name).casefold(), []).append(name)
            collisions = {name for group in groups.values() if len(group) > 1 for name in group}
            for name in names:
                safe_name = ("\\" not in name and unicodedata.normalize("NFC", name) == name
                             and not any(ord(char) < 32 or ord(char) == 127 for char in name))
                safe_path = "/".join(("wiki", *prefix, name)) if safe_name else "wiki/<unsafe-path>"
                if name in collisions:
                    issues.append({"path": safe_path, "state": "unsafe-unresolvable", "reason": "path-collision"})
                    continue
                if not safe_name:
                    issues.append({"path": safe_path, "state": "unsafe-unresolvable", "reason": "unsafe-page-name"})
                    continue
                if name.startswith("."):
                    continue
                child_rel = (*prefix, name)
                child_path = "/".join(("wiki", *child_rel))
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError as exc:
                    issues.append({"path": child_path, "state": "unstable" if _reason(exc) == "missing" else "operational-error", "reason": "page-entry-changed"})
                    continue
                if stat.S_ISLNK(info.st_mode):
                    issues.append({"path": child_path, "state": "unsafe-unresolvable", "reason": "symlink-page-path"})
                    continue
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child_fd = os.open(name, _DIR_FLAGS, dir_fd=fd)
                    except OSError:
                        issues.append({"path": child_path, "state": "unstable", "reason": "directory-changed"})
                        continue
                    opened.append(child_fd)
                    walk(child_fd, child_rel)
                elif name.casefold().endswith(".md") and name.casefold() not in _RESERVED:
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        issues.append({"path": child_path, "state": "unsafe-unresolvable", "reason": "unsafe-page-file"})
                    else:
                        files.add(child_path)
        walk(wiki_fd, ())
        # Directory identity/listings are rechecked at completion, independently of page content reads.
        for rel_dir, before_stamp, before_names in directory_state:
            fd = root_fd
            chain: list[int] = []
            try:
                for part in rel_dir.split("/"):
                    READS._check_siblings(fd, part)
                    fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                    chain.append(fd)
                if _stamp(os.fstat(fd)) != before_stamp or tuple(sorted(os.listdir(fd))) != before_names:
                    issues.append({"path": rel_dir, "state": "unstable", "reason": "page-set-changed"})
            except OSError:
                issues.append({"path": rel_dir, "state": "unstable", "reason": "page-set-changed"})
            finally:
                for child_fd in reversed(chain):
                    os.close(child_fd)
        READS._verify_root(Path(os.path.abspath(vault)), root_chain)
        inventory = (tuple(sorted(files)), tuple(sorted(directory_state)))
        return files, inventory, issues
    finally:
        for fd in reversed(opened):
            os.close(fd)
        for fd in reversed(root_chain):
            os.close(fd)


def _read_page(vault: Path, relative: str) -> tuple[bytes, str, tuple[int, ...]]:
    parts = READS._canonical_page(relative)
    canonical_vault = Path(os.path.abspath(vault))
    root_chain = READS._open_root(canonical_vault)
    fd = root_chain[-1]
    opened: list[int] = []
    try:
        for part in parts[:-1]:
            READS._check_siblings(fd, part)
            fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            opened.append(fd)
        name = parts[-1]
        READS._check_siblings(fd, name)
        try:
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=fd)
        except OSError:
            raise
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("unsafe-page-file")
            blocks = []
            while block := os.read(descriptor, 1024 * 1024):
                blocks.append(block)
            raw = b"".join(blocks)
            after = os.fstat(descriptor)
            path_info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if _stamp(before) != _stamp(after) or _stamp(after) != _stamp(path_info):
                raise READS.SourceChanged("wiki page changed while being read")
            verify_fd = root_chain[-1]
            verify_opened: list[int] = []
            try:
                for part, original in zip(parts[:-1], opened):
                    READS._check_siblings(verify_fd, part)
                    child = os.open(part, _DIR_FLAGS, dir_fd=verify_fd)
                    verify_opened.append(child)
                    if _stamp(os.fstat(child)) != _stamp(os.fstat(original)):
                        raise READS.SourceChanged("wiki parent changed while page was read")
                    verify_fd = child
                READS._check_siblings(verify_fd, name)
                terminal = os.stat(name, dir_fd=verify_fd, follow_symlinks=False)
                if _stamp(terminal) != _stamp(after):
                    raise READS.SourceChanged("wiki page changed while being read")
                READS._verify_root(canonical_vault, root_chain)
            finally:
                for child_fd in reversed(verify_opened):
                    os.close(child_fd)
            return raw, "sha256:" + hashlib.sha256(raw).hexdigest(), _stamp(after)
        finally:
            os.close(descriptor)
    finally:
        for child_fd in reversed(opened):
            os.close(child_fd)
        for root_fd in reversed(root_chain):
            os.close(root_fd)


def _metadata(raw: bytes) -> tuple[dict[str, Any], str | None]:
    try:
        value, error = VALIDATOR._parse_frontmatter_yaml(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):
        return {}, "invalid-frontmatter"
    if error or not isinstance(value, dict):
        return {}, "invalid-frontmatter"
    return value, None


def _graph(records: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Validate provenance edges, preserving unrelated valid graph components."""
    observations: dict[str, list[dict[str, Any]]] = {}
    bad_assets: dict[str, str] = {}
    sources = {key: value for key, value in records.items() if value["kind"] == "source"}
    assets = {key: value for key, value in records.items() if value["kind"] == "asset"}
    for source in sources.values():
        try:
            canonical = LEDGER.make_source_digest(source["locator"], source["contentHash"])
            if canonical["id"] != source["id"]:
                bad_assets[source["id"]] = "invalid-source-identity"
        except (ValueError, TypeError):
            bad_assets[source["id"]] = "invalid-source-identity"
    for observation in (r for r in records.values() if r["kind"] == "observation"):
        observations.setdefault(observation["assetId"], []).append(observation)
        asset = assets.get(observation["assetId"])
        source = sources.get(observation["sourceId"])
        portable = BASELINES._portable_source_locator(observation["rootId"], observation["locator"])
        identity = LEDGER.make_source_digest(portable, observation["contentHash"])
        if (asset is None or asset["rootId"] != observation["rootId"] or source is None
                or observation["locator"] != BASELINES._relative_locator(observation["locator"]).as_posix()
                or source["locator"] != portable or source["contentHash"] != observation["contentHash"]
                or source["id"] != identity["id"]):
            bad_assets[observation["assetId"]] = "broken-observation-dependency"
    bad_baselines: dict[str, str] = {}
    for baseline in (r for r in records.values() if r["kind"] == "baseline"):
        asset = assets.get(baseline["assetId"])
        if asset is None:
            bad_baselines[baseline["id"]] = "missing-baseline-asset"
            continue
        for entry in baseline["history"]:
            observation = records.get(entry["observationId"])
            source = sources.get(entry["sourceId"])
            if (not observation or observation.get("kind") != "observation"
                    or observation["assetId"] != baseline["assetId"]
                    or observation["sourceId"] != entry["sourceId"]
                    or observation["contentHash"] != entry["contentHash"]
                    or not source or source["contentHash"] != entry["contentHash"]):
                bad_baselines[baseline["id"]] = "broken-baseline-history-link"
                break
    return observations, {**bad_assets, **bad_baselines}


def _state(code: str) -> str:
    if code in {"unsafe-path", "path-collision"}:
        return "unsafe-unresolvable"
    if code in {"permission-denied", "inspection-failed"}:
        return "operational-error"
    return code


def _refresh_impacts(selected: dict[str, dict[str, Any]], pages: dict[str, dict[str, Any]],
                     claims: list[dict[str, Any]], observations: dict[str, list[dict[str, Any]]],
                     bad_graph: dict[str, str]) -> None:
    """Refresh all source-dependent worklist edges after final source revalidation."""
    by_source: dict[str, list[dict[str, Any]]] = {}
    for values in observations.values():
        for observation in values:
            by_source.setdefault(observation["sourceId"], []).append(observation)

    def compare(source_id: str, asset_id: str) -> tuple[str, str | None]:
        if asset_id in bad_graph:
            return "unsafe-unresolvable", bad_graph[asset_id]
        item = selected.get(asset_id, {})
        if item.get("state") in {"unstable", "missing", "unsafe-unresolvable", "operational-error"}:
            return item["state"], item.get("reason", item["state"])
        current_hash = item.get("currentHash")
        candidates = [o for o in by_source.get(source_id, []) if o["assetId"] == asset_id]
        if not candidates:
            return "unchecked", "missing-observation-link"
        if current_hash is None:
            return item.get("state", "unchecked"), item.get("reason", "source-unavailable")
        if any(o["contentHash"] == current_hash for o in candidates):
            return "unchanged", None
        return "changed", "recorded-source-snapshot-differs"

    for page in pages.values():
        for reference in page["sourceReferences"]:
            if reference.get("assetId") and reference.get("id"):
                state, reason = compare(reference["id"], reference["assetId"])
                reference["state"] = state
                reference.pop("reason", None)
                if reason:
                    reference["reason"] = reason
                if state in {"changed", "missing", "unsafe-unresolvable", "operational-error", "unstable"}:
                    page["state"] = "unstable" if state == "unstable" else "affected" if page["state"] == "current" else page["state"]
                    page["reviewReasons"].append("direct-source-" + state)
        for baseline in page["baselines"]:
            if baseline.get("reason") in {"broken-baseline-history-link", "missing-baseline-asset"} or baseline["state"] == "unchecked":
                continue
            item = selected.get(baseline["assetId"], {})
            if item.get("state") == "unstable":
                baseline["state"] = "unstable"
                baseline["pageState"] = "unstable"
                baseline["reason"] = item.get("reason", "source-state-unstable")
                page["state"] = "unstable"
                page["reviewReasons"].append("latest-baseline-source-unstable")
            elif item.get("currentHash") is not None:
                baseline["state"] = "unchanged" if item["currentHash"] == baseline["recordedHash"] else "changed"
                if baseline["state"] == "changed":
                    baseline["reason"] = "latest-baseline-source-differs"
                    page["state"] = "affected" if page["state"] == "current" else page["state"]
                    page["reviewReasons"].append("latest-baseline-source-differs")
                else:
                    baseline.pop("reason", None)
            elif item.get("state") in {"missing", "unsafe-unresolvable", "operational-error", "permission-denied"}:
                baseline["state"] = item["state"]
                baseline["reason"] = item.get("reason", item["state"])
                page["state"] = "affected" if page["state"] == "current" else page["state"]
                page["reviewReasons"].append(baseline["reason"])
            current_page_hash = page.get("currentHash")
            if page["state"] == "unstable":
                baseline["pageState"] = "unstable"
            elif current_page_hash is not None:
                baseline["pageState"] = "unchanged" if current_page_hash == baseline["pageHash"] else "changed"
        page["reviewReasons"] = sorted(set(page["reviewReasons"]))
        page["sourceReferences"].sort(key=lambda x: (x.get("id") or "", x.get("assetId", ""), x["state"], x.get("reason", "")))

    for claim in claims:
        reasons: list[str] = []
        for evidence in claim["evidence"]:
            asset_id = evidence.get("assetId")
            source_id = evidence.get("sourceId")
            if asset_id and source_id:
                state, reason = compare(source_id, asset_id)
                evidence["state"] = state
                evidence.pop("reason", None)
                if reason:
                    evidence["reason"] = reason
            state = evidence["state"]
            relation = evidence["relation"]
            if state in {"changed", "missing", "unsafe-unresolvable", "operational-error", "unstable", "missing-source-record"}:
                reasons.append(f"{relation}-source-{state}")
        linked_pages = sorted(path for path, page in pages.items() if claim["id"] in page["claims"])
        claim["pages"] = linked_pages
        claim["pageLinkState"] = "unstable" if any(pages[path]["state"] == "unstable" for path in linked_pages) else "stable"
        if claim["pageLinkState"] == "unstable":
            reasons.append("page-link-coverage-unstable")
        claim["reviewReasons"] = sorted(set(reasons))
        claim["reviewRequired"] = bool(reasons)
        if claim["pageLinkState"] == "unstable" or any(e["state"] == "unstable" for e in claim["evidence"]):
            claim["state"] = "unstable"
        else:
            claim["state"] = "review-suggested" if claim["reviewRequired"] else "declared-provenance"

    # Propagate only refreshed claim conclusions, never their pre-revalidation state.
    for page in pages.values():
        for claim_id in page["claims"]:
            claim = next((row for row in claims if row["id"] == claim_id), None)
            if claim is None:
                if page["state"] != "unstable":
                    page["state"] = "unsafe-unresolvable"
                page["reviewReasons"].append("missing-page-claim-record")
            elif claim["reviewRequired"]:
                if claim["state"] == "unstable":
                    page["state"] = "unstable"
                elif page["state"] == "current":
                    page["state"] = "affected"
                page["reviewReasons"].append("declared-claim-evidence-needs-review")
        page["reviewReasons"] = sorted(set(page["reviewReasons"]))


def check(vault: Path, root_id: str, source_root: Path) -> dict[str, Any]:
    """Compare exact selected-root bytes, declared provenance, and current page hashes."""
    if not isinstance(root_id, str) or not ROOT_ID.fullmatch(root_id):
        raise DriftInputError("--root-id must be a portable identifier")
    source_root = Path(source_root).expanduser()
    if not source_root.is_absolute():
        raise DriftInputError("--source-root must be an explicit absolute path")
    vault = Path(vault)
    records, raw_ledger, issues = _load(vault)
    observations, bad_graph = _graph(records)
    issues.extend({"id": identity, "state": "invalid", "reason": reason} for identity, reason in sorted(bad_graph.items()))
    assets = {key: value for key, value in records.items() if value["kind"] == "asset"}
    selected_assets = {key: value for key, value in assets.items() if value["rootId"] == root_id}
    sources = {key: value for key, value in records.items() if value["kind"] == "source"}
    selected: dict[str, dict[str, Any]] = {}
    source_stamps: dict[str, tuple[str, tuple[int, ...]] | None] = {}
    inspected_assets: set[str] = set()
    mapping = None
    root_error = None
    try:
        root = BASELINES._absolute_root(source_root)
        mapping = BASELINES._mapping_snapshot(root)
    except (OSError, ValueError) as exc:
        root_error = _reason(exc)
        root_state = "operational-error" if root_error == "missing" else _state(root_error)
        issues.append({"state": root_state, "reason": "selected-root-unavailable"})

    for asset_id, asset in sorted(selected_assets.items()):
        item = {"id": asset_id, "rootId": root_id, "locator": asset["locator"], "state": "unchecked"}
        obs = sorted(observations.get(asset_id, []), key=lambda record: (record["observedAt"], record["id"]))
        if asset_id in bad_graph:
            item.update(state="unsafe-unresolvable", reason=bad_graph[asset_id])
            selected[asset_id] = item
            source_stamps[asset_id] = None
            continue
        if root_error:
            item.update(state="operational-error" if root_error == "missing" else _state(root_error), reason=root_error)
            selected[asset_id] = item
            source_stamps[asset_id] = None
            continue
        if not obs:
            item.update(state="unchecked", reason="no-recorded-observation")
            selected[asset_id] = item
            source_stamps[asset_id] = None
            continue
        inspected_assets.add(asset_id)
        try:
            current_hash, stamp = BASELINES._inspect_selected(root, BASELINES._relative_locator(asset["locator"]), mapping,
                                                               reject_hardlinks=True, strict_aliases=True)
            item.update(currentHash=current_hash, state="unchanged" if all(o["contentHash"] == current_hash for o in obs) else "changed")
            item["observations"] = [{"id": o["id"], "sourceId": o["sourceId"], "recordedHash": o["contentHash"],
                                     "state": "unchanged" if o["contentHash"] == current_hash else "changed",
                                     "locatorChanged": o["locator"] != asset["locator"]} for o in obs]
            source_stamps[asset_id] = (current_hash, stamp)
        except (OSError, ValueError) as exc:
            code = _reason(exc)
            state = _state(code)
            item.update(state=state, reason=code)
            source_stamps[asset_id] = None
        selected[asset_id] = item

    # Assets and source records outside this invocation's root are never read or authorization inputs.
    for asset_id, asset in sorted(assets.items()):
        if asset["rootId"] != root_id:
            selected[asset_id] = {"id": asset_id, "rootId": asset["rootId"], "state": "unchecked", "reason": "unselected-root"}
    observed_source_ids = {o["sourceId"] for values in observations.values() for o in values}
    for source_id, source in sorted(sources.items()):
        if source_id in bad_graph:
            selected[source_id] = {"id": source_id, "rootId": None, "state": "unsafe-unresolvable", "reason": bad_graph[source_id]}
        elif source_id not in observed_source_ids:
            selected[source_id] = {"id": source_id, "rootId": None, "state": "unchecked",
                                   "reason": "no-recorded-observation" if source["locator"].startswith(f"local:{root_id}/") else "external-or-unmapped-source"}
        elif not source["locator"].startswith(f"local:{root_id}/"):
            selected[source_id] = {"id": source_id, "rootId": None, "state": "unchecked", "reason": "external-or-unmapped-source"}

    baselines = [r for r in records.values() if r["kind"] == "baseline"]
    observations_for_source: dict[str, list[dict[str, Any]]] = {}
    for values in observations.values():
        for observation in values:
            observations_for_source.setdefault(observation["sourceId"], []).append(observation)
    try:
        all_pages, page_inventory, inventory_issues = _discover_pages(vault)
        issues.extend(inventory_issues)
    except (OSError, ValueError) as exc:
        all_pages, page_inventory = set(), ((), ())
        code = _reason(exc)
        issues.append({"state": _state(code), "reason": "wiki-page-discovery-" + code})
    page_paths = all_pages | {baseline["page"] for baseline in baselines}
    pages: dict[str, dict[str, Any]] = {}
    page_snapshots: dict[str, tuple[bytes, tuple[int, ...]] | None] = {}
    page_meta: dict[str, dict[str, Any]] = {}
    for page_path in sorted(page_paths):
        page = {"path": page_path, "state": "current", "claims": [], "sourceReferences": [], "baselines": [], "reviewReasons": []}
        pages[page_path] = page
        try:
            raw, page_hash, stamp = _read_page(vault, page_path)
            page_snapshots[page_path] = (raw, stamp)
            metadata, error = _metadata(raw)
            page_meta[page_path] = metadata
            if error:
                page["state"] = "unsafe-unresolvable"
                page["reviewReasons"].append(error)
                issues.append({"path": page_path, "state": "invalid", "reason": error})
            else:
                page["currentHash"] = page_hash
        except (OSError, ValueError) as exc:
            page_snapshots[page_path] = None
            page_meta[page_path] = {}
            code = _reason(exc)
            page["state"] = _state(code)
            page["reviewReasons"].append(code)
            issues.append({"path": page_path, "state": page["state"], "reason": code})
        meta = page_meta[page_path]
        claim_refs = meta.get("claims", [])
        if claim_refs is None:
            claim_refs = []
        if not isinstance(claim_refs, list):
            issues.append({"path": page_path, "state": "invalid", "reason": "invalid-page-claims"})
            page["reviewReasons"].append("invalid-page-claims")
        else:
            for claim_id in claim_refs:
                if not isinstance(claim_id, str) or not _CLAIM_ID.fullmatch(claim_id):
                    issues.append({"path": page_path, "state": "invalid", "reason": "invalid-page-claim-reference"})
                    page["reviewReasons"].append("invalid-page-claim-reference")
                else:
                    page["claims"].append(claim_id)
        refs = meta.get("sources", [])
        if refs is None:
            refs = []
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list):
            issues.append({"path": page_path, "state": "invalid", "reason": "invalid-page-sources"})
            page["reviewReasons"].append("invalid-page-sources")
            refs = []
        singleton_resource = meta.get("resource")
        if isinstance(singleton_resource, str):
            refs = [{"resource": singleton_resource}, *refs]
        elif singleton_resource is not None:
            issues.append({"path": page_path, "state": "invalid", "reason": "invalid-page-resource"})
        for ref in refs:
            identity = ref.get("id") if isinstance(ref, dict) else None
            has_identity = isinstance(ref, dict) and "id" in ref
            has_resource = isinstance(ref, dict) and "resource" in ref
            resource = ref.get("resource") if isinstance(ref, dict) else ref if isinstance(ref, str) else None
            canonical_identity = isinstance(identity, str) and bool(_SOURCE_ID.fullmatch(identity))
            legacy_identity = (isinstance(identity, str) and bool(_LEGACY_SOURCE_ID.fullmatch(identity))
                               and not identity.startswith("source-"))
            if has_identity and not canonical_identity and not legacy_identity:
                issues.append({"path": page_path, "state": "invalid", "reason": "invalid-page-source-id"})
                page["sourceReferences"].append({"id": None, "state": "invalid-reference", "reason": "invalid-page-source-id"})
            elif has_resource and not isinstance(resource, str):
                issues.append({"path": page_path, "id": identity if isinstance(identity, str) and _SOURCE_ID.fullmatch(identity) else None,
                               "state": "invalid", "reason": "invalid-page-source-resource"})
                page["sourceReferences"].append({"id": identity if isinstance(identity, str) and _SOURCE_ID.fullmatch(identity) else None,
                                                  "state": "invalid-reference", "reason": "invalid-page-source-resource"})
            elif canonical_identity and identity not in sources:
                issues.append({"path": page_path, "id": identity, "state": "invalid", "reason": "missing-source-record"})
                page["sourceReferences"].append({"id": identity, "state": "missing-source-record", "reason": "missing-source-record"})
            elif canonical_identity and has_resource and resource != sources[identity]["locator"]:
                issues.append({"path": page_path, "id": identity, "state": "invalid", "reason": "page-source-resource-mismatch"})
                page["sourceReferences"].append({"id": identity, "state": "invalid-reference", "reason": "page-source-resource-mismatch"})
            elif legacy_identity:
                page["sourceReferences"].append({"id": identity, "state": "external-or-legacy-unchecked"})
            elif canonical_identity:
                linked = observations_for_source.get(identity, [])
                if not linked:
                    page["sourceReferences"].append({"id": identity, "state": "unchecked", "reason": "no-recorded-observation"})
                for observation in sorted(linked, key=lambda value: (value["assetId"], value["id"])):
                    if observation["rootId"] != root_id:
                        page["sourceReferences"].append({"id": identity, "assetId": observation["assetId"],
                                                          "state": "unchecked", "reason": "unselected-root"})
                        continue
                    current = selected.get(observation["assetId"], {})
                    if current.get("state") in {"unstable", "missing", "unsafe-unresolvable", "operational-error"}:
                        state, reason = current["state"], current.get("reason", current["state"])
                    elif current.get("currentHash") is None:
                        state, reason = "unchecked", current.get("reason", "source-unavailable")
                    elif current["currentHash"] == observation["contentHash"]:
                        state, reason = "unchanged", None
                    else:
                        state, reason = "changed", "recorded-source-snapshot-differs"
                    entry = {"id": identity, "assetId": observation["assetId"], "state": state}
                    if reason:
                        entry["reason"] = reason
                    page["sourceReferences"].append(entry)
                    if state in {"changed", "missing", "unsafe-unresolvable", "operational-error", "unstable"}:
                        page["state"] = "affected" if page["state"] == "current" else page["state"]
                        page["reviewReasons"].append("direct-source-" + state)
            elif isinstance(resource, str) and resource.startswith(f"local:{root_id}/"):
                page["sourceReferences"].append({"id": None, "state": "portable-source-unmapped", "reason": "missing-source-id"})
            elif resource is not None:
                page["sourceReferences"].append({"id": None, "state": "external-or-legacy-unchecked"})
            else:
                issues.append({"path": page_path, "state": "invalid", "reason": "invalid-page-source-reference"})
                page["sourceReferences"].append({"id": None, "state": "invalid-reference", "reason": "invalid-page-source-reference"})
        page["claims"] = sorted(set(page["claims"]))
        invalid_refs = [ref for ref in page["sourceReferences"] if ref["state"] in {"invalid-reference", "missing-source-record"}]
        if invalid_refs:
            page["state"] = "unsafe-unresolvable"
            page["reviewReasons"].extend(ref.get("reason", ref["state"]) for ref in invalid_refs)
        page["sourceReferences"].sort(key=lambda x: (x.get("id") or "", x.get("assetId", ""), x["state"], x.get("reason", "")))

    observation_by_id = {r["id"]: r for r in records.values() if r["kind"] == "observation"}
    baseline_bad = {identity: reason for identity, reason in bad_graph.items() if identity in {r["id"] for r in baselines}}
    for baseline in sorted(baselines, key=lambda r: (r["page"], r["assetId"], r["id"])):
        page = pages[baseline["page"]]
        latest = baseline["history"][-1]
        asset = assets.get(baseline["assetId"])
        entry: dict[str, Any] = {"id": baseline["id"], "assetId": baseline["assetId"],
                                 "latestSourceId": latest["sourceId"], "recordedHash": latest["contentHash"],
                                 "pageHash": latest["pageHash"]}
        if baseline["id"] in baseline_bad:
            entry.update(state="unsafe-unresolvable", reason=baseline_bad[baseline["id"]])
            page["state"] = "unsafe-unresolvable"
            page["reviewReasons"].append(baseline_bad[baseline["id"]])
            issues.append({"path": baseline["page"], "id": baseline["id"], "state": "invalid", "reason": baseline_bad[baseline["id"]]})
        elif asset is None:
            entry.update(state="unsafe-unresolvable", reason="missing-baseline-asset")
            page["state"] = "unsafe-unresolvable"
            page["reviewReasons"].append("missing-baseline-asset")
            issues.append({"path": baseline["page"], "id": baseline["id"], "state": "invalid", "reason": "missing-baseline-asset"})
        elif asset["rootId"] != root_id:
            entry.update(state="unchecked", reason="unselected-root")
        else:
            item = selected.get(asset["id"], {})
            entry["state"] = "unchanged" if item.get("currentHash") == latest["contentHash"] else item.get("state", "unchecked")
            if item.get("state") == "changed" and item.get("currentHash") == latest["contentHash"]:
                entry["state"] = "unchanged"
            observation = observation_by_id.get(latest["observationId"])
            entry["locatorChanged"] = bool(observation and observation["locator"] != asset["locator"])
            if entry["state"] in {"changed", "missing", "unsafe-unresolvable", "operational-error", "unstable"}:
                entry["reason"] = "latest-baseline-source-differs" if entry["state"] == "changed" else item.get("reason", entry["state"])
                page["state"] = "affected" if page["state"] == "current" else page["state"]
                page["reviewReasons"].append("latest-baseline-source-differs" if entry["state"] == "changed" else entry["reason"])
        page_hash = page.get("currentHash")
        entry["pageState"] = "unchecked" if page_hash is None else "unchanged" if page_hash == latest["pageHash"] else "changed"
        if entry["pageState"] == "changed":
            page["state"] = "page-changed" if page["state"] == "current" else page["state"]
            page["reviewReasons"].append("page-hash-differs-from-latest-baseline")
        page["baselines"].append(entry)

    claim_rows = []
    for claim in sorted((r for r in records.values() if r["kind"] == "claim"), key=lambda r: r["id"]):
        evidence = []
        review_reasons = []
        for relation in ("support", "contradictions"):
            for source_id in claim[relation]:
                source = sources.get(source_id)
                linked = observations_for_source.get(source_id, [])
                if source is None or source_id in bad_graph:
                    evidence.append({"sourceId": source_id, "relation": relation, "state": "missing-source-record" if source is None else "unsafe-unresolvable", "reason": "broken-claim-reference"})
                    if source is None:
                        issues.append({"id": claim["id"], "state": "invalid", "reason": "missing-claim-source"})
                    review_reasons.append("broken-claim-reference")
                    continue
                if not linked:
                    evidence.append({"sourceId": source_id, "relation": relation, "state": "unchecked", "reason": "no-recorded-observation"})
                    continue
                for observation in sorted(linked, key=lambda x: (x["assetId"], x["id"])):
                    if observation["rootId"] != root_id:
                        evidence.append({"sourceId": source_id, "relation": relation, "assetId": observation["assetId"], "state": "unchecked", "reason": "unselected-root"})
                        continue
                    current = selected.get(observation["assetId"], {})
                    if current.get("currentHash") is None:
                        state = current.get("state", "unchecked")
                        reason = current.get("reason", "source-unavailable")
                    elif observation["contentHash"] == current["currentHash"]:
                        state, reason = "unchanged", None
                    else:
                        state, reason = "changed", "recorded-source-snapshot-differs"
                    detail = {"sourceId": source_id, "relation": relation, "assetId": observation["assetId"], "state": state}
                    if reason:
                        detail["reason"] = reason
                    evidence.append(detail)
                    if state in {"changed", "missing", "unsafe-unresolvable", "operational-error", "unstable"}:
                        review_reasons.append(f"{relation}-source-{state}")
        explicit_pages = sorted(page_path for page_path, page in pages.items() if claim["id"] in page["claims"])
        claim_rows.append({"id": claim["id"], "support": claim["support"], "contradictions": claim["contradictions"],
                           "evidence": evidence, "pages": explicit_pages,
                           "state": "review-suggested" if review_reasons else "declared-provenance",
                           "reviewRequired": bool(review_reasons), "reviewReasons": sorted(set(review_reasons))})
    known_claims = {r["id"] for r in claim_rows}
    for page in pages.values():
        for identity in page["claims"]:
            if identity not in known_claims:
                issues.append({"path": page["path"], "id": identity, "state": "invalid", "reason": "missing-page-claim-record"})
                page["reviewReasons"].append("missing-page-claim-record")
        page["reviewReasons"] = sorted(set(page["reviewReasons"]))

    # Final revalidation covers source identities, all inspected pages, page-set entries, and ledger bytes.
    ledger_changed = False
    page_set_changed = False
    if mapping is not None:
        for asset_id, asset in selected_assets.items():
            if asset_id not in inspected_assets:
                continue
            initial = source_stamps.get(asset_id)
            if initial is None or selected.get(asset_id, {}).get("currentHash") is None:
                # A missing/unsafe path is checked again so creation/removal during the run is unstable.
                try:
                    final_hash, final_stamp = BASELINES._inspect_selected(root, BASELINES._relative_locator(asset["locator"]), mapping,
                                                                          reject_hardlinks=True, strict_aliases=True)
                    if selected.get(asset_id, {}).get("state") != "unchanged":
                        selected[asset_id].update(state="unstable", reason="source-appeared-during-check")
                except (OSError, ValueError) as exc:
                    final_code = _reason(exc)
                    initial_code = selected.get(asset_id, {}).get("reason", selected.get(asset_id, {}).get("state"))
                    if final_code != initial_code and selected.get(asset_id, {}).get("state") not in {"unsafe-unresolvable"}:
                        selected[asset_id].update(state="unstable", reason="source-state-changed-during-check")
                continue
            try:
                final_hash, final_stamp = BASELINES._inspect_selected(root, BASELINES._relative_locator(asset["locator"]), mapping,
                                                                      reject_hardlinks=True, strict_aliases=True)
                if (final_hash, final_stamp) != initial:
                    selected[asset_id].update(state="unstable", reason="source-identity-changed")
            except (OSError, ValueError):
                selected[asset_id].update(state="unstable", reason="source-state-changed-during-check")
        try:
            BASELINES._assert_mapping(mapping)
        except (OSError, ValueError):
            issues.append({"state": "unstable", "reason": "selected-root-remapped"})
            for item in selected.values():
                if item.get("rootId") == root_id and item.get("state") in {"unchanged", "changed", "missing"}:
                    item.update(state="unstable", reason="selected-root-remapped")
    try:
        current_raw = load_source("source_drift_batch_final", str(Path(__file__).with_name("wiki_batch.py")))._ledger_files(
            vault, reject_hardlinks=True)
        if current_raw != raw_ledger:
            raise RuntimeError("ledger-snapshot-changed")
    except (OSError, ValueError, RuntimeError):
        ledger_changed = True
        issues.append({"state": "unstable", "reason": "ledger-changed-during-check"})
        for item in selected.values():
            if item.get("state") in {"unchanged", "changed", "missing"}:
                item.update(state="unstable", reason="ledger-changed-during-check")
        for page in pages.values():
            page.update(state="unstable", reason="ledger-changed-during-check")
        for claim in claim_rows:
            claim.update(state="unstable", reviewRequired=True)
            claim["reviewReasons"] = sorted(set(claim["reviewReasons"] + ["ledger-changed-during-check"]))
    unstable_page_paths: set[str] = set()
    for page_path, initial in page_snapshots.items():
        try:
            raw, page_hash, stamp = _read_page(vault, page_path)
            final = (raw, stamp)
        except (OSError, ValueError):
            final = None
        if initial != final:
            unstable_page_paths.add(page_path)
            pages[page_path].update(state="unstable", reason="page-identity-changed")
            pages[page_path]["reviewReasons"] = sorted(set(pages[page_path]["reviewReasons"] + ["page-identity-changed"]))
    try:
        final_pages, final_inventory, final_issues = _discover_pages(vault)
        if final_inventory != page_inventory or final_pages != all_pages:
            raise RuntimeError("page-set-changed")
        if final_issues != inventory_issues:
            issues.extend(final_issues)
            raise RuntimeError("page-set-invalid")
    except (OSError, ValueError, RuntimeError):
        page_set_changed = True
        issues.append({"state": "unstable", "reason": "page-set-changed-during-check"})
        for page in pages.values():
            page.update(state="unstable", reason="page-set-changed-during-check")
        for claim in claim_rows:
            claim.update(state="unstable", reviewRequired=True)
            claim["reviewReasons"] = sorted(set(claim["reviewReasons"] + ["page-set-changed-during-check"]))

    for item in selected.values():
        if item.get("state") == "unstable":
            for observation in item.get("observations", []):
                observation.update(state="unstable", reason=item.get("reason", "source-state-unstable"))
    _refresh_impacts(selected, pages, claim_rows, observations, bad_graph)
    for page_path in unstable_page_paths:
        page = pages[page_path]
        for reference in page["sourceReferences"]:
            reference["state"] = "unstable"
            reference["reason"] = "page-identity-changed"
        for baseline in page["baselines"]:
            baseline["pageState"] = "unstable"
        for claim in claim_rows:
            if page_path in claim["pages"]:
                claim["pageLinkState"] = "unstable"
                claim["state"] = "unstable"
                claim["reviewRequired"] = True
                claim["reviewReasons"] = sorted(set(claim["reviewReasons"] + ["page-link-coverage-unstable"]))
    if ledger_changed or page_set_changed:
        invalidation_reason = "ledger-changed-during-check" if ledger_changed else "page-set-changed-during-check"
        for page in pages.values():
            page.update(state="unstable", reason=invalidation_reason)
            for baseline in page["baselines"]:
                baseline["state"] = "unstable"
                baseline["pageState"] = "unstable"
                baseline["reason"] = invalidation_reason
            for reference in page["sourceReferences"]:
                reference["state"] = "unstable"
                reference["reason"] = invalidation_reason
        for claim in claim_rows:
            claim.update(state="unstable", reviewRequired=True, pageLinkState="unstable")
            claim["reviewReasons"] = sorted(set(claim["reviewReasons"] + [invalidation_reason]))
            for evidence in claim["evidence"]:
                evidence["state"] = "unstable"
                evidence["reason"] = invalidation_reason
        if ledger_changed:
            for item in selected.values():
                if item.get("rootId") == root_id and item.get("state") not in {"unchecked", "unsafe-unresolvable"}:
                    item.update(state="unstable", reason=invalidation_reason)
    snapshot_validity = "unstable" if any(issue.get("state") == "unstable" for issue in issues) or any(item.get("state") == "unstable" for item in selected.values()) or any(page.get("state") == "unstable" for page in pages.values()) else "stable"
    unchecked = [{"id": item["id"], "reason": item.get("reason", "unchecked")}
                 for item in selected.values() if item.get("state") == "unchecked"]
    unsafe_states = {"unsafe-unresolvable", "operational-error", "permission-denied", "path-collision", "unstable"}
    incomplete = (bool(issues)
                  or any(item.get("state") in unsafe_states or item.get("state") == "unchecked"
                         and item.get("reason") not in {"unselected-root", "external-or-unmapped-source"}
                         for item in selected.values())
                  or any(page.get("state") in unsafe_states for page in pages.values()))
    ordered_assets = sorted(selected.values(), key=lambda x: (x.get("rootId") or "", x["id"]))
    ordered_pages = sorted(pages.values(), key=lambda x: x["path"])
    return {"schema": "okf-source-drift-v1", "complete": not incomplete, "snapshotValidity": snapshot_validity,
            "rootId": root_id, "assets": ordered_assets, "pages": ordered_pages,
            "claims": claim_rows,
            "coverage": {"selectedAssets": len(selected_assets), "discoveredPages": len(all_pages),
                         "baselinedPages": len({r["page"] for r in baselines}),
                         "unlinkedClaims": sorted(c["id"] for c in claim_rows if not c["pages"]),
                         "unlinkedPages": sorted(p["path"] for p in pages.values() if not p["claims"] and not p["sourceReferences"] and not p["baselines"]),
                         "unsupportedReferenceCount": sum(ref["state"] in {"external-or-legacy-unchecked", "portable-source-unmapped", "ledger-source-unchecked"}
                                                           for p in pages.values() for ref in p["sourceReferences"]),
                         "scope": "declared provenance only; no exhaustive or factual impact inference"},
            "issues": sorted(issues, key=lambda x: (x.get("path", ""), x.get("id", ""), x.get("reason", ""))),
            "unchecked": unchecked}
