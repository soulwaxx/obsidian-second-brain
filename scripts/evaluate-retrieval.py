#!/usr/bin/env python3
"""Read-only retrieval evaluation on prepared caches and reviewed, versioned gold.

BM25 is offline by default. --live-semantic uses only an installed loopback
Ollama model; it never builds caches or installs models. Results are private
owner-run artifacts, not CI quality gates. See docs/retrieval-evaluation.md.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time

from module_loading import load_source

HERE = Path(__file__).resolve().parent
CLI = load_source("evaluation_cli", str(HERE / "obsidian-second-brain.py"))
R = CLI.RETRIEVAL_MODULE
CHUNKS = load_source("evaluation_chunks", str(HERE / "semantic-chunks.py"))
LABELS = {"current", "stale", "mixed", "neither", "unjudgeable"}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def corpus_manifest(vault):
    return {p.relative_to(vault).as_posix(): digest(p.read_bytes())
            for p in sorted((vault / "wiki").rglob("*"))
            if p.name.casefold().endswith(".md") and CLI.INDEX_MODULE.eligible(p, vault)}


def validate_gold(gold, vault):
    vault = Path(vault).resolve()
    if (not isinstance(gold, dict) or type(gold.get("version")) is not int or gold["version"] != 1
            or not isinstance(gold.get("revision"), str) or not gold["revision"].strip()):
        raise ValueError("gold requires version 1 and a reviewed revision")
    intents = gold.get("intents")
    if not isinstance(intents, list) or not intents:
        raise ValueError("gold requires nonempty intents")
    manifest = corpus_manifest(vault)
    seen = set()
    for case in intents:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"].strip() or case["id"] in seen:
            raise ValueError("gold requires unique stable intent IDs")
        seen.add(case["id"])
        if (not isinstance(case.get("queries"), dict) or set(case["queries"]) != {"en", "it"}
                or any(not isinstance(q, str) or not q.strip() or len(q) > R.MAX_QUERY_CHARS
                       for q in case["queries"].values())):
            raise ValueError("each intent requires paired EN/IT queries")
        if (case.get("intent") not in ("current", "historical") or case.get("split") not in ("development", "held-out")
                or not isinstance(case.get("category"), str) or not case["category"].strip()):
            raise ValueError("gold requires temporal intent, split and category")
        dates = [R.DATE.search(case["queries"][language]) for language in ("en", "it")]
        date_scopes = [date.group() if date else None for date in dates]
        if date_scopes[0] != date_scopes[1]:
            raise ValueError("paired queries require the same ISO-date policy/scope")
        if case["intent"] == "historical" and any(not date for date in dates):
            raise ValueError("historical intent currently requires an ISO date in both queries")
        targets = case.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ValueError("gold requires reviewed acceptable targets")
        paths = []
        for target in targets:
            if not isinstance(target, dict) or not isinstance(target.get("path"), str):
                raise ValueError("gold target requires a wiki-relative path")
            relative = Path(target["path"])
            path = vault / relative
            if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                    or relative.parts[0] != "wiki" or target["path"] not in manifest
                    or not CLI.INDEX_MODULE.eligible(path, vault)):
                raise ValueError("unsafe or missing gold target")
            source = path.read_bytes().decode("utf-8")
            if digest(source.encode()) != target.get("hash"):
                raise ValueError(f"gold drift: {target['path']}")
            if target["path"] in paths:
                raise ValueError("duplicate target")
            paths.append(target["path"])
            if not isinstance(target.get("evidence"), list) or not target["evidence"]:
                raise ValueError("targets require reviewed evidence spans")
            for span in target["evidence"]:
                if not isinstance(span, dict) or not isinstance(span.get("text"), str) or not span["text"].strip():
                    raise ValueError("gold requires nonempty reviewed evidence text")
                start, end = span.get("start"), span.get("end")
                if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source)
                        or source[start:end] != span.get("text") or not isinstance(span.get("label"), str)
                        or span["label"] not in LABELS
                        or type(span.get("supportsAnswer")) is not bool):
                    raise ValueError(f"gold evidence drift or invalid judgment: {target['path']}")
        canonical = case.get("canonicalPages", [])
        if not isinstance(canonical, list) or any(not isinstance(p, str) for p in canonical) or not set(canonical) <= set(paths):
            raise ValueError("canonical pages must also be acceptable targets")


def page_metrics(results, acceptable, ks, cutoff):
    pages = [r["path"] for r in results]
    targets = set(acceptable)
    metrics = {}
    for k in ks:
        found = len(set(pages[:k]) & targets)
        metrics[f"hit@{k}"] = int(found > 0)
        metrics[f"recall@{k}"] = found / len(targets)
    rank = next((i for i, p in enumerate(pages[:cutoff], 1) if p in targets), None)
    metrics[f"mrr@{cutoff}"] = 1 / rank if rank else 0
    return metrics


def combine_labels(labels):
    if not labels:
        return "unjudgeable"
    if "unjudgeable" in labels:
        return "unjudgeable"
    if "mixed" in labels or {"current", "stale"} <= set(labels):
        return "mixed"
    for label in ["current", "stale", "neither"]:
        if label in labels:
            return label
    return "unjudgeable"


def judged_context(source, start, end, spans):
    """A fact substring does not judge the rest of an exposed/source passage."""
    intervals, labels = [], []
    for span in spans:
        left, right = max(start, span["start"]), min(end, span["end"])
        if left >= right:
            continue
        intervals.append((left, right))
        if not source[left:right].strip():
            continue
        trimmed_start = span["start"] + len(span["text"]) - len(span["text"].lstrip())
        trimmed_end = span["end"] - len(span["text"]) + len(span["text"].rstrip())
        if span["label"] == "neither" or start <= trimmed_start <= trimmed_end <= end:
            labels.append(span["label"])
        else:
            labels.append("unjudgeable")  # Truncated factual/temporal context.
    cursor = start
    for left, right in sorted(intervals):
        if source[cursor:left].strip():
            labels.append("unjudgeable")
        cursor = max(cursor, right)
    if source[cursor:end].strip():
        labels.append("unjudgeable")
    return combine_labels(labels)


def evidence_metrics(results, case, vault):
    targets = {t["path"]: t for t in case["targets"]}
    full, exposed, answers = [], [], []
    for result in results:
        target = targets.get(result["path"])
        if not target:
            full.append("unjudgeable")
            exposed.append("unjudgeable")
            answers.append(False)
            continue
        source = (vault / result["path"]).read_bytes().decode("utf-8")
        if "charStart" in result:
            start, end = result["charStart"], result["charEnd"]
        else:
            lines = source.splitlines(keepends=True)
            start = sum(map(len, lines[:result["lineStart"] - 1]))
            end = sum(map(len, lines[:result["lineEnd"]]))
        spans = [s for s in target["evidence"] if start <= s["start"] and s["end"] <= end]
        view = CLI.INDEX_MODULE.projection.view(source, case["queries"]["en"])
        expected, visible_end = CLI.INDEX_MODULE.projection.render_span(view, start, end)
        # Bind exposure to this occurrence's coordinates, not a matching copy
        # elsewhere in the excerpt. Unknown rendering cannot establish KU.
        visible = [s for s in spans if s["end"] - (len(s["text"]) - len(s["text"].rstrip())) <= visible_end
                   and " ".join(s["text"].split()) in result["excerpt"]] if expected == result["excerpt"] else []
        full.append(judged_context(source, start, end, target["evidence"]))
        exposed.append(judged_context(view, start, visible_end, target["evidence"]) if expected == result["excerpt"] else "unjudgeable")
        # Human judgments, not substring heuristics, determine usefulness. A
        # current substring in mixed/unjudgeable context does not establish KU.
        supporting = any(s["supportsAnswer"] and s["label"] in {"current", "stale", "mixed"} for s in visible)
        reviewed_history = any(s["supportsAnswer"] and s["label"] == "mixed" for s in visible)
        answers.append(supporting and (exposed[-1] in {"current", "stale", "mixed"} if case["intent"] == "historical"
                                      else exposed[-1] == "current" or (exposed[-1] == "mixed" and reviewed_history)))
    stale = next((i for i, label in enumerate(exposed) if label == "stale"), None)
    mixed = next((i for i, label in enumerate(exposed) if label == "mixed"), None)
    current = next((i for i, label in enumerate(exposed) if label == "current"), None)
    return {"fullSpanLabels": full, "exposedLabels": exposed,
            "ku@1": (None if exposed and exposed[0] == "unjudgeable" else int(bool(answers and answers[0]))) if case["intent"] == "current" else None,
            "answerHit": 1 if any(answers) else None if "unjudgeable" in exposed else 0,
            "staleBeforeCurrent": stale is not None and (current is None or stale < current),
            "mixedBeforeCurrent": mixed is not None and (current is None or mixed < current)}


def paired_interval(rows, samples=2000):
    groups = {}
    for row in rows:
        groups.setdefault(row["id"], []).append(row["difference"])
    values = [statistics.mean(group) for group in groups.values()]
    if not values:
        return {"intents": 0, "mean": None, "interval95": None}
    rng = random.Random(0)
    estimates = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(samples))
    return {"intents": len(values), "mean": statistics.mean(values),
            "interval95": [estimates[int(.025 * samples)], estimates[min(samples - 1, int(.975 * samples))]]}


def revision():
    def git(*args):
        result = subprocess.run(["git", "-C", str(HERE.parent), *args], capture_output=True, text=True, check=False,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        status = git("status", "--porcelain")
        return {"commit": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
                "dirty": bool(status) if status is not None else None,
                "workingTreeDiffHash": digest((git("diff", "HEAD") or "").encode()),
                "scriptHashes": {p.name: digest(p.read_bytes()) for p in sorted(HERE.glob("*.py"))}}
    except OSError:
        return {"commit": None, "tree": None, "dirty": None}


def cache_provenance(vault):
    result = {}
    for name in ["bm25.json", "chunks.json"]:
        path = CLI.INDEX_MODULE.safe_output(vault, Path(".vault-meta/retrieval") / name)
        if path.is_file():
            raw = path.read_bytes()
            result[name] = {"hash": digest(raw), "bytes": len(raw)}
            if name == "chunks.json":
                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("semantic cache root is not an object")
                except (ValueError, UnicodeError) as exc:
                    result[name]["error"] = str(exc)
                    continue
                result[name]["settings"] = data.get("settings")
                result[name]["buildStats"] = data.get("buildStats")
                result[name]["embeddingInputScheme"] = data.get("inputScheme", "raw-units-pooled-v1")
    return result


def model_metadata(endpoint, model, timeout):
    """Record only reported metadata; loopback may forward to unknown hardware."""
    result = {"quantization": "unknown", "parameterSize": "unknown", "runtimeVersion": "unknown"}
    try:
        tags = CHUNKS._request(endpoint, "/api/tags", timeout=timeout)
        for entry in tags.get("models", []):
            if CHUNKS._canonical_model_name(entry.get("name")) == CHUNKS._canonical_model_name(model):
                details = entry.get("details") or {}
                result["quantization"] = details.get("quantization_level", "unknown")
                result["parameterSize"] = details.get("parameter_size", "unknown")
        version = CHUNKS._request(endpoint, "/api/version", timeout=timeout)
        result["runtimeVersion"] = version.get("version", "unknown")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return result


def latency_distribution(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"samples": len(values), "median": statistics.median(values), "min": min(values), "max": max(values),
            "p95NearestRank": ordered[max(0, math.ceil(.95 * len(values)) - 1)]}


def summarize(rows):
    summary = {}
    for language in ["en", "it"]:
        selected = [r for r in rows if r["language"] == language]
        summary[language] = {}
        for category in sorted({r["category"] for r in selected} | {"all"}):
            group = [r for r in selected if category == "all" or r["category"] == category]
            summary[language][category] = {}
            for method in sorted({m for r in group for m in r["methods"]}):
                available = [r["methods"][method]["metrics"] for r in group if method in r["methods"]]
                evidence = {}
                for key in ["ku@1", "answerHit", "staleBeforeCurrent", "mixedBeforeCurrent"]:
                    values = [r["methods"][method]["evidence"][key] for r in group
                              if method in r["methods"] and r["methods"][method]["evidence"][key] is not None]
                    evidence[key] = {"judgedQueries": len(values), "mean": statistics.mean(values) if values else None}
                summary[language][category][method] = {"queries": len(available), "expected": len(group),
                    "means": {key: statistics.mean(m[key] for m in available) for key in available[0]}, "evidence": evidence}
    return summary


def evaluate(vault, gold, *, live=False, model="qwen3-embedding:4b", endpoint="http://127.0.0.1:11434",
             timeout=8, ks=(1, 3, 10), cutoff=10, candidate_sizes=(10, 20, 50), repeats=1, condition="unspecified",
             legacy=False):
    vault = Path(vault).resolve()
    validate_gold(gold, vault)
    manifest = corpus_manifest(vault)
    caches = cache_provenance(vault)
    if "bm25.json" not in caches:
        raise ValueError("prepared BM25 cache is required; evaluation never builds it")
    source_revision = revision()
    gold_hash = digest(json.dumps(gold, sort_keys=True).encode())
    pages, identity, error = {}, None, None
    if live:
        try:
            url = CHUNKS.local_endpoint(endpoint)
            identity = CHUNKS.model_digest(url, model, timeout=timeout)
            if not identity:
                raise ValueError("installed model digest unavailable/unverified")
            _, pages = R._valid_semantic_pages(vault, model, endpoint, timeout)
            if not pages:
                raise ValueError("no current compatible semantic evidence")
        except Exception as exc:
            error = str(exc)
    metadata = model_metadata(url, model, timeout) if live and identity and not error else {}
    rows = []
    for case in gold["intents"]:
        for language, query in case["queries"].items():
            lexical_latencies = []
            for _ in range(repeats):
                before = time.perf_counter()
                response = CLI.search(vault, query, 100)
                lexical_latencies.append(time.perf_counter() - before)
            if response["fallback"] == "navigation" and response.get("reason", "").startswith("retrieval unavailable"):
                raise ValueError(response["reason"])
            lexical = [(r["path"], r["score"]) for r in response["results"]]
            # CLI exposes up to 100. Fixed comparisons use at most that many;
            # full RRF uses all lexical matches, just as shipped retrieval does.
            data = CLI.INDEX_MODULE.load_index(vault / CLI.DEFAULT_INDEX)
            full_lexical = [(p, s) for p, s in CLI.INDEX_MODULE.query(data, query)
                            if CLI.INDEX_MODULE.eligible(vault / p, vault)]
            methods = {"bm25": response["results"][:cutoff]}
            latencies = []
            legacy_latencies = []
            fixed_coverage = {}
            compatible_pages = 0
            semantic_status = "not-requested"
            query_error = error
            if live and R.DATE.search(query):
                semantic_status = "lexical-date-policy"
            elif live and not error:
                try:
                    for _ in range(repeats):
                        before = time.perf_counter()
                        vector = CHUNKS.embeddings([query[:R.MAX_QUERY_CHARS]], url, model, timeout)[0]
                        if not any(vector):
                            raise ValueError("zero query embedding")
                        semantic = R.score_semantic_pages(pages, vector, vault)
                        latencies.append(time.perf_counter() - before)
                    if not semantic:
                        raise ValueError("query vector has no compatible current semantic candidates")
                    compatible_pages = sum(any(len(c["vector"]) == len(vector) for c in chunks) for chunks in pages.values())
                    semantic_status = "partial" if compatible_pages < len(manifest) else "complete"
                    def rendered(lex, sem):
                        output = R.hybrid_results(query, data, vault, lex, endpoint, model, timeout, cutoff, semantic=sem)
                        return [{**r, "path": r["page"], "charStart": r["start"], "charEnd": r["end"]} for r in output]
                    methods["rrf"] = rendered(full_lexical, semantic)
                    methods["semantic-only"] = rendered([], semantic)
                    for size in candidate_sizes:
                        requested = lexical[:size]
                        selected = {p: pages[p] for p, _ in requested if p in pages}
                        fixed_semantic = R.score_semantic_pages(selected, vector, vault)
                        scored_pages = {p for p, *_ in fixed_semantic}
                        fixed = [(p, score) for p, score in requested if p in scored_pages]
                        fixed_coverage[size] = {"requestedCandidates": len(requested), "withCompatibleVectors": len(fixed)}
                        methods[f"fixed-rrf-{size}"] = rendered(fixed, fixed_semantic)
                        methods[f"fixed-cosine-{size}"] = rendered([], fixed_semantic)
                    if legacy:
                        before = time.perf_counter()
                        ranked = R.rerank(query, lexical[:cutoff], vault, endpoint, model, timeout)
                        # The legacy CLI exposes page ranks, not an answer
                        # passage. Its document representation is the page head.
                        methods["legacy-page-head"] = [{"path": p, "score": score, "charStart": 0,
                            "charEnd": min(R.MAX_DOCUMENT_CHARS, len((vault / p).read_text())),
                            "excerpt": ""} for p, score in ranked]
                        legacy_latencies.append(time.perf_counter() - before)
                except Exception as exc:
                    semantic_status, query_error = "failed", str(exc)
                    methods = {"bm25": methods["bm25"]}
            elif live:
                semantic_status = "unavailable"
            acceptable = [t["path"] for t in case["targets"]]
            scored = {name: {"results": result, "metrics": page_metrics(result, acceptable, ks, cutoff),
                             "canonicalMetrics": page_metrics(result, case.get("canonicalPages") or acceptable, ks, cutoff),
                             "evidence": evidence_metrics(result, case, vault)} for name, result in methods.items()}
            rows.append({"id": case["id"], "language": language, "category": case["category"], "split": case["split"],
                         "intent": case["intent"], "query": query, "methods": scored,
                         "candidateCoverage": page_metrics(response["results"], acceptable, candidate_sizes, cutoff),
                         "semanticStatus": semantic_status, "error": query_error, "freshness": response["freshness"],
                         "latencySeconds": latencies, "bm25LatencySeconds": lexical_latencies,
                         "legacyLatencySeconds": legacy_latencies, "fixedSemanticCoverage": fixed_coverage,
                         "compatibleSemanticPages": compatible_pages,
                         "latencyDistributions": {"bm25": latency_distribution(lexical_latencies),
                             "queryEmbeddingAndCandidateScoring": latency_distribution(latencies),
                             "legacyPageHead": latency_distribution(legacy_latencies)},
                         "condition": condition})
    if corpus_manifest(vault) != manifest or cache_provenance(vault) != caches:
        raise ValueError("corpus/cache drift during evaluation; results are not comparable")
    if digest(json.dumps(gold, sort_keys=True).encode()) != gold_hash or revision().get("scriptHashes") != source_revision.get("scriptHashes"):
        raise ValueError("gold/source-code drift during evaluation; results are not comparable")
    if live and identity and CHUNKS.model_digest(url, model, timeout) != identity:
        raise ValueError("installed model identity drift during evaluation")
    summary = summarize(rows)
    comparisons = {}
    for method in sorted({m for r in rows for m in r["methods"]} - {"bm25"}):
        for language in ["en", "it", "paired-intents"]:
            differences = [{"id": r["id"], "difference": r["methods"][method]["metrics"][f"mrr@{cutoff}"]
                           - r["methods"]["bm25"]["metrics"][f"mrr@{cutoff}"]} for r in rows
                           if method in r["methods"] and (language == "paired-intents" or r["language"] == language)]
            comparisons[f"{method}-minus-bm25/{language}"] = {"perQuery": differences, **paired_interval(differences)}
    for size in candidate_sizes:
        left, right = f"fixed-cosine-{size}", f"fixed-rrf-{size}"
        differences = [{"id": r["id"], "language": r["language"],
                        "difference": r["methods"][left]["metrics"][f"mrr@{cutoff}"]
                        - r["methods"][right]["metrics"][f"mrr@{cutoff}"]} for r in rows
                       if left in r["methods"] and right in r["methods"]]
        comparisons[f"{left}-minus-{right}/paired-intents"] = {"perQuery": differences, **paired_interval(differences)}
    return {"version": 1, "qualityGate": "none: owner-reviewed judgments and gate required",
            "provenance": {"source": source_revision, "pythonVersion": sys.version, "corpusManifest": manifest,
                "corpusManifestHash": digest(json.dumps(manifest, sort_keys=True).encode()),
                "goldHash": gold_hash, "goldRevision": gold["revision"],
                "model": model if live else None, "modelDigest": identity, "quantization": metadata.get("quantization", "unknown"),
                "runtime": "Ollama" if live else None, "runtimeMetadata": metadata, "hardware": "unknown", "caches": caches,
                "queryInputScheme": "raw-query-v1", "projectionScheme": CLI.INDEX_MODULE.projection.SCHEME,
                "latencyScope": "BM25 includes CLI freshness checks; semantic timings cover query inference and candidate scoring, not initial cache validation or end-to-end hybrid display",
                "semanticPages": len(pages), "eligiblePages": len(manifest), "candidateSizes": candidate_sizes,
                "cutoff": cutoff, "ks": ks, "condition": condition, "repeats": repeats,
                "actualModes": sorted({m for r in rows for m in r["methods"]})},
            "queries": rows, "summary": summary, "pairedDifferences": comparisons,
            "summaryBySplit": {split: summarize([r for r in rows if r["split"] == split]) for split in sorted({r["split"] for r in rows})}}


def compare_reports(baseline, candidate, method="rrf", split="held-out"):
    """Internal scorer for isolated model reports; never promotes a default.

    Labels/projection/chunk/input schemes/corpus/candidates must already be
    frozen. Incomplete live runs cannot enter a held-out model comparison.
    """
    left, right = baseline["provenance"], candidate["provenance"]
    if any(not p.get("modelDigest") or not p["caches"].get("chunks.json", {}).get("embeddingInputScheme") for p in [left, right]):
        raise ValueError("unverified identities/inputs are not comparable")
    for key in ["goldHash", "corpusManifestHash", "queryInputScheme", "projectionScheme", "cutoff", "candidateSizes"]:
        if left.get(key) != right.get(key):
            raise ValueError(f"reports are not comparable: {key}")
    for key in ["embeddingInputScheme", "settings"]:
        if left["caches"].get("chunks.json", {}).get(key) != right["caches"].get("chunks.json", {}).get(key):
            raise ValueError(f"reports are not comparable: {key}")
    if left["source"].get("scriptHashes") != right["source"].get("scriptHashes"):
        raise ValueError("reports are not comparable: source scripts changed")
    lrows = {(r["id"], r["language"]): r for r in baseline["queries"] if r["split"] == split}
    rrows = {(r["id"], r["language"]): r for r in candidate["queries"] if r["split"] == split}
    if not lrows or set(lrows) != set(rrows):
        raise ValueError("reports require identical nonempty reviewed split/pairs")
    differences = []
    regressions = []
    unjudgeable = []
    metric = f"mrr@{left['cutoff']}"
    for key, old in lrows.items():
        new = rrows[key]
        for row in [old, new]:
            if (row["semanticStatus"] not in {"complete", "lexical-date-policy"} or row.get("error")
                    or row.get("freshness") != "current-by-hash"):
                raise ValueError("failed/unavailable/partial runs are not comparable")
        actual_method = "bm25" if old["semanticStatus"] == "lexical-date-policy" else method
        if actual_method not in old["methods"] or actual_method not in new["methods"]:
            raise ValueError("requested comparison method missing")
        old_result, new_result = old["methods"][actual_method], new["methods"][actual_method]
        differences.append({"id": key[0], "language": key[1],
                            "difference": new_result["metrics"][metric] - old_result["metrics"][metric]})
        if old["intent"] == "current":
            before, after = old_result["evidence"], new_result["evidence"]
            if before["ku@1"] is None or after["ku@1"] is None:
                unjudgeable.append({"id": key[0], "language": key[1]})
            stale_before = bool(before["exposedLabels"] and (before["exposedLabels"][0] == "stale"
                                or (before["exposedLabels"][0] == "mixed" and not before["ku@1"])))
            stale_after = bool(after["exposedLabels"] and (after["exposedLabels"][0] == "stale"
                               or (after["exposedLabels"][0] == "mixed" and not after["ku@1"])))
            if (before["ku@1"] or 0) > (after["ku@1"] or 0) or (stale_after and not stale_before):
                regressions.append({"id": key[0], "language": key[1], "newStaleTop1": stale_after and not stale_before,
                                    "baseline": before, "candidate": after})
    return {"split": split, "method": method, "metric": metric, "perQuery": differences,
            "paired": {lang: paired_interval([r for r in differences if lang == "paired-intents" or r["language"] == lang])
                       for lang in ["en", "it", "paired-intents"]},
            "temporalRegressions": regressions, "unjudgeableTemporalCases": unjudgeable, "recommendation": "owner review required; no automatic model/fusion promotion",
            "latencyComparable": left.get("condition") == right.get("condition") == "warm"
                                 and left.get("latencyScope") == right.get("latencyScope")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--live-semantic", action="store_true")
    parser.add_argument("--legacy-page-head", action="store_true", help="separate expensive legacy rerank probe; requires --live-semantic")
    parser.add_argument("--ollama-model", default="qwen3-embedding:4b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--cutoff", type=int, default=10)
    parser.add_argument("--candidate-sizes", type=int, nargs="+", default=[10, 20, 50])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--condition", choices=["cold", "warm", "unspecified"], default="unspecified",
                        help="owner-established condition; command never flushes host/model caches")
    args = parser.parse_args()
    if not 1 <= args.cutoff <= 100 or args.repeats < 1 or any(not 1 <= n <= 100 for n in args.candidate_sizes):
        parser.error("cutoff/candidates must be 1–100 and repeats positive")
    if args.legacy_page_head and not args.live_semantic:
        parser.error("--legacy-page-head requires --live-semantic")
    try:
        gold_bytes = args.gold.read_bytes()
        report = evaluate(args.vault, json.loads(gold_bytes), live=args.live_semantic,
                          model=args.ollama_model, endpoint=args.ollama_url, timeout=args.timeout,
                          cutoff=args.cutoff, candidate_sizes=args.candidate_sizes,
                          repeats=args.repeats, condition=args.condition, legacy=args.legacy_page_head)
        if args.gold.read_bytes() != gold_bytes:
            raise ValueError("gold file drift during evaluation")
        report["provenance"]["goldFileHash"] = digest(gold_bytes)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return int(any(r["semanticStatus"] in {"failed", "unavailable", "partial"} for r in report["queries"]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"evaluate-retrieval: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
