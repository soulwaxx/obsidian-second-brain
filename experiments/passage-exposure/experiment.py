#!/usr/bin/env python3
"""Service-free passage mechanics experiment. Never a model-quality benchmark."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source

RETRIEVE = load_source("passage_retrieve", str(ROOT / "scripts/retrieve.py"))
EVALUATE = load_source("passage_evaluate", str(ROOT / "scripts/evaluate-retrieval.py"))
PROJECTION = RETRIEVE._index_module.projection
POLICIES = ("prefix", "body-prefix", "query-paragraph", "lexical-chunk-prefix")
STOP = set("what where who are is the in of a an qual quale quali chi dove è il la le i di del dell vengono si".split())
WORD = re.compile(r"\w+", re.UNICODE)
FRONTMATTER = re.compile(r"\A---\r?\n.*?^(?:---|\.\.\.)[ \t]*\r?$(?:\n|$)", re.M | re.S)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def body_start(source):
    match = FRONTMATTER.match(source)
    return match.end() if match else 0


def paragraphs(source, chunk):
    start, end = max(body_start(source), chunk["start"]), chunk["end"]
    return [(start + m.start(), start + m.end()) for m in
            re.finditer(r"\S[\s\S]*?(?=\r?\n[ \t]*\r?\n|\Z)", source[start:end])
            if not m.group().startswith("#")]


def overlap(query, text):
    terms = set(WORD.findall(query.casefold())) - STOP
    return len(terms & set(WORD.findall(text.casefold())))


def select_window(policy, query, source, chunks):
    """Uses query/source only. Evidence labels and target identity are unavailable."""
    view = PROJECTION.current(source)
    selected = chunks[0]  # Frozen synthetic winner; not a predicted model winner.
    scanned = 0
    if policy == "lexical-chunk-prefix":
        scored = []
        for chunk in chunks:
            spans = paragraphs(view, chunk)
            scanned += len(spans)
            scored.append(max((overlap(query, view[a:b]) for a, b in spans), default=0))
        selected = chunks[max(range(len(chunks)), key=lambda i: (scored[i], -i))]
    start, end = selected["start"], selected["end"]
    if policy in {"body-prefix", "query-paragraph"}:
        start = min(end, max(start, body_start(view)))
    if policy == "query-paragraph":
        spans = paragraphs(view, selected)
        scanned += len(spans)
        if spans:
            best = max(spans, key=lambda span: (overlap(query, view[span[0]:span[1]]), -span[0]))
            if overlap(query, view[best[0]:best[1]]) > 0:
                start, end = best
    excerpt, visible_end = PROJECTION.render_span(view, start, end)
    return {"start": start, "end": end, "visibleEnd": visible_end,
            "excerpt": excerpt, "paragraphsScanned": scanned}


def prepare(fixture, vault):
    pages, cases = {}, []
    for raw in fixture["cases"]:
        page = "wiki/" + raw["id"] + ".md"
        source = "".join(raw["chunks"])
        chunks, offset = [], 0
        for text in raw["chunks"]:
            chunks.append({"start": offset, "end": offset + len(text), "source": source,
                           "displaySource": PROJECTION.current(source)})
            offset += len(text)
        spans = []
        for evidence in raw["evidence"]:
            if source.count(evidence["text"]) != 1:
                raise ValueError("each reviewed occurrence must be unique")
            start = source.index(evidence["text"])
            spans.append({**evidence, "start": start, "end": start + len(evidence["text"])})
        cursor = 0
        for span in sorted(spans.copy(), key=lambda item: item["start"]):
            if source[cursor:span["start"]].strip():
                spans.append({"start": cursor, "end": span["start"], "text": source[cursor:span["start"]],
                              "label": "neither", "supportsAnswer": False})
            cursor = span["end"]
        if source[cursor:].strip():
            spans.append({"start": cursor, "end": len(source), "text": source[cursor:],
                          "label": "neither", "supportsAnswer": False})
        case = {"id": raw["id"], "category": raw["category"], "split": fixture["split"],
                "intent": "current", "queries": raw["queries"], "targetRank": raw["targetRank"],
                "targets": [{"path": page, "hash": digest(source.encode()), "evidence": spans}]}
        pages[page] = {"source": source, "chunks": chunks}
        cases.append(case)
    for name in ("unknown-a", "unknown-b", "unknown-c"):
        source = "# Unreviewed result\n\nOther returned evidence has no judgment.\n"
        pages[f"wiki/{name}.md"] = {"source": source, "chunks": [{"start": 0, "end": len(source),
                                               "source": source, "displaySource": source}]}
    (vault / "wiki").mkdir()
    for page, content in pages.items():
        (vault / page).write_bytes(content["source"].encode())
    EVALUATE.validate_gold({"version": 1, "revision": fixture["revision"], "intents": cases}, vault)
    return pages, cases


def frozen_results(case, language, pages, vault):
    target = case["targets"][0]["path"]
    order = [f"wiki/unknown-{name}.md" for name in "abc"]
    order.insert(case["targetRank"] - 1, target)
    lexical = [(page, 4 - i) for i, page in enumerate(order)]
    semantic = [(page, 1 - i / 10, 0, pages[page]["chunks"][0]) for i, page in enumerate(order)]
    results = RETRIEVE.hybrid_results(case["queries"][language], {}, vault, lexical,
                                     None, None, None, 4, semantic=semantic)
    if [result["page"] for result in results] != order:
        raise AssertionError("frozen page order changed")
    return results


def evaluate_policy(policy, case, language, pages, baseline, vault):
    results, windows = [], []
    for result in baseline:
        page = result["page"]
        window = select_window(policy, case["queries"][language], pages[page]["source"], pages[page]["chunks"])
        if policy == "prefix" and window["excerpt"] != result["excerpt"]:
            raise AssertionError("baseline diverged from shipped hybrid renderer")
        results.append({"path": page, "charStart": window["start"], "charEnd": window["end"],
                        "excerpt": window["excerpt"]})
        windows.append(window)
    target_index = case["targetRank"] - 1
    target = results[target_index]
    evidence = case["targets"][0]["evidence"]
    answer = next(span for span in evidence if span["supportsAnswer"])
    visible_end = windows[target_index]["visibleEnd"]
    contains = target["charStart"] <= answer["start"] and answer["end"] <= target["charEnd"]
    exposed = contains and answer["end"] <= visible_end and " ".join(answer["text"].split()) in target["excerpt"]
    cutoffs = {}
    for cutoff in (1, 3):
        metrics = EVALUATE.evidence_metrics(results[:cutoff], case, vault)
        cutoffs[str(cutoff)] = {"pageHit": int(case["targetRank"] <= cutoff),
                                "targetSpanExposed": int(exposed and case["targetRank"] <= cutoff),
                                "answerHit": metrics["answerHit"], "labels": metrics["exposedLabels"]}
    return {"targetRank": case["targetRank"], "targetContainsSpan": contains, "targetSpanExposed": exposed,
            "targetLabel": EVALUATE.evidence_metrics([target], case, vault)["exposedLabels"][0],
            "targetWindow": {**target, "visibleEnd": visible_end}, "cutoffs": cutoffs,
            "returnedCharacters": sum(len(item["excerpt"]) for item in results[:3]),
            "paragraphsScanned": sum(item["paragraphsScanned"] for item in windows)}


def experiment(timing_repeats=0):
    fixture_bytes = (HERE / "fixtures.json").read_bytes()
    fixture = json.loads(fixture_bytes)
    rows, timings = [], {policy: [] for policy in POLICIES}
    with tempfile.TemporaryDirectory(prefix="passage-experiment-") as temporary:
        vault = Path(temporary)
        pages, cases = prepare(fixture, vault)
        for case in cases:
            for language in ("en", "it"):
                baseline = frozen_results(case, language, pages, vault)
                policies = {policy: evaluate_policy(policy, case, language, pages, baseline, vault) for policy in POLICIES}
                rows.append({"id": case["id"], "language": language, "category": case["category"], "policies": policies})
                for policy in POLICIES:
                    for _ in range(timing_repeats):
                        start = time.perf_counter_ns()
                        for result in baseline:
                            content = pages[result["page"]]
                            select_window(policy, case["queries"][language], content["source"], content["chunks"])
                        timings[policy].append((time.perf_counter_ns() - start) / 1e6)
    summary = {}
    for language in ("en", "it"):
        subset = [row for row in rows if row["language"] == language]
        summary[language] = {}
        for policy in POLICIES:
            observations = [row["policies"][policy] for row in subset]
            summary[language][policy] = {"queries": len(observations), "cutoffs": {},
                "returnedCharacters": sum(item["returnedCharacters"] for item in observations),
                "paragraphsScanned": sum(item["paragraphsScanned"] for item in observations)}
            for cutoff in ("1", "3"):
                values = [item["cutoffs"][cutoff] for item in observations]
                labels = [label for value in values for label in value["labels"]]
                summary[language][policy]["cutoffs"][cutoff] = {
                    "pageHits": sum(value["pageHit"] for value in values),
                    "targetSpanExposed": sum(value["targetSpanExposed"] for value in values),
                    "answerHits": sum(value["answerHit"] == 1 for value in values),
                    "answerUnknown": sum(value["answerHit"] is None for value in values),
                    "judgedResults": sum(label != "unjudgeable" for label in labels),
                    "unknownResults": labels.count("unjudgeable")}
    sources = {page: digest(content["source"].encode()) for page, content in pages.items()}
    paths = [Path(__file__), HERE / "fixtures.json", ROOT / "scripts/module_loading.py",
             ROOT / "scripts/retrieve.py", ROOT / "scripts/retrieval_projection.py", ROOT / "scripts/evaluate-retrieval.py",
             ROOT / "scripts/bm25-index.py"]
    report = {"experiment": fixture["revision"], "split": fixture["split"],
              "annotationAuthority": fixture["annotationAuthority"],
              "liveModelQualityEvaluated": False, "freshHoldoutEvaluated": False,
              "ranking": "injected fixed page order and chunk-0 winners; not model predictions",
              "model": None, "cache": None, "projection": PROJECTION.SCHEME, "maxExcerptChars": 360,
              "fixtureHash": digest(fixture_bytes), "sourceHashes": sources,
              "scriptHashes": {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in paths},
              "summary": summary, "rows": rows}
    if timing_repeats:
        report["timing"] = {"scope": "four-page select_window calls only; no ranking, evaluator, startup, or model",
                            "repeatsPerQuery": timing_repeats, "policies": {}}
        for policy, samples in timings.items():
            ordered = sorted(samples)
            report["timing"]["policies"][policy] = {"samples": len(samples), "medianMs": statistics.median(samples),
                "p95Ms": ordered[(95 * len(samples) + 99) // 100 - 1]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timing-repeats", type=int, default=0)
    args = parser.parse_args()
    if args.timing_repeats < 0:
        parser.error("--timing-repeats must be nonnegative")
    print(json.dumps(experiment(args.timing_repeats), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
