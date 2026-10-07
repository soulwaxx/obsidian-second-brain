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

    def test_no_implicit_ledger_creation_and_unknown_fields_survive_prepared_update(self):
        self.assertFalse((self.vault / "wiki/meta/evidence").exists())
        record = LEDGER.make_claim("claim-one", "original")
        record["future"] = {"preserve": True}
        self.assertEqual(LEDGER.merge_record(record, {"statement": "updated"})["future"], {"preserve": True})
        self.assertFalse((self.vault / "wiki/meta/evidence").exists())


if __name__ == "__main__":
    unittest.main()
