#!/usr/bin/env python3
"""Reviewed, ordered, recoverable vault batches (not filesystem-wide atomic)."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import uuid
from typing import Any
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
        for op, entry in zip(bundle["operations"], entries):
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
    return data


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
                latest = _read_at(parent, name)
                if (sha(latest) if latest is not None else None) != entry["beforeHash"]:
                    raise BatchError(f"target changed immediately before publication: {entry['path']}")
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


def apply(vault: Path, bundle: dict, config: Path, plan_hash: str, authority: str = "wiki-page") -> dict:
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
                return {"batchId": saved["batchId"], "planHash": plan_hash, "phase": "complete", "noop": True}
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
            return {"batchId": batch_id, "phase": "complete", "applied": True, "noop": True}
        if journal["phase"] in {"rollback-conflict", "rolled-back"}:
            raise BatchError("batch is in rollback recovery; use rollback, not apply/recover")
        _preflight_recovery(vault, journal)
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
