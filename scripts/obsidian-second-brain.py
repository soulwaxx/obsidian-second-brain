#!/usr/bin/env python3
"""Read-only doctor and search commands for an Obsidian second-brain vault."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
from typing import Any

from config_contract import load_config
from module_loading import load_source

INDEX_MODULE = load_source("obsidian_cli_bm25", str(Path(__file__).with_name("bm25-index.py")))
RETRIEVAL_MODULE = load_source("obsidian_cli_retrieve", str(Path(__file__).with_name("retrieve.py")))
LIFECYCLE_MODULE = load_source("obsidian_cli_lifecycle", str(Path(__file__).with_name("wiki_lifecycle.py")))
DEFAULT_CONFIG = Path.home() / ".config/obsidian-second-brain/properties.json"
DEFAULT_INDEX = Path(".vault-meta/retrieval/bm25.json")
MAX_EXCERPT = 360


def config_path(explicit: str | None) -> Path:
    return Path(explicit or os.environ.get("OBSIDIAN_AGENT_CONFIG") or DEFAULT_CONFIG).expanduser().absolute()


def resolve_vault(explicit: str | None, config: dict[str, Any] | None) -> Path | None:
    selected = explicit or os.environ.get("OBSIDIAN_VAULT_PATH")
    if isinstance(selected, str):
        selected = selected.strip() or None
    if selected is None and config and isinstance(config.get("vaultPath"), str):
        selected = config["vaultPath"]
    if not selected:
        return None
    path = Path(selected).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path)).resolve()


def exclusion_status(vault: Path) -> dict[str, Any]:
    git = vault / ".git"
    if not git.exists():
        return {"applicable": False, "effective": None, "tracked": None, "detail": "vault is not a Git worktree"}
    if git.is_symlink():
        return {"applicable": True, "effective": False, "tracked": None, "detail": "refusing symlinked .git path"}
    try:
        top = subprocess.run(["git", "-C", str(vault), "rev-parse", "--show-toplevel"], text=True, capture_output=True, check=False)
        if top.returncode or Path(top.stdout.strip()).resolve() != vault.resolve():
            return {"applicable": True, "effective": False, "tracked": None, "detail": "Git worktree root could not be verified"}
        scopes = (
            (".vault-meta/lifecycle/", (".vault-meta/lifecycle/state.json", ".vault-meta/lifecycle/finalize.lock", ".vault-meta/lifecycle/.state-diagnostic")),
            (".vault-meta/retrieval/", (".vault-meta/retrieval/bm25.json", ".vault-meta/retrieval/bm25.json.tmp")),
        )
        checks = []
        tracked_paths = []
        for directory, outputs in scopes:
            checks.append(LIFECYCLE_MODULE._git_directory_ignored(vault, directory))
            for output in outputs:
                checked = subprocess.run(["git", "-C", str(vault), "check-ignore", "--no-index", "-q", "--", output], check=False)
                if checked.returncode not in (0, 1):
                    raise OSError(f"git check-ignore failed for {output} (exit {checked.returncode})")
                checks.append(checked.returncode == 0)
            tracked = subprocess.run(["git", "-C", str(vault), "ls-files", "--", directory], text=True, capture_output=True, check=False)
            if tracked.returncode:
                raise OSError(tracked.stderr.strip() or "git ls-files failed")
            tracked_paths.extend(line for line in tracked.stdout.splitlines() if line)
        return {"applicable": True, "effective": all(checks), "tracked": bool(tracked_paths),
                "trackedPaths": tracked_paths[:20], "detail": "all derived-state scopes and outputs ignored" if all(checks) else "one or more derived-state scopes or outputs are not effectively ignored"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"applicable": True, "effective": False, "tracked": None, "detail": str(exc)}


def doctor(config_file: Path, config: dict[str, Any] | None, config_error: str | None, vault: Path | None) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "config": {"path": str(config_file), "valid": config_error is None, "exists": config_file.exists(), "error": config_error},
        "dependencies": {"python": sys.version.split()[0], "retrieval": True,
                         "pyyaml": importlib.util.find_spec("yaml") is not None,
                         "git": bool(shutil.which("git")), "jq": bool(shutil.which("jq"))},
        "vault": str(vault) if vault else None,
    }
    if vault is None:
        checks.update({"cacheExclusions": {"applicable": False, "effective": None, "detail": "no vault selected"},
                       "lifecycle": {"pending": False, "count": 0, "state": "unavailable: no vault selected"},
                       "retrieval": {"ready": False, "indexExists": False, "reason": "no vault selected"}})
        return checks
    wiki = vault / "wiki"
    checks["vaultExists"] = vault.is_dir() and not vault.is_symlink()
    checks["wikiExists"] = wiki.is_dir() and not wiki.is_symlink()
    checks["cacheExclusions"] = exclusion_status(vault)
    state_path = vault / ".vault-meta/lifecycle/state.json"
    lifecycle: dict[str, Any] = {"pending": False, "count": 0, "state": "clean"}
    try:
        if any((vault / part).is_symlink() for part in (".vault-meta", ".vault-meta/lifecycle")):
            raise ValueError("lifecycle state parent is symlinked")
        if state_path.is_symlink():
            raise ValueError("lifecycle state is a symlink")
        if state_path.exists():
            if not state_path.is_file():
                raise ValueError("lifecycle state is not a regular file")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("lifecycle state root must be a JSON object")
            if state.get("version") != 1 or state.get("vault") != str(vault) or not isinstance(state.get("pending"), dict):
                raise ValueError("lifecycle state has an incompatible vault boundary")
            pending = state["pending"]
            lifecycle.update({"pending": bool(pending), "count": len(pending), "state": "pending" if pending else "clean",
                              "paths": sorted(pending)[:20]})
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        lifecycle.update({"state": "unavailable", "error": str(exc)})
    checks["lifecycle"] = lifecycle
    index = vault / DEFAULT_INDEX
    index_valid = False
    reason = "index missing"
    if any((vault / part).is_symlink() for part in (".vault-meta", ".vault-meta/retrieval")):
        reason = "retrieval cache path contains a symlink"
    elif index.is_symlink():
        reason = "index is a symlink"
    elif index.is_file():
        try:
            INDEX_MODULE.load_index(index)
            index_valid, reason = True, None
        except (OSError, ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError) as exc:
            reason = f"index invalid: {exc}"
    retrieval = {"ready": index_valid, "indexExists": index.is_file() and not index.is_symlink(),
                 "index": str(index), "reason": reason}
    if index_valid:
        retrieval["freshness"] = freshness(index, list(INDEX_MODULE.load_index(index)["lengths"]), vault)
        if retrieval["freshness"] == "stale":
            retrieval.update({"ready": False, "reason": "index is stale; use the reviewed index-build workflow"})
    checks["retrieval"] = retrieval
    return checks


def freshness(index: Path, pages: list[str], vault: Path) -> str:
    """Conservative page-set/mtime freshness; index format has no content digest."""
    try:
        wiki = vault / "wiki"
        indexed = set(pages)
        current = set()
        latest_page = 0
        candidates = sorted(
            candidate for candidate in wiki.rglob("*")
            if candidate.is_file() and candidate.name.casefold().endswith(".md")
        )
        for candidate in candidates:
            if not INDEX_MODULE.eligible(candidate, vault):
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if not INDEX_MODULE.tokens(text):
                continue
            relative = candidate.relative_to(vault).as_posix()
            current.add(relative)
            latest_page = max(latest_page, candidate.stat(follow_symlinks=False).st_mtime_ns)
        if current != indexed:
            return "stale"
        index_time = index.stat(follow_symlinks=False).st_mtime_ns
        return "stale" if latest_page > index_time else "current-by-mtime"
    except OSError:
        return "unknown"


def excerpt_for(path: Path, terms: list[str]) -> tuple[str, int, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines() or [""]
    folded = [term.casefold() for term in terms if term]
    selected = next((i for i, line in enumerate(lines) if any(term in line.casefold() for term in folded)), 0)
    value = " ".join(lines[selected:selected + 3]).strip()
    if len(value) > MAX_EXCERPT:
        value = value[:MAX_EXCERPT - 1].rstrip() + "…"
    return value, selected + 1, min(len(lines), selected + 3)


def search(vault: Path | None, query: str, limit: int, hybrid: bool = False,
           ollama_url: str = "http://127.0.0.1:11434", ollama_model: str = "qwen3-embedding:4b",
           timeout: float = 8.0) -> dict[str, Any]:
    response: dict[str, Any] = {"query": query, "vault": str(vault) if vault else None, "results": [], "fallback": "navigation", "freshness": "unavailable"}
    if vault is None:
        response["reason"] = "no vault selected"
        return response
    if len(query) > RETRIEVAL_MODULE.MAX_QUERY_CHARS:
        response["reason"] = f"query exceeds {RETRIEVAL_MODULE.MAX_QUERY_CHARS} characters"
        return response
    index = vault / DEFAULT_INDEX
    response["index"] = str(index)
    try:
        if any((vault / part).is_symlink() for part in (".vault-meta", ".vault-meta/retrieval")) or index.is_symlink():
            raise ValueError("retrieval cache path contains a symlink")
        data = INDEX_MODULE.load_index(index)
        candidates = []
        for page, score in INDEX_MODULE.query(data, query):
            relative = Path(page)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "wiki":
                continue
            if INDEX_MODULE.eligible(vault / relative, vault):
                candidates.append((page, score))
        date_match = RETRIEVAL_MODULE.DATE.search(query)
        exact: set[str] = set()
        canonical: set[str] = set()
        if date_match:
            exact, canonical = RETRIEVAL_MODULE.dated_matches(data, candidates, query, date_match, vault)
            candidates.sort(key=lambda item: (0 if item[0] in exact else 1 if item[0] in canonical else 2, -item[1], item[0]))
        freshness_state = freshness(index, list(data["lengths"]), vault)
        semantic = []
        semantic_error = None
        if hybrid and not date_match:
            try:
                semantic = RETRIEVAL_MODULE.semantic_candidates(query, vault, ollama_url, ollama_model, timeout)
            except Exception as exc:
                semantic_error = str(exc)
        safe = []
        if semantic and hybrid and not date_match:
            lexical_rank = {page: rank for rank, (page, _) in enumerate(candidates, 1)}
            semantic_rank = {}
            semantic_page_data = {}
            for rank, (page, score, _chunk_index, chunk) in enumerate(semantic, 1):
                if page not in semantic_rank:
                    semantic_rank[page] = rank
                    semantic_page_data[page] = (score, chunk)
            fused = {}
            for page in set(lexical_rank) | set(semantic_rank):
                fused[page] = ((1 / (RETRIEVAL_MODULE.RRF_K + lexical_rank[page]) if page in lexical_rank else 0.0)
                               + (1 / (RETRIEVAL_MODULE.RRF_K + semantic_rank[page]) if page in semantic_rank else 0.0))
            ranked_pages = sorted(fused, key=lambda page: (
                -fused[page], semantic_rank.get(page, RETRIEVAL_MODULE.MAX_SEMANTIC_CANDIDATES + 1),
                lexical_rank.get(page, RETRIEVAL_MODULE.MAX_SEMANTIC_CANDIDATES + 1), page))[:max(0, min(limit, 100))]
            for page in ranked_pages:
                rel = Path(page)
                source = vault / rel
                if not INDEX_MODULE.eligible(source, vault):
                    continue
                semantic_data = semantic_page_data.get(page)
                if semantic_data:
                    similarity, chunk = semantic_data
                    source_text = chunk["source"]
                    start, end = chunk["start"], chunk["end"]
                    excerpt = " ".join(source_text[start:end].split())
                    if len(excerpt) > MAX_EXCERPT:
                        excerpt = excerpt[:MAX_EXCERPT - 1].rstrip() + "…"
                    line_start = source_text.count("\n", 0, start) + 1
                    line_end = source_text.count("\n", 0, end) + (1 if end == 0 or source_text[end - 1:end] != "\n" else 0)
                    match_type = "hybrid" if page in lexical_rank else "semantic"
                    safe.append({"path": rel.as_posix(), "score": fused[page], "match": match_type,
                                 "lineStart": line_start, "lineEnd": max(line_start, line_end),
                                 "charStart": start, "charEnd": end, "semanticScore": similarity,
                                 "excerpt": excerpt})
                else:
                    excerpt, line_start, line_end = excerpt_for(source, [x.casefold() for x in INDEX_MODULE.tokens(query)])
                    safe.append({"path": rel.as_posix(), "score": fused[page], "match": "bm25",
                                 "lineStart": line_start, "lineEnd": line_end, "excerpt": excerpt})
        else:
            candidates = candidates[:max(0, min(limit, 100))]
        for page, score in ([] if semantic and hybrid and not date_match else candidates):
            rel = Path(page)
            if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "wiki":
                continue
            source = vault / rel
            if not INDEX_MODULE.eligible(source, vault):
                continue
            excerpt, line_start, line_end = excerpt_for(source, [x.casefold() for x in INDEX_MODULE.tokens(query)])
            match_type = "exact-date" if page in exact else "canonical-date-section" if page in canonical else "bm25"
            safe.append({"path": rel.as_posix(), "score": score, "match": match_type,
                         "lineStart": line_start, "lineEnd": line_end, "excerpt": excerpt})
        response.update({"results": safe, "fallback": ("hybrid" if semantic and hybrid and not date_match else "bm25") if safe else "navigation",
                         "freshness": freshness_state, "reason": None if safe else "no indexed matches; use wiki/index.md navigation"})
        if semantic_error:
            response["semanticError"] = semantic_error
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError) as exc:
        response["reason"] = f"retrieval unavailable ({exc}); use wiki/index.md navigation"
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "search", "build", "evidence-report", "batch-inspect", "batch-apply", "batch-recover", "batch-rollback", "capture-inspect", "capture-apply"))
    parser.add_argument("query", nargs="?", help="search terms (required for search)")
    parser.add_argument("--vault", help="explicit vault root; overrides OBSIDIAN_VAULT_PATH and config")
    parser.add_argument("--config", help="config JSON path (default: OBSIDIAN_AGENT_CONFIG or standard user config)")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--limit", type=int, default=10, help="maximum search results (0–100)")
    parser.add_argument("--hybrid", action="store_true", help="opt in to cached semantic chunk + BM25 rank fusion")
    parser.add_argument("--semantic-chunks", action="store_true", help="explicitly build/reuse local semantic chunk cache; requires build")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="loopback Ollama URL (semantic operations only)")
    parser.add_argument("--ollama-model", default="qwen3-embedding:4b", help="already-installed embedding model; no download is attempted")
    parser.add_argument("--timeout", type=float, default=8.0, help="embedding request timeout in seconds")
    parser.add_argument("--as-of", help="fixed ISO date for repeatable evidence-report freshness evaluation")
    parser.add_argument("--bundle", help="versioned batch operation JSON file")
    parser.add_argument("--plan-hash", help="exact reviewed plan hash from batch-inspect")
    parser.add_argument("--batch-id", help="batch identifier for recovery/rollback")
    parser.add_argument("--authority", choices=("wiki-page", "source-capture", "wiki-ledger"), default="wiki-page")
    parser.add_argument("--source", action="append", default=[], help="explicit absolute local text source; repeat to select multiple")
    parser.add_argument("--page", action="append", default=[], help="existing wiki page to link; repeat as wiki-relative path")
    args = parser.parse_args()
    config_file = config_path(args.config)
    config, error = load_config(config_file)
    write_commands = {"batch-inspect", "batch-apply", "batch-recover", "batch-rollback",
                      "capture-inspect", "capture-apply"}
    if args.command in write_commands and error:
        print(f"obsidian-second-brain: invalid selected integration config: {error}", file=sys.stderr)
        return 1
    vault = resolve_vault(args.vault, config)
    if args.command == "evidence-report":
        try:
            if vault is None:
                raise ValueError("no vault selected")
            reports = load_source("obsidian_cli_evidence_reports", str(Path(__file__).with_name("evidence_reports.py")))
            if args.query is not None:
                parser.error("evidence-report does not accept query terms")
            result = reports.build_report(vault, args.as_of)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"obsidian-second-brain: {exc}", file=sys.stderr)
            return 1
    if args.as_of is not None:
        parser.error("--as-of requires evidence-report")
    if args.command in {"capture-inspect", "capture-apply"}:
        try:
            capture_module = load_source("obsidian_cli_capture", str(Path(__file__).with_name("local_capture.py")))
            if not args.source:
                parser.error(f"{args.command} requires at least one --source")
            if vault is None:
                raise capture_module.CaptureError("no vault selected")
            result = (capture_module.inspect(vault, [Path(item) for item in args.source], config_file, args.page)
                      if args.command == "capture-inspect" else
                      capture_module.apply(vault, [Path(item) for item in args.source], config_file, args.plan_hash or "", args.page))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        except (OSError, ValueError, capture_module.CaptureError, capture_module.BATCH.BatchError) as exc:
            print(f"obsidian-second-brain: {exc}", file=sys.stderr)
            return 1
    batch_commands = {"batch-inspect", "batch-apply", "batch-recover", "batch-rollback"}
    if args.command in batch_commands:
        try:
            batch_module = load_source("obsidian_cli_batch", str(Path(__file__).with_name("wiki_batch.py")))
            if args.command in {"batch-inspect", "batch-apply"}:
                if not args.bundle:
                    parser.error(f"{args.command} requires --bundle")
                if vault is None:
                    raise batch_module.BatchError("no vault selected")
                bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
                result = (batch_module.inspect(vault, bundle, config_file, args.authority) if args.command == "batch-inspect"
                          else batch_module.apply(vault, bundle, config_file, args.plan_hash or "", args.authority))
            else:
                if not args.batch_id:
                    parser.error(f"{args.command} requires --batch-id")
                if vault is None:
                    raise batch_module.BatchError("no vault selected")
                result = (batch_module.recover(vault, args.batch_id, config_file) if args.command == "batch-recover"
                          else batch_module.rollback(vault, args.batch_id, config_file))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        except (OSError, ValueError, batch_module.BatchError) as exc:
            print(f"obsidian-second-brain: {exc}", file=sys.stderr)
            return 1
    if args.command == "search" and not args.query:
        parser.error("search requires query terms")
    if args.command != "search" and args.query is not None:
        parser.error(f"{args.command} does not accept query terms")
    if args.semantic_chunks and args.command != "build":
        parser.error("--semantic-chunks requires build")
    if args.hybrid and args.command != "search":
        parser.error("--hybrid requires search")
    if args.command == "doctor":
        output = doctor(config_file, config, error, vault)
        status = 0 if output["config"]["valid"] else 1
    elif args.command == "search":
        output = search(vault, args.query, args.limit, args.hybrid, args.ollama_url, args.ollama_model, args.timeout)
        status = 0
    else:
        if vault is None:
            output = {"vault": None, "indexed": 0, "semantic": None, "error": "no vault selected"}
            status = 1
        else:
            try:
                count = INDEX_MODULE.build(vault)
            except (OSError, UnicodeError, ValueError) as exc:
                output = {"vault": str(vault), "indexed": 0, "semantic": None, "error": str(exc)}
                status = 1
            else:
                output = {"vault": str(vault), "indexed": count, "semantic": None}
                status = 0
                if args.semantic_chunks:
                    try:
                        chunk_module = load_source("obsidian_cli_semantic_chunks", str(Path(__file__).with_name("semantic-chunks.py")))
                        semantic = chunk_module.build(vault, args.ollama_url, args.ollama_model, timeout=args.timeout)
                        output["semantic"] = {"indexed": semantic["indexed"], "reused": semantic["reused"], "deferred": semantic["deferred"]}
                    except Exception as exc:
                        output["semantic"] = {"error": str(exc), "deferred": []}
                        output["semanticFallback"] = "page-level BM25 remains available"
    if args.json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    else:
        if args.command == "doctor":
            print(f"Vault: {output['vault'] or 'not selected'}")
            print(f"Config: {'valid' if output['config']['valid'] else 'invalid'} ({output['config']['path']})")
            print(f"Retrieval: {'ready' if output['retrieval']['ready'] else output['retrieval']['reason']}")
            print(f"Lifecycle: {output['lifecycle']['state']}")
            print(f"Cache exclusions: {output['cacheExclusions']['detail']}")
            if output['config']['error']:
                print(output['config']['error'], file=sys.stderr)
        elif args.command == "search":
            for rank, item in enumerate(output["results"], 1):
                print(f"{rank}. {item['path']}:{item['lineStart']}-{item['lineEnd']}\t{item['score']:.6f}\t{item['excerpt']}")
            if not output["results"]:
                print(output.get("reason") or "No indexed matches; use wiki/index.md navigation.")
            print(f"Freshness: {output['freshness']}; fallback: {output['fallback']}", file=sys.stderr)
        else:
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
