#!/usr/bin/env python3
"""Bounded bridge for Pi's four native read-only wiki tools and the MCP server's three reviewed batch tools.

The batch operations (`batch-inspect`, `batch-apply`, `batch-recover`) are never Pi tools: they take their
parameters as JSON on stdin (not argv), are wiki-page authority only, and reuse wiki_batch.py unchanged.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys
from typing import Any

try:
    from module_loading import load_source

    cli = load_source("pi_wiki_cli", str(Path(__file__).with_name("obsidian-second-brain.py")))
    CLI_LOAD_ERROR: str | None = None
except Exception as exc:
    cli = None
    CLI_LOAD_ERROR = getattr(exc, "name", None) or "required package module"

BATCH_OPERATIONS = {"batch-inspect", "batch-apply", "batch-recover"}
OPERATIONS = {"search", "outline", "read", "doctor"} | BATCH_OPERATIONS
# Transport limits for batch parameters. The serialized `operations` cap sits below the 64 KiB stdin/MCP-message limit
# (JSON framing and escaping differences) and, with the bounded review summary, below the launcher's 64 KiB result cap.
MAX_BATCH_BYTES = 48 * 1024
MAX_BATCH_STDIN = 64 * 1024
MAX_BATCH_OPERATIONS = 20
MAX_REVIEW_BYTES = 56 * 1024
JOURNAL_PERSISTED = "osb-journal-persisted"  # stderr marker: the launcher stops its 30 s deadline once an apply is recoverable
PARAMETERS = {
    "batch-inspect": {"operations"},
    "batch-apply": {"operations", "planHash"},
    "batch-recover": {"planHash", "batchId"},
    "search": {"query", "vault", "limit", "hybrid", "ollamaUrl", "ollamaModel", "timeout"},
    "outline": {"page", "vault", "expectedHash", "limit", "offset"},
    "read": {"page", "vault", "expectedHash", "section", "start", "end", "view", "maxChars", "cursor"},
    "doctor": {"vault"},
}


def validate_batch(operation: str, params: dict[str, Any]) -> None:
    if operation != "batch-recover":
        operations = params.get("operations")
        if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_BATCH_OPERATIONS:
            raise ValueError(f"operations must be a list of 1–{MAX_BATCH_OPERATIONS} entries")
        for item in operations:
            if (not isinstance(item, dict) or set(item) != {"path", "expectedHash", "content"}
                    or not isinstance(item["path"], str) or not 1 <= len(item["path"]) <= 1024
                    or not isinstance(item["content"], str)
                    or not (item["expectedHash"] is None or (isinstance(item["expectedHash"], str) and re.fullmatch(r"[0-9a-f]{64}", item["expectedHash"])))):
                raise ValueError("each operation must contain exactly path, expectedHash (64 lowercase hex or null) and content")
        if len(json.dumps(operations, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_BATCH_BYTES:
            raise ValueError(f"operations exceed the {MAX_BATCH_BYTES // 1024} KiB bundle limit; split the change into several batches")
    plan_hash = params.get("planHash")
    if (operation == "batch-apply" or (operation == "batch-recover" and "batchId" not in params)) and plan_hash is None:
        raise ValueError("planHash is required")
    if plan_hash is not None and (not isinstance(plan_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_hash)):
        raise ValueError("planHash must be 64 lowercase hexadecimal characters")
    if operation == "batch-recover":
        if ("planHash" in params) == ("batchId" in params):
            raise ValueError("provide exactly one of planHash or batchId")
        if "batchId" in params and (not isinstance(params["batchId"], str) or not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", params["batchId"])):
            raise ValueError("batchId must be a lowercase UUID")


def validate(operation: str, params: dict[str, Any]) -> None:
    extra = params.keys() - PARAMETERS[operation]
    if extra:
        raise ValueError("unsupported tool parameter")
    if operation in BATCH_OPERATIONS:
        return validate_batch(operation, params)
    if "vault" in params and params["vault"] is not None and (not isinstance(params["vault"], str) or len(params["vault"]) > 4096 or not os.path.isabs(params["vault"])):
        raise ValueError("vault must be an absolute path")
    if operation in {"outline", "read"}:
        page = params.get("page")
        if not isinstance(page, str) or len(page) > 1024:
            raise ValueError("page must be a canonical wiki-relative Markdown path")
    if operation == "search":
        if not isinstance(params.get("query"), str) or not 1 <= len(params["query"]) <= cli.RETRIEVAL_MODULE.MAX_QUERY_CHARS:
            raise ValueError(f"query must contain 1–{cli.RETRIEVAL_MODULE.MAX_QUERY_CHARS} characters")
        if "hybrid" in params and type(params["hybrid"]) is not bool:
            raise ValueError("hybrid must be boolean")
        if type(params.get("limit", 10)) is not int or not 1 <= params.get("limit", 10) <= 100:
            raise ValueError("limit must be between 1 and 100")
        if type(params.get("timeout", 8.0)) not in {int, float} or not 0.1 <= params.get("timeout", 8.0) <= 30:
            raise ValueError("timeout must be between 0.1 and 30 seconds")
        for key, maximum in (("ollamaUrl", 300), ("ollamaModel", 200)):
            if key in params and (not isinstance(params[key], str) or not 1 <= len(params[key]) <= maximum):
                raise ValueError(f"{key} has invalid length")
    if operation == "outline":
        if "expectedHash" in params and (not isinstance(params["expectedHash"], str) or not re.fullmatch(r"[0-9a-f]{64}", params["expectedHash"])):
            raise ValueError("expectedHash must be 64 lowercase hexadecimal characters")
        if type(params.get("limit", 10)) is not int or not 1 <= params.get("limit", 10) <= 100:
            raise ValueError("limit must be between 1 and 100")
        if type(params.get("offset", 0)) is not int or not 0 <= params.get("offset", 0) <= 1_000_000:
            raise ValueError("offset is out of bounds")
    if operation == "read":
        if not isinstance(params.get("expectedHash"), str) or not re.fullmatch(r"[0-9a-f]{64}", params["expectedHash"]):
            raise ValueError("expectedHash must be 64 lowercase hexadecimal characters")
        if "section" in params and (not isinstance(params["section"], str) or not 1 <= len(params["section"]) <= 200):
            raise ValueError("section is invalid")
        for key in ("start", "end", "cursor"):
            if key in params and (type(params[key]) is not int or not 0 <= params[key] <= 100_000_000):
                raise ValueError(f"{key} is out of bounds")
        if params.get("view", "raw") not in {"raw", "current"}:
            raise ValueError("view must be raw or current")
        if type(params.get("maxChars", 12000)) is not int or not 1 <= params.get("maxChars", 12000) <= 12000:
            raise ValueError("maxChars must be between 1 and 12000")


def fail(operation: str, code: str, message: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"operation": operation, "ok": False, "code": code[:64], "message": message[:1000]}
    if payload is not None:
        result["payload"] = payload
    return result


def valid_envelope(operation: str, result: Any) -> bool:
    if not isinstance(result, dict) or result.get("operation") != operation or type(result.get("ok")) is not bool:
        return False
    if result.keys() - {"operation", "ok", "payload", "code", "message"}:
        return False
    if "payload" in result and not isinstance(result["payload"], dict):
        return False
    if "code" in result and (not isinstance(result["code"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", result["code"])):
        return False
    if "message" in result and (not isinstance(result["message"], str) or len(result["message"]) > 1000):
        return False
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def selected_vault(params: dict[str, Any], config: dict[str, Any] | None) -> Path | None:
    explicit = params.get("vault")
    if explicit is not None:
        if not isinstance(explicit, str) or not os.path.isabs(explicit):
            raise ValueError("vault must be an absolute path")
    return cli.resolve_vault(explicit, config)


def run(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    explicit_read_vault = operation in {"read", "outline"} and params.get("vault") is not None
    if explicit_read_vault:
        config_file, config, config_error = None, None, None
    else:
        config_file = cli.config_path(None)
        config, config_error = cli.load_config(config_file)
    if operation == "doctor":
        if params.get("vault") is not None and (not isinstance(params["vault"], str) or not os.path.isabs(params["vault"])):
            raise ValueError("vault must be an absolute path")
        vault = selected_vault(params, config)
        payload = cli.doctor(config_file, config, config_error, vault)
        return {"operation": operation, "ok": config_error is None, "payload": payload,
                **({"code": "invalid-config", "message": "Selected integration config is invalid; see payload.config.error"} if config_error else {})}
    if config_file is not None and params.get("vault") is None and not os.environ.get("OBSIDIAN_VAULT_PATH", "").strip() and config_error:
        return fail(operation, "invalid-config", "Selected integration config is invalid", {"config": {"path": str(config_file), "error": config_error}})
    vault = selected_vault(params, config)
    if vault is None:
        return fail(operation, "no-vault", "No vault selected; provide an absolute vault or select one in configuration")
    if operation == "search":
        query = params["query"]
        if len(query) > cli.RETRIEVAL_MODULE.MAX_QUERY_CHARS:
            return fail(operation, "invalid-query", f"query exceeds {cli.RETRIEVAL_MODULE.MAX_QUERY_CHARS} characters")
        payload = cli.search(vault, query, params.get("limit", 10), params.get("hybrid", False),
                             params.get("ollamaUrl", "http://127.0.0.1:11434"),
                             params.get("ollamaModel", "qwen3-embedding:4b"), params.get("timeout", 8.0))
        return {"operation": operation, "ok": True, "payload": payload}
    reads = cli.READS_MODULE
    page = params["page"]
    if operation == "outline":
        payload = reads.outline_page(vault, page, expected_hash=params.get("expectedHash"),
                                     limit=params.get("limit", 10), offset=params.get("offset", 0))
    else:
        has_section = "section" in params
        has_span = "start" in params or "end" in params
        if has_section == has_span or (has_span and not ("start" in params and "end" in params)):
            raise ValueError("provide exactly one section or complete start/end range")
        payload = reads.read_page(vault, page, params["expectedHash"], section=params.get("section"),
                                  start=params.get("start"), end=params.get("end"), view=params.get("view", "raw"),
                                  max_chars=params.get("maxChars", 12000), cursor=params.get("cursor"))
    return {"operation": operation, "ok": True, "payload": payload}


def fit_review(review: dict[str, Any]) -> dict[str, Any]:
    """Drop diff excerpts, last path first, until the summary fits the result budget (hashes and sizes always stay)."""
    for item in reversed(review["operations"]):
        if len(json.dumps(review, ensure_ascii=False).encode("utf-8")) <= MAX_REVIEW_BYTES:
            break
        item.update({"diff": "", "diffTruncated": True})
    return review


def run_batch(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """Reviewed batch operations on the configured vault only (no `vault` input), through wiki_batch.py."""
    config_file = cli.config_path(None)
    config, config_error = cli.load_config(config_file)
    if config_error:
        return fail(operation, "invalid-config", "Selected integration config is invalid", {"config": {"path": str(config_file), "error": config_error}})
    vault = cli.resolve_vault(None, config)
    if vault is None:
        return fail(operation, "no-vault", "No vault selected; select one in configuration or OBSIDIAN_VAULT_PATH")
    batch = load_source("pi_wiki_batch", str(Path(__file__).with_name("wiki_batch.py")))
    persisted = False

    def mark_persisted() -> None:
        nonlocal persisted
        persisted = True
        print(JOURNAL_PERSISTED, file=sys.stderr, flush=True)

    plan_hash = params.get("planHash")
    try:
        if operation == "batch-recover":
            batch_id = params.get("batchId") or batch.batch_for_plan(vault, plan_hash)
            authority = batch._load_journal(vault, batch_id)["authority"]
            if authority != "wiki-page":
                return fail(operation, "authority-cli-only", f"Batch {batch_id} has {authority} authority; recover it with the CLI batch-recover (batch-rollback is CLI-only too)")
            payload = batch.recover(vault, batch_id, config_file, on_locked=mark_persisted)
        else:
            bundle = {"version": 1, "operations": params["operations"]}
            if operation == "batch-inspect":
                payload = fit_review(batch.review(vault, bundle, config_file))
            else:
                payload = batch.apply(vault, bundle, config_file, plan_hash, "wiki-page", on_persisted=mark_persisted)
    except (batch.BatchError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if operation == "batch-apply" and persisted:
            return fail(operation, "batch-interrupted", f"Apply stopped after its journal was saved; call wiki_batch_recover with this planHash before retrying. Cause: {exc}", {"planHash": plan_hash})
        return fail(operation, "batch-refused", str(exc))
    return {"operation": operation, "ok": True, "payload": payload}


def main() -> int:
    requested = sys.argv[1] if len(sys.argv) > 1 else ""
    operation = requested if requested in OPERATIONS else "doctor"
    try:
        if requested not in OPERATIONS or len(sys.argv) != (2 if requested in BATCH_OPERATIONS else 3):
            result = fail(operation, "invalid-request", "Expected a supported operation and one JSON parameter object (batch operations read it from stdin)")
        elif CLI_LOAD_ERROR:
            result = fail(operation, "dependency-missing", "A required Python package module could not be loaded")
        else:
            if operation in BATCH_OPERATIONS:
                raw = sys.stdin.buffer.read(MAX_BATCH_STDIN + 1)
                if len(raw) > MAX_BATCH_STDIN:
                    raise ValueError("batch parameters exceed the 64 KiB transport limit")
                params = json.loads(raw)
            else:
                params = json.loads(sys.argv[2])
            if not isinstance(params, dict):
                raise ValueError("tool parameters must be an object")
            validate(operation, params)
            result = run_batch(operation, params) if operation in BATCH_OPERATIONS else run(operation, params)
    except ModuleNotFoundError as exc:
        missing = "markdown-it-py is missing; install the shipped requirements.txt" if exc.name in {"markdown_it", "mdurl"} else "A required Python dependency is missing"
        result = fail(operation, "dependency-missing", missing)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError) as exc:
        # CLI-owned diagnostics are concise; never serialize a traceback or stderr.
        code = "source-changed" if type(exc).__name__ == "SourceChanged" else "invalid-request"
        result = fail(operation, code, str(exc))
    except Exception:
        result = fail(operation, "internal-error", "Wiki operation failed without a safe diagnostic")
    if not valid_envelope(operation, result):
        result = fail(operation, "invalid-result", "Wiki operation returned an invalid structured result")
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        encoded = json.dumps(fail(operation, "invalid-result", "Wiki operation returned non-JSON result data"), sort_keys=True)
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
