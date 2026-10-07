#!/usr/bin/env python3
"""Explicit, bounded, immutable local/text source capture."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from module_loading import load_source
from typing import Any
from urllib.parse import quote

BATCH = load_source("local_capture_batch", str(Path(__file__).with_name("wiki_batch.py")))
MAX_SOURCES = 20
MAX_SOURCE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 4_194_304
MAX_LINKED_PAGES = 50
MAX_LINKED_PAGE_BYTES = 16_777_216
SOURCE_ROOT = PurePosixPath(".raw/agent-captures")
SAFE_HASH = re.compile(r"[a-f0-9]{64}\Z")


class CaptureError(RuntimeError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _selected_source(path: Path) -> tuple[Path, bytes]:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        raise CaptureError("source selections must be explicit absolute paths")
    # Reject symlinks at every existing component, including a symlinked parent.
    absolute = Path(os.path.abspath(requested))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                raise CaptureError("source selection traverses a symlink")
        except OSError as exc:
            raise CaptureError(f"cannot inspect source selection: {exc}") from exc
    try:
        info = absolute.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise CaptureError("source selection must be a regular text file")
        if info.st_size > MAX_SOURCE_BYTES:
            raise CaptureError(f"source exceeds the {MAX_SOURCE_BYTES}-byte per-file limit")
        data = absolute.read_bytes()
        after = absolute.lstat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise CaptureError("source changed while it was being captured")
    except OSError as exc:
        raise CaptureError(f"cannot read selected source: {exc}") from exc
    if b"\x00" in data:
        raise CaptureError("binary/NUL-containing sources are not supported")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CaptureError("source must be valid UTF-8 text") from exc
    return absolute, data


def _canonical_page_path(raw: Any) -> bool:
    if not isinstance(raw, str) or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return False
    rel = PurePosixPath(raw)
    return not (
        rel.is_absolute() or "\\" in raw or rel.as_posix() != raw
        or len(rel.parts) < 2 or rel.parts[0] != "wiki" or not rel.name.lower().endswith(".md")
        or rel.name.casefold() in {"index.md", "log.md", "_plan.md"}
        or any(part.startswith(".") or part in {"", ".", ".."} for part in rel.parts[1:])
    )


def _page_evidence(vault: Path, raw: str) -> tuple[str, str]:
    if not _canonical_page_path(raw):
        raise CaptureError(f"linked page must be a canonical eligible wiki Markdown path: {raw!r}")
    rel = PurePosixPath(raw)
    root = vault / "wiki"
    if root.is_symlink() or not root.is_dir():
        raise CaptureError("wiki directory is missing or symlinked")
    cursor = vault
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise CaptureError(f"linked page traverses a symlink: {raw}")
        if cursor.exists() and cursor.parent.is_dir():
            names = [name for name in os.listdir(cursor.parent) if name.casefold() == part.casefold()]
            if len(names) != 1 or names[0] != part:
                raise CaptureError(f"linked page has a case-insensitive path collision: {raw}")
    try:
        fd = os.open(cursor, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise CaptureError(f"linked page is not a regular file: {raw}")
            if before.st_size > MAX_LINKED_PAGE_BYTES:
                raise CaptureError(f"linked page exceeds the {MAX_LINKED_PAGE_BYTES}-byte evidence limit: {raw}")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                data = stream.read(MAX_LINKED_PAGE_BYTES + 1)
            if len(data) > MAX_LINKED_PAGE_BYTES:
                raise CaptureError(f"linked page exceeds the {MAX_LINKED_PAGE_BYTES}-byte evidence limit: {raw}")
            after = cursor.lstat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise CaptureError(f"linked page changed while being captured: {raw}")
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        raise CaptureError(f"linked page is unavailable: {raw}") from exc
    return raw, sha(data)


def _page_link(page: str) -> str:
    # Keep legacy/simple wikilinks stable; use a percent-encoded Markdown target
    # when characters would change wikilink syntax or escape its destination.
    if any(char in page for char in "[]|#^\\\\()<>\"'"):
        target = "../../" + "/".join(quote(part, safe="") for part in page.split("/"))
        label = page.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        return f"[{label}]({target})"
    return f"[[{page[:-3]}]]"


def _render_record(identity: str, content_hash: str, pages: list[dict[str, str]]) -> bytes:
    lines = ["---", "type: source-capture", f"source_sha256: {content_hash}",
             f"source_identity_sha256: {identity}", "---", "",
             "# Captured source", "", f"Source payload: [[{content_hash}.txt]]"]
    if pages:
        lines.extend(["", "Related wiki pages:"])
        lines.extend(f"- {_page_link(page['path'])}" for page in pages)
    lines.extend(["", "Source-page hashes:", "```json",
                  json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(",", ":")), "```", ""])
    return "\n".join(lines).encode("utf-8")


def _record(source: Path, data: bytes, page_hashes: list[tuple[str, str]]) -> tuple[str, bytes, str, bytes]:
    content_hash = sha(data)
    identity = sha(str(source).encode("utf-8"))
    identity_key = sha((identity + ":" + content_hash).encode("ascii"))
    pages = [{"path": page, "sha256": digest} for page, digest in page_hashes]
    # Keep the original UTF-8 text byte-for-byte as a dedicated immutable payload.
    payload_path = f"{SOURCE_ROOT.as_posix()}/{content_hash}.txt"
    record_path = f"{SOURCE_ROOT.as_posix()}/{identity_key}.md"
    return payload_path, data, record_path, _render_record(identity, content_hash, pages)


def prepare(vault: Path, sources: list[Path], pages: list[str] | None = None) -> dict[str, Any]:
    """Create a deterministic batch plan input without disclosing source paths."""
    vault = Path(vault).resolve(strict=True)
    if not sources or len(sources) > MAX_SOURCES:
        raise CaptureError(f"select between 1 and {MAX_SOURCES} local text sources")
    if len(set(map(str, sources))) != len(sources):
        raise CaptureError("duplicate source selections are not allowed")
    selected = [_selected_source(source) for source in sources]
    total = sum(len(data) for _, data in selected)
    if total > MAX_TOTAL_BYTES:
        raise CaptureError(f"selected sources exceed the {MAX_TOTAL_BYTES}-byte total limit")
    selected_pages = sorted(set(pages or []))
    if len(selected_pages) > MAX_LINKED_PAGES:
        raise CaptureError(f"select no more than {MAX_LINKED_PAGES} linked wiki pages")
    page_hashes = [_page_evidence(vault, page) for page in selected_pages]
    operations = []
    identities = []
    pairs = []
    for source, data in selected:
        payload_path, payload, record_path, record = _record(source, data, page_hashes)
        identity = {"identityHash": sha(str(source).encode("utf-8")), "contentHash": sha(data), "size": len(data)}
        pair_operations = [
            {"path": payload_path, "expectedHash": None, "content": payload.decode("utf-8")},
            {"path": record_path, "expectedHash": None, "content": record.decode("utf-8")},
        ]
        identities.append(identity)
        pairs.append({"source": identity, "operations": pair_operations})
        operations.extend(pair_operations)
    unique_operations = {}
    folded_paths = set()
    for operation in operations:
        path_key = operation["path"].casefold()
        if path_key in folded_paths and operation["path"] not in unique_operations:
            raise CaptureError("selected sources produce colliding immutable capture destinations")
        folded_paths.add(path_key)
        prior = unique_operations.get(operation["path"])
        if prior is not None and (prior["content"] != operation["content"] or prior["expectedHash"] != operation["expectedHash"]):
            raise CaptureError("selected sources produce conflicting immutable capture operations")
        unique_operations[operation["path"]] = operation
    return {"version": 1, "operations": list(unique_operations.values()), "pairs": pairs, "sources": identities,
            "pageHashes": page_hashes, "pageCount": len(page_hashes), "totalBytes": total}


def _parse_completed_reference(record: bytes, candidate_name: str, content_hash: str) -> bool:
    try:
        text = record.decode("utf-8", errors="strict")
        lines = text.splitlines()
        if len(lines) < 9 or lines[0:2] != ["---", "type: source-capture"]:
            return False
        if not isinstance(lines[2], str) or not lines[2].startswith("source_sha256: "):
            return False
        recorded_content_hash = lines[2][len("source_sha256: "):]
        if not isinstance(recorded_content_hash, str) or not SAFE_HASH.fullmatch(recorded_content_hash):
            return False
        if recorded_content_hash != content_hash:
            return False
        if not isinstance(lines[3], str) or not lines[3].startswith("source_identity_sha256: "):
            return False
        identity_hash = lines[3][len("source_identity_sha256: "):]
        if not isinstance(identity_hash, str) or not SAFE_HASH.fullmatch(identity_hash):
            return False
        if lines[4:9] != ["---", "", "# Captured source", "", f"Source payload: [[{content_hash}.txt]]"]:
            return False
        if candidate_name != sha((identity_hash + ":" + content_hash).encode("ascii")) + ".md":
            return False
        marker = "\nSource-page hashes:\n```json\n"
        if text.count(marker) != 1:
            return False
        _body, _separator, tail = text.rpartition(marker)
        if not tail.endswith("\n```\n"):
            return False
        pages_value = json.loads(tail[:-5])
        if not isinstance(pages_value, list) or len(pages_value) > MAX_LINKED_PAGES:
            return False
        pages = []
        seen_paths = set()
        for item in pages_value:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                return False
            page_path = item["path"]
            page_hash = item["sha256"]
            if (not isinstance(page_path, str) or not _canonical_page_path(page_path)
                    or not isinstance(page_hash, str) or not SAFE_HASH.fullmatch(page_hash)
                    or page_path in seen_paths):
                return False
            seen_paths.add(page_path)
            pages.append({"path": page_path, "sha256": page_hash})
        if pages != sorted(pages, key=lambda item: item["path"]):
            return False
        return record == _render_record(identity_hash, content_hash, pages)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return False


def _has_completed_payload_reference(root: Path, content_hash: str,
                                     history: list[dict[str, Any]] | None = None) -> bool:
    capture_root = root / SOURCE_ROOT.as_posix()
    try:
        if history is None:
            history = BATCH.source_capture_history(root)
        if (capture_root.parent.is_symlink() or capture_root.is_symlink()
                or not capture_root.is_dir()):
            return False
        for candidate in sorted(capture_root.glob("*.md")):
            try:
                record = _read_target(root, f"{SOURCE_ROOT.as_posix()}/{candidate.name}")
            except (CaptureError, OSError):
                continue
            if record is None or not _parse_completed_reference(record, candidate.name, content_hash):
                continue
            record_path = f"{SOURCE_ROOT.as_posix()}/{candidate.name}"
            record_hash = BATCH.sha(record)
            payload_path = f"{SOURCE_ROOT.as_posix()}/{content_hash}.txt"
            payload = _read_target(root, payload_path)
            if payload is None or BATCH.sha(payload) != content_hash:
                continue
            record_published = any(journal["phase"] == "complete"
                                   and journal["targets"].get(record_path) == record_hash
                                   for journal in history)
            payload_published = any(journal["phase"] == "complete"
                                    and journal["targets"].get(payload_path) == content_hash
                                    for journal in history)
            if record_published and payload_published:
                return True
    except (OSError, BATCH.BatchError):
        return False
    return False


def _read_target(root: Path, path: str) -> bytes | None:
    try:
        _target, parent, name = BATCH._target(root, path, "source-capture", allow_missing=True)
        try:
            return BATCH._read_at(parent, name) if parent is not None else None
        finally:
            if parent is not None:
                os.close(parent)
    except BATCH.BatchError as exc:
        raise CaptureError(f"cannot inspect immutable capture target {path}: {exc}") from exc


def _identity_history(root: Path, record_path: str, expected_record_hash: str,
                      history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches = [journal for journal in history if record_path in journal["targets"]]
    unfinished = [journal for journal in matches if journal["phase"] not in {"complete", "rolled-back"}]
    if unfinished:
        journal = unfinished[0]
        raise CaptureError(
            f"source identity has an unfinished capture batch {journal['batchId']}; "
            "recover or roll back that original batch before retrying"
        )
    completed = [journal for journal in matches if journal["phase"] == "complete"]
    if completed:
        for journal in completed:
            if journal["targets"][record_path] != expected_record_hash:
                raise CaptureError(f"completed source identity has different approved record content: {record_path}")
            for path, postimage_hash in journal["targets"].items():
                actual = _read_target(root, path)
                if actual is None or BATCH.sha(actual) != postimage_hash:
                    raise CaptureError(f"completed source-capture history postimage drifted; refusing repair: {path}")
    elif matches and _read_target(root, record_path) is not None:
        raise CaptureError(f"rolled-back source identity has a conflicting record: {record_path}")
    return matches


def _review(vault: Path, sources: list[Path], config: Path, pages: list[str] | None = None) -> dict[str, Any]:
    BATCH.require_valid_config(config)
    prepared = prepare(vault, sources, pages)
    root = Path(vault).resolve(strict=True)
    try:
        history = BATCH.source_capture_history(root)
    except BATCH.BatchError as exc:
        raise CaptureError(f"cannot verify source-capture publication history: {exc}") from exc
    missing = []
    pair_states = []
    for pair in prepared["pairs"]:
        operations = pair["operations"]
        payload_op, record_op = operations
        payload_actual = _read_target(root, payload_op["path"])
        record_actual = _read_target(root, record_op["path"])
        payload_expected = payload_op["content"].encode("utf-8")
        record_expected = record_op["content"].encode("utf-8")
        identity_history = _identity_history(root, record_op["path"], BATCH.sha(record_expected), history)
        if payload_actual is not None and payload_actual != payload_expected:
            raise CaptureError(f"immutable capture target drifted; refusing overwrite: {payload_op['path']}")
        if record_actual is not None and record_actual != record_expected:
            raise CaptureError(f"immutable capture target drifted; refusing overwrite: {record_op['path']}")
        if payload_actual is None and record_actual is not None:
            raise CaptureError("incomplete immutable capture pair; refusing repair or overwrite")
        if payload_actual is None:
            missing.extend(operations)
            state = "new"
        elif record_actual is not None:
            if not any(journal["phase"] == "complete" for journal in identity_history):
                raise CaptureError("existing source record has no completed publication history")
            state = "reused"
        elif _has_completed_payload_reference(root, pair["source"]["contentHash"], history):
            missing.append(record_op)
            state = "reused"
        else:
            raise CaptureError("unverified immutable payload without completed source reference")
        pair_states.append({
            "identityHash": pair["source"]["identityHash"],
            "contentHash": pair["source"]["contentHash"],
            "targets": [
                {"path": payload_op["path"], "contentHash": BATCH.sha(payload_expected),
                 "observedHash": BATCH.sha(payload_actual) if payload_actual is not None else None,
                 "reused": payload_actual is not None},
                {"path": record_op["path"], "contentHash": BATCH.sha(record_expected),
                 "observedHash": BATCH.sha(record_actual) if record_actual is not None else None,
                 "reused": record_actual is not None},
            ],
        })
    missing = list({operation["path"]: operation for operation in missing}.values())
    missing_bundle = {"version": 1, "operations": missing} if missing else None
    batch_plan = BATCH.inspect(root, missing_bundle, config, "source-capture") if missing else None
    capture_plan = {
        "version": 1,
        "vault": str(root),
        "config": BATCH.config_identity(config),
        "sources": prepared["sources"],
        "pageHashes": [{"path": page, "sha256": digest} for page, digest in prepared["pageHashes"]],
        "pairs": [
            {"identityHash": pair["identityHash"], "contentHash": pair["contentHash"],
             "targets": [{"path": target["path"], "contentHash": target["contentHash"]}
                         for target in pair["targets"]]}
            for pair in pair_states
        ],
    }
    plan_hash = BATCH.sha(BATCH.canonical_json(capture_plan))
    targets = list(dict.fromkeys(op["path"] for op in prepared["operations"]))
    result = {
        "noop": not missing,
        "planHash": plan_hash,
        "targets": targets,
        "sourceCount": len(prepared["sources"]),
        "sourceBytes": prepared["totalBytes"],
        "sources": prepared["sources"],
        "linkedPageCount": prepared["pageCount"],
        "linkedPageHashes": [digest for _, digest in prepared["pageHashes"]],
    }
    return {"result": result, "bundle": missing_bundle, "batchPlan": batch_plan}


def inspect(vault: Path, sources: list[Path], config: Path, pages: list[str] | None = None) -> dict[str, Any]:
    return _review(vault, sources, config, pages)["result"]


def apply(vault: Path, sources: list[Path], config: Path, plan_hash: str, pages: list[str] | None = None) -> dict[str, Any]:
    BATCH.require_valid_config(config)
    reviewed = _review(vault, sources, config, pages)
    result = reviewed["result"]
    if result["planHash"] != plan_hash:
        raise CaptureError("capture approval hash does not match current sources, links, targets, or config")
    if result["noop"]:
        return {"noop": True, "planHash": plan_hash, "targets": result["targets"]}
    batch_plan = reviewed["batchPlan"]
    applied = BATCH.apply(vault, reviewed["bundle"], config, batch_plan["planHash"], "source-capture")
    return {**applied, "planHash": plan_hash, "sourceCount": result["sourceCount"],
            "sourceBytes": result["sourceBytes"], "targets": result["targets"]}
