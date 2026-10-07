"""Repeatable benchmark contract and cache publication concurrency regression."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source
BENCHMARK = ROOT / "scripts/benchmark-semantic-retrieval.py"
CHUNKS = load_source(
    "integration_semantic_chunks", str(ROOT / "scripts/semantic-chunks.py"))


class SemanticIntegrationTests(unittest.TestCase):
    def test_deterministic_benchmark_covers_retrieval_and_freshness_cases(self):
        runs = [subprocess.run([sys.executable, str(BENCHMARK), "--json"], check=True,
                               text=True, capture_output=True).stdout for _ in range(2)]
        self.assertEqual(runs[0], runs[1], "the checked-in fixture report is repeatable")
        report = json.loads(runs[0])
        self.assertFalse(report["liveModelQualityEvaluated"])
        self.assertIn("not evidence of natural-language model quality", report["interpretation"])
        cases = {item["case"]: item for item in report["cases"]}
        self.assertEqual(set(cases), {"exact-name", "paraphrase", "paraphrase-role", "date", "long-page-tail", "update-freshness"})
        for name in ("exact-name", "paraphrase", "paraphrase-role", "date", "long-page-tail"):
            for method in ("semantic", "headingBaseline"):
                self.assertIsNotNone(cases[name][method]["evidenceRank"], (name, method))
        self.assertTrue(cases["update-freshness"]["staleEntryRejected"])

    def test_concurrent_atomic_cache_publication_never_leaves_partial_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "wiki").mkdir()
            barrier = threading.Barrier(2)
            outcomes = []

            def publish(value):
                barrier.wait()
                try:
                    CHUNKS.publish_cache(vault, {"version": CHUNKS.CACHE_VERSION,
                                                 "pages": {"writer": {"value": value}}})
                    outcomes.append("published")
                except (FileExistsError, ValueError):
                    outcomes.append("conflict")

            workers = [threading.Thread(target=publish, args=(value,)) for value in ("a", "b")]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(len(outcomes), 2)
            cache = json.loads((vault / ".vault-meta/retrieval/chunks.json").read_text(encoding="utf-8"))
            self.assertIn(cache["pages"]["writer"]["value"], {"a", "b"})
            self.assertFalse((vault / ".vault-meta/retrieval/chunks.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
