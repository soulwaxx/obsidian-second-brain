#!/usr/bin/env python3
"""Read-only structural, provenance, and freshness reports for an OKF wiki."""
from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from module_loading import load_source

_ROOT = Path(__file__).resolve().parents[1]
_INDEX = load_source("evidence_report_index", str(_ROOT / "scripts/bm25-index.py"))
_MIDDLEWARE = str(_ROOT / "skills/wiki/scripts/okf_mw")
sys.path.insert(0, _MIDDLEWARE)
try:
    _VALIDATOR = load_source("evidence_report_validator", str(Path(_MIDDLEWARE) / "validate.py"))
finally:
    sys.path.remove(_MIDDLEWARE)
_LIFECYCLE = load_source("evidence_report_lifecycle", str(_ROOT / "scripts/wiki_lifecycle.py"))
_LEDGER = load_source("evidence_report_ledger", str(Path(__file__).with_name("evidence_ledger.py")))


class ReportError(ValueError):
    """Report input could not be inspected safely."""


def _frontmatter(text: str) -> dict[str, Any]:
    value, error = _VALIDATOR._parse_frontmatter_yaml(text)
    return value if error is None and isinstance(value, dict) else {}


def _unavailable_ledger(error: str, issue: str = "unsafe-ledger-namespace") -> tuple[dict[str, dict], dict[str, Any]]:
    return {}, {"state": "unavailable", "recordCount": 0, "invalidCount": 0,
                "error": error, "issues": [issue]}


def _load_ledger(vault: Path) -> tuple[dict[str, dict], dict[str, Any]]:
    directory = vault / "wiki/meta/evidence"
    try:
        directory_fd = _LIFECYCLE.secure_dir(directory, create=False)
    except FileNotFoundError:
        return {}, {"state": "missing", "recordCount": 0, "invalidCount": 0, "issues": []}
    except OSError as exc:
        return _unavailable_ledger(f"cannot open evidence ledger namespace safely (symlink or unsafe path): {exc}")

    records: dict[str, dict] = {}
    invalid = []
    try:
        try:
            candidates = sorted(os.listdir(directory_fd))
        except OSError as exc:
            return _unavailable_ledger(f"cannot inspect evidence ledger safely: {exc}", "ledger-read-error")
        for name in candidates:
            if not name.endswith(".json"):
                invalid.append({"path": name, "issues": ["unsafe-or-unrecognized-ledger-entry"]})
                continue
            fd = None
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    invalid.append({"path": name, "issues": ["unsafe-or-unrecognized-ledger-entry"]})
                    continue
                fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    invalid.append({"path": name, "issues": ["unsafe-or-unrecognized-ledger-entry"]})
                    continue
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    fd = None
                    record = json.load(stream)
                _LEDGER.validate_record(record, expected_id=Path(name).stem)
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                invalid.append({"path": name, "issues": ["unsafe-or-invalid-ledger-record"], "detail": str(exc)})
                continue
            finally:
                if fd is not None:
                    os.close(fd)
            if record["id"] in records:
                invalid.append({"path": name, "issues": ["duplicate-ledger-id"]})
                continue
            records[record["id"]] = record
    finally:
        os.close(directory_fd)
    return records, {"state": "present", "recordCount": len(records), "invalidCount": len(invalid),
                     "invalidRecords": invalid, "issues": ["invalid-ledger-records"] if invalid else []}


def _structural(path: Path) -> dict[str, Any]:
    try:
        result = _VALIDATOR.validate_file(str(path))
        return {"status": "valid" if result["valid"] else "invalid", "issues": result.get("errors", [])}
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return {"status": "unavailable", "issues": [str(exc)]}


def _local_reference(resource: str, vault: Path, existing: set[str]) -> str:
    value = resource.split("#", 1)[0].split("?", 1)[0]
    if not value:
        return "unresolved"
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        return "external-unchecked"
    if value.startswith("wiki/"):
        relative = value
    elif value.startswith("/") or value.startswith("."):
        return "unsafe-or-unresolved"
    else:
        return "external-unchecked"
    return "resolved-in-vault" if relative in existing else "missing"


def _references(metadata: dict[str, Any], vault: Path, pages: set[str], records: dict[str, dict]) -> list[dict[str, Any]]:
    entries = metadata.get("sources", [])
    if entries is None:
        return []
    if isinstance(entries, str):
        entries = [entries]
    elif not isinstance(entries, list):
        return [{"id": None, "resource": None, "state": "invalid-source-metadata",
                 "issues": ["sources-must-be-string-list-or-mappings"]}]
    result = []
    source_records = {identity: record for identity, record in records.items() if record["kind"] == "source"}
    for entry in entries:
        if isinstance(entry, str):
            result.append({"id": None, "resource": entry, "state": "legacy-source-unchecked"})
            continue
        if not isinstance(entry, dict):
            result.append({"id": None, "resource": None, "state": "invalid-source-reference",
                           "issues": ["source-entry-must-be-string-or-mapping"]})
            continue
        identity = entry.get("id")
        resource = entry.get("resource")
        safe_identity = identity if isinstance(identity, str) else None
        safe_resource = resource if isinstance(resource, str) else None
        issues = []
        if identity is not None and safe_identity is None:
            issues.append("invalid-source-id")
        if resource is not None and safe_resource is None:
            issues.append("invalid-source-resource")
        if safe_identity and safe_identity in source_records:
            state = {"current": "ledger-source-current", "stale": "ledger-source-stale", "unknown": "ledger-source-currentness-unknown"}[source_records[safe_identity]["freshness"]]
        elif safe_resource is not None:
            state = _local_reference(safe_resource, vault, pages)
        elif safe_identity is not None:
            state = "missing-source-record" if safe_identity.startswith("source-") else "reference-id-only-unverified"
        else:
            state = "invalid-source-reference"
            if not issues:
                issues.append("source-reference-needs-id-or-resource")
        reference = {"id": safe_identity, "resource": safe_resource, "state": state}
        if issues:
            reference["issues"] = issues
        result.append(reference)
    return result


def _citations(text: str, metadata: dict[str, Any]) -> list[dict[str, str]]:
    source_entries = metadata.get("sources", [])
    source_ids = {entry.get("id") for entry in source_entries
                  if isinstance(entry, dict) and isinstance(entry.get("id"), str)} if isinstance(source_entries, list) else set()
    result = []
    for identity in sorted(set(re.findall(r"\[\^([A-Za-z0-9._-]+)\](?!:)", text))):
        result.append({"id": identity, "state": "source-entry-present" if identity in source_ids else "missing-source-entry"})
    return result


def _claim_summary(record: dict[str, Any], records: dict[str, dict]) -> dict[str, Any]:
    issues = []
    evidence = []
    for relation in ("support", "contradictions"):
        for source_id in record[relation]:
            source = records.get(source_id)
            if source is None or source.get("kind") != "source":
                issues.append("missing-source-record")
                evidence.append({"id": source_id, "relation": relation, "state": "missing-source-record"})
            else:
                freshness = source["freshness"]
                if freshness == "stale":
                    issues.append("stale-source-record")
                evidence.append({"id": source_id, "relation": relation, "state": freshness,
                                 "reviewStatus": source["review"]["status"]})
    if record["contradictions"]:
        issues.append("contradictory-evidence-recorded")
    if record["evidenceState"] == "provisional":
        issues.append("provisional-claim")
    return {"id": record["id"],
            "state": "contradictory-evidence-recorded" if record["contradictions"] else record["evidenceState"],
            "reviewStatus": record["review"]["status"], "support": record["support"],
            "contradictions": record["contradictions"], "evidence": evidence,
            "uncertainty": record.get("uncertainty"), "issues": sorted(set(issues))}


def _stale_after_date(value: Any) -> tuple[date | None, str | None]:
    """Normalize valid date-like metadata to day precision and JSON-safe text."""
    if isinstance(value, datetime):
        return value.date(), value.isoformat()
    if isinstance(value, date):
        return value, value.isoformat()
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            return parsed, value
        except ValueError:
            try:
                parsed_timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed_timestamp.date(), value
            except ValueError:
                return None, value
    return None, None


def _claims(metadata: dict[str, Any], records: dict[str, dict]) -> list[dict[str, Any]]:
    linked = metadata.get("claims", [])
    if linked is None:
        return []
    if not isinstance(linked, list):
        return [{"id": None, "state": "invalid-claim-reference", "issues": ["claims-must-be-list"]}]
    by_id = {identity: record for identity, record in records.items() if record["kind"] == "claim"}
    result = []
    for identity in linked:
        if not isinstance(identity, str):
            result.append({"id": None, "state": "invalid-claim-reference", "issues": ["claim-id-must-be-string"]})
            continue
        record = by_id.get(identity)
        if record is None:
            result.append({"id": identity, "state": "missing-claim-record", "issues": ["missing-claim-record"]})
            continue
        result.append(_claim_summary(record, records))
    return result


def build_report(vault: Path, as_of: str | date | None = None) -> dict[str, Any]:
    """Return a read-only evidence report; no result asserts factual truth."""
    try:
        report_date = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of or date.today()
    except (TypeError, ValueError) as exc:
        raise ReportError("--as-of must be an ISO date (YYYY-MM-DD)") from exc
    if not isinstance(report_date, date):
        raise ReportError("--as-of must be an ISO date (YYYY-MM-DD)")
    vault = Path(vault).expanduser().resolve(strict=True)
    wiki = vault / "wiki"
    if not wiki.is_dir() or wiki.is_symlink():
        raise ReportError("wiki directory is missing or symlinked")
    records, ledger = _load_ledger(vault)
    candidates = sorted(path for path in wiki.rglob("*") if path.is_file() and path.name.casefold().endswith(".md")
                        and _INDEX.eligible(path, vault))
    page_paths = {path.relative_to(vault).as_posix() for path in candidates}
    pages = []
    for path in candidates:
        relative = path.relative_to(vault).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            pages.append({"path": relative, "structuralValidity": {"status": "unavailable", "issues": [str(exc)]},
                          "freshness": {"status": "unknown", "issues": []}, "references": [], "claims": [], "issues": ["page-unavailable"]})
            continue
        metadata = _frontmatter(text)
        validation = _structural(path)
        freshness_issues = []
        stale_after = metadata.get("stale_after")
        expiry, stale_after_json = _stale_after_date(stale_after)
        if stale_after is None:
            freshness_status = "unknown"
        elif expiry is None:
            freshness_status = "unknown"
            freshness_issues.append("invalid-stale-after")
        else:
            freshness_status = "stale" if expiry < report_date else "current-through-as-of"
        references = _references(metadata, vault, page_paths, records)
        citations = _citations(text, metadata)
        claims = _claims(metadata, records)
        issues = list(freshness_issues)
        issues.extend("missing-citation-source-entry" for citation in citations if citation["state"] == "missing-source-entry")
        issues.extend(reference["state"] for reference in references if reference["state"] not in {"resolved-in-vault", "external-unchecked", "legacy-source-unchecked", "reference-id-only-unverified", "ledger-source-current", "ledger-source-currentness-unknown"})
        for reference in references:
            issues.extend(reference.get("issues", []))
        for claim in claims:
            issues.extend(claim["issues"])
        issues.extend(ledger["issues"])
        review_meta = metadata.get("verified")
        review_status = metadata.get("status")
        review_issues = []
        if "status" in metadata and not isinstance(review_status, str):
            review_status = None
            review_issues.append("invalid-review-status")
        issues.extend(review_issues)
        pages.append({"path": relative, "structuralValidity": validation,
                      "freshness": {"status": freshness_status, "asOf": report_date.isoformat(), "staleAfter": stale_after_json,
                                    "issues": freshness_issues},
                      "review": {"status": review_status,
                                 "verificationMetadata": "present-unverified-by-report" if review_meta else "absent"},
                      "references": references, "citations": citations, "claims": claims, "issues": sorted(set(issues))})
    claim_records = [record for record in records.values() if record["kind"] == "claim"]
    source_records = [record for record in records.values() if record["kind"] == "source"]
    source_reports = [{"id": record["id"], "locator": record["locator"], "contentHash": record["contentHash"],
                       "freshness": record["freshness"], "reviewStatus": record["review"]["status"]}
                      for record in source_records]
    claim_reports = [_claim_summary(record, records) for record in claim_records]
    return {"report": "okf-evidence-report-v1", "vault": str(vault), "asOf": report_date.isoformat(),
            "scope": "read-only structural and reference metadata; no factual truth verification",
            "ledger": ledger,
            "ledgerSummary": {"sources": len(source_records), "claims": len(claim_records),
                              "staleSources": sum(record["freshness"] == "stale" for record in source_records),
                              "unknownFreshnessSources": sum(record["freshness"] == "unknown" for record in source_records),
                              "contradictoryClaims": sum(bool(record["contradictions"]) for record in claim_records)},
            "sources": source_reports, "claims": claim_reports, "pages": pages}
