#!/usr/bin/env python3
"""Read-only bridge for Pi's four native wiki locator tools."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sys
from typing import Any

try:
    from module_loading import load_source

    cli = load_source("pi_wiki_cli", str(Path(__file__).with_name("obsidian-second-brain.py")))
    CLI_LOAD_ERROR: str | None = None
except Exception as exc:
    cli = None
    CLI_LOAD_ERROR = getattr(exc, "name", None) or "required package module"

OPERATIONS = {"search", "outline", "read", "doctor"}
PARAMETERS = {
    "search": {"query", "vault", "limit", "hybrid", "ollamaUrl", "ollamaModel", "timeout"},
    "outline": {"page", "vault", "expectedHash", "limit", "offset"},
    "read": {"page", "vault", "expectedHash", "section", "start", "end", "view", "maxChars", "cursor"},
    "doctor": {"vault"},
}


def validate(operation: str, params: dict[str, Any]) -> None:
    extra = params.keys() - PARAMETERS[operation]
    if extra:
        raise ValueError("unsupported tool parameter")
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


def main() -> int:
    requested = sys.argv[1] if len(sys.argv) > 1 else ""
    operation = requested if requested in OPERATIONS else "doctor"
    try:
        if requested not in OPERATIONS or len(sys.argv) != 3:
            result = fail(operation, "invalid-request", "Expected a supported operation and one JSON parameter object")
        elif CLI_LOAD_ERROR:
            result = fail(operation, "dependency-missing", "A required Python package module could not be loaded")
        else:
            params = json.loads(sys.argv[2])
            if not isinstance(params, dict):
                raise ValueError("tool parameters must be an object")
            validate(operation, params)
            result = run(operation, params)
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
