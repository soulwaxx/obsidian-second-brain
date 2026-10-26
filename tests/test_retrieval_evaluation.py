"""Service-free evaluation contracts; synthetic labels are not live quality evidence."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from module_loading import load_source
E = load_source("test_evaluation", os.environ.get("OSB_EVALUATOR_PATH") or str(Path(__file__).resolve().parents[1] / "scripts/evaluate-retrieval.py"))


class EvaluationTests(unittest.TestCase):
    def test_exact_metrics_and_cutoff(self):
        results = [{"path": page} for page in ["wiki/x.md", "wiki/a.md", "wiki/b.md"]]
        score = E.page_metrics(results, ["wiki/a.md", "wiki/b.md"], [1, 2], 2)
        self.assertEqual(score, {"hit@1": 0, "recall@1": 0, "hit@2": 1, "recall@2": .5, "mrr@2": .5})
        self.assertEqual(E.page_metrics(results, ["wiki/z.md"], [3], 3)["mrr@3"], 0)

    def fixture(self, vault):
        (vault / "wiki").mkdir()
        source = "---\ntype: Note\n---\n# Archive\n\nOld value was 10.\n" + "padding " * 60 + "\nCurrent value is 20.\n"
        (vault / "wiki/archive.md").write_text(source)
        spans = []
        for text, label in [("Old value was 10.", "stale"), ("Current value is 20.", "current")]:
            start = source.index(text)
            spans.append({"start": start, "end": start + len(text), "text": text,
                          "label": label, "supportsAnswer": label == "current"})
        # Review non-factual surrounding context explicitly, not just markers.
        cursor = 0
        for span in list(spans):
            if source[cursor:span["start"]].strip():
                spans.append({"start": cursor, "end": span["start"], "text": source[cursor:span["start"]],
                              "label": "neither", "supportsAnswer": False})
            cursor = span["end"]
        gold = {"version": 1, "revision": "synthetic-v1", "intents": [{"id": "archive", "split": "held-out",
                "category": "current-fact", "intent": "current", "queries": {"en": "Archive value", "it": "Valore archivio"},
                "canonicalPages": ["wiki/archive.md"], "targets": [{"path": "wiki/archive.md",
                "hash": hashlib.sha256(source.encode()).hexdigest(), "evidence": spans}]}]}
        return source, gold

    def test_temporal_full_span_is_not_exposed_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            source, gold = self.fixture(vault)
            E.validate_gold(gold, vault)
            target = gold["intents"][0]["targets"][0]
            result = {"path": target["path"], "charStart": 0, "charEnd": len(source),
                      "excerpt": " ".join(source.split())[:359].rstrip() + "…"}
            score = E.evidence_metrics([result], gold["intents"][0], vault)
            self.assertEqual(score["fullSpanLabels"], ["mixed"])
            self.assertEqual(score["exposedLabels"], ["stale"])
            self.assertEqual(score["ku@1"], 0)
            self.assertTrue(score["staleBeforeCurrent"])

    def test_projected_current_evidence_keeps_answer_support_credit_with_trailing_strike(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "wiki").mkdir()
            source = "The timeout is 30 seconds. ~~90 seconds.~~"
            (vault / "wiki/timeout.md").write_text(source)
            case = {"intent": "current", "queries": {"en": "current timeout", "it": "timeout attuale"},
                    "targets": [{"path": "wiki/timeout.md", "evidence": [{"start": 0,
                    "end": len(source), "text": source, "label": "current", "supportsAnswer": True}]}]}
            result = {"path": "wiki/timeout.md", "charStart": 0, "charEnd": len(source),
                      "excerpt": "The timeout is 30 seconds."}
            score = E.evidence_metrics([result], case, vault)
            self.assertEqual(score["exposedLabels"], ["current"])
            self.assertEqual(score["ku@1"], 1)
            self.assertEqual(score["answerHit"], 1)

    def test_projected_evidence_with_masked_leading_line_uses_actual_search_excerpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "wiki").mkdir()
            source = "~~The timeout was 90 seconds.~~\nThe timeout is 30 seconds.\n"
            page = vault / "wiki/timeout.md"
            page.write_text(source)
            E.CLI.INDEX_MODULE.build(vault)
            query = "current timeout"
            result = next(row for row in E.CLI.search(vault.resolve(), query, 10)["results"]
                          if row["path"] == "wiki/timeout.md")
            self.assertEqual(result["excerpt"], "The timeout is 30 seconds.")
            self.assertEqual((result["lineStart"], result["lineEnd"]), (2, 2))
            case = {"intent": "current", "queries": {"en": query, "it": "timeout attuale"},
                    "targets": [{"path": "wiki/timeout.md", "evidence": [{"start": 0,
                    "end": len(source), "text": source, "label": "current", "supportsAnswer": True}]}]}
            score = E.evidence_metrics([result], case, vault)
            self.assertEqual(score["exposedLabels"], ["current"])
            self.assertEqual(score["ku@1"], 1)
            self.assertEqual(score["answerHit"], 1)

    def test_fully_masked_current_span_does_not_receive_answer_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "wiki").mkdir()
            source = "~~The timeout was 90 seconds.~~"
            (vault / "wiki/timeout.md").write_text(source)
            case = {"intent": "current", "queries": {"en": "current timeout", "it": "timeout attuale"},
                    "targets": [{"path": "wiki/timeout.md", "evidence": [{"start": 0,
                    "end": len(source), "text": source, "label": "current", "supportsAnswer": True}]}]}
            result = {"path": "wiki/timeout.md", "charStart": 0, "charEnd": len(source), "excerpt": ""}
            score = E.evidence_metrics([result], case, vault)
            self.assertEqual(score["exposedLabels"], ["unjudgeable"])
            self.assertIsNone(score["ku@1"])
            self.assertIsNone(score["answerHit"])

    def test_historical_evidence_keeps_unprojected_answer_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "wiki").mkdir()
            source = "~~The timeout was 90 seconds.~~"
            (vault / "wiki/timeout.md").write_text(source)
            case = {"intent": "historical", "queries": {"en": "timeout 2025-02-14", "it": "timeout 2025-02-14"},
                    "targets": [{"path": "wiki/timeout.md", "evidence": [{"start": 0,
                    "end": len(source), "text": source, "label": "stale", "supportsAnswer": True}]}]}
            result = {"path": "wiki/timeout.md", "charStart": 0, "charEnd": len(source), "excerpt": source}
            score = E.evidence_metrics([result], case, vault)
            self.assertEqual(score["exposedLabels"], ["stale"])
            self.assertEqual(score["answerHit"], 1)

    def test_truncated_current_evidence_does_not_receive_answer_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "wiki").mkdir()
            source = "The timeout is 30 seconds. All workers use this value."
            (vault / "wiki/timeout.md").write_text(source)
            case = {"intent": "current", "queries": {"en": "current timeout", "it": "timeout attuale"},
                    "targets": [{"path": "wiki/timeout.md", "evidence": [{"start": 0,
                    "end": len(source), "text": source, "label": "current", "supportsAnswer": True}]}]}
            cut = len("The timeout is 30 seconds.")
            result = {"path": "wiki/timeout.md", "charStart": 0, "charEnd": cut,
                      "excerpt": "The timeout is 30 seconds."}
            score = E.evidence_metrics([result], case, vault)
            self.assertEqual(score["exposedLabels"], ["unjudgeable"])
            self.assertIsNone(score["ku@1"])
            self.assertIsNone(score["answerHit"])

    def test_evaluation_hit_and_recall_depth_is_independent_of_mrr_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _, gold = self.fixture(vault)
            E.CLI.INDEX_MODULE.build(vault)
            target = gold["intents"][0]["targets"][0]["path"]
            results = [{"path": "wiki/other.md", "score": 2}, {"path": target, "score": 1}]
            response = {"results": results, "fallback": None, "freshness": "current-by-hash"}
            with patch.object(E.CLI, "search", return_value=response):
                report = E.evaluate(vault, gold, cutoff=1)
            metrics = report["queries"][0]["methods"]["bm25"]["metrics"]
            self.assertEqual(metrics["mrr@1"], 0)
            self.assertEqual(metrics["hit@3"], 1)
            self.assertEqual(metrics["recall@3"], 1)
            self.assertEqual(metrics["hit@10"], 1)
            self.assertEqual(metrics["recall@10"], 1)

    def test_repeated_text_beyond_excerpt_is_not_visible_gold_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            source, gold = self.fixture(vault)
            source = source.replace("Current value is 20.", "Old value was 10.")
            (vault / "wiki/archive.md").write_text(source)
            case = gold["intents"][0]
            target = case["targets"][0]
            target["hash"] = E.digest(source.encode())
            text = "Old value was 10."
            start = source.rindex(text)
            target["evidence"] = [{"start": start, "end": start + len(text), "text": text,
                                   "label": "current", "supportsAnswer": True}]
            E.validate_gold(gold, vault)
            result = {"path": target["path"], "charStart": 0, "charEnd": len(source),
                      "excerpt": " ".join(source.split())[:359].rstrip() + "…"}
            score = E.evidence_metrics([result], case, vault)
            self.assertEqual(score["exposedLabels"], ["unjudgeable"])
            self.assertIsNone(score["ku@1"])

    def test_current_substring_does_not_judge_unreviewed_surrounding_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _, gold = self.fixture(vault)
            source = "# Quota\n\nCurrent value is 20.\n\nUnmarked old value 10.\n"
            (vault / "wiki/archive.md").write_text(source)
            case = gold["intents"][0]
            target = case["targets"][0]
            target["hash"] = E.digest(source.encode())
            text = "Current value is 20."
            start = source.index(text)
            target["evidence"] = [{"start": start, "end": start + len(text), "text": text,
                                   "label": "current", "supportsAnswer": True}]
            result = {"path": target["path"], "charStart": 0, "charEnd": len(source),
                      "excerpt": " ".join(source.split())}
            score = E.evidence_metrics([result], case, vault)
            self.assertEqual(score["fullSpanLabels"], ["unjudgeable"])
            self.assertEqual(score["exposedLabels"], ["unjudgeable"])
            self.assertIsNone(score["ku@1"])

    def test_unknown_context_cannot_be_overruled_by_mixed_current_substrings(self):
        self.assertEqual(E.combine_labels(["current", "stale", "unjudgeable"]), "unjudgeable")
        self.assertEqual(E.combine_labels(["mixed", "unjudgeable"]), "unjudgeable")

    def test_reviewed_mixed_negated_quoted_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            source, gold = self.fixture(vault)
            for label in ["mixed", "neither", "unjudgeable"]:
                gold["intents"][0]["targets"][0]["evidence"][0]["label"] = label
                span = gold["intents"][0]["targets"][0]["evidence"][0]
                result = {"path": "wiki/archive.md", "charStart": span["start"], "charEnd": span["end"],
                          "excerpt": "Old value was 10."}
                score = E.evidence_metrics([result], gold["intents"][0], vault)
                self.assertEqual(score["exposedLabels"], [label])
                self.assertEqual(score["ku@1"], None if label == "unjudgeable" else 0)

    def test_reviewed_mixed_history_is_useful_not_automatically_a_contradiction(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            source, gold = self.fixture(vault)
            case = gold["intents"][0]
            spans = case["targets"][0]["evidence"]
            spans[0]["label"] = "mixed"
            spans[0]["supportsAnswer"] = True
            result = {"path": "wiki/archive.md", "charStart": spans[0]["start"], "charEnd": spans[0]["end"],
                      "excerpt": spans[0]["text"]}
            score = E.evidence_metrics([result], case, vault)
            self.assertEqual(score["exposedLabels"], ["mixed"])
            self.assertEqual(score["ku@1"], 1, "credit requires a whole-span reviewed answer judgment")
            self.assertFalse(score["staleBeforeCurrent"])
            self.assertTrue(score["mixedBeforeCurrent"])

    def test_model_report_pairing_rejects_drift_and_degradation(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _, gold = self.fixture(vault)
            E.CLI.INDEX_MODULE.build(vault)
            baseline = E.evaluate(vault, gold)
            baseline["provenance"]["modelDigest"] = "synthetic-baseline"
            baseline["provenance"]["caches"]["chunks.json"] = {"embeddingInputScheme": "fixture", "settings": {"maxChars": 100}}
            for row in baseline["queries"]:
                row["semanticStatus"] = "complete"
                row["methods"]["rrf"] = copy.deepcopy(row["methods"]["bm25"])
            candidate = copy.deepcopy(baseline)
            candidate["provenance"]["modelDigest"] = "synthetic-candidate"
            candidate["queries"][0]["methods"]["rrf"]["metrics"]["mrr@10"] = .5
            compared = E.compare_reports(baseline, candidate)
            self.assertEqual(compared["paired"]["en"]["mean"], -.5)
            self.assertEqual(compared["paired"]["paired-intents"]["intents"], 1)
            candidate["queries"][0]["semanticStatus"] = "partial"
            with self.assertRaisesRegex(ValueError, "not comparable"):
                E.compare_reports(baseline, candidate)
            candidate = copy.deepcopy(baseline)
            candidate["provenance"]["corpusManifestHash"] = "drift"
            with self.assertRaisesRegex(ValueError, "corpusManifestHash"):
                E.compare_reports(baseline, candidate)

    def test_drift_unsafe_paths_and_unpaired_queries_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            source, gold = self.fixture(vault)
            E.validate_gold(gold, vault)
            (vault / "wiki/archive.md").write_text(source + "changed")
            with self.assertRaisesRegex(ValueError, "drift"):
                E.validate_gold(gold, vault)
            gold["intents"][0]["targets"][0]["path"] = "../private.md"
            with self.assertRaises(ValueError):
                E.validate_gold(gold, vault)
            gold["intents"][0]["queries"].pop("it")
            with self.assertRaises(ValueError):
                E.validate_gold(gold, vault)

    def test_offline_run_is_read_only_and_paired(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _, gold = self.fixture(vault)
            E.CLI.INDEX_MODULE.build(vault)
            before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
            with patch.object(E.R, "semantic_candidates", side_effect=AssertionError("no inference")):
                report = E.evaluate(vault, gold, live=False, repeats=2)
            self.assertEqual(len(report["queries"]), 2)
            self.assertEqual(report["provenance"]["hardware"], "unknown")
            self.assertEqual(before, {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()})
            self.assertEqual(set(report["summary"]), {"en", "it"})

    def test_offline_bm25_ignores_corrupt_optional_semantic_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _, gold = self.fixture(vault)
            E.CLI.INDEX_MODULE.build(vault)
            (vault / ".vault-meta/retrieval/chunks.json").write_text("broken optional cache")
            with patch.object(E.CHUNKS, "model_digest", side_effect=AssertionError("offline identity request")):
                report = E.evaluate(vault, gold)
            self.assertIn("error", report["provenance"]["caches"]["chunks.json"])
            self.assertTrue(all(set(r["methods"]) == {"bm25"} for r in report["queries"]))

    def test_missing_identity_is_failed_not_semantic_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _, gold = self.fixture(vault)
            E.CLI.INDEX_MODULE.build(vault)
            with patch.object(E.CHUNKS, "model_digest", return_value=None):
                report = E.evaluate(vault, gold, live=True)
            for row in report["queries"]:
                self.assertEqual(row["semanticStatus"], "unavailable")
                self.assertNotIn("rrf", row["methods"])

    def test_fake_loopback_partial_coverage_and_query_only_inference(self):
        from http.server import HTTPServer
        import threading
        from test_hybrid_retrieval import QueryEmbedding
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            _, gold = self.fixture(vault)
            E.CLI.INDEX_MODULE.build(vault)
            QueryEmbedding.model_digest = "digest"
            QueryEmbedding.model_name = "fixture"
            QueryEmbedding.failing = QueryEmbedding.tags_failing = False
            server = HTTPServer(("127.0.0.1", 0), QueryEmbedding)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                (vault / "wiki/other.md").write_text("# Other\n" + "\n".join(
                    f"Paragraph {index}. " + ("distinct semantic content " * 35) for index in range(8)))
                E.CLI.INDEX_MODULE.build(vault)
                E.CHUNKS.build(vault, url, "fixture")
                complete = E.evaluate(vault, gold, live=True, model="fixture", endpoint=url)
                self.assertTrue(all(row["semanticStatus"] == "complete" for row in complete["queries"]))
                cache_path = vault / ".vault-meta/retrieval/chunks.json"
                saved_cache = cache_path.read_bytes()
                cache = json.loads(saved_cache)
                cache["pages"].pop("wiki/archive.md")
                cache_path.write_text(json.dumps(cache))
                missing_record = E.evaluate(vault, gold, live=True, model="fixture", endpoint=url)
                self.assertTrue(all(row["semanticStatus"] == "partial" for row in missing_record["queries"]))
                cache = json.loads(saved_cache)
                self.assertGreater(len(cache["pages"]["wiki/other.md"]["chunks"]), 1)
                cache["pages"]["wiki/other.md"]["chunks"].pop()
                cache_path.write_text(json.dumps(cache))
                missing_tail = E.evaluate(vault, gold, live=True, model="fixture", endpoint=url)
                self.assertTrue(all(row["semanticStatus"] == "partial" for row in missing_tail["queries"]))
                cache = json.loads(saved_cache)
                cache["pages"]["wiki/archive.md"]["chunks"][0]["vector"] = [1.0, 2.0, 3.0]
                cache_path.write_text(json.dumps(cache))
                bad_dimension = E.evaluate(vault, gold, live=True, model="fixture", endpoint=url)
                self.assertTrue(all(row["semanticStatus"] == "partial" for row in bad_dimension["queries"]))
                cache_path.write_bytes(saved_cache)
                (vault / "wiki/missing.md").write_text("# Missing\nNot built semantically.\n")
                E.CLI.INDEX_MODULE.build(vault)
                with patch.object(E.CHUNKS, "embeddings", wraps=E.CHUNKS.embeddings) as embed:
                    report = E.evaluate(vault, gold, live=True, model="fixture", endpoint=url, repeats=2)
                self.assertEqual(embed.call_count, 4)
                self.assertTrue(all(len(c.args[0]) == 1 for c in embed.call_args_list))
                for row in report["queries"]:
                    self.assertEqual(row["semanticStatus"], "partial")
                    self.assertIn("rrf", row["methods"])
                    self.assertEqual(len(row["latencySeconds"]), 2)
                QueryEmbedding.failing = True
                failed = E.evaluate(vault, gold, live=True, model="fixture", endpoint=url)
                self.assertTrue(all(r["semanticStatus"] == "failed" for r in failed["queries"]))
                self.assertTrue(all(set(r["methods"]) == {"bm25"} for r in failed["queries"]))
            finally:
                QueryEmbedding.failing = False
                server.shutdown()
                thread.join()
                server.server_close()

    def test_date_policy_keeps_live_evaluation_lexical(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _, gold = self.fixture(vault)
            E.CLI.INDEX_MODULE.build(vault)
            case = gold["intents"][0]
            case["intent"] = "historical"
            case["queries"] = {"en": "Archive 2025-02-14", "it": "Archivio 2025-02-14"}
            with patch.object(E.CHUNKS, "model_digest", return_value=None), patch.object(E.CHUNKS, "embeddings", side_effect=AssertionError("date inference")):
                report = E.evaluate(vault, gold, live=True)
            self.assertTrue(all(r["semanticStatus"] == "lexical-date-policy" for r in report["queries"]))
            self.assertTrue(all(set(r["methods"]) == {"bm25"} for r in report["queries"]))

    def test_fixed_fusion_matches_production(self):
        lexical = [("wiki/a.md", 2), ("wiki/b.md", 1)]
        semantic = [("wiki/b.md", .9, 0, {}), ("wiki/c.md", .8, 0, {})]
        ranked, _ = E.R.fuse_ranks(lexical, semantic)
        self.assertEqual(ranked, ["wiki/b.md", "wiki/a.md", "wiki/c.md"])
        self.assertEqual(E.R.fuse_ranks(lexical, semantic), E.R.fuse_ranks(lexical, semantic))

    def test_bootstrap_resamples_intents_not_translations(self):
        rows = [{"id": "a", "language": lang, "difference": 1} for lang in ["en", "it"]]
        rows += [{"id": "b", "language": lang, "difference": -1} for lang in ["en", "it"]]
        result = E.paired_interval(rows, samples=100)
        self.assertEqual(result["intents"], 2)
        self.assertEqual(result["mean"], 0)
        self.assertEqual(result["interval95"], [-1, 1])


class PassageExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.experiment = load_source("test_passage_experiment", str(root / "experiments/passage-exposure/experiment.py"))
        cls.report = cls.experiment.experiment()
        cls.rows = {(row["id"], row["language"]): row["policies"] for row in cls.report["rows"]}

    def test_repeatable_offline_report_and_explicit_provenance(self):
        with patch("urllib.request.build_opener", side_effect=AssertionError("no network")):
            self.assertEqual(self.report, self.experiment.experiment())
        self.assertFalse(self.report["liveModelQualityEvaluated"])
        self.assertFalse(self.report["freshHoldoutEvaluated"])
        self.assertIsNone(self.report["model"])
        self.assertIsNone(self.report["cache"])
        self.assertEqual(self.report["fixtureHash"], "fcb4e8781f933361442de101e515fd230f41b65cf29c5adef4770a33fc247b7a")
        self.assertEqual(len(self.report["sourceHashes"]), 11)
        self.assertEqual(len(self.report["rows"]), 16)

    def test_rendering_and_selection_failures_are_distinct(self):
        late = self.rows["late-answer", "en"]
        self.assertTrue(late["prefix"]["targetContainsSpan"])
        self.assertFalse(late["prefix"]["targetSpanExposed"])
        self.assertTrue(late["query-paragraph"]["targetSpanExposed"])
        wrong = self.rows["wrong-chunk", "en"]
        self.assertFalse(wrong["prefix"]["targetContainsSpan"])
        self.assertFalse(wrong["query-paragraph"]["targetSpanExposed"])
        self.assertTrue(wrong["lexical-chunk-prefix"]["targetSpanExposed"])
        self.assertEqual(wrong["lexical-chunk-prefix"]["cutoffs"]["1"]["targetSpanExposed"], 0)
        self.assertEqual(wrong["lexical-chunk-prefix"]["cutoffs"]["3"]["targetSpanExposed"], 1)

    def test_metadata_removal_has_a_measured_counterexample(self):
        for language in ("en", "it"):
            displacement = self.rows["metadata-displacement", language]
            metadata = self.rows["metadata-answer", language]
            self.assertFalse(displacement["prefix"]["targetSpanExposed"])
            self.assertTrue(displacement["body-prefix"]["targetSpanExposed"])
            self.assertTrue(metadata["prefix"]["targetSpanExposed"])
            self.assertFalse(metadata["body-prefix"]["targetSpanExposed"])

    def test_temporal_unknown_and_language_limits_survive_scoring(self):
        self.assertEqual(self.rows["temporal-correction", "en"]["query-paragraph"]["targetLabel"], "current")
        self.assertEqual(self.rows["temporal-correction", "it"]["query-paragraph"]["targetLabel"], "stale")
        self.assertEqual(self.rows["long-context", "en"]["query-paragraph"]["targetLabel"], "unjudgeable")
        for language, expected in (("en", 5), ("it", 3)):
            summary = self.report["summary"][language]
            self.assertEqual(summary["query-paragraph"]["cutoffs"]["3"]["targetSpanExposed"], expected)
            for policy in summary.values():
                self.assertEqual(policy["cutoffs"]["1"]["pageHits"], 4)
                self.assertEqual(policy["cutoffs"]["3"]["pageHits"], 8)
                self.assertEqual(policy["cutoffs"]["3"]["unknownResults"], 17)
                self.assertEqual(policy["cutoffs"]["3"]["judgedResults"], 7)
            self.assertEqual(summary["prefix"]["cutoffs"]["3"]["answerUnknown"], 6)

    def test_original_crlf_coordinates_and_character_budget(self):
        fixture = json.loads((self.experiment.HERE / "fixtures.json").read_bytes())
        source = "".join(next(case for case in fixture["cases"] if case["id"] == "unicode-crlf")["chunks"])
        window = self.rows["unicode-crlf", "en"]["query-paragraph"]["targetWindow"]
        self.assertIn("\r\n", source)
        self.assertEqual(source[window["charStart"]:window["charEnd"]], "Caffè retention is 12 days.\r\n")
        for row in self.report["rows"]:
            for policy in row["policies"].values():
                self.assertLessEqual(len(policy["targetWindow"]["excerpt"]), 360)
                self.assertLessEqual(policy["returnedCharacters"], 3 * 360)


if __name__ == "__main__":
    unittest.main()
