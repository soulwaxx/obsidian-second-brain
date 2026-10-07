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

SCHEMA = "okf-evidence-v1"
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SOURCE_ID = re.compile(r"^source-[0-9a-f]{24}$")
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
    if not isinstance(review, dict) or review.get("status") not in {"unreviewed", "reviewed"}:
        raise LedgerError("review.status must be unreviewed or reviewed")


def validate_record(record: Any, expected_id: str | None = None) -> dict[str, Any]:
    """Validate known fields while allowing unknown fields to round-trip."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise LedgerError(f"record must be an object with schema {SCHEMA}")
    kind, identity = record.get("kind"), record.get("id")
    if kind not in {"source", "claim"} or not _identifier(identity):
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
        if record.get("freshness") not in {"unknown", "current", "stale"}:
            raise LedgerError("source freshness must be unknown, current, or stale")
    else:
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
    return record


def make_source(locator: str, content: bytes, *, freshness: str = "unknown") -> dict[str, Any]:
    if not isinstance(locator, str) or not locator.strip() or not isinstance(content, bytes):
        raise LedgerError("source requires a nonempty locator and byte content")
    digest = hashlib.sha256(content).hexdigest()
    identity_material = json.dumps([locator, digest], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    identity = "source-" + hashlib.sha256(identity_material).hexdigest()[:24]
    record = {"schema": SCHEMA, "kind": "source", "id": identity,
              "locator": locator, "contentHash": "sha256:" + digest,
              "freshness": freshness, "review": {"status": "unreviewed"}}
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
