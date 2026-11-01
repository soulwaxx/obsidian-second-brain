#!/usr/bin/env python3
"""Reviewed, ordered, recoverable vault batches (not filesystem-wide atomic)."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import uuid
from typing import Any
from urllib.parse import quote
from module_loading import load_source

LIFECYCLE = load_source("wiki_batch_lifecycle", str(Path(__file__).with_name("wiki_lifecycle.py")))
CONFIG = load_source("wiki_batch_config", str(Path(__file__).with_name("config_contract.py")))
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MIDDLEWARE = PACKAGE_ROOT / "skills/wiki/scripts/okf_mw"
BM25_SCRIPT = PACKAGE_ROOT / "scripts/bm25-index.py"
AUTHORITIES = {"wiki-page", "source-capture", "wiki-ledger"}


class BatchError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_valid_config(config: Path) -> None:
    _value, error = CONFIG.load_config(Path(config).expanduser().absolute())
    if error:
        raise BatchError("invalid selected integration config: " + error)


def config_identity(config: Path) -> dict[str, str | None]:
    config = config.expanduser().absolute()
    require_valid_config(config)
    try:
        content = config.read_bytes()
    except FileNotFoundError:
        content = None
    except OSError as exc:
        raise BatchError(f"cannot bind approval to config: {exc}") from exc
    return {"path": str(config), "sha256": sha(content) if content is not None else None}


def validate_bundle(bundle: Any) -> dict:
    if not isinstance(bundle, dict) or set(bundle) != {"version", "operations"} or bundle.get("version") != 1:
        raise BatchError("bundle must be a version-1 object with an operations list")
    operations = bundle["operations"]
    if not isinstance(operations, list) or not operations:
        raise BatchError("operations must be a nonempty list")
    for op in operations:
        if not isinstance(op, dict) or set(op) != {"path", "expectedHash", "content"}:
            raise BatchError("each operation must contain exactly path, expectedHash, and content")
        if not isinstance(op["path"], str) or not isinstance(op["content"], str):
            raise BatchError("operation path and content must be strings")
        expected = op["expectedHash"]
        if expected is not None:
            if not isinstance(expected, str) or len(expected) != 64:
                raise BatchError("expectedHash must be a SHA-256 hex digest or null for absence")
            try:
                int(expected, 16)
            except ValueError as exc:
                raise BatchError("expectedHash must be a SHA-256 hex digest") from exc
    return bundle


def _authority_path(authority: str, rel: PurePosixPath) -> None:
    parts = rel.parts
    if authority == "wiki-page":
        if len(parts) < 2 or parts[0] != "wiki" or not rel.name.lower().endswith(".md"):
            raise BatchError("wiki-page authority is limited to Markdown under wiki/")
        if rel.name.casefold() in {"index.md", "log.md", "_plan.md"}:
            raise BatchError("index.md, log.md, and _plan.md are lifecycle/middleware-owned")
        if any(part.startswith(".") for part in parts[1:]):
            raise BatchError("hidden wiki paths are not batch destinations")
    elif authority == "source-capture":
        if len(parts) < 3 or parts[:2] != (".raw", "agent-captures") or rel.suffix.casefold() not in {".md", ".txt"}:
            raise BatchError("source-capture authority is limited to Markdown/text beneath .raw/agent-captures/")
    elif authority == "wiki-ledger":
        if len(parts) < 4 or parts[:3] != ("wiki", "meta", "evidence") or not rel.name.endswith(".json"):
            raise BatchError("wiki-ledger authority is limited to JSON beneath wiki/meta/evidence/")
    else:
        raise BatchError("unknown batch authority")
    if any(part in {"", ".", ".."} for part in parts):
        raise BatchError("unsafe destination path")


def _validate_paths(operations: list[dict], authority: str) -> None:
    folded: dict[str, str] = {}
    directory_spellings: dict[str, str] = {}
    parts_by_path = []
    for op in operations:
        raw = op["path"]
        rel = PurePosixPath(raw)
        if rel.is_absolute() or "\\" in raw or not raw or rel.as_posix() != raw:
            raise BatchError("destination must be a canonical vault-relative POSIX path")
        _authority_path(authority, rel)
        key = "/".join(part.casefold() for part in rel.parts)
        if key in folded:
            raise BatchError(f"duplicate or case-colliding destinations: {folded[key]} and {raw}")
        folded[key] = raw
        for depth in range(1, len(rel.parts)):
            prefix = rel.parts[:depth]
            folded_prefix = "/".join(part.casefold() for part in prefix)
            spelling = "/".join(prefix)
            prior = directory_spellings.setdefault(folded_prefix, spelling)
            if prior != spelling:
                raise BatchError(f"case-insensitive directory collision: {prior} and {spelling}")
        parts_by_path.append((raw, tuple(part.casefold() for part in rel.parts)))
    for i, (left, left_parts) in enumerate(parts_by_path):
        for right, right_parts in parts_by_path[i + 1:]:
            shorter = min(len(left_parts), len(right_parts))
            if left_parts[:shorter] == right_parts[:shorter]:
                raise BatchError(f"ancestor-conflicting destinations: {left} and {right}")
    if authority == "source-capture" and any(op["expectedHash"] is not None for op in operations):
        raise BatchError("source captures are immutable create-only operations")


def _target(vault: Path, raw: str, authority: str, create_dirs: bool = False, allow_missing: bool = False) -> tuple[Path, int | None, str]:
    rel = PurePosixPath(raw)
    _authority_path(authority, rel)
    vault = vault.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(vault, flags)
    try:
        for part in rel.parts[:-1]:
            names = os.listdir(fd)
            if part not in names and any(name.casefold() == part.casefold() for name in names):
                raise BatchError(f"case-insensitive destination collision at {part}")
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if create_dirs:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    child = os.open(part, flags, dir_fd=fd)
                elif allow_missing:
                    os.close(fd)
                    return vault / Path(*rel.parts), None, rel.parts[-1]
                else:
                    raise
            except OSError as exc:
                raise BatchError(f"unsafe destination parent {part}: {exc}") from exc
            os.close(fd)
            fd = child
        name = rel.parts[-1]
        names = os.listdir(fd)
        if name not in names and any(item.casefold() == name.casefold() for item in names):
            raise BatchError(f"case-insensitive destination collision at {name}")
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise BatchError("destination exists and is not a regular file")
        except FileNotFoundError:
            pass
        return vault / Path(*rel.parts), fd, name
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _read_at(parent: int, name: str) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BatchError(f"cannot read target safely: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BatchError("target is not a regular file")
        chunks = []
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            while chunk := stream.read(1024 * 1024):
                chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if fd >= 0:
            os.close(fd)


def _validate_draft(vault: Path, op: dict, authority: str) -> None:
    content = op["content"]
    if authority == "wiki-page":
        if not (vault / "wiki").is_dir() or (vault / "wiki").is_symlink():
            raise BatchError("wiki directory is missing or symlinked")
        fd, temp = tempfile.mkstemp(suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
            result = subprocess.run([sys.executable, str(MIDDLEWARE / "validate.py"), temp], text=True, capture_output=True)
            if result.returncode:
                raise BatchError("draft page failed OKF validation: " + (result.stdout + result.stderr).strip())
        finally:
            os.unlink(temp)
    elif authority == "wiki-ledger":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise BatchError(f"ledger draft is not valid JSON: {exc}") from exc
        try:
            ledger = load_source("wiki_batch_evidence_ledger", str(Path(__file__).with_name("evidence_ledger.py")))
            ledger.validate_record(parsed, expected_id=PurePosixPath(op["path"]).stem)
            if op["path"] != ledger.record_path(parsed):
                raise BatchError("ledger destination must match the canonical evidence ledger path")
        except (OSError, ValueError, TypeError) as exc:
            raise BatchError(f"ledger draft failed evidence schema validation: {exc}") from exc
    elif authority == "source-capture" and (not content or "\x00" in content):
        raise BatchError("source capture draft must be nonempty text without NUL bytes")


def _journal_preimages(journal: dict) -> dict[str, bytes | None]:
    return {entry["path"]: (base64.b64decode(entry["beforeContent"]) if entry["beforeContent"] is not None else None)
            for entry in journal["entries"]}


def _ledger_files(vault: Path) -> dict[str, bytes]:
    """Safely enumerate ledger bytes without parsing records or following links."""
    vault = Path(vault).resolve(strict=True)
    probe = os.open(vault, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for part in ("wiki", "meta", "evidence"):
            names = os.listdir(probe)
            matching = [name for name in names if name.casefold() == part.casefold()]
            if matching and matching != [part]:
                raise BatchError(f"case-insensitive ledger namespace collision at {part}")
            if not matching:
                return {}
            child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=probe)
            os.close(probe)
            probe = child
    except OSError as exc:
        raise BatchError(f"evidence ledger namespace is unsafe: {exc}") from exc
    finally:
        os.close(probe)
    try:
        directory_fd = LIFECYCLE.secure_dir(vault / "wiki/meta/evidence", create=False)
    except OSError as exc:
        raise BatchError(f"evidence ledger namespace is unsafe: {exc}") from exc
    try:
        names = os.listdir(directory_fd)
        folded: dict[str, str] = {}
        record_names = []
        for name in names:
            key = name.casefold()
            if key in folded:
                raise BatchError(f"case-insensitive ledger filename collision: {folded[key]} and {name}")
            folded[key] = name
            if re.fullmatch(r"\.batch-[0-9a-f-]{36}-[0-9a-f]{32}\.tmp", name):
                try:
                    temp_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
                    try:
                        if not stat.S_ISREG(os.fstat(temp_fd).st_mode):
                            raise BatchError("ledger batch temporary entry is not a regular file")
                    finally:
                        os.close(temp_fd)
                except OSError as exc:
                    raise BatchError("ledger batch temporary entry is unsafe") from exc
                continue
            if not name.endswith(".json") or name[:-5] != name[:-5].casefold():
                raise BatchError(f"noncanonical ledger filename: {name}")
            record_names.append(name)
        result: dict[str, bytes] = {}
        for name in record_names:
            try:
                fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
            except OSError as exc:
                raise BatchError(f"cannot open ledger record safely: {name}") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise BatchError(f"ledger record is not a regular file: {name}")
                chunks = []
                with os.fdopen(fd, "rb") as stream:
                    fd = -1
                    while chunk := stream.read(1024 * 1024):
                        chunks.append(chunk)
                result[name] = b"".join(chunks)
            finally:
                if fd >= 0:
                    os.close(fd)
        return result
    finally:
        os.close(directory_fd)


def _ledger_snapshot(vault: Path) -> dict[str, tuple[dict, bytes]]:
    files = _ledger_files(vault)
    result = {}
    ledger = load_source("wiki_batch_evidence_ledger", str(Path(__file__).with_name("evidence_ledger.py")))
    for name, raw in files.items():
        try:
            value = json.loads(raw)
            ledger.validate_record(value, expected_id=name[:-5])
            if ledger.record_path(value).rsplit("/", 1)[-1] != name:
                raise ValueError("record path does not match canonical filename")
        except (ValueError, TypeError) as exc:
            raise BatchError(f"invalid ledger record {name}: {exc}") from exc
        result[value["id"]] = (value, raw)
    return result


def _page_batch_evidence(vault: Path, batch_id: str, page: str, page_hash: str, outcome: str) -> None:
    try:
        uuid.UUID(batch_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise BatchError("baseline references an invalid page batch ID") from exc
    journal = _load_journal(vault, batch_id)
    if journal["authority"] != "wiki-page" or journal["phase"] != "complete":
        raise BatchError("baseline requires a completed wiki-page authority batch")
    entry = next((item for item in journal["entries"] if item["path"] == page), None)
    if (entry is None or entry["state"] != "published" or entry["afterHash"] != page_hash
            or (outcome == "no-change") != (entry["beforeHash"] == entry["afterHash"])):
        raise BatchError("baseline page batch does not publish the referenced page bytes and outcome")
    path, parent, name = _target(vault, page, "wiki-page", allow_missing=True)
    try:
        current = _read_at(parent, name) if parent is not None else None
    finally:
        if parent is not None:
            os.close(parent)
    if current is None or sha(current) != page_hash:
        raise BatchError("baseline page bytes no longer match the completed page batch")


def _validate_ledger_operations(vault: Path, operations: list[dict], *, publication: bool = False,
                                original_preimages: dict[str, bytes | None] | None = None,
                                check_current_asset_locator: bool = True,
                                current_batch_id: str | None = None) -> None:
    ensure_no_unfinished_ledger_publication(vault, set(), except_batch_id=current_batch_id,
                                            operations=operations)
    ledger = load_source("wiki_batch_evidence_ledger", str(Path(__file__).with_name("evidence_ledger.py")))
    records = {key: value for key, (value, _raw) in _ledger_snapshot(vault).items()}
    for operation in operations:
        try:
            record = json.loads(operation["content"])
            ledger.validate_record(record, expected_id=PurePosixPath(operation["path"]).stem)
        except (ValueError, TypeError) as exc:
            raise BatchError(f"invalid ledger operation: {exc}") from exc
        raw_before = (original_preimages or {}).get(operation["path"], ...)
        prior = records.get(record["id"])
        if raw_before is not ...:
            prior = None
            if raw_before is not None:
                try:
                    prior = json.loads(raw_before)
                    ledger.validate_record(prior, expected_id=record["id"])
                except (ValueError, TypeError) as exc:
                    raise BatchError("journal ledger preimage is invalid") from exc
        if prior is not None:
            if record["kind"] in {"asset", "observation", "baseline"}:
                merged = ledger.merge_record(prior, record)
                if merged != record:
                    raise BatchError("ledger update must preserve existing fields and unknown extensions")
            if record["kind"] == "observation" and ledger._observation_material(prior) != ledger._observation_material(record):
                raise BatchError("observation immutable identity material cannot change")
            if record["kind"] == "source":
                referenced = any(value.get("kind") == "observation" and value.get("sourceId") == record["id"]
                                 for value in records.values())
                referenced = referenced or any(
                    value.get("kind") == "baseline" and any(entry.get("sourceId") == record["id"]
                                                               for entry in value.get("history", []))
                    for value in records.values())
                if referenced and (prior["locator"] != record["locator"]
                                   or prior["contentHash"] != record["contentHash"]):
                    raise BatchError("source locator and content are immutable while referenced by observations or baselines")
            if record["kind"] == "asset" and prior["rootId"] != record["rootId"]:
                raise BatchError("asset root identity cannot change")
            if record["kind"] == "baseline":
                old_history = prior["history"]
                history = record["history"]
                if history[:len(old_history)] != old_history:
                    raise BatchError("baseline history must retain its existing prefix")
                added = history[len(old_history):]
            else:
                added = []
        else:
            added = record.get("history", []) if record["kind"] == "baseline" else []
        if record["kind"] == "observation":
            asset = records.get(record["assetId"])
            if asset is None or asset["kind"] != "asset" or asset["rootId"] != record["rootId"]:
                raise BatchError("observation requires its matching durable asset")
            if (check_current_asset_locator and prior is None
                    and asset["locator"] != record["locator"]):
                raise BatchError("new observation locator must match the asset at publication")
            if not publication and record["assetId"] not in records:
                raise BatchError("observation asset must precede it in the approved ledger batch")
            source = records.get(record["sourceId"])
            if source is None or source["kind"] != "source" or source["contentHash"] != record["contentHash"]:
                raise BatchError("observation requires its matching durable source digest")
            if not publication and record["sourceId"] not in records:
                raise BatchError("observation source must precede it in the approved ledger batch")
            expected_locator = "local:" + record["rootId"] + "/" + "/".join(quote(part, safe="-._~") for part in record["locator"].split("/"))
            source_id = ledger.make_source_digest(source["locator"], source["contentHash"])["id"]
            if (source["locator"] != expected_locator or source_id != source["id"]
                    or source["contentHash"] != record["contentHash"]):
                raise BatchError("observation source identity, locator, or content does not match its asset")
        if record["kind"] == "asset" and prior is not None and prior["rootId"] != record["rootId"]:
            raise BatchError("asset root identity cannot change")
        if record["kind"] == "baseline":
            asset = records.get(record["assetId"])
            if asset is None or asset["kind"] != "asset":
                raise BatchError("baseline requires its matching durable asset")
            for entry in record["history"]:
                observation = records.get(entry["observationId"])
                if (observation is None or observation["kind"] != "observation"
                        or observation["assetId"] != record["assetId"]):
                    raise BatchError("baseline entry requires its matching observation")
                source = records.get(entry["sourceId"])
                if source is None or source["kind"] != "source" or observation["sourceId"] != entry["sourceId"]:
                    raise BatchError("baseline entry source does not match its observation")
                source_id = ledger.make_source_digest(source["locator"], source["contentHash"])["id"]
                expected_locator = "local:" + observation["rootId"] + "/" + "/".join(quote(part, safe="-._~") for part in observation["locator"].split("/"))
                if (source_id != source["id"] or source["locator"] != expected_locator
                        or observation["contentHash"] != source["contentHash"]
                        or entry["contentHash"] != source["contentHash"]):
                    raise BatchError("baseline entry source identity, locator, or content hash is inconsistent")
                if observation["rootId"] != asset["rootId"]:
                    raise BatchError("baseline observation asset/root identity mismatch")
            if added:
                for entry in added:
                    _page_batch_evidence(vault, entry["pageBatchId"], record["page"],
                                         entry["pageHash"].removeprefix("sha256:"), entry["outcome"])
            else:
                latest = record["history"][-1]
                _page_batch_evidence(vault, latest["pageBatchId"], record["page"],
                                     latest["pageHash"].removeprefix("sha256:"), latest["outcome"])
        records[record["id"]] = record


def _journal_dir(vault: Path, create: bool) -> Path:
    path = vault / ".vault-meta/lifecycle/batches"
    fd = LIFECYCLE.secure_dir(path, create=create)
    os.close(fd)
    return path


def _locked(vault: Path):
    class Lock:
        def __enter__(self):
            for warning in LIFECYCLE.exclusion_diagnostic(vault):
                if "lifecycle derived-state scope" in warning or "lifecycle contains tracked Git state" in warning:
                    raise BatchError("batch journal state is not safely Git-excluded: " + warning)
            parent = LIFECYCLE.secure_dir(_journal_dir(vault, True))
            try:
                self.fd = os.open("writer.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
            finally:
                os.close(parent)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self

        def __exit__(self, *_):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
    return Lock()


def _save_journal(vault: Path, journal: dict) -> None:
    LIFECYCLE.atomic_json(_journal_dir(vault, True) / f"{journal['batchId']}.json", journal)


def _load_journal(vault: Path, batch_id: str) -> dict:
    try:
        uuid.UUID(batch_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise BatchError("invalid batch ID") from exc
    parent = LIFECYCLE.secure_dir(_journal_dir(vault, False))
    try:
        fd = os.open(f"{batch_id}.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise BatchError("batch journal is not a regular file")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                data = json.load(stream)
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, ValueError) as exc:
        raise BatchError(f"cannot read batch journal safely: {exc}") from exc
    finally:
        os.close(parent)
    if (not isinstance(data, dict) or data.get("version") != 1
            or data.get("vault") != str(vault.resolve(strict=True)) or data.get("batchId") != batch_id):
        raise BatchError("batch journal has an incompatible vault boundary")
    try:
        bundle = validate_bundle(data["bundle"])
        _validate_paths(bundle["operations"], data["authority"])
        entries = data["entries"]
        if len(entries) != len(bundle["operations"]):
            raise BatchError("journal operation count does not match bundle")
        if data["authority"] not in AUTHORITIES:
            raise BatchError("journal authority is invalid")
        if data.get("phase") not in {"approved", "recorded", "finalizing", "complete", "rollback-conflict", "rolled-back"}:
            raise BatchError("journal phase is invalid")
        for index, (op, entry) in enumerate(zip(bundle["operations"], entries)):
            if type(entry.get("index")) is not int or entry["index"] != index:
                raise BatchError("journal operation indexes do not match approved bundle order")
            content = op["content"].encode("utf-8")
            before = base64.b64decode(entry["beforeContent"]) if entry["beforeContent"] is not None else None
            if (entry["path"] != op["path"] or entry["afterHash"] != sha(content)
                    or entry["beforeHash"] != op["expectedHash"]):
                raise BatchError("journal operation content does not verify")
            if (sha(before) if before is not None else None) != entry["beforeHash"]:
                raise BatchError("journal preimage does not verify")
            if entry["state"] not in {"pending", "publishing", "published", "rolled-back"}:
                raise BatchError("journal operation state is invalid")
            if type(entry["captured"]) is not bool or type(entry["recorded"]) is not bool:
                raise BatchError("journal lifecycle progress is invalid")
        plan = {"version": 1, "vault": data["vault"], "config": data["config"],
                "authority": data["authority"], "bundle": bundle,
                "contentHash": sha(canonical_json(bundle["operations"]))}
        if sha(canonical_json(plan)) != data["planHash"]:
            raise BatchError("journal approval hash does not verify")
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchError(f"batch journal is malformed: {exc}") from exc
    if "cliBinding" in data:
        if (not isinstance(data.get("cliBinding"), dict)
                or data["cliBinding"].get("ledgerPlanHash") != data["planHash"]
                or data.get("cliPlanHash") != sha(canonical_json(data["cliBinding"]))):
            raise BatchError("journal CLI approval binding does not verify")
    return data


def completed_cli_replay(vault: Path, cli_plan_hash: str, stable_binding: dict,
                         config: Path) -> dict | None:
    vault = Path(vault).resolve(strict=True)
    directory = vault / ".vault-meta/lifecycle/batches"
    try:
        dir_fd = LIFECYCLE.secure_dir(directory, create=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BatchError(f"batch history is unsafe: {exc}") from exc
    try:
        for name in os.listdir(dir_fd):
            if name == "writer.lock":
                continue
            if not name.endswith(".json"):
                raise BatchError("batch history contains an unrecognized entry")
            try:
                journal = _load_journal(vault, name[:-5])
            except (OSError, ValueError, TypeError, BatchError) as exc:
                raise BatchError(f"batch history is incomplete or unsafe at {name}: {exc}") from exc
            binding = journal.get("cliBinding")
            if (journal["phase"] != "complete" or journal.get("cliPlanHash") != cli_plan_hash
                    or not isinstance(binding, dict)):
                continue
            stable = {key: value for key, value in binding.items() if key != "ledgerPlanHash"}
            if stable != stable_binding:
                continue
            _require_approved_config(journal, config)
            _verify_postimages(vault, journal)
            if journal["authority"] == "wiki-ledger":
                _validate_ledger_operations(vault, journal["bundle"]["operations"],
                                            original_preimages=_journal_preimages(journal),
                                            check_current_asset_locator=False)
            return {"batchId": journal["batchId"], "planHash": cli_plan_hash,
                    "phase": "complete", "noop": True}
        return None
    finally:
        os.close(dir_fd)


def _ledger_dependency_ids(vault: Path, operations: list[dict], seeds: set[str] | None = None) -> set[str]:
    records = {key: value for key, (value, _raw) in _ledger_snapshot(vault).items()}
    ids = set(seeds or ())
    for operation in operations:
        try:
            record = json.loads(operation["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise BatchError("cannot determine ledger batch dependencies") from exc
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise BatchError("cannot determine ledger batch dependencies")
        ids.add(record["id"])
        records[record["id"]] = record
    changed = True
    while changed:
        before = len(ids)
        for record in records.values():
            kind = record.get("kind")
            if kind == "observation":
                if (record.get("id") in ids or record.get("assetId") in ids
                        or record.get("sourceId") in ids):
                    ids.update((record.get("id"), record.get("assetId"), record.get("sourceId")))
            elif kind == "baseline":
                history = record.get("history", [])
                related = {record.get("id"), record.get("assetId")}
                for entry in history if isinstance(history, list) else ():
                    if isinstance(entry, dict):
                        related.update((entry.get("observationId"), entry.get("sourceId")))
                if ids & related:
                    ids.update(related)
        ids.discard(None)
        changed = len(ids) != before
    return ids


def ensure_no_unfinished_ledger_publication(vault: Path, record_ids: set[str],
                                            *, except_batch_id: str | None = None,
                                            operations: list[dict] | None = None) -> None:
    vault = Path(vault).resolve(strict=True)
    record_ids = _ledger_dependency_ids(vault, operations or [], record_ids)
    directory = vault / ".vault-meta/lifecycle/batches"
    try:
        dir_fd = LIFECYCLE.secure_dir(directory, create=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BatchError(f"batch history is unsafe: {exc}") from exc
    try:
        names = os.listdir(dir_fd)
        for name in names:
            if name == "writer.lock":
                continue
            if not name.endswith(".json"):
                raise BatchError("batch history contains an unrecognized entry")
            try:
                journal = _load_journal(vault, name[:-5])
            except (OSError, ValueError, TypeError, BatchError) as exc:
                raise BatchError(f"batch history is incomplete or unsafe at {name}: {exc}") from exc
            if (journal["authority"] != "wiki-ledger" or journal["phase"] in {"complete", "rolled-back"}
                    or journal["batchId"] == except_batch_id):
                continue
            touched = _ledger_dependency_ids(vault, journal["bundle"]["operations"])
            if touched & record_ids:
                raise BatchError("a relevant wiki-ledger batch is unfinished; recover or roll it back first")
    finally:
        os.close(dir_fd)


def source_capture_history(vault: Path) -> list[dict[str, Any]]:
    """Read retained source-capture publication journals without creating state.

    Any unsafe, malformed, or unrecognized journal entry makes the history
    unusable as publication evidence and fails closed. A missing journal
    namespace is an empty history, not an instruction to create one.
    """
    vault = Path(vault).resolve(strict=True)
    directory = vault / ".vault-meta/lifecycle/batches"
    try:
        dir_fd = LIFECYCLE.secure_dir(directory, create=False)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BatchError(f"cannot inspect batch history safely: {exc}") from exc
    lock_fd = None
    try:
        names = os.listdir(dir_fd)
        if not names:
            return []
        if any(name != "writer.lock" and not name.endswith(".json") for name in names):
            raise BatchError("batch history contains an unrecognized entry")
        if "writer.lock" in names:
            lock_info = os.stat("writer.lock", dir_fd=dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(lock_info.st_mode):
                raise BatchError("batch history lock is unsafe")
            lock_fd = os.open("writer.lock", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
        elif any(name.endswith(".json") for name in names):
            raise BatchError("batch history journals have no safe lock")

        history = []
        for name in sorted(names):
            if name == "writer.lock":
                continue
            stem = name[:-5]
            try:
                if str(uuid.UUID(stem)) != stem:
                    raise ValueError("noncanonical batch ID")
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise BatchError("batch journal is not a regular file")
                journal = _load_journal(vault, stem)
            except (OSError, ValueError, TypeError, AttributeError, BatchError) as exc:
                raise BatchError(f"batch history is incomplete or unsafe at {name}: {exc}") from exc
            if journal["authority"] == "source-capture":
                history.append({
                    "batchId": journal["batchId"],
                    "phase": journal["phase"],
                    "targets": {entry["path"]: entry["afterHash"] for entry in journal["entries"]},
                    "states": {entry["path"]: entry["state"] for entry in journal["entries"]},
                })
        return history
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(dir_fd)


def _approval_plan(vault: Path, bundle: dict, config: Path, authority: str) -> dict:
    require_valid_config(config)
    vault = Path(vault).resolve(strict=True)
    bundle = validate_bundle(bundle)
    if authority not in AUTHORITIES:
        raise BatchError("unknown batch authority")
    operations = bundle["operations"]
    _validate_paths(operations, authority)
    for op in operations:
        _validate_draft(vault, op, authority)
    if authority == "wiki-ledger":
        _validate_ledger_operations(vault, operations)
    return {"version": 1, "vault": str(vault), "config": config_identity(config),
            "authority": authority, "bundle": bundle,
            "contentHash": sha(canonical_json(operations))}


def _plan(vault: Path, bundle: dict, config: Path, authority: str) -> dict:
    plan = _approval_plan(vault, bundle, config, authority)
    vault = Path(vault).resolve(strict=True)
    operations = bundle["operations"]
    preimages = []
    for op in operations:
        path, parent, name = _target(vault, op["path"], authority, allow_missing=True)
        try:
            current = _read_at(parent, name) if parent is not None else None
        finally:
            if parent is not None:
                os.close(parent)
        actual = sha(current) if current is not None else None
        if actual != op["expectedHash"]:
            raise BatchError(f"target precondition mismatch for {op['path']}: expected {op['expectedHash']}, found {actual}")
        preimages.append(current)
    return {"plan": plan, "planHash": sha(canonical_json(plan)), "preimages": preimages}


def inspect(vault: Path, bundle: dict, config: Path, authority: str = "wiki-page") -> dict:
    planned = _plan(vault, bundle, config, authority)
    return {"plan": planned["plan"], "planHash": planned["planHash"],
            "targets": [op["path"] for op in planned["plan"]["bundle"]["operations"]],
            "authority": authority}


def _require_approved_config(journal: dict, selected: Path | None = None) -> Path:
    approved = journal.get("config")
    if not isinstance(approved, dict) or not isinstance(approved.get("path"), str):
        raise BatchError("batch journal has no valid approved configuration identity")
    config = Path(selected) if selected is not None else Path(approved["path"])
    if config_identity(config) != approved:
        raise BatchError("selected integration config identity changed since batch approval")
    return config.expanduser().absolute()


def _retrieval_refresh_enabled(config: Path) -> bool:
    value, error = CONFIG.load_config(config.expanduser().absolute())
    if error:
        raise BatchError("invalid selected integration config: " + error)
    features = (value or {}).get("features") or {}
    return features.get("retrievalRefresh") is not False


def _call_lifecycle(vault: Path, path: Path, batch_id: str, action: str,
                    config: Path | None = None, expected_config: dict | None = None) -> None:
    owner = f"batch-{batch_id}"
    if action == "capture":
        command = ["capture", "--vault", str(vault), "--cwd", str(vault), "--path", str(path), "--owner", owner, "--tool", "batch"]
    elif action == "record":
        command = ["record", "--vault", str(vault), "--cwd", str(vault), "--path", str(path), "--owner", owner, "--tool", "batch", "--validator", str(MIDDLEWARE / "validate.py")]
    else:
        command = ["finalize", "--vault", str(vault), "--middleware", str(MIDDLEWARE), "--owner", owner]
        if config is not None:
            if expected_config is not None and config_identity(config) != expected_config:
                raise BatchError("selected integration config identity changed before lifecycle finalization")
            if _retrieval_refresh_enabled(config):
                command.extend(("--retrieval-script", str(BM25_SCRIPT)))
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("wiki_lifecycle.py")), *command], text=True, capture_output=True)
    if result.returncode:
        raise BatchError("lifecycle " + action + " failed: " + (result.stdout + result.stderr).strip())


def _preflight_recovery(vault: Path, journal: dict) -> None:
    """Reject every known target conflict before recovery starts mutating."""
    authority = journal["authority"]
    for entry in journal["entries"]:
        path, parent, name = _target(vault, entry["path"], authority, allow_missing=True)
        try:
            current = _read_at(parent, name) if parent is not None else None
        finally:
            if parent is not None:
                os.close(parent)
        actual = sha(current) if current is not None else None
        if entry["state"] == "pending":
            allowed = {entry["beforeHash"]}
        elif entry["state"] == "publishing":
            # The process may have stopped on either side of the atomic replace.
            allowed = {entry["beforeHash"], entry["afterHash"]}
        elif entry["state"] == "published":
            allowed = {entry["afterHash"]}
        else:
            raise BatchError(f"recovery preflight refuses operation state {entry['state']}: {entry['path']}")
        if actual not in allowed:
            raise BatchError(f"recovery preflight conflict at {entry['path']}; preserving current bytes")


def _verify_postimages(vault: Path, journal: dict) -> None:
    for entry in journal["entries"]:
        path, parent, name = _target(vault, entry["path"], journal["authority"], allow_missing=True)
        try:
            current = _read_at(parent, name) if parent is not None else None
        finally:
            if parent is not None:
                os.close(parent)
        if (sha(current) if current is not None else None) != entry["afterHash"]:
            raise BatchError(f"completed batch target drifted; preserving current bytes: {entry['path']}")


def _clean_record_acknowledged(vault: Path, journal: dict, entry: dict) -> bool:
    """Recognize verified no-op or rolled-back bytes after clean owner settlement."""
    original_noop = entry["state"] == "published" and entry["beforeHash"] == entry["afterHash"]
    restored_preimage = entry["state"] == "rolled-back"
    if not (original_noop or restored_preimage):
        return False
    path, parent, name = _target(vault, entry["path"], journal["authority"], allow_missing=True)
    try:
        current = _read_at(parent, name) if parent is not None else None
    finally:
        if parent is not None:
            os.close(parent)
    if (sha(current) if current is not None else None) != entry["beforeHash"]:
        return False
    life, state_path, lock_path = LIFECYCLE.layout(vault, False)
    if not state_path.is_file() or state_path.is_symlink():
        return False
    owner = f"batch-{journal['batchId']}"
    with LIFECYCLE.locked(lock_path):
        state = LIFECYCLE.load_state(state_path, vault)
        key = str(path)
        if key in state["pending"]:
            return False
        for pending in state["pending"].values():
            captures = pending.get("captures") or {"manual": {"legacy": "active"}}
            if any(candidate != owner for candidate in captures):
                return False
    return True


def _record_lifecycle(vault: Path, journal: dict, entry: dict) -> None:
    try:
        _call_lifecycle(vault, vault / entry["path"], journal["batchId"], "record")
    except BatchError as exc:
        if ("postwrite result has no matching owner capture" not in str(exc)
                or not _clean_record_acknowledged(vault, journal, entry)):
            raise
    entry["recorded"] = True
    _save_journal(vault, journal)


def _prepare_lifecycle(vault: Path, journal: dict, config: Path) -> None:
    if journal["authority"] != "wiki-page":
        return
    for i, entry in enumerate(journal["entries"]):
        _require_approved_config(journal, config)
        if not entry["captured"]:
            _call_lifecycle(vault, vault / entry["path"], journal["batchId"], "capture")
            entry["captured"] = True
            _save_journal(vault, journal)


def _publish(vault: Path, journal: dict, finalize: bool = True,
             selected_config: Path | None = None) -> None:
    authority = journal["authority"]
    config = _require_approved_config(journal, selected_config)
    _prepare_lifecycle(vault, journal, config)
    for entry in journal["entries"]:
        _require_approved_config(journal, config)
        if authority == "wiki-ledger":
            _validate_ledger_operations(vault, [journal["bundle"]["operations"][entry["index"]]],
                                        publication=True,
                                        original_preimages={entry["path"]: base64.b64decode(entry["beforeContent"]) if entry["beforeContent"] is not None else None},
                                        current_batch_id=journal["batchId"])
        if entry["state"] == "published":
            path, parent, name = _target(vault, entry["path"], authority, allow_missing=True)
            try:
                current = _read_at(parent, name) if parent is not None else None
            finally:
                if parent is not None:
                    os.close(parent)
            if (sha(current) if current is not None else None) != entry["afterHash"]:
                raise BatchError(f"published target drifted; preserving current bytes: {entry['path']}")
            continue
        path, parent, name = _target(vault, entry["path"], authority, create_dirs=True)
        try:
            current = _read_at(parent, name)
            current_hash = sha(current) if current is not None else None
            if current_hash == entry["afterHash"]:
                entry["state"] = "published"
                _save_journal(vault, journal)
                continue
            if current_hash != entry["beforeHash"]:
                raise BatchError(f"target differs from approved preimage/result; preserving concurrent edit: {entry['path']}")
            temp_name = f".batch-{journal['batchId']}-{uuid.uuid4().hex}.tmp"
            fd = os.open(temp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
            identity = os.fstat(fd)
            try:
                with os.fdopen(fd, "wb") as stream:
                    fd = -1
                    stream.write(journal["bundle"]["operations"][entry["index"]]["content"].encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                latest = _read_at(parent, name)
                if (sha(latest) if latest is not None else None) != entry["beforeHash"]:
                    raise BatchError(f"target changed immediately before publication: {entry['path']}")
                tmp_stat = os.stat(temp_name, dir_fd=parent, follow_symlinks=False)
                if (tmp_stat.st_dev, tmp_stat.st_ino) != (identity.st_dev, identity.st_ino):
                    raise BatchError("prepared operation file was replaced")
                if not LIFECYCLE.same_public_parent(path.parent, parent):
                    raise BatchError("destination directory mapping changed before publication")
                entry["state"] = "publishing"
                _save_journal(vault, journal)
                if authority == "wiki-ledger":
                    _validate_ledger_operations(vault, [journal["bundle"]["operations"][entry["index"]]],
                                                publication=True,
                                                original_preimages={entry["path"]: base64.b64decode(entry["beforeContent"]) if entry["beforeContent"] is not None else None},
                                                current_batch_id=journal["batchId"])
                latest = _read_at(parent, name)
                if (sha(latest) if latest is not None else None) != entry["beforeHash"]:
                    raise BatchError(f"target changed immediately before publication: {entry['path']}")
                if entry["beforeHash"] is None:
                    try:
                        os.link(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent,
                                follow_symlinks=False)
                    except FileExistsError as exc:
                        raise BatchError(f"create-only target appeared immediately before publication; preserving concurrent bytes: {entry['path']}") from exc
                    os.unlink(temp_name, dir_fd=parent)
                else:
                    os.replace(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temp_name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            entry["state"] = "published"
            _save_journal(vault, journal)
        finally:
            os.close(parent)

    if authority == "wiki-page" and finalize:
        for entry in journal["entries"]:
            if not entry["recorded"]:
                _require_approved_config(journal, config)
                _record_lifecycle(vault, journal, entry)
        if journal["phase"] != "finalizing":
            journal["phase"] = "recorded"
            _save_journal(vault, journal)
            journal["phase"] = "finalizing"
            _save_journal(vault, journal)
        _require_approved_config(journal, config)
        _call_lifecycle(vault, vault, journal["batchId"], "finalize", config, journal["config"])
    journal["phase"] = "complete"
    _save_journal(vault, journal)


def apply(vault: Path, bundle: dict, config: Path, plan_hash: str, authority: str = "wiki-page",
          *, cli_binding: dict | None = None, cli_plan_hash: str | None = None) -> dict:
    vault = Path(vault).resolve(strict=True)
    bundle = validate_bundle(bundle)
    # Bind identity/content before locking; inspect target preconditions after
    # checking completed journals so an exact replay can be a verified no-op.
    approval_plan = _approval_plan(vault, bundle, config, authority)
    if sha(canonical_json(approval_plan)) != plan_hash:
        raise BatchError("approval hash does not match current vault/config/content plan")
    with _locked(vault):
        directory = _journal_dir(vault, False)
        for prior in directory.glob("*.json"):
            try:
                saved = _load_journal(vault, prior.stem)
            except BatchError:
                continue
            if saved["planHash"] == plan_hash and saved["phase"] == "complete":
                _verify_postimages(vault, saved)
                if saved["authority"] == "wiki-ledger":
                    _validate_ledger_operations(vault, saved["bundle"]["operations"],
                                                original_preimages=_journal_preimages(saved),
                                                check_current_asset_locator=False)
                return {"batchId": saved["batchId"], "planHash": plan_hash, "phase": "complete", "noop": True}
        if authority == "wiki-ledger":
            target_ids = {PurePosixPath(op["path"]).stem for op in bundle["operations"]}
            for prior in directory.glob("*.json"):
                try:
                    saved = _load_journal(vault, prior.stem)
                except BatchError as exc:
                    raise BatchError(f"cannot verify existing batch history at {prior.name}: {exc}") from exc
                if saved["authority"] == "wiki-ledger" and saved["phase"] not in {"complete", "rolled-back"}:
                    saved_ids = {PurePosixPath(op["path"]).stem for op in saved["bundle"]["operations"]}
                    if target_ids & saved_ids:
                        raise BatchError("a matching wiki-ledger batch is unfinished; recover or roll it back first")
        planned = _plan(vault, bundle, config, authority)
        if planned["planHash"] != plan_hash:
            raise BatchError("approval hash does not match current plan")
        batch_id = str(uuid.uuid4())
        entries = []
        for index, (op, before) in enumerate(zip(bundle["operations"], planned["preimages"])):
            entries.append({"index": index, "path": op["path"], "beforeHash": op["expectedHash"],
                            "beforeContent": base64.b64encode(before).decode("ascii") if before is not None else None,
                            "afterHash": sha(op["content"].encode("utf-8")), "state": "pending",
                            "captured": False, "recorded": False})
        journal = {"version": 1, "batchId": batch_id, "vault": str(vault), "authority": authority,
                   "config": planned["plan"]["config"], "planHash": plan_hash, "bundle": bundle,
                   "entries": entries, "phase": "approved"}
        if cli_binding is not None:
            if not isinstance(cli_binding, dict) or cli_plan_hash != sha(canonical_json(cli_binding)):
                raise BatchError("CLI approval binding is invalid")
            journal["cliBinding"] = cli_binding
            journal["cliPlanHash"] = cli_plan_hash
        _require_approved_config(journal, config)
        _save_journal(vault, journal)
        _publish(vault, journal, selected_config=config)
        return {"batchId": batch_id, "planHash": plan_hash, "phase": journal["phase"], "noop": False}


def recover(vault: Path, batch_id: str, config: Path | None = None) -> dict:
    vault = Path(vault).resolve(strict=True)
    initial = _load_journal(vault, batch_id)
    require_valid_config(config or Path(initial["config"]["path"]))
    _require_approved_config(initial, config)
    with _locked(vault):
        journal = _load_journal(vault, batch_id)
        require_valid_config(config or Path(journal["config"]["path"]))
        approved_config = _require_approved_config(journal, config)
        if journal["phase"] == "complete":
            _verify_postimages(vault, journal)
            if journal["authority"] == "wiki-ledger":
                _validate_ledger_operations(vault, journal["bundle"]["operations"],
                                            original_preimages=_journal_preimages(journal),
                                            check_current_asset_locator=False)
            return {"batchId": batch_id, "phase": "complete", "applied": True, "noop": True}
        if journal["phase"] in {"rollback-conflict", "rolled-back"}:
            raise BatchError("batch is in rollback recovery; use rollback, not apply/recover")
        _preflight_recovery(vault, journal)
        if journal["authority"] == "wiki-ledger":
            _validate_ledger_operations(vault, journal["bundle"]["operations"], publication=True,
                                        original_preimages=_journal_preimages(journal),
                                        current_batch_id=journal["batchId"])
        for op in journal["bundle"]["operations"]:
            _validate_draft(vault, op, journal["authority"])
        _publish(vault, journal, selected_config=approved_config)
        return {"batchId": batch_id, "phase": journal["phase"], "applied": True, "noop": False}


def rollback(vault: Path, batch_id: str, config: Path | None = None) -> dict:
    # Rollback is preimage cleanup, not forward execution of the approved plan;
    # require a valid current config but do not require its old content identity.
    vault = Path(vault).resolve(strict=True)
    initial = _load_journal(vault, batch_id)
    require_valid_config(config or Path(initial["config"]["path"]))
    with _locked(vault):
        journal = _load_journal(vault, batch_id)
        require_valid_config(config or Path(journal["config"]["path"]))
        if journal["phase"] in {"finalizing", "complete"}:
            raise BatchError("batch lifecycle finalization may have published; rollback is not allowed")
        conflicts = []
        for entry in reversed(journal["entries"]):
            if entry["state"] in {"pending", "rolled-back"}:
                continue
            path, parent, name = _target(vault, entry["path"], journal["authority"], allow_missing=True)
            if parent is None:
                conflicts.append(entry["path"])
                continue
            try:
                current = _read_at(parent, name)
                current_hash = sha(current) if current is not None else None
                if current_hash == entry["beforeHash"]:
                    entry["state"] = "rolled-back"
                elif current_hash != entry["afterHash"]:
                    conflicts.append(entry["path"])
                    continue
                elif not LIFECYCLE.same_public_parent(path.parent, parent):
                    conflicts.append(entry["path"])
                    continue
                elif entry["beforeHash"] is None:
                    os.unlink(name, dir_fd=parent)
                    os.fsync(parent)
                    entry["state"] = "rolled-back"
                else:
                    previous = base64.b64decode(entry["beforeContent"])
                    if sha(previous) != entry["beforeHash"]:
                        raise BatchError("journal preimage is corrupt")
                    temp = f".batch-rollback-{batch_id}-{entry['index']}-{uuid.uuid4().hex}.tmp"
                    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            fd = -1
                            stream.write(previous)
                            stream.flush()
                            os.fsync(stream.fileno())
                        latest = _read_at(parent, name)
                        if (sha(latest) if latest is not None else None) != entry["afterHash"] or not LIFECYCLE.same_public_parent(path.parent, parent):
                            conflicts.append(entry["path"])
                            continue
                        os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                        os.fsync(parent)
                        entry["state"] = "rolled-back"
                    finally:
                        if fd >= 0:
                            os.close(fd)
                        try:
                            os.unlink(temp, dir_fd=parent)
                        except FileNotFoundError:
                            pass
            finally:
                os.close(parent)
            _save_journal(vault, journal)
        if conflicts:
            journal["phase"] = "rollback-conflict"
            _save_journal(vault, journal)
            return {"batchId": batch_id, "phase": journal["phase"], "rolledBack": False, "conflicts": conflicts}
        if journal["authority"] == "wiki-page":
            for entry in journal["entries"]:
                if entry["captured"] and not entry["recorded"]:
                    _record_lifecycle(vault, journal, entry)
            # A rollback leaves original bytes; lifecycle record/reconcile clears
            # no-op captures while preserving the lifecycle's existing owner rules.
            _call_lifecycle(vault, vault, batch_id, "finalize", config or Path(journal["config"]["path"]))
        journal["phase"] = "rolled-back"
        _save_journal(vault, journal)
        return {"batchId": batch_id, "phase": journal["phase"], "rolledBack": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "apply"):
        p = sub.add_parser(name)
        p.add_argument("bundle", type=Path)
        p.add_argument("--vault", required=True, type=Path)
        p.add_argument("--config", required=True, type=Path)
        p.add_argument("--authority", choices=tuple(sorted(AUTHORITIES)), default="wiki-page")
        if name == "apply":
            p.add_argument("--plan-hash", required=True)
    for name in ("recover", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("batch_id")
        p.add_argument("--vault", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command in {"inspect", "apply"}:
            bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
            result = inspect(args.vault, bundle, args.config, args.authority) if args.command == "inspect" else apply(args.vault, bundle, args.config, args.plan_hash, args.authority)
        elif args.command == "recover":
            result = recover(args.vault, args.batch_id)
        else:
            result = rollback(args.vault, args.batch_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (BatchError, LIFECYCLE.LifecycleError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"wiki-batch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
