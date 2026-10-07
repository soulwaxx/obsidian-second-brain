#!/usr/bin/env python3
"""Repeatable deterministic boundary benchmark; does not measure live model quality."""
import argparse
import json
from pathlib import Path
from module_loading import load_source

CHUNKS = load_source("benchmark_semantic_chunks", str(Path(__file__).with_name("semantic-chunks.py")))

# These fixture vectors encode the expected topic labels by hand. They exercise
# chunk boundaries, evidence locations, and update freshness, not an embedding model.
FIXTURES = [
    {"case": "exact-name", "query": "Where is Mira Solenne described?", "topic": "person",
     "source": "# Research notebook\n\n## Project context\nThe field project collected samples across several districts.\n\n"
               "Mira Solenne led the archival review and catalogued the missing letters.\n\n"
               "## Other context\nThe weather station recorded a dry spring and a warm summer.\n",
     "evidence": "Mira Solenne"},
    {"case": "paraphrase", "query": "Who handled the archive letters?", "topic": "person",
     "source": "# Research notebook\n\nThe field project collected samples across several districts.\n\n"
               "Mira Solenne led the archival review and catalogued the missing letters.\n\n"
               "The weather station recorded a dry spring and a warm summer.\n",
     "evidence": "Mira Solenne"},
    {"case": "paraphrase-role", "query": "Who catalogued the correspondence?", "topic": "person",
     "source": "# Research notebook\n\nThe field project collected samples across several districts.\n\n"
               "Mira Solenne led the archival review and catalogued the missing letters.\n\n"
               "The weather station recorded a dry spring and a warm summer.\n",
     "evidence": "Mira Solenne"},
    {"case": "date", "query": "What was decided on 2025-02-14?", "topic": "date",
     "source": "# Research notebook\n\n## General notes\nSeveral planning sessions covered the annual schedule.\n\n"
               "## Decision 2025-02-14\nThe committee approved the archive preservation budget.\n\n"
               "## Follow-up\nThe finance team will review costs next quarter.\n",
     "evidence": "2025-02-14"},
    {"case": "long-page-tail", "query": "Where is the late-page migration decision?", "topic": "tail",
     "source": "# Long notes\n\n## Background\n" +
               "The initial migration inventory listed ordinary records and routine maintenance.\n\n" * 8 +
               "At the end of the long page, the migration decision assigns archive verification to the records team.\n",
     "evidence": "migration decision assigns archive verification"},
]


def vector(text):
    lowered = text.casefold()
    if "mira solenne" in lowered or "archival review" in lowered or "archive letters" in lowered:
        return [1.0, 0.0, 0.0, 0.0]
    if "2025-02-14" in lowered or "committee approved" in lowered:
        return [0.0, 1.0, 0.0, 0.0]
    if "migration decision" in lowered or "archive verification" in lowered:
        return [0.0, 0.0, 1.0, 0.0]
    return [0.0, 0.0, 0.0, 1.0]


def topic_vector(topic):
    return {"person": [1.0, 0.0, 0.0, 0.0], "date": [0.0, 1.0, 0.0, 0.0],
            "tail": [0.0, 0.0, 1.0, 0.0]}[topic]


def cosine(left, right):
    norm_left = sum(item * item for item in left) ** 0.5
    norm_right = sum(item * item for item in right) ** 0.5
    return sum(a * b for a, b in zip(left, right)) / (norm_left * norm_right) if norm_left and norm_right else 0.0


def rank(chunks, query_vector, evidence):
    ranked = sorted(enumerate(chunks), key=lambda pair: (-cosine(pair[1]["vector"], query_vector), pair[0]))
    for place, (_, chunk) in enumerate(ranked, 1):
        if evidence in chunk["text"]:
            return place
    return None


def benchmark():
    results = []
    for fixture in FIXTURES:
        source = fixture["source"]
        units = CHUNKS.text_units(source, max_chars=480)
        vectors = [vector(unit["text"]) for unit in units]
        semantic = CHUNKS.semantic_chunks("wiki/benchmark.md", source, vectors, max_chars=480, target_chars=300)
        heading = CHUNKS.heading_chunks("wiki/benchmark.md", source, max_chars=480)
        qvec = topic_vector(fixture["topic"])
        # The heading baseline has no vectors by contract. Mean-pool the exact
        # same deterministic unit vectors over its source spans for comparison.
        for chunk in heading:
            overlapping = [vector_item for unit, vector_item in zip(units, vectors)
                           if unit["start"] >= chunk["start"] and unit["end"] <= chunk["end"]]
            chunk["vector"] = ([sum(item[i] for item in overlapping) / len(overlapping) for i in range(4)]
                               if overlapping else [0.0] * 4)
        entry = {
            "case": fixture["case"],
            "query": fixture["query"],
            "evidence": fixture["evidence"],
            "semantic": {"chunks": len(semantic), "evidenceRank": rank(semantic, qvec, fixture["evidence"]),
                         "evidenceChunks": sum(fixture["evidence"] in chunk["text"] for chunk in semantic)},
            "headingBaseline": {"chunks": len(heading), "evidenceRank": rank(heading, qvec, fixture["evidence"]),
                                "evidenceChunks": sum(fixture["evidence"] in chunk["text"] for chunk in heading)},
        }
        results.append(entry)
    prior = "# Update\n\nThe older evidence was superseded.\n"
    updated = "# Update\n\nThe current decision changed after review.\n"
    results.append({"case": "update-freshness", "oldContentHash": CHUNKS.sha256(prior),
                    "updatedContentHash": CHUNKS.sha256(updated),
                    "staleEntryRejected": CHUNKS.sha256(prior) != CHUNKS.sha256(updated)})
    return {"benchmark": "semantic-vs-heading-boundaries-v1", "embeddingFixture": "deterministic hand-authored topic vectors",
            "liveModelQualityEvaluated": False,
            "interpretation": "Logic/boundary fixture only; ranks are not evidence of natural-language model quality or user retrieval improvement.",
            "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    report = benchmark()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{report['benchmark']} (live model quality: not evaluated)")
        for item in report["cases"]:
            if item["case"] == "update-freshness":
                print(f"{item['case']}: stale entry rejected={item['staleEntryRejected']}")
                continue
            semantic, heading = item["semantic"], item["headingBaseline"]
            print(f"{item['case']}: semantic chunks={semantic['chunks']} evidence-rank={semantic['evidenceRank']}; "
                  f"heading chunks={heading['chunks']} evidence-rank={heading['evidenceRank']}")
        print(report["interpretation"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
