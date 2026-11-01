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
READS_MODULE = load_source("obsidian_cli_retrieval_reads", str(Path(__file__).with_name("retrieval_reads.py")))


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
        if retrieval["freshness"] in {"stale", "stale-projection", "pending-content", "pending-refresh", "unknown"}:
            retrieval.update({"ready": False, "reason": "index is stale; use the reviewed index-build workflow"})
    retry = LIFECYCLE_MODULE.retrieval_status(vault)
    retrieval["retry"] = retry
    features = config.get("features") if isinstance(config, dict) else None
    retrieval["automaticRefreshEnabled"] = features.get("retrievalRefresh") is not False if isinstance(features, dict) else True if features is None else None
    if retry.get("pending") or retry.get("error"):
        retrieval.update({"ready": False, "reason": "published wiki batch has a pending retrieval refresh" if retry.get("pending") else retry["error"]})
    checks["retrieval"] = retrieval
    return checks


def freshness(index: Path, pages: list[str], vault: Path) -> str:
    """Prefer source hashes; retain conservative mtime checks for old caches."""
    try:
        retry = LIFECYCLE_MODULE.retrieval_status(vault)
        if retry.get("error"):
            return "unknown"
        if retry.get("pending"):
            return "pending-refresh"
        _, state_path, _ = LIFECYCLE_MODULE.layout(vault, False)
        if state_path.exists() and LIFECYCLE_MODULE.load_state(state_path, vault)["pending"]:
            return "pending-content"
        data = INDEX_MODULE.load_index(index)
        if data.get("projectionScheme") != INDEX_MODULE.projection.SCHEME:
            return "stale-projection"
        if "sourceHashes" in data:
            return "current-by-hash" if data["sourceHashes"] == INDEX_MODULE.source_manifest(vault) else "stale"
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
    except (OSError, ValueError, KeyError, TypeError, LIFECYCLE_MODULE.LifecycleError):
        return "unknown"


def read_reference(vault: Path, page: str, view: str, start: int, end: int, expected_source: str) -> dict[str, Any]:
    """Attach a ref only when its safely-opened snapshot is the excerpt's source."""
    try:
        raw, digest = READS_MODULE.read_snapshot(vault, page)
        source = raw.decode("utf-8", errors="replace")
        if source != expected_source or not 0 <= start <= end <= len(source):
            return {}
        return {"sourceHash": digest, "readRef": {"page": page, "expectedHash": digest,
                "view": view, "start": start, "end": end}}
    except (OSError, ValueError):
        return {}


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
    retry = LIFECYCLE_MODULE.retrieval_status(vault)
    response["retrievalPending"] = retry.get("pending")
    response["retrievalRetry"] = retry
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
        response["projection"] = data.get("projectionScheme", "legacy-unprojected")
        response["view"] = "historical" if date_match else "current"
        semantic = []
        semantic_error = None
        if hybrid and not date_match:
            try:
                semantic = RETRIEVAL_MODULE.semantic_candidates(query, vault, ollama_url, ollama_model, timeout)
                if not semantic:
                    semantic_error = "no current compatible semantic evidence (missing/stale cache or unverified installed model identity)"
            except Exception as exc:
                semantic_error = str(exc)
        safe = []
        if semantic and hybrid and not date_match:
            lexical_rank = {page: rank for rank, (page, _) in enumerate(candidates, 1)}
            semantic_page_data = {}
            for page, score, _chunk_index, chunk in semantic:
                if page not in semantic_page_data:
                    semantic_page_data[page] = (score, chunk)
            ranked_pages, fused_data = RETRIEVAL_MODULE.fuse_ranks(candidates, semantic)
            fused = {page: value[0] for page, value in fused_data.items()}
            ranked_pages = ranked_pages[:max(0, min(limit, 100))]
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
                    excerpt = " ".join(chunk.get("displaySource", source_text)[start:end].split())
                    if len(excerpt) > MAX_EXCERPT:
                        excerpt = excerpt[:MAX_EXCERPT - 1].rstrip() + "…"
                    line_start = source_text.count("\n", 0, start) + 1
                    line_end = source_text.count("\n", 0, end) + (1 if end == 0 or source_text[end - 1:end] != "\n" else 0)
                    match_type = "hybrid" if page in lexical_rank else "semantic"
                    item = {"path": rel.as_posix(), "score": fused[page], "match": match_type,
                            "lineStart": line_start, "lineEnd": max(line_start, line_end),
                            "charStart": start, "charEnd": end, "semanticScore": similarity,
                            "excerpt": excerpt}
                    item.update(read_reference(vault, rel.as_posix(), "current", start, end, source_text))
                    safe.append(item)
                else:
                    source_text = INDEX_MODULE.projection.read_source(source)
                    excerpt, line_start, line_end = INDEX_MODULE.projection.excerpt(source_text, [x.casefold() for x in INDEX_MODULE.tokens(query)], max_chars=MAX_EXCERPT)
                    item = {"path": rel.as_posix(), "score": fused[page], "match": "bm25",
                            "lineStart": line_start, "lineEnd": line_end, "excerpt": excerpt}
                    lines = source_text.splitlines(keepends=True)
                    start = sum(map(len, lines[:line_start - 1]))
                    end = sum(map(len, lines[:line_end]))
                    item.update(read_reference(vault, rel.as_posix(), "current", start, end, source_text))
                    safe.append(item)
        else:
            candidates = candidates[:max(0, min(limit, 100))]
        for page, score in ([] if semantic and hybrid and not date_match else candidates):
            rel = Path(page)
            if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "wiki":
                continue
            source = vault / rel
            if not INDEX_MODULE.eligible(source, vault):
                continue
            source_text = INDEX_MODULE.projection.read_source(source)
            excerpt, line_start, line_end = INDEX_MODULE.projection.excerpt(source_text, [x.casefold() for x in INDEX_MODULE.tokens(query)], historical=bool(date_match), iso_date=date_match.group() if date_match else None, max_chars=MAX_EXCERPT)
            match_type = "exact-date" if page in exact else "canonical-date-section" if page in canonical else "bm25"
            item = {"path": rel.as_posix(), "score": score, "match": match_type,
                    "lineStart": line_start, "lineEnd": line_end, "excerpt": excerpt}
            lines = source_text.splitlines(keepends=True)
            start = sum(map(len, lines[:line_start - 1]))
            end = sum(map(len, lines[:line_end]))
            item.update(read_reference(vault, rel.as_posix(), "raw" if date_match else "current", start, end, source_text))
            safe.append(item)
        response.update({"results": safe, "fallback": ("hybrid" if semantic and hybrid and not date_match else "bm25") if safe else "navigation",
                         "freshness": freshness_state, "reason": None if safe else "no indexed matches; use wiki/index.md navigation"})
        if semantic_error:
            response["semanticError"] = semantic_error
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError) as exc:
        response["reason"] = f"retrieval unavailable ({exc}); use wiki/index.md navigation"
    return response


def main() -> int:
    class CliParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            arguments = sys.argv[1:]
            # Options may precede the command. Skip their values so a path or
            # search query named "read"/"outline" cannot select the error format.
            remaining = iter(arguments)
            command = None
            for argument in remaining:
                if argument == "--":
                    command = next(remaining, None)
                    break
                option, separator, _ = argument.partition("=")
                action = self._option_string_actions.get(option)
                if action is not None:
                    if action.nargs != 0 and not separator:
                        next(remaining, None)
                elif not argument.startswith("-"):
                    command = argument
                    break
            if "--json" in arguments:
                if command in {"read", "outline"}:
                    code = "invalid-read"
                else:
                    code = "invalid-arguments"
                print(json.dumps({"code": code, "error": message}, ensure_ascii=False))
                raise SystemExit(2)
            super().error(message)

    parser = CliParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "search", "read", "outline", "build", "evidence-report", "batch-inspect", "batch-apply", "batch-recover", "batch-rollback", "capture-inspect", "capture-apply", "observe-inspect", "observe-apply", "baseline-inspect", "baseline-apply", "source-history"))
    parser.add_argument("query", nargs="?", help="search terms (required for search)")
    parser.add_argument("--vault", help="explicit vault root; overrides OBSIDIAN_VAULT_PATH and config")
    parser.add_argument("--config", help="config JSON path (default: OBSIDIAN_AGENT_CONFIG or standard user config)")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--limit", type=int, default=10, help="maximum search results or outline entries (outline: 1–100)")
    parser.add_argument("--offset", type=int, default=0, help="outline entry offset")
    parser.add_argument("--start", type=int, help="read range start character offset (zero-based)")
    parser.add_argument("--end", type=int, help="read range end character offset (zero-based, exclusive)")
    parser.add_argument("--section", help="read an outline section ID")
    parser.add_argument("--expected-hash", help="raw source SHA-256 from outline or search readRef")
    parser.add_argument("--view", choices=("raw", "current"), default="raw", help="read raw or current-projected content")
    parser.add_argument("--max-chars", type=int, default=READS_MODULE.MAX_CHARS_DEFAULT, help="read budget (1–12000 decoded characters)")
    parser.add_argument("--cursor", type=int, help="absolute continuation cursor within the original selection")
    parser.add_argument("--hybrid", action="store_true", help="opt in to cached semantic chunk + BM25 rank fusion")
    parser.add_argument("--semantic-chunks", action="store_true", help="manual input-incremental semantic document build; never installs a model; requires build")
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
    parser.add_argument("--selection", help="strict version-1 source observation or reconciliation selection JSON")
    parser.add_argument("--source-root", help="invocation-only absolute mapping for selected local source root")
    parser.add_argument("--observation-id", help="selected observation ID for source-history")
    parser.add_argument("--allow-move", action="store_true", help="explicitly approve review of a selected asset locator move")
    args = parser.parse_args()

    def fail(message: str, code: str = "command-error") -> int:
        if args.json:
            print(json.dumps({"code": code, "error": message}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"obsidian-second-brain: {message}", file=sys.stderr)
        return 1

    if args.command in {"read", "outline"}:
        try:
            if args.config is not None:
                raise ValueError("read and outline do not load or accept --config")
            if not args.vault:
                raise ValueError(f"{args.command} requires --vault")
            if args.page is None or len(args.page) != 1:
                raise ValueError(f"{args.command} requires exactly one --page")
            if args.query is not None:
                raise ValueError(f"{args.command} does not accept query terms")
            if args.semantic_chunks or args.hybrid or args.source or args.bundle:
                raise ValueError(f"unsupported option for {args.command}")
            reads = load_source("obsidian_cli_retrieval_reads", str(Path(__file__).with_name("retrieval_reads.py")))
            vault_root = Path(args.vault).expanduser()
            if args.command == "outline":
                if any(value is not None for value in (args.section, args.start, args.end, args.cursor)):
                    raise ValueError("outline does not accept read selectors or cursor")
                if args.offset and not args.expected_hash:
                    raise ValueError("paginated outline continuation requires --expected-hash")
                result = reads.outline_page(vault_root, args.page[0], expected_hash=args.expected_hash,
                                            limit=args.limit, offset=args.offset)
            else:
                if args.offset != 0 or args.limit != 10:
                    raise ValueError("read does not accept --offset or --limit")
                result = reads.read_page(vault_root, args.page[0], args.expected_hash or "",
                                         section=args.section, start=args.start, end=args.end,
                                         view=args.view, max_chars=args.max_chars, cursor=args.cursor)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        except ModuleNotFoundError as exc:
            if exc.name in {"markdown_it", "mdurl"}:
                message = "read/outline requires markdown-it-py; install requirements.txt"
            else:
                message = str(exc)
            print(json.dumps({"error": message, "code": "dependency-missing"}) if args.json else f"obsidian-second-brain: {message}", file=sys.stderr if not args.json else sys.stdout)
            return 1
        except (OSError, UnicodeError, ValueError) as exc:
            payload = {"error": str(exc), "code": "source-changed" if type(exc).__name__ == "SourceChanged" else "invalid-read"}
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if args.json else f"obsidian-second-brain: {exc}", file=sys.stderr if not args.json else sys.stdout)
            return 1
    config_file = config_path(args.config)
    config, error = load_config(config_file)
    write_commands = {"batch-inspect", "batch-apply", "batch-recover", "batch-rollback",
                      "capture-inspect", "capture-apply", "observe-inspect", "observe-apply",
                      "baseline-inspect", "baseline-apply"}
    if args.command in write_commands and error:
        return fail(f"invalid selected integration config: {error}")
    vault = resolve_vault(args.vault, config)
    if args.command in {"observe-inspect", "observe-apply", "baseline-inspect", "baseline-apply", "source-history"}:
        try:
            source_module = load_source("obsidian_cli_source_baselines", str(Path(__file__).with_name("source_baselines.py")))
            batch_module = load_source("source_baselines_batch", str(Path(__file__).with_name("wiki_batch.py")))
            if vault is None:
                raise source_module.SourceBaselineError("no vault selected")
            if args.command in {"observe-inspect", "observe-apply", "source-history"} and not args.source_root:
                raise source_module.SourceBaselineError("--source-root requires an explicit absolute mapped root")
            if args.command == "source-history":
                if not args.observation_id:
                    raise source_module.SourceBaselineError("source-history requires --observation-id")
                records = source_module._ledger_records(vault)
                pair = records.get(args.observation_id)
                if pair is None or pair[0].get("kind") != "observation":
                    raise source_module.SourceBaselineError("selected observation is unavailable")
                context = pair[0].get("gitContext", {})
                result = source_module.history_availability(Path(args.source_root), context.get("head"))
                result["observationId"] = args.observation_id
            else:
                if not args.selection:
                    raise source_module.SourceBaselineError(f"{args.command} requires --selection")
                selection = json.loads(Path(args.selection).read_text(encoding="utf-8"))
                baseline_command = args.command.startswith("baseline-")
                bundle = (source_module.baseline_bundle(vault, selection) if baseline_command else
                          source_module.observation_bundle(vault, Path(args.source_root), selection, allow_move=args.allow_move))
                inspect_command = args.command.endswith("inspect")
                binding = {"command": "baseline" if baseline_command else "observe",
                           "vault": str(Path(vault).resolve(strict=True)),
                           "config": batch_module.config_identity(config_file), "selection": selection}
                if not baseline_command:
                    binding["sourceRootHash"] = batch_module.sha(
                        str(Path(args.source_root).expanduser().absolute()).encode("utf-8"))
                if not bundle["operations"]:
                    current_records = source_module._ledger_records(vault)
                    noop_material = {**binding, "records": {
                        key: batch_module.sha(raw) for key, (_record, raw) in sorted(current_records.items())},
                        "noop": True}
                    outer_hash = batch_module.sha(batch_module.canonical_json(noop_material))
                    if inspect_command:
                        result = {"planHash": outer_hash, "authority": "wiki-ledger", "targets": [], "noop": True}
                    else:
                        replay = batch_module.completed_cli_replay(vault, args.plan_hash or "", binding, config_file)
                        if replay is not None:
                            result = replay
                        elif args.plan_hash != outer_hash:
                            raise source_module.SourceBaselineError("approval hash does not bind current no-op selection, config, and records")
                        else:
                            result = {"planHash": outer_hash, "phase": "complete", "noop": True}
                else:
                    reviewed = batch_module.inspect(vault, bundle, config_file, "wiki-ledger")
                    approval_binding = {**binding, "ledgerPlanHash": reviewed["planHash"]}
                    outer_hash = batch_module.sha(batch_module.canonical_json(approval_binding))
                    if inspect_command:
                        result = {**reviewed, "planHash": outer_hash}
                    else:
                        if args.plan_hash != outer_hash:
                            raise source_module.SourceBaselineError("approval hash does not bind current selection, root mapping, config, or ledger plan")
                        result = batch_module.apply(vault, bundle, config_file, reviewed["planHash"], "wiki-ledger",
                                                    cli_binding=approval_binding, cli_plan_hash=outer_hash)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError,
                source_module.SourceBaselineError, batch_module.BatchError) as exc:
            return fail(str(exc))
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
        except ModuleNotFoundError as exc:
            if exc.name in {"markdown_it", "mdurl"}:
                message = ("evidence-report requires markdown-it-py; install it with "
                           "python3 -m pip install 'markdown-it-py>=4.2.0,<5' "
                           "or install the package's requirements.txt")
                return fail(message, "dependency-missing")
            return fail(str(exc), "dependency-missing")
        except (OSError, UnicodeError, ValueError) as exc:
            return fail(str(exc))
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
        except ModuleNotFoundError as exc:
            return fail(str(exc), "dependency-missing")
        except (OSError, ValueError, capture_module.CaptureError, capture_module.BATCH.BatchError) as exc:
            return fail(str(exc))
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
        except ModuleNotFoundError as exc:
            return fail(str(exc), "dependency-missing")
        except (OSError, ValueError, batch_module.BatchError) as exc:
            return fail(str(exc))
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
                retry_token = LIFECYCLE_MODULE.retrieval_status(vault).get("token")
                count = INDEX_MODULE.build(vault)
                if retry_token:
                    LIFECYCLE_MODULE.acknowledge_retrieval(vault, retry_token)
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
                        output["semantic"] = {key: semantic[key] for key in ("indexed", "reused", "deferred", "embeddedInputs", "embeddedChars", "reusedInputs", "buildSeconds", "cacheBytes")}
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
