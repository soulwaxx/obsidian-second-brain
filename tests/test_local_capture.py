from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source
MODULE = load_source("local_capture_test", str(ROOT / "scripts/local_capture.py"))
CLI = ROOT / "scripts/obsidian-second-brain.py"


class LocalCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        self.page = self.vault / "wiki/topic.md"
        self.page.write_text("---\ntype: note\n---\n\n# Topic\n", encoding="utf-8")
        self.source = self.root / "inbox.txt"
        self.source.write_text("Local notes; preserve exactly.\n", encoding="utf-8")
        self.config = self.root / "properties.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")

    def test_review_is_private_bounded_and_apply_creates_immutable_linked_records(self):
        reviewed = MODULE.inspect(self.vault, [self.source], self.config, ["wiki/topic.md"])
        self.assertFalse((self.vault / ".vault-meta").exists())
        self.assertEqual(reviewed["sourceCount"], 1)
        self.assertEqual(reviewed["sourceBytes"], len(self.source.read_bytes()))
        self.assertEqual(reviewed["linkedPageCount"], 1)
        rendered = json.dumps(reviewed)
        self.assertNotIn(str(self.source), rendered)
        self.assertNotIn("Local notes", rendered)
        applied = MODULE.apply(self.vault, [self.source], self.config, reviewed["planHash"], ["wiki/topic.md"])
        self.assertFalse(applied["noop"])
        payload = next((self.vault / ".raw/agent-captures").glob("*.txt"))
        record = next((self.vault / ".raw/agent-captures").glob("*.md"))
        self.assertEqual(payload.read_bytes(), self.source.read_bytes())
        self.assertEqual(payload.stem, MODULE.sha(self.source.read_bytes()))
        self.assertIn("[[wiki/topic]]", record.read_text(encoding="utf-8"))
        self.assertIn(MODULE.sha(self.page.read_bytes()), record.read_text(encoding="utf-8"))
        self.assertTrue(MODULE.inspect(self.vault, [self.source], self.config, ["wiki/topic.md"])["noop"])
        repeated = MODULE.apply(self.vault, [self.source], self.config, reviewed["planHash"], ["wiki/topic.md"])
        self.assertTrue(repeated["noop"])
        self.assertEqual(payload.read_bytes(), self.source.read_bytes())

    def test_source_and_linked_page_drift_invalidate_review(self):
        source_review = MODULE.inspect(self.vault, [self.source], self.config, ["wiki/topic.md"])
        self.source.write_text("changed source\n", encoding="utf-8")
        with self.assertRaises(MODULE.CaptureError):
            MODULE.apply(self.vault, [self.source], self.config, source_review["planHash"], ["wiki/topic.md"])
        self.assertFalse((self.vault / ".raw").exists())

        self.source.write_text("Local notes; preserve exactly.\n", encoding="utf-8")
        page_review = MODULE.inspect(self.vault, [self.source], self.config, ["wiki/topic.md"])
        self.page.write_text("---\ntype: note\n---\n\n# Changed\n", encoding="utf-8")
        with self.assertRaises(MODULE.CaptureError):
            MODULE.apply(self.vault, [self.source], self.config, page_review["planHash"], ["wiki/topic.md"])
        self.assertFalse((self.vault / ".raw").exists())

    def test_target_drift_and_partial_record_pair_fail_closed(self):
        reviewed = MODULE.inspect(self.vault, [self.source], self.config)
        payload = self.vault / reviewed["targets"][0]
        payload.parent.mkdir(parents=True)
        payload.write_text("concurrent user content\n", encoding="utf-8")
        with self.assertRaises(MODULE.CaptureError):
            MODULE.apply(self.vault, [self.source], self.config, reviewed["planHash"])
        self.assertEqual(payload.read_text(), "concurrent user content\n")

        payload.unlink()
        payload.write_bytes(self.source.read_bytes())
        with self.assertRaises(MODULE.CaptureError):
            MODULE.inspect(self.vault, [self.source], self.config)

    def test_completed_source_can_be_reused_with_new_source_and_replayed(self):
        first = MODULE.inspect(self.vault, [self.source], self.config)
        MODULE.apply(self.vault, [self.source], self.config, first["planHash"])
        capture_root = self.vault / ".raw/agent-captures"
        old_files = sorted(capture_root.iterdir())
        old_snapshot = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                        for path in old_files}
        second = self.root / "second.txt"
        second.write_bytes(b"A different new source.\n")

        review = MODULE.inspect(self.vault, [self.source, second], self.config)
        self.assertFalse(review["noop"])
        applied = MODULE.apply(self.vault, [self.source, second], self.config, review["planHash"])
        self.assertFalse(applied["noop"])
        self.assertEqual((capture_root / (MODULE.sha(second.read_bytes()) + ".txt")).read_bytes(), second.read_bytes())
        for path, (contents, inode, mtime_ns) in old_snapshot.items():
            self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(path.stat().st_mtime_ns, mtime_ns)

        replay = MODULE.inspect(self.vault, [self.source, second], self.config)
        self.assertTrue(replay["noop"])
        self.assertTrue(MODULE.apply(self.vault, [self.source, second], self.config,
                                     review["planHash"])["noop"])
        self.assertEqual(len(list(capture_root.glob("*.txt"))), 2)

    def test_mixed_capture_review_binds_selection_and_linked_page_content(self):
        initial = MODULE.inspect(self.vault, [self.source], self.config, ["wiki/topic.md"])
        MODULE.apply(self.vault, [self.source], self.config, initial["planHash"], ["wiki/topic.md"])
        second = self.root / "second.txt"
        second.write_bytes(b"New source.\n")
        selected = [self.source, second]

        review = MODULE.inspect(self.vault, selected, self.config, ["wiki/topic.md"])
        second.write_bytes(b"Changed after review.\n")
        with self.assertRaises(MODULE.CaptureError):
            MODULE.apply(self.vault, selected, self.config, review["planHash"], ["wiki/topic.md"])
        self.assertFalse((self.vault / ".raw/agent-captures" / (MODULE.sha(second.read_bytes()) + ".txt")).exists())

        second.write_bytes(b"New source.\n")
        review = MODULE.inspect(self.vault, selected, self.config, ["wiki/topic.md"])
        third = self.root / "third.txt"
        third.write_bytes(b"A newly selected third source.\n")
        with self.assertRaisesRegex(MODULE.CaptureError, "approval hash"):
            MODULE.apply(self.vault, [self.source, second, third],
                         self.config, review["planHash"], ["wiki/topic.md"])
        self.page.write_text("---\ntype: note\n---\n\n# Changed after review\n", encoding="utf-8")
        with self.assertRaises(MODULE.CaptureError):
            MODULE.apply(self.vault, selected, self.config, review["planHash"], ["wiki/topic.md"])
        self.assertFalse((self.vault / ".raw/agent-captures" / (MODULE.sha(second.read_bytes()) + ".txt")).exists())

    def test_mixed_capture_refuses_drifted_reused_target_before_new_source_write(self):
        first = MODULE.inspect(self.vault, [self.source], self.config)
        MODULE.apply(self.vault, [self.source], self.config, first["planHash"])
        second = self.root / "second.txt"
        second.write_bytes(b"New source.\n")
        review = MODULE.inspect(self.vault, [self.source, second], self.config)
        reused_payload = self.vault / ".raw/agent-captures" / (MODULE.sha(self.source.read_bytes()) + ".txt")
        original = reused_payload.read_bytes()
        reused_payload.write_bytes(b"modified existing immutable source")
        with self.assertRaises(MODULE.CaptureError):
            MODULE.apply(self.vault, [self.source, second], self.config, review["planHash"])
        self.assertFalse((self.vault / ".raw/agent-captures" / (MODULE.sha(second.read_bytes()) + ".txt")).exists())
        self.assertEqual(reused_payload.read_bytes(), b"modified existing immutable source")
        reused_payload.write_bytes(original)

    def test_rejects_symlinks_nontext_oversize_and_unsafe_or_missing_page_links(self):
        linked = self.root / "linked.txt"
        linked.symlink_to(self.source)
        with self.assertRaises(MODULE.CaptureError):
            MODULE.inspect(self.vault, [linked], self.config)
        binary = self.root / "binary.txt"
        binary.write_bytes(b"a\x00b")
        with self.assertRaises(MODULE.CaptureError):
            MODULE.inspect(self.vault, [binary], self.config)
        oversized = self.root / "large.txt"
        oversized.write_bytes(b"x" * (MODULE.MAX_SOURCE_BYTES + 1))
        with self.assertRaises(MODULE.CaptureError):
            MODULE.inspect(self.vault, [oversized], self.config)
        for invalid in ("wiki/index.md", "wiki/missing.md", "../outside.md", "wiki/Topic.md"):
            with self.subTest(path=invalid), self.assertRaises(MODULE.CaptureError):
                MODULE.inspect(self.vault, [self.source], self.config, [invalid])
        (self.vault / "wiki/linked.md").symlink_to(self.page)
        with self.assertRaises(MODULE.CaptureError):
            MODULE.inspect(self.vault, [self.source], self.config, ["wiki/linked.md"])
        original_listdir = MODULE.os.listdir
        def colliding_listdir(path):
            names = original_listdir(path)
            if Path(path) == self.vault / "wiki":
                names.append("Topic.md")
            return names
        with mock.patch.object(MODULE.os, "listdir", side_effect=colliding_listdir):
            with self.assertRaisesRegex(MODULE.CaptureError, "case-insensitive"):
                MODULE.inspect(self.vault, [self.source], self.config, ["wiki/topic.md"])

    def test_cli_links_spaces_unicode_and_syntax_names_with_exact_review_binding(self):
        names = ["wiki/Topic Notes.md", "wiki/café.md", "wiki/Name [draft]#part|alias^.md"]
        for name in names:
            page = self.vault / name
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("---\\ntype: note\\n---\\n\\n# Existing page\\n", encoding="utf-8")
        reviewed = MODULE.inspect(self.vault, [self.source], self.config, names)
        changed_page = self.vault / names[0]
        changed_page.write_text("---\\ntype: note\\n---\\n\\n# Drifted\\n", encoding="utf-8")
        with self.assertRaises(MODULE.CaptureError):
            MODULE.apply(self.vault, [self.source], self.config, reviewed["planHash"], names)
        changed_page.write_text("---\\ntype: note\\n---\\n\\n# Existing page\\n", encoding="utf-8")
        reviewed = MODULE.inspect(self.vault, [self.source], self.config, names)
        applied = MODULE.apply(self.vault, [self.source], self.config, reviewed["planHash"], names)
        self.assertFalse(applied["noop"])
        record = next((self.vault / ".raw/agent-captures").glob("*.md"))
        rendered = record.read_text(encoding="utf-8")
        self.assertIn("Topic Notes", rendered)
        self.assertIn("café", rendered)
        self.assertIn(r"wiki/Name \[draft\]#part|alias^.md", rendered)
        self.assertIn("Name%20%5Bdraft%5D%23part%7Calias%5E.md", rendered)
        self.assertTrue(MODULE.inspect(self.vault, [self.source], self.config, names)["noop"])

    def test_identical_sources_share_one_payload_and_keep_distinct_records(self):
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        selected = [self.source, second]
        review = MODULE.inspect(self.vault, selected, self.config, ["wiki/topic.md"])
        self.assertEqual(review["sourceCount"], 2)
        self.assertEqual(len(review["targets"]), 3)
        MODULE.apply(self.vault, selected, self.config, review["planHash"], ["wiki/topic.md"])
        capture_root = self.vault / ".raw/agent-captures"
        payload = capture_root / f"{MODULE.sha(self.source.read_bytes())}.txt"
        self.assertEqual(payload.read_bytes(), self.source.read_bytes())
        self.assertEqual(len(list(capture_root.glob("*.txt"))), 1)
        self.assertEqual(len(list(capture_root.glob("*.md"))), 2)
        snapshot = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in capture_root.iterdir()}
        replay = MODULE.inspect(self.vault, selected, self.config, ["wiki/topic.md"])
        self.assertTrue(replay["noop"])
        MODULE.apply(self.vault, selected, self.config, replay["planHash"], ["wiki/topic.md"])
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in capture_root.iterdir()}, snapshot)

    def test_sequential_reference_and_separate_payload_evidence_survive_damaged_older_record(self):
        second = self.root / "second.txt"
        third = self.root / "third.txt"
        second.write_bytes(self.source.read_bytes())
        third.write_bytes(self.source.read_bytes())
        capture_root = self.vault / ".raw/agent-captures"
        first_review = MODULE.inspect(self.vault, [self.source], self.config)
        MODULE.apply(self.vault, [self.source], self.config, first_review["planHash"])
        first_record_path = self.vault / MODULE.prepare(self.vault, [self.source])["pairs"][0]["operations"][1]["path"]
        payload = capture_root / f"{MODULE.sha(self.source.read_bytes())}.txt"
        payload_before = (payload.read_bytes(), payload.stat().st_ino, payload.stat().st_mtime_ns)

        second_review = MODULE.inspect(self.vault, [second], self.config)
        MODULE.apply(self.vault, [second], self.config, second_review["planHash"])
        second_record_path = self.vault / MODULE.prepare(self.vault, [second])["pairs"][0]["operations"][1]["path"]
        second_before = (second_record_path.read_bytes(), second_record_path.stat().st_ino,
                         second_record_path.stat().st_mtime_ns)
        first_record_path.write_bytes(b"damaged older reference\\n")
        damaged_before = (first_record_path.read_bytes(), first_record_path.stat().st_ino,
                          first_record_path.stat().st_mtime_ns)

        third_review = MODULE.inspect(self.vault, [third], self.config)
        self.assertFalse(third_review["noop"])
        result = MODULE.apply(self.vault, [third], self.config, third_review["planHash"])
        self.assertFalse(result["noop"])
        self.assertEqual((payload.read_bytes(), payload.stat().st_ino, payload.stat().st_mtime_ns), payload_before)
        self.assertEqual((second_record_path.read_bytes(), second_record_path.stat().st_ino,
                          second_record_path.stat().st_mtime_ns), second_before)
        self.assertEqual((first_record_path.read_bytes(), first_record_path.stat().st_ino,
                          first_record_path.stat().st_mtime_ns), damaged_before)
        self.assertTrue(MODULE.inspect(self.vault, [third], self.config)["noop"])

    def test_damaged_only_shared_reference_does_not_authorize_reuse(self):
        first = MODULE.inspect(self.vault, [self.source], self.config)
        MODULE.apply(self.vault, [self.source], self.config, first["planHash"])
        record = next((self.vault / ".raw/agent-captures").glob("*.md"))
        record.write_bytes(b"damaged reference\\n")
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(MODULE.CaptureError, "unverified"):
            MODULE.inspect(self.vault, [second], self.config)

    def test_identical_sources_can_be_captured_sequentially_and_mixed_with_new(self):
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        first_review = MODULE.inspect(self.vault, [self.source], self.config)
        MODULE.apply(self.vault, [self.source], self.config, first_review["planHash"])
        payload = self.vault / ".raw/agent-captures" / f"{MODULE.sha(self.source.read_bytes())}.txt"
        payload_before = (payload.read_bytes(), payload.stat().st_mtime_ns)
        second_review = MODULE.inspect(self.vault, [second], self.config)
        MODULE.apply(self.vault, [second], self.config, second_review["planHash"])
        self.assertEqual((payload.read_bytes(), payload.stat().st_mtime_ns), payload_before)
        third = self.root / "third.txt"
        third.write_bytes(b"New source alongside verified reuse.\\n")
        mixed = MODULE.inspect(self.vault, [self.source, second, third], self.config)
        self.assertFalse(mixed["noop"])
        MODULE.apply(self.vault, [self.source, second, third], self.config, mixed["planHash"])
        self.assertTrue(MODULE.inspect(self.vault, [self.source, second, third], self.config)["noop"])

    def test_unverified_lone_payload_and_incomplete_or_damaged_pair_are_refused(self):
        review = MODULE.inspect(self.vault, [self.source], self.config)
        capture_root = self.vault / ".raw/agent-captures"
        capture_root.mkdir(parents=True)
        payload = capture_root / f"{MODULE.sha(self.source.read_bytes())}.txt"
        payload.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(MODULE.CaptureError, "unverified"):
            MODULE.inspect(self.vault, [self.source], self.config)
        payload.unlink()
        MODULE.apply(self.vault, [self.source], self.config, review["planHash"])
        record = next(capture_root.glob("*.md"))
        record.unlink()
        with self.assertRaisesRegex(MODULE.CaptureError, "completed|incomplete|unverified"):
            MODULE.inspect(self.vault, [self.source], self.config)
        record.write_text("damaged record\\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.CaptureError, "drifted"):
            MODULE.inspect(self.vault, [self.source], self.config)

    def test_deleted_completed_identity_record_cannot_be_recreated_from_original_approval(self):
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        first_review = MODULE.inspect(self.vault, [self.source], self.config)
        MODULE.apply(self.vault, [self.source], self.config, first_review["planHash"])
        second_review = MODULE.inspect(self.vault, [second], self.config)
        MODULE.apply(self.vault, [second], self.config, second_review["planHash"])
        capture_root = self.vault / ".raw/agent-captures"
        record_path = MODULE.prepare(self.vault, [self.source])["pairs"][0]["operations"][1]["path"]
        first_record = self.vault / record_path
        first_record.unlink()
        before_journals = sorted((self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        before_targets = sorted(path.name for path in capture_root.iterdir())
        for operation in ("inspect", "apply"):
            with self.subTest(operation=operation), self.assertRaisesRegex(MODULE.CaptureError, "completed|missing|history"):
                if operation == "inspect":
                    MODULE.inspect(self.vault, [self.source], self.config)
                else:
                    MODULE.apply(self.vault, [self.source], self.config, first_review["planHash"])
        self.assertEqual(sorted((self.vault / ".vault-meta/lifecycle/batches").glob("*.json")), before_journals)
        self.assertEqual(sorted(path.name for path in capture_root.iterdir()), before_targets)
        self.assertFalse(first_record.exists())

    def test_known_interrupted_identity_recovers_through_its_original_journal(self):
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        first = MODULE.inspect(self.vault, [self.source], self.config)
        MODULE.apply(self.vault, [self.source], self.config, first["planHash"])
        second_review = MODULE.inspect(self.vault, [second], self.config)
        publish = MODULE.BATCH._publish
        MODULE.BATCH._publish = lambda *args, **kwargs: (_ for _ in ()).throw(MODULE.BATCH.BatchError("interrupted"))
        try:
            with self.assertRaisesRegex(MODULE.BATCH.BatchError, "interrupted"):
                MODULE.apply(self.vault, [second], self.config, second_review["planHash"])
        finally:
            MODULE.BATCH._publish = publish
        journal = next(item for item in MODULE.BATCH.source_capture_history(self.vault)
                       if item["phase"] != "complete")
        before = sorted((self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        with self.assertRaisesRegex(MODULE.CaptureError, journal["batchId"]):
            MODULE.apply(self.vault, [second], self.config, second_review["planHash"])
        self.assertEqual(sorted((self.vault / ".vault-meta/lifecycle/batches").glob("*.json")), before)
        MODULE.BATCH.recover(self.vault, journal["batchId"], self.config)
        self.assertTrue(MODULE.inspect(self.vault, [second], self.config)["noop"])

    def test_rolled_back_known_identity_can_be_reapproved_without_repairing_old_journal(self):
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        first = MODULE.inspect(self.vault, [self.source], self.config)
        MODULE.apply(self.vault, [self.source], self.config, first["planHash"])
        second_review = MODULE.inspect(self.vault, [second], self.config)
        publish = MODULE.BATCH._publish
        MODULE.BATCH._publish = lambda *args, **kwargs: (_ for _ in ()).throw(MODULE.BATCH.BatchError("interrupted"))
        try:
            with self.assertRaises(MODULE.BATCH.BatchError):
                MODULE.apply(self.vault, [second], self.config, second_review["planHash"])
        finally:
            MODULE.BATCH._publish = publish
        interrupted = next(item for item in MODULE.BATCH.source_capture_history(self.vault)
                           if item["phase"] != "complete")
        MODULE.BATCH.rollback(self.vault, interrupted["batchId"], self.config)
        approved = MODULE.inspect(self.vault, [second], self.config)
        result = MODULE.apply(self.vault, [second], self.config, approved["planHash"])
        self.assertFalse(result["noop"])
        self.assertTrue(MODULE.inspect(self.vault, [second], self.config)["noop"])

    def test_shared_reference_requires_exact_canonical_record_and_historical_pages(self):
        page_name = "wiki/topic.md"
        first = MODULE.inspect(self.vault, [self.source], self.config, [page_name])
        MODULE.apply(self.vault, [self.source], self.config, first["planHash"], [page_name])
        capture_root = self.vault / ".raw/agent-captures"
        record = next(capture_root.glob("*.md"))
        original = record.read_bytes()
        # Historical references remain valid even after their linked pages change or disappear.
        self.page.unlink()
        other = self.root / "other.txt"
        other.write_bytes(self.source.read_bytes())
        review = MODULE.inspect(self.vault, [other], self.config)
        self.assertFalse(review["noop"])
        result = MODULE.apply(self.vault, [other], self.config, review["planHash"])
        self.assertFalse(result["noop"])
        self.assertTrue((capture_root / MODULE.prepare(self.vault, [other])["pairs"][0]["operations"][1]["path"].split("/")[-1]).exists())

    def test_reference_tampering_and_malformed_candidates_are_skipped_safely(self):
        first = MODULE.inspect(self.vault, [self.source], self.config, ["wiki/topic.md"])
        MODULE.apply(self.vault, [self.source], self.config, first["planHash"], ["wiki/topic.md"])
        capture_root = self.vault / ".raw/agent-captures"
        record = next(capture_root.glob("*.md"))
        original = record.read_bytes()
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        for damaged in (
            original.replace(b"Related wiki pages:", b"Damaged related links:"),
            original.replace(b"- [[wiki/topic]]", b"- [[wiki/altered]]"),
            original.replace(b'[{"path":', b'[ {"path":'),
            original.replace(b'"path":"wiki/topic.md"', b'"path":17'),
        ):
            record.write_bytes(damaged)
            self.assertFalse(MODULE._parse_completed_reference(damaged, record.name,
                                                                MODULE.sha(self.source.read_bytes())))
        record.write_bytes(original)
        self.assertTrue(MODULE._parse_completed_reference(original, record.name,
                                                           MODULE.sha(self.source.read_bytes())))
        (capture_root / "000-malformed.md").write_text("not a canonical record\\n", encoding="utf-8")
        self.assertTrue(MODULE._has_completed_payload_reference(self.vault, MODULE.sha(self.source.read_bytes())))
        self.assertFalse(MODULE.inspect(self.vault, [second], self.config)["noop"])
        # Exact replay of the original identity remains byte-stable.
        before = (record.read_bytes(), record.stat().st_ino, record.stat().st_mtime_ns)
        self.assertTrue(MODULE.inspect(self.vault, [self.source], self.config, ["wiki/topic.md"])["noop"])
        self.assertEqual((record.read_bytes(), record.stat().st_ino, record.stat().st_mtime_ns), before)

    def test_unpublished_canonical_record_is_not_publication_evidence(self):
        payload_path, payload, record_path, record = MODULE._record(self.source, self.source.read_bytes(), [])
        payload_target = self.vault / payload_path
        record_target = self.vault / record_path
        payload_target.parent.mkdir(parents=True)
        payload_target.write_bytes(payload)
        record_target.write_bytes(record)
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(MODULE.CaptureError, "unverified"):
            MODULE.inspect(self.vault, [second], self.config)
        self.assertEqual(MODULE.BATCH.source_capture_history(self.vault), [])
        self.assertFalse((self.vault / ".vault-meta").exists())
        record_hash = MODULE.BATCH.sha(record)
        payload_hash = MODULE.sha(self.source.read_bytes())
        self.assertFalse(MODULE._has_completed_payload_reference(
            self.vault, payload_hash, [{"phase": "complete", "targets": {record_path: record_hash}}]))
        self.assertFalse(MODULE._has_completed_payload_reference(
            self.vault, payload_hash, [{"phase": "complete", "targets": {payload_path: payload_hash}}]))
        self.assertFalse(MODULE._has_completed_payload_reference(
            self.vault, payload_hash, [{"phase": "applying", "targets": {
                record_path: record_hash, payload_path: payload_hash}}]))

    def test_corrupt_capture_history_cannot_authorize_shared_payload_reuse(self):
        first = MODULE.inspect(self.vault, [self.source], self.config)
        MODULE.apply(self.vault, [self.source], self.config, first["planHash"])
        journal = next((self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        journal.write_text("{corrupt", encoding="utf-8")
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        journal_count = len(list(journal.parent.glob("*.json")))
        with self.assertRaisesRegex(MODULE.CaptureError, "history"):
            MODULE.inspect(self.vault, [second], self.config)
        self.assertEqual(len(list(journal.parent.glob("*.json"))), journal_count)
        payload = self.vault / ".raw/agent-captures" / (MODULE.sha(self.source.read_bytes()) + ".txt")
        self.assertEqual(payload.read_bytes(), self.source.read_bytes())

    def test_identical_source_reuse_rejects_source_page_and_config_drift(self):
        second = self.root / "second.txt"
        second.write_bytes(self.source.read_bytes())
        first = MODULE.inspect(self.vault, [self.source], self.config, ["wiki/topic.md"])
        MODULE.apply(self.vault, [self.source], self.config, first["planHash"], ["wiki/topic.md"])
        reviewed = MODULE.inspect(self.vault, [second], self.config, ["wiki/topic.md"])
        self.page.write_text("---\\ntype: note\\n---\\n\\n# Drifted page\\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.CaptureError, "approval hash"):
            MODULE.apply(self.vault, [second], self.config, reviewed["planHash"], ["wiki/topic.md"])
        self.page.write_text("---\\ntype: note\\n---\\n\\n# Topic\\n", encoding="utf-8")
        config_review = MODULE.inspect(self.vault, [second], self.config)
        self.config.write_text(json.dumps({"vaultPath": str(self.vault), "features": {"retrievalRefresh": False}}), encoding="utf-8")
        with self.assertRaises(MODULE.CaptureError):
            MODULE.apply(self.vault, [second], self.config, config_review["planHash"])

    def test_cli_capture_inspect_apply_special_page_links_and_bind_page_hashes(self):
        names = ["wiki/Topic Notes.md", "wiki/café.md", "wiki/Name [draft]#part|alias^.md"]
        for name in names:
            page = self.vault / name
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("---\\ntype: note\\n---\\n\\n# Linked page\\n", encoding="utf-8")
        env = {"PATH": __import__("os").environ["PATH"], "HOME": str(self.root),
               "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        common = ["--source", str(self.source), *sum((["--page", page] for page in names), [])]
        inspected = subprocess.run([sys.executable, str(CLI), "capture-inspect", *common, "--json"],
                                    cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        plan_hash = json.loads(inspected.stdout)["planHash"]
        (self.vault / names[0]).write_text("---\\ntype: note\\n---\\n\\n# Changed\\n", encoding="utf-8")
        refused = subprocess.run([sys.executable, str(CLI), "capture-apply", *common,
                                  "--plan-hash", plan_hash, "--json"],
                                 cwd=self.root, env=env, text=True, capture_output=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.vault / ".raw").exists())
        (self.vault / names[0]).write_text("---\\ntype: note\\n---\\n\\n# Linked page\\n", encoding="utf-8")
        approved = subprocess.run([sys.executable, str(CLI), "capture-apply", *common,
                                   "--plan-hash", plan_hash, "--json"],
                                  cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(approved.returncode, 0, approved.stderr)
        record = next((self.vault / ".raw/agent-captures").glob("*.md"))
        rendered = record.read_text(encoding="utf-8")
        self.assertIn("[[wiki/Topic Notes]]", rendered)
        self.assertIn("[[wiki/café]]", rendered)
        self.assertIn(r"wiki/Name \[draft\]#part|alias^.md", rendered)
        self.assertIn("Name%20%5Bdraft%5D%23part%7Calias%5E.md", rendered)

    def test_cli_capture_inspect_and_apply_use_explicit_source_and_exact_approval(self):
        env = {"PATH": __import__("os").environ["PATH"], "HOME": str(self.root), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        inspected = subprocess.run([sys.executable, str(CLI), "capture-inspect", "--source", str(self.source),
                                    "--page", "wiki/topic.md", "--json"], cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        plan_hash = json.loads(inspected.stdout)["planHash"]
        applied = subprocess.run([sys.executable, str(CLI), "capture-apply", "--source", str(self.source),
                                  "--page", "wiki/topic.md", "--plan-hash", plan_hash, "--json"],
                                 cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertTrue(json.loads(applied.stdout)["batchId"])
        self.assertEqual(len(list((self.vault / ".raw/agent-captures").glob("*.txt"))), 1)


if __name__ == "__main__":
    unittest.main()
