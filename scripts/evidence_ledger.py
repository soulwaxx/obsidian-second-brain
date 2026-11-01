#!/usr/bin/env python3
"""Optional canonical source and claim records for selected wiki workflows.

Structural validation describes record shape only; it does not assess factual
truth, source quality, or calibrated confidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

SCHEMA = "okf-evidence-v1"
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SOURCE_ID = re.compile(r"^source-[0-9a-f]{24}$")
_ASSET_ID = re.compile(r"^asset-[a-z0-9][a-z0-9._-]{0,119}$")
_OBSERVATION_ID = re.compile(r"^observation-[0-9a-f]{24}$")
_BASELINE_ID = re.compile(r"^baseline-[0-9a-f]{24}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class LedgerError(ValueError):
    pass


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _source_ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not _SOURCE_ID.fullmatch(item) for item in value):
        raise LedgerError(f"{field} must be an array of stable source IDs")
    if len(value) != len(set(value)):
        raise LedgerError(f"{field} cannot contain duplicate source IDs")
    return value


def _validate_review(record: dict[str, Any]) -> None:
    review = record.get("review")
    if (not isinstance(review, dict) or not isinstance(review.get("status"), str)
            or review["status"] not in {"unreviewed", "reviewed"}):
        raise LedgerError("review.status must be unreviewed or reviewed")


def _canonical_locator(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        return False
    from pathlib import PurePosixPath
    path = PurePosixPath(value)
    return (bool(path.parts) and path.as_posix() == value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and all(part not in {"", ".", ".."} for part in path.parts))


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _known_string(record: dict[str, Any], field: str, pattern: re.Pattern[str]) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LedgerError(f"{field} is invalid")
    return value


def _validate_git_context(value: Any) -> None:
    if not isinstance(value, dict):
        raise LedgerError("gitContext must be an object")
    allowed_types = {"head": str, "repositoryLocator": str, "selectedBytes": str, "limitation": str}
    for key, item in value.items():
        if key in allowed_types and not isinstance(item, allowed_types[key]):
            raise LedgerError(f"gitContext.{key} has an invalid type")
    if "head" in value and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value["head"]):
        raise LedgerError("gitContext.head must be a commit object id")
    if "repositoryLocator" in value and not _canonical_locator(value["repositoryLocator"]):
        raise LedgerError("gitContext.repositoryLocator must be canonical and relative")
    if "selectedBytes" in value and value["selectedBytes"] not in {"equal", "different", "untracked", "unknown"}:
        raise LedgerError("gitContext.selectedBytes is invalid")


def _identity_id(prefix: str, material: Any) -> str:
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()[:24]


def _observation_material(record: dict[str, Any]) -> dict[str, Any]:
    fields = ("assetId", "rootId", "locator", "sourceId", "contentHash", "observedAt")
    material = {field: record[field] for field in fields if field in record}
    if "gitContext" in record:
        known = {"head", "repositoryLocator", "selectedBytes", "limitation"}
        material["gitContext"] = {key: value for key, value in record["gitContext"].items() if key in known}
    return material


def _validate_page_path(value: Any) -> bool:
    if not _canonical_locator(value):
        return False
    from pathlib import PurePosixPath
    path = PurePosixPath(value)
    return (len(path.parts) >= 2 and path.parts[0] == "wiki" and path.name.lower().endswith(".md")
            and path.name.casefold() not in {"index.md", "log.md", "_plan.md"}
            and not any(part.startswith(".") for part in path.parts[1:]))


def validate_record(record: Any, expected_id: str | None = None) -> dict[str, Any]:
    """Validate known fields while allowing unknown fields to round-trip."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise LedgerError(f"record must be an object with schema {SCHEMA}")
    kind, identity = record.get("kind"), record.get("id")
    if not isinstance(kind, str) or kind not in {"source", "claim", "asset", "observation", "baseline"} or not _identifier(identity):
        raise LedgerError("record kind or stable id is invalid")
    if expected_id is not None and identity != expected_id:
        raise LedgerError("record id must match its canonical filename")
    _validate_review(record)
    if kind == "source":
        if not _SOURCE_ID.fullmatch(identity):
            raise LedgerError("source id must be a stable generated source identity")
        if not isinstance(record.get("locator"), str) or not record["locator"].strip():
            raise LedgerError("source locator must be a nonempty string")
        if not isinstance(record.get("contentHash"), str) or not _HASH.fullmatch(record["contentHash"]):
            raise LedgerError("source contentHash must be a sha256:<hex> digest")
        if not isinstance(record.get("freshness"), str) or record["freshness"] not in {"unknown", "current", "stale"}:
            raise LedgerError("source freshness must be unknown, current, or stale")
    elif kind == "claim":
        if not identity.startswith("claim-"):
            raise LedgerError("claim id must begin with claim-")
        statement = record.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise LedgerError("claim statement must be nonempty text")
        support = _source_ids(record.get("support"), "support")
        contradictions = _source_ids(record.get("contradictions"), "contradictions")
        if set(support) & set(contradictions):
            raise LedgerError("a source cannot be both support and contradiction in the same record")
        expected_state = "evidence-linked" if support or contradictions else "provisional"
        if record.get("evidenceState") != expected_state:
            raise LedgerError(f"claim evidenceState must be {expected_state} for its listed evidence")
        uncertainty = record.get("uncertainty")
        if uncertainty is not None and not isinstance(uncertainty, str):
            raise LedgerError("uncertainty, when present, must be a descriptive note")
    elif kind == "asset":
        _known_string(record, "id", _ASSET_ID)
        if not isinstance(record.get("rootId"), str) or not _identifier(record["rootId"]):
            raise LedgerError("asset rootId must be a stable portable identifier")
        if not _canonical_locator(record.get("locator")):
            raise LedgerError("asset locator must be canonical, relative POSIX path")
    elif kind == "observation":
        _known_string(record, "id", _OBSERVATION_ID)
        _known_string(record, "assetId", _ASSET_ID)
        if not isinstance(record.get("rootId"), str) or not _identifier(record["rootId"]):
            raise LedgerError("observation rootId must be a stable portable identifier")
        if not _canonical_locator(record.get("locator")):
            raise LedgerError("observation locator must be canonical, relative POSIX path")
        _known_string(record, "sourceId", _SOURCE_ID)
        if not isinstance(record.get("contentHash"), str) or not _HASH.fullmatch(record["contentHash"]):
            raise LedgerError("observation contentHash must be a sha256:<hex> digest")
        if not _timestamp(record.get("observedAt")):
            raise LedgerError("observation observedAt must be a timezone-aware ISO timestamp")
        if "gitContext" in record:
            _validate_git_context(record["gitContext"])
        if identity != _identity_id("observation-", _observation_material(record)):
            raise LedgerError("observation id does not match immutable identity material")
    else:
        _known_string(record, "id", _BASELINE_ID)
        if not _validate_page_path(record.get("page")):
            raise LedgerError("baseline page must be an eligible canonical wiki Markdown path")
        _known_string(record, "assetId", _ASSET_ID)
        history = record.get("history")
        if not isinstance(history, list) or not history:
            raise LedgerError("baseline history must be a nonempty array")
        for entry in history:
            if not isinstance(entry, dict):
                raise LedgerError("baseline history entries must be objects")
            _known_string(entry, "observationId", _OBSERVATION_ID)
            _known_string(entry, "sourceId", _SOURCE_ID)
            if not isinstance(entry.get("contentHash"), str) or not _HASH.fullmatch(entry["contentHash"]):
                raise LedgerError("baseline contentHash must be a sha256:<hex> digest")
            if not isinstance(entry.get("pageHash"), str) or not _HASH.fullmatch(entry["pageHash"]):
                raise LedgerError("baseline pageHash must be a sha256:<hex> digest")
            if not isinstance(entry.get("pageBatchId"), str) or not _identifier(entry["pageBatchId"]):
                raise LedgerError("baseline pageBatchId must be a stable identifier")
            if not _timestamp(entry.get("reconciledAt")):
                raise LedgerError("baseline reconciledAt must be a timezone-aware ISO timestamp")
            if not isinstance(entry.get("outcome"), str) or entry["outcome"] not in {"updated", "no-change"}:
                raise LedgerError("baseline outcome must be updated or no-change")
        if len(history) != len({json.dumps(entry, sort_keys=True, ensure_ascii=False) for entry in history}):
            raise LedgerError("baseline history cannot contain duplicate entries")
        if identity != _identity_id("baseline-", [record["page"], record["assetId"]]):
            raise LedgerError("baseline id does not match its page and asset pair")
    return record


def make_source_digest(locator: str, content_hash: str, *, freshness: str = "unknown") -> dict[str, Any]:
    """Build the legacy source snapshot identity from a portable locator and byte digest."""
    if not isinstance(locator, str) or not locator.strip() or not isinstance(content_hash, str) or not _HASH.fullmatch(content_hash):
        raise LedgerError("source requires a nonempty locator and sha256 content hash")
    identity_material = json.dumps([locator, content_hash.removeprefix("sha256:")], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    identity = "source-" + hashlib.sha256(identity_material).hexdigest()[:24]
    record = {"schema": SCHEMA, "kind": "source", "id": identity,
              "locator": locator, "contentHash": content_hash,
              "freshness": freshness, "review": {"status": "unreviewed"}}
    return validate_record(record)


def make_source(locator: str, content: bytes, *, freshness: str = "unknown") -> dict[str, Any]:
    if not isinstance(content, bytes):
        raise LedgerError("source requires byte content")
    return make_source_digest(locator, "sha256:" + hashlib.sha256(content).hexdigest(), freshness=freshness)


def make_asset(asset_id: str, root_id: str, locator: str) -> dict[str, Any]:
    record = {"schema": SCHEMA, "kind": "asset", "id": asset_id, "rootId": root_id,
              "locator": locator, "review": {"status": "unreviewed"}}
    return validate_record(record)


def make_observation(asset: dict[str, Any], source: dict[str, Any], observed_at: str,
                     *, git_context: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_record(asset)
    validate_record(source)
    if asset["kind"] != "asset" or source["kind"] != "source":
        raise LedgerError("observation requires an asset and source record")
    portable_locator = f"local:{asset['rootId']}/" + "/".join(
        quote(part, safe="-._~") for part in asset["locator"].split("/")
    )
    if source["locator"] != portable_locator:
        raise LedgerError("source locator must match the asset root and locator")
    record = {"schema": SCHEMA, "kind": "observation", "assetId": asset["id"],
              "rootId": asset["rootId"], "locator": asset["locator"],
              "sourceId": source["id"], "contentHash": source["contentHash"],
              "observedAt": observed_at, "review": {"status": "unreviewed"}}
    if git_context is not None:
        record["gitContext"] = copy.deepcopy(git_context)
    record["id"] = _identity_id("observation-", _observation_material(record))
    return validate_record(record)


def make_baseline(page: str, asset_id: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    identity = _identity_id("baseline-", [page, asset_id])
    record = {"schema": SCHEMA, "kind": "baseline", "id": identity,
              "page": page, "assetId": asset_id, "history": copy.deepcopy(history),
              "review": {"status": "unreviewed"}}
    return validate_record(record)


def make_claim(identity: str, statement: str, *, support: list[str] | None = None,
               contradictions: list[str] | None = None, uncertainty: str | None = None) -> dict[str, Any]:
    if not isinstance(identity, str) or not identity.startswith("claim-") or not _identifier(identity):
        raise LedgerError("claim id must be a stable identifier beginning claim-")
    support = list(support or [])
    contradictions = list(contradictions or [])
    record: dict[str, Any] = {
        "schema": SCHEMA, "kind": "claim", "id": identity, "statement": statement,
        "support": support, "contradictions": contradictions,
        "evidenceState": "evidence-linked" if support or contradictions else "provisional",
        "review": {"status": "unreviewed"},
    }
    if uncertainty is not None:
        record["uncertainty"] = uncertainty
    return validate_record(record)


def _merge(existing: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(existing)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def merge_record(existing: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Apply an explicit patch without discarding unknown record extensions."""
    validate_record(existing)
    if not isinstance(changes, dict):
        raise LedgerError("record changes must be an object")
    result = _merge(existing, changes)
    validate_record(result, expected_id=existing["id"])
    if result["kind"] != existing["kind"] or result["schema"] != existing["schema"]:
        raise LedgerError("record kind and schema identity are immutable")
    if existing["kind"] == "observation" and _observation_material(result) != _observation_material(existing):
        raise LedgerError("observation immutable identity material cannot be changed")
    if existing["kind"] == "asset":
        if result["rootId"] != existing["rootId"]:
            raise LedgerError("asset root identity is immutable")
    if existing["kind"] == "baseline":
        if (result["page"], result["assetId"]) != (existing["page"], existing["assetId"]):
            raise LedgerError("baseline page and asset pair are immutable")
        old_history, new_history = existing["history"], result["history"]
        if new_history[:len(old_history)] != old_history:
            raise LedgerError("baseline history must retain its existing prefix")
    return result


def record_path(record: dict[str, Any]) -> str:
    validate_record(record)
    return f"wiki/meta/evidence/{record['id']}.json"


def operation(record: dict[str, Any], existing_bytes: bytes | None = None) -> dict[str, Any]:
    """Build the exact-preimage operation consumed by wiki_batch authority."""
    validate_record(record)
    digest = hashlib.sha256(existing_bytes).hexdigest() if existing_bytes is not None else None
    return {"path": record_path(record), "expectedHash": digest,
            "content": json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"}
