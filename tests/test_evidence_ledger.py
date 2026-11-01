from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source
LEDGER = load_source(
    "evidence_ledger_test", str(ROOT / "scripts/evidence_ledger.py")
)
CLI = ROOT / "scripts/obsidian-second-brain.py"
BATCH = load_source(
    "evidence_batch_test", str(ROOT / "scripts/wiki_batch.py")
)


class EvidenceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        self.config = self.root / "properties.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")

    def test_source_identity_and_content_hash_are_stable(self):
        source_a = LEDGER.make_source("file:///notes/a.txt", b"source bytes")
        source_b = LEDGER.make_source("file:///notes/a.txt", b"source bytes")
        source_changed = LEDGER.make_source("file:///notes/a.txt", b"changed")
        self.assertEqual(source_a["id"], source_b["id"])
        self.assertNotEqual(source_a["id"], source_changed["id"])
        self.assertEqual(source_a["contentHash"], "sha256:" + hashlib.sha256(b"source bytes").hexdigest())
        LEDGER.validate_record(source_a, expected_id=source_a["id"])

    def test_claim_without_evidence_stays_provisional_and_no_confidence_is_invented(self):
        claim = LEDGER.make_claim("claim-example", "A statement awaiting evidence")
        self.assertEqual(claim["evidenceState"], "provisional")
        self.assertEqual(claim["support"], [])
        self.assertEqual(claim["contradictions"], [])
        self.assertNotIn("confidence", claim)
        LEDGER.validate_record(claim, expected_id="claim-example")

    def test_validation_checks_identity_and_references_but_preserves_unknown_fields(self):
        source = LEDGER.make_source("https://example.test/doc", b"x")
        source["futureExtension"] = {"opaque": True}
        LEDGER.validate_record(source, expected_id=source["id"])
        claim = LEDGER.make_claim("claim-one", "Text", support=[source["id"]])
        LEDGER.validate_record(claim, expected_id="claim-one")
        claim["support"] = ["../outside"]
        with self.assertRaises(LEDGER.LedgerError):
            LEDGER.validate_record(claim, expected_id="claim-one")
        with self.assertRaises(LEDGER.LedgerError):
            LEDGER.validate_record(source, expected_id="different-id")

    def test_merge_update_preserves_unknown_top_level_and_nested_fields(self):
        current = LEDGER.make_claim("claim-one", "old")
        current["futureField"] = {"keep": "yes"}
        current["review"] = {"status": "reviewed", "futureReview": 7}
        merged = LEDGER.merge_record(current, {"statement": "new", "review": {"status": "unreviewed"}})
        self.assertEqual(merged["statement"], "new")
        self.assertEqual(merged["futureField"], {"keep": "yes"})
        self.assertEqual(merged["review"], {"status": "unreviewed", "futureReview": 7})

    def test_batch_authority_enforces_schema_and_cli_reviewed_write(self):
        valid = LEDGER.make_source("https://example.test/doc", b"bytes")
        relative = f"wiki/meta/evidence/{valid['id']}.json"
        bundle = {"version": 1, "operations": [{"path": relative, "expectedHash": None,
                    "content": json.dumps(valid, ensure_ascii=False, sort_keys=True)}]}
        bundle_path = self.root / "bundle.json"
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        common = ["--vault", str(self.vault), "--config", str(self.config), "--bundle", str(bundle_path), "--authority", "wiki-ledger", "--json"]
        inspected = subprocess.run([sys.executable, str(CLI), "batch-inspect", *common], text=True, capture_output=True)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        approval = json.loads(inspected.stdout)["planHash"]
        applied = subprocess.run([sys.executable, str(CLI), "batch-apply", *common, "--plan-hash", approval], text=True, capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads((self.vault / relative).read_text())["id"], valid["id"])
        preexisting_bytes = (self.vault / relative).read_bytes()
        malformed = dict(valid)
        malformed["contentHash"] = "not-a-hash"
        bad_bundle = {"version": 1, "operations": [{"path": relative, "expectedHash": None,
                       "content": json.dumps(malformed)}]}
        with self.assertRaises(BATCH.BatchError):
            BATCH.inspect(self.vault, bad_bundle, self.config, "wiki-ledger")
        bundle_path.write_text(json.dumps(bad_bundle), encoding="utf-8")
        refused = subprocess.run([sys.executable, str(CLI), "batch-inspect", *common], text=True, capture_output=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual((self.vault / relative).read_bytes(), preexisting_bytes)

    def test_asset_observation_and_pair_baseline_are_distinct_reviewable_records(self):
        asset = LEDGER.make_asset("asset-report", "root-reports", "reports/annual.pdf")
        source_a = LEDGER.make_source_digest("local:root-reports/reports/annual.pdf", "sha256:" + "a" * 64)
        source_b = LEDGER.make_source_digest("local:root-reports/reports/annual.pdf", "sha256:" + "b" * 64)
        observed_a = LEDGER.make_observation(asset, source_a, "2025-01-02T03:04:05+00:00")
        observed_b = LEDGER.make_observation(asset, source_b, "2025-01-03T03:04:05Z")
        self.assertNotEqual(observed_a["id"], observed_b["id"])
        self.assertEqual(observed_a["review"]["status"], "unreviewed")
        entry = {"observationId": observed_a["id"], "sourceId": source_a["id"],
                 "contentHash": source_a["contentHash"], "pageHash": "sha256:" + "c" * 64,
                 "pageBatchId": "batch-123", "reconciledAt": "2025-01-04T00:00:00+00:00",
                 "outcome": "no-change"}
        baseline = LEDGER.make_baseline("wiki/research/report.md", asset["id"], [entry])
        self.assertEqual(LEDGER.record_path(baseline), f"wiki/meta/evidence/{baseline['id']}.json")
        second = dict(entry, observationId=observed_b["id"], sourceId=source_b["id"],
                      contentHash=source_b["contentHash"], reconciledAt="2025-01-05T00:00:00+00:00")
        merged = LEDGER.merge_record(baseline, {"history": [entry, second]})
        self.assertEqual(len(merged["history"]), 2)
        with self.assertRaises(LEDGER.LedgerError):
            LEDGER.merge_record(baseline, {"history": [second]})
        with self.assertRaises(LEDGER.LedgerError):
            LEDGER.merge_record(observed_a, {"locator": "moved.pdf"})
        self.assertNotEqual(LEDGER.make_baseline("wiki/research/other.md", asset["id"], [entry])["id"], baseline["id"])
        other_asset = LEDGER.make_asset("asset-other", "root-reports", "reports/second.pdf")
        other_source = LEDGER.make_source_digest("local:root-reports/reports/second.pdf", "sha256:" + "d" * 64)
        other_observation = LEDGER.make_observation(other_asset, other_source, "2025-01-05T00:00:00Z")
        other_entry = dict(entry, observationId=other_observation["id"], sourceId=other_source["id"],
                           contentHash=other_source["contentHash"])
        other_pair = LEDGER.make_baseline("wiki/research/report.md", other_asset["id"], [other_entry])
        self.assertNotEqual(other_pair["id"], baseline["id"])
        self.assertEqual(other_pair["page"], baseline["page"])
        self.assertEqual(other_pair["assetId"], other_asset["id"])
        with self.assertRaises(LEDGER.LedgerError):
            mismatched = LEDGER.make_source_digest("local:root-other/reports/annual.pdf", source_a["contentHash"])
            LEDGER.make_observation(asset, mismatched, "2025-01-02T03:04:05+00:00")

    def test_unknown_git_context_extensions_do_not_change_observation_identity(self):
        asset = LEDGER.make_asset("asset-paper", "research", "paper.pdf")
        source = LEDGER.make_source("local:research/paper.pdf", b"bytes")
        observation = LEDGER.make_observation(asset, source, "2025-01-01T00:00:00Z",
                                               git_context={"head": "a" * 40, "vendor": {"x": 1}})
        changed = dict(observation)
        changed["gitContext"] = {"head": "a" * 40, "vendor": {"x": 2}}
        LEDGER.validate_record(changed)
        merged = LEDGER.merge_record(observation, changed)
        self.assertEqual(merged["id"], observation["id"])
        self.assertEqual(merged["gitContext"]["vendor"], {"x": 2})

    def test_new_known_fields_reject_bad_types_and_retain_unknown_extensions(self):
        asset = LEDGER.make_asset("asset-report", "root-reports", "reports/annual.pdf")
        for changes in ({"rootId": []}, {"locator": "../escape"}, {"locator": "."},
                        {"locator": "reports/annual\n.pdf"}):
            malformed = dict(asset, **changes)
            with self.assertRaises(LEDGER.LedgerError):
                LEDGER.validate_record(malformed)
        changed = LEDGER.merge_record(asset, {"locator": "archive/annual.pdf", "future": {"x": 1}})
        self.assertEqual(changed["id"], asset["id"])
        self.assertEqual(changed["future"], {"x": 1})
        self.assertEqual(changed["review"]["status"], "unreviewed")
        source = LEDGER.make_source_digest("local:root-reports/reports/annual.pdf", "sha256:" + "a" * 64)
        observation = LEDGER.make_observation(asset, source, "2025-01-02T03:04:05+00:00")
        valid_sha256_git = LEDGER.make_observation(
            asset, source, "2025-01-02T03:04:05+00:00",
            git_context={"head": "a" * 64, "repositoryLocator": "repo/path", "selectedBytes": "equal"})
        LEDGER.validate_record(valid_sha256_git)
        for changes in ({"observedAt": 3}, {"locator": "."}, {"locator": "reports/a\x01.pdf"},
                        {"gitContext": {"head": "a" * 39}}, {"gitContext": {"head": "a" * 41}},
                        {"gitContext": {"head": "a" * 63}},
                        {"gitContext": {"repositoryLocator": "repo/with\x01control"}}):
            malformed = dict(observation, **changes)
            with self.assertRaises(LEDGER.LedgerError):
                LEDGER.validate_record(malformed)
        with self.assertRaises(LEDGER.LedgerError):
            LEDGER.make_baseline("wiki/research/bad\x01name.md", asset["id"], [{
                "observationId": observation["id"], "sourceId": source["id"],
                "contentHash": source["contentHash"], "pageHash": "sha256:" + "b" * 64,
                "pageBatchId": "batch-report", "reconciledAt": "2025-01-03T00:00:00Z", "outcome": "updated"}])
        baseline = LEDGER.make_baseline("wiki/research/report.md", asset["id"], [{
            "observationId": observation["id"], "sourceId": source["id"],
            "contentHash": source["contentHash"], "pageHash": "sha256:" + "b" * 64,
            "pageBatchId": "batch-report", "reconciledAt": "2025-01-03T00:00:00Z", "outcome": "updated"}])
        observation["futureNested"] = {"retain": [1, 2]}
        baseline["futureBaseline"] = {"retain": True}
        self.assertEqual(LEDGER.merge_record(observation, {"review": {"status": "reviewed"}})["futureNested"], {"retain": [1, 2]})
        self.assertEqual(LEDGER.merge_record(baseline, {"review": {"status": "reviewed"}})["futureBaseline"], {"retain": True})
        self.assertFalse((self.vault / "wiki/meta/evidence").exists())

    def test_no_implicit_ledger_creation_and_unknown_fields_survive_prepared_update(self):
        self.assertFalse((self.vault / "wiki/meta/evidence").exists())
        record = LEDGER.make_claim("claim-one", "original")
        record["future"] = {"preserve": True}
        self.assertEqual(LEDGER.merge_record(record, {"statement": "updated"})["future"], {"preserve": True})
        self.assertFalse((self.vault / "wiki/meta/evidence").exists())


if __name__ == "__main__":
    unittest.main()
