from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source

BASELINES = load_source("publication_test_source_baselines", str(ROOT / "scripts/source_baselines.py"))
LEDGER = load_source("publication_test_evidence_ledger", str(ROOT / "scripts/evidence_ledger.py"))
BATCH = load_source("publication_test_wiki_batch", str(ROOT / "scripts/wiki_batch.py"))


class SourceBaselinePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "wiki/meta/evidence").mkdir(parents=True)
        self.config = self.root / "properties.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        self.source_root = self.root / "sources"
        self.source_root.mkdir()
        (self.source_root / "old.txt").write_bytes(b"old")
        (self.source_root / "new.txt").write_bytes(b"new")
        self.asset = LEDGER.make_asset("asset-example", "example", "old.txt")
        self.asset["review"] = {"status": "reviewed", "note": "prior approval"}
        self.asset["future"] = {"keep": True}
        self.write_record(self.asset)

    def write_record(self, record):
        path = self.vault / LEDGER.record_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")

    def page_batch(self, content: str, before: str | None = None, page: str = "wiki/page.md"):
        path = self.vault / page
        if before is not None:
            path.write_text(before, encoding="utf-8")
        old_hash = hashlib.sha256(before.encode()).hexdigest() if before is not None else None
        bundle = {"version": 1, "operations": [{"path": page, "expectedHash": old_hash, "content": content}]}
        review = BATCH.inspect(self.vault, bundle, self.config, "wiki-page")
        return BATCH.apply(self.vault, bundle, self.config, review["planHash"], "wiki-page")

    def interrupt_observation_batch(self):
        asset = LEDGER.make_asset("asset-pending", "example", "old.txt")
        source = LEDGER.make_source_digest("local:example/old.txt", "sha256:" + "e" * 64)
        observation = LEDGER.make_observation(asset, source, "2025-03-04T05:06:07Z")
        bundle = {"version": 1, "operations": [LEDGER.operation(asset),
            LEDGER.operation(source), LEDGER.operation(observation)]}
        review = BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")
        original = BATCH._save_journal
        def interrupt(vault, journal):
            original(vault, journal)
            if (journal["authority"] == "wiki-ledger"
                    and all(entry["state"] == "published" for entry in journal["entries"])):
                raise RuntimeError("synthetic all-records-durable interruption")
        BATCH._save_journal = interrupt
        try:
            with self.assertRaisesRegex(RuntimeError, "all-records-durable"):
                BATCH.apply(self.vault, bundle, self.config, review["planHash"], "wiki-ledger")
        finally:
            BATCH._save_journal = original
        journal = next(json.loads(path.read_text()) for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json")
                       if json.loads(path.read_text())["authority"] == "wiki-ledger")
        return journal["batchId"], asset, source, observation

    def baseline_for(self, page_result, asset, source, observation):
        page_journal = BATCH._load_journal(self.vault, page_result["batchId"])
        page_entry = next(item for item in page_journal["entries"] if item["path"] == "wiki/page.md")
        entry = {"observationId": observation["id"], "sourceId": source["id"],
                 "contentHash": source["contentHash"], "pageHash": "sha256:" + page_entry["afterHash"],
                 "pageBatchId": page_result["batchId"], "reconciledAt": "2025-03-05T00:00:00Z",
                 "outcome": "no-change"}
        return {"version": 1, "operations": [LEDGER.operation(
            LEDGER.make_baseline("wiki/page.md", asset["id"], [entry]))]}

    def seed_observation(self):
        asset = LEDGER.make_asset("asset-seed", "example", "old.txt")
        source = LEDGER.make_source_digest("local:example/old.txt", "sha256:" + "a" * 64)
        observation = LEDGER.make_observation(asset, source, "2025-03-04T05:06:07Z")
        for record in (asset, source, observation):
            self.write_record(record)
        return asset, source, observation

    def baseline_bundle(self, batch_result, outcome: str, *, page_hash: str | None = None,
                        page_batch_id: str | None = None):
        asset, source, observation = self.seed_observation()
        journal = BATCH._load_journal(self.vault, page_batch_id or batch_result["batchId"])
        page_entry = next(item for item in journal["entries"] if item["path"] == "wiki/page.md")
        entry = {"observationId": observation["id"], "sourceId": source["id"],
                 "contentHash": source["contentHash"], "pageHash": "sha256:" + (page_hash or page_entry["afterHash"]),
                 "pageBatchId": page_batch_id or batch_result["batchId"],
                 "reconciledAt": "2025-03-05T05:06:07Z", "outcome": outcome}
        return {"version": 1, "operations": [LEDGER.operation(LEDGER.make_baseline("wiki/page.md", asset["id"], [entry]))]}

    def test_explicit_move_builds_review_without_promoting_review_status(self):
        selection = {"version": 1, "rootId": "example", "assetId": "asset-example",
                     "locator": "new.txt", "observedAt": "2025-03-04T05:06:07Z"}
        with self.assertRaisesRegex(ValueError, "--allow-move"):
            BASELINES.observation_bundle(self.vault, self.source_root, selection)
        bundle = BASELINES.observation_bundle(self.vault, self.source_root, selection, allow_move=True)
        moved = json.loads(bundle["operations"][0]["content"])
        self.assertEqual(moved["locator"], "new.txt")
        self.assertEqual(moved["review"], {"status": "reviewed", "note": "prior approval"})
        self.assertEqual(moved["future"], {"keep": True})
        original = json.loads((self.vault / LEDGER.record_path(self.asset)).read_text())
        self.assertEqual(original["locator"], "old.txt")
        self.assertEqual(original["review"]["status"], "reviewed")

    def test_generic_baseline_cannot_consume_unfinished_observation_dependencies(self):
        page = "---\ntype: note\n---\n\n# Pending deps\n"
        page_result = self.page_batch(page, page)
        observation_batch, asset, source, observation = self.interrupt_observation_batch()
        baseline = self.baseline_for(page_result, asset, source, observation)
        with self.assertRaisesRegex(BATCH.BatchError, "unfinished"):
            BATCH.inspect(self.vault, baseline, self.config, "wiki-ledger")
        BATCH.recover(self.vault, observation_batch, self.config)
        review = BATCH.inspect(self.vault, baseline, self.config, "wiki-ledger")
        result = BATCH.apply(self.vault, baseline, self.config, review["planHash"], "wiki-ledger")
        self.assertEqual(result["phase"], "complete")

    def test_observation_rollback_after_interruption_cannot_leave_completed_baseline(self):
        page = "---\ntype: note\n---\n\n# Rolled back deps\n"
        page_result = self.page_batch(page, page)
        observation_batch, asset, source, observation = self.interrupt_observation_batch()
        baseline = self.baseline_for(page_result, asset, source, observation)
        with self.assertRaisesRegex(BATCH.BatchError, "unfinished"):
            BATCH.inspect(self.vault, baseline, self.config, "wiki-ledger")
        BATCH.rollback(self.vault, observation_batch, self.config)
        for record in (asset, source, observation):
            self.assertFalse((self.vault / LEDGER.record_path(record)).exists())
        with self.assertRaises(BATCH.BatchError):
            BATCH.inspect(self.vault, baseline, self.config, "wiki-ledger")
        self.assertFalse(any(json.loads(path.read_text()).get("kind") == "baseline"
                             for path in (self.vault / "wiki/meta/evidence").glob("*.json")))

    def test_ordered_dependency_records_in_one_batch_recover_with_self_exclusion(self):
        page = "---\ntype: note\n---\n\n# Ordered bundle\n"
        page_result = self.page_batch(page, page)
        asset = LEDGER.make_asset("asset-ordered", "example", "old.txt")
        source = LEDGER.make_source_digest("local:example/old.txt", "sha256:" + "f" * 64)
        observation = LEDGER.make_observation(asset, source, "2025-03-04T05:06:07Z")
        page_journal = BATCH._load_journal(self.vault, page_result["batchId"])
        page_entry = page_journal["entries"][0]
        receipt = {"observationId": observation["id"], "sourceId": source["id"],
                   "contentHash": source["contentHash"], "pageHash": "sha256:" + page_entry["afterHash"],
                   "pageBatchId": page_result["batchId"], "reconciledAt": "2025-03-05T00:00:00Z",
                   "outcome": "no-change"}
        baseline = LEDGER.operation(LEDGER.make_baseline("wiki/page.md", asset["id"], [receipt]))
        bundle = {"version": 1, "operations": [LEDGER.operation(asset), LEDGER.operation(source),
            LEDGER.operation(observation), baseline]}
        review = BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")
        original = BATCH._save_journal
        def interrupt_complete(vault, journal):
            original(vault, journal)
            if journal["authority"] == "wiki-ledger" and all(entry["state"] == "published" for entry in journal["entries"]):
                raise RuntimeError("synthetic ordered-batch final acknowledgement interruption")
        BATCH._save_journal = interrupt_complete
        try:
            with self.assertRaisesRegex(RuntimeError, "final acknowledgement"):
                BATCH.apply(self.vault, bundle, self.config, review["planHash"], "wiki-ledger")
        finally:
            BATCH._save_journal = original
        journal = next(json.loads(path.read_text()) for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json")
                       if json.loads(path.read_text())["authority"] == "wiki-ledger")
        recovered = BATCH.recover(self.vault, journal["batchId"], self.config)
        self.assertEqual(recovered["phase"], "complete")

    def test_generic_baseline_noop_and_completed_replay_recheck_latest_page_receipt(self):
        page = "---\ntype: note\n---\n\n# No-op\n"
        page_result = self.page_batch(page, page)
        bundle = self.baseline_bundle(page_result, "no-change")
        review = BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")
        BATCH.apply(self.vault, bundle, self.config, review["planHash"], "wiki-ledger")
        baseline_path = self.vault / bundle["operations"][0]["path"]
        current = json.loads(baseline_path.read_bytes())
        raw = baseline_path.read_bytes()
        no_op = {"version": 1, "operations": [LEDGER.operation(current, raw)]}
        no_op_review = BATCH.inspect(self.vault, no_op, self.config, "wiki-ledger")
        no_op_applied = BATCH.apply(self.vault, no_op, self.config, no_op_review["planHash"], "wiki-ledger")
        page_path = self.vault / "wiki/page.md"
        page_path.write_text(page + "\nDrift\n", encoding="utf-8")
        with self.assertRaisesRegex(BATCH.BatchError, "page bytes"):
            BATCH.recover(self.vault, no_op_applied["batchId"], self.config)
        with self.assertRaisesRegex(BATCH.BatchError, "page bytes"):
            BATCH.apply(self.vault, no_op, self.config, no_op_review["planHash"], "wiki-ledger")
        with self.assertRaisesRegex(BATCH.BatchError, "page bytes"):
            BATCH.inspect(self.vault, no_op, self.config, "wiki-ledger")

    def test_generic_batch_rejects_invalid_receipts_hashes_outcomes_and_authorities(self):
        old = "---\ntype: note\n---\n\n# Before\n"
        changed = "---\ntype: note\n---\n\n# After\n"
        changed_batch = self.page_batch(changed, old)
        invalid_outcome = self.baseline_bundle(changed_batch, "no-change")
        with self.assertRaisesRegex(BATCH.BatchError, "outcome"):
            BATCH.inspect(self.vault, invalid_outcome, self.config, "wiki-ledger")

        # An actual no-change page receipt must still bind its exact hash.
        same_result = self.page_batch(old, old)
        wrong_hash = self.baseline_bundle(same_result, "no-change", page_hash="0" * 64)
        with self.assertRaisesRegex(BATCH.BatchError, "page batch"):
            BATCH.inspect(self.vault, wrong_hash, self.config, "wiki-ledger")

        unrelated_source = LEDGER.make_source("https://example.test/x", b"x")
        capture = {"version": 1, "operations": [LEDGER.operation(unrelated_source)]}
        capture_review = BATCH.inspect(self.vault, capture, self.config, "wiki-ledger")
        capture_result = BATCH.apply(self.vault, capture, self.config, capture_review["planHash"], "wiki-ledger")
        valid = self.baseline_bundle(same_result, "no-change")
        record = json.loads(valid["operations"][0]["content"])
        record["history"][0]["pageBatchId"] = capture_result["batchId"]
        wrong_authority = {"version": 1, "operations": [LEDGER.operation(record)]}
        with self.assertRaisesRegex(BATCH.BatchError, "wiki-page"):
            BATCH.inspect(self.vault, wrong_authority, self.config, "wiki-ledger")

    def test_incomplete_page_finalization_cannot_authorize_a_baseline(self):
        content = "---\ntype: note\n---\n\n# Interrupted\n"
        page_bundle = {"version": 1, "operations": [{"path": "wiki/page.md", "expectedHash": None, "content": content}]}
        review = BATCH.inspect(self.vault, page_bundle, self.config, "wiki-page")
        original = BATCH._call_lifecycle
        def interrupt_finalize(vault, path, batch_id, action, *args, **kwargs):
            if action == "finalize":
                raise BATCH.BatchError("synthetic finalization interruption")
            return original(vault, path, batch_id, action, *args, **kwargs)
        BATCH._call_lifecycle = interrupt_finalize
        try:
            with self.assertRaisesRegex(BATCH.BatchError, "interruption"):
                BATCH.apply(self.vault, page_bundle, self.config, review["planHash"], "wiki-page")
        finally:
            BATCH._call_lifecycle = original
        batch_dir = self.vault / ".vault-meta/lifecycle/batches"
        journal_path = next(path for path in batch_dir.glob("*.json") if json.loads(path.read_text())["authority"] == "wiki-page")
        incomplete = json.loads(journal_path.read_text())
        self.seed_observation()
        operation = self.baseline_bundle({"batchId": incomplete["batchId"]}, "updated")
        with self.assertRaises(BATCH.BatchError):
            BATCH.inspect(self.vault, operation, self.config, "wiki-ledger")

    def test_partial_multi_pair_recovery_preflights_all_remaining_page_dependencies(self):
        content_a = "---\ntype: note\n---\n\n# A\n"
        content_b = "---\ntype: note\n---\n\n# B\n"
        batch_a = self.page_batch(content_a, content_a, "wiki/a.md")
        batch_b = self.page_batch(content_b, content_b, "wiki/b.md")
        selected = []
        for asset_id, locator, marker, page, page_batch, minute in (
                ("asset-alpha", "old.txt", "a", "wiki/a.md", batch_a, "01"),
                ("asset-beta", "new.txt", "b", "wiki/b.md", batch_b, "02")):
            asset = LEDGER.make_asset(asset_id, "example", locator)
            source = LEDGER.make_source_digest("local:example/" + locator, "sha256:" + marker * 64)
            observation = LEDGER.make_observation(asset, source, f"2025-03-04T05:06:{minute}Z")
            for record in (asset, source, observation):
                self.write_record(record)
            selected.append({"page": page, "observationId": observation["id"],
                "pageBatchId": page_batch["batchId"], "reconciledAt": "2025-03-05T00:00:00Z",
                "outcome": "no-change"})
        bundle = BASELINES.baseline_bundle(self.vault, {"version": 1, "reconciliations": selected})
        review = BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")
        original = BATCH._save_journal
        def interrupt_first(vault, journal):
            original(vault, journal)
            if (journal["authority"] == "wiki-ledger" and journal["entries"][0]["state"] == "published"
                    and journal["entries"][1]["state"] == "pending"):
                raise RuntimeError("synthetic partial pair publication")
        BATCH._save_journal = interrupt_first
        try:
            with self.assertRaisesRegex(RuntimeError, "partial pair"):
                BATCH.apply(self.vault, bundle, self.config, review["planHash"], "wiki-ledger")
        finally:
            BATCH._save_journal = original
        incomplete = next(json.loads(path.read_text()) for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json")
                          if json.loads(path.read_text())["authority"] == "wiki-ledger"
                          and json.loads(path.read_text())["phase"] != "complete")
        (self.vault / "wiki/b.md").write_text(content_b + "\nDrift\n", encoding="utf-8")
        with self.assertRaisesRegex(BATCH.BatchError, "page bytes"):
            BATCH.recover(self.vault, incomplete["batchId"], self.config)
        self.assertEqual(sum(json.loads(path.read_text()).get("kind") == "baseline"
                             for path in (self.vault / "wiki/meta/evidence").glob("*.json")), 1)

    def test_original_history_prefix_survives_interrupted_append_recovery(self):
        old_page = "---\ntype: note\n---\n\n# Old\n"
        old_receipt = self.page_batch(old_page, old_page)
        asset = LEDGER.make_asset("asset-prefix", "example", "old.txt")
        source_old = LEDGER.make_source_digest("local:example/old.txt", "sha256:" + "1" * 64)
        observation_old = LEDGER.make_observation(asset, source_old, "2025-03-04T05:06:01Z")
        for record in (asset, source_old, observation_old):
            self.write_record(record)
        old_entry = {"observationId": observation_old["id"], "sourceId": source_old["id"],
                     "contentHash": source_old["contentHash"], "pageHash": "sha256:" + hashlib.sha256(old_page.encode()).hexdigest(),
                     "pageBatchId": old_receipt["batchId"], "reconciledAt": "2025-03-05T00:00:00Z", "outcome": "no-change"}
        baseline = LEDGER.make_baseline("wiki/page.md", asset["id"], [old_entry])
        self.write_record(baseline)
        new_page = "---\ntype: note\n---\n\n# New\n"
        new_receipt = self.page_batch(new_page, old_page)
        source_new = LEDGER.make_source_digest("local:example/old.txt", "sha256:" + "2" * 64)
        observation_new = LEDGER.make_observation(asset, source_new, "2025-03-04T05:06:02Z")
        self.write_record(source_new)
        self.write_record(observation_new)
        new_entry = {"observationId": observation_new["id"], "sourceId": source_new["id"],
                     "contentHash": source_new["contentHash"], "pageHash": "sha256:" + hashlib.sha256(new_page.encode()).hexdigest(),
                     "pageBatchId": new_receipt["batchId"], "reconciledAt": "2025-03-06T00:00:00Z", "outcome": "updated"}
        appended = LEDGER.merge_record(baseline, {"history": [old_entry, new_entry]})
        bundle = {"version": 1, "operations": [LEDGER.operation(appended, (self.vault / LEDGER.record_path(baseline)).read_bytes())]}
        plan = BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")
        original = BATCH._save_journal
        def interrupt(vault, journal):
            original(vault, journal)
            if journal["authority"] == "wiki-ledger" and journal["entries"][0]["state"] == "published":
                raise RuntimeError("synthetic append acknowledgement interruption")
        BATCH._save_journal = interrupt
        try:
            with self.assertRaisesRegex(RuntimeError, "append acknowledgement"):
                BATCH.apply(self.vault, bundle, self.config, plan["planHash"], "wiki-ledger")
        finally:
            BATCH._save_journal = original
        incomplete = next(json.loads(path.read_text()) for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json")
                          if json.loads(path.read_text())["authority"] == "wiki-ledger" and json.loads(path.read_text())["phase"] != "complete")
        BATCH.recover(self.vault, incomplete["batchId"], self.config)
        restored = json.loads((self.vault / LEDGER.record_path(baseline)).read_text())
        self.assertEqual(restored["history"][0], old_entry)
        self.assertEqual(restored["history"], [old_entry, new_entry])

    def test_interrupted_baseline_recovery_rechecks_original_append_against_page(self):
        content = "---\ntype: note\n---\n\n# Stable\n"
        page_result = self.page_batch(content, content)
        valid = self.baseline_bundle(page_result, "no-change")
        review = BATCH.inspect(self.vault, valid, self.config, "wiki-ledger")
        original = BATCH._save_journal
        def interrupt_after_rename(vault, journal):
            original(vault, journal)
            if journal["authority"] == "wiki-ledger" and journal["entries"][0]["state"] == "published":
                raise RuntimeError("synthetic baseline journal acknowledgement interruption")
        BATCH._save_journal = interrupt_after_rename
        try:
            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                BATCH.apply(self.vault, valid, self.config, review["planHash"], "wiki-ledger")
        finally:
            BATCH._save_journal = original
        journals = [json.loads(path.read_text()) for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json")]
        ledger_journal = next(j for j in journals if j["authority"] == "wiki-ledger" and j["phase"] != "complete")
        page_path = self.vault / "wiki/page.md"
        page_path.write_text(content + "\nDrift\n", encoding="utf-8")
        with self.assertRaisesRegex(BATCH.BatchError, "page bytes"):
            BATCH.recover(self.vault, ledger_journal["batchId"], self.config)
        with self.assertRaises(BATCH.BatchError):
            BATCH.apply(self.vault, valid, self.config, review["planHash"], "wiki-ledger")

    def test_selected_page_asset_pairs_advance_independently(self):
        content = "---\ntype: note\n---\n\n# Same bytes\n"
        batch_a = self.page_batch(content, content, "wiki/a.md")
        batch_b = self.page_batch(content, content, "wiki/b.md")
        pairs = []
        for asset_id, locator, marker, observed_at in (("asset-alpha", "old.txt", "a", "2025-03-04T05:06:01Z"),
                                                       ("asset-beta", "new.txt", "b", "2025-03-04T05:06:02Z")):
            asset = LEDGER.make_asset(asset_id, "example", locator)
            source = LEDGER.make_source_digest(
                "local:example/" + locator, "sha256:" + marker * 64)
            observation = LEDGER.make_observation(asset, source, observed_at)
            for record in (asset, source, observation):
                self.write_record(record)
            pairs.append((asset, source, observation))
        def selected(page, batch_id, observation, timestamp):
            return {"page": page, "observationId": observation["id"], "pageBatchId": batch_id,
                    "reconciledAt": timestamp, "outcome": "no-change"}
        selection = {"version": 1, "reconciliations": [
            selected("wiki/a.md", batch_a["batchId"], pairs[0][2], "2025-03-05T00:00:00Z"),
            selected("wiki/a.md", batch_a["batchId"], pairs[1][2], "2025-03-05T00:01:00Z"),
            selected("wiki/b.md", batch_b["batchId"], pairs[0][2], "2025-03-05T00:02:00Z"),
        ]}
        unrelated = LEDGER.make_asset("asset-unselected", "example", "old.txt")
        unrelated_source = LEDGER.make_source_digest("local:example/old.txt", "sha256:" + "c" * 64)
        unrelated_observation = LEDGER.make_observation(unrelated, unrelated_source, "2025-03-04T05:06:03Z")
        for record in (unrelated, unrelated_source, unrelated_observation):
            self.write_record(record)
        bundle = BASELINES.baseline_bundle(self.vault, selection)
        self.assertEqual(len(bundle["operations"]), 3)
        reviewed = BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")
        BATCH.apply(self.vault, bundle, self.config, reviewed["planHash"], "wiki-ledger")
        baseline_records = [json.loads(path.read_text()) for path in (self.vault / "wiki/meta/evidence").glob("*.json")
                            if json.loads(path.read_text()).get("kind") == "baseline"]
        self.assertEqual({(item["page"], item["assetId"]) for item in baseline_records},
                         {("wiki/a.md", "asset-alpha"), ("wiki/a.md", "asset-beta"), ("wiki/b.md", "asset-alpha")})
        self.assertTrue(all(len(item["history"]) == 1 for item in baseline_records))
        self.assertFalse(any(item["assetId"] == "asset-unselected" for item in baseline_records))
        self.assertEqual(BASELINES.baseline_bundle(self.vault, selection)["operations"], [])
        baseline_bytes = {path.name: path.read_bytes() for path in (self.vault / "wiki/meta/evidence").glob("*.json")
                          if json.loads(path.read_text()).get("kind") == "baseline"}
        subprocess.run(["git", "-C", str(self.source_root), "init", "-q"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.source_root), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(self.source_root), "config", "user.email", "fixture@example.invalid"], check=True)
        unrelated = self.source_root / "unrelated.txt"
        unrelated.write_text("not selected\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.source_root), "add", "unrelated.txt"], check=True)
        subprocess.run(["git", "-C", str(self.source_root), "commit", "-m", "unrelated fixture change"],
                       check=True, capture_output=True)
        self.assertEqual({path.name: path.read_bytes() for path in (self.vault / "wiki/meta/evidence").glob("*.json")
                          if json.loads(path.read_text()).get("kind") == "baseline"}, baseline_bytes)
        duplicate = dict(selection)
        duplicate["reconciliations"] = [selection["reconciliations"][0],
                                         selected("wiki/a.md", batch_a["batchId"], pairs[0][2], "2025-03-05T00:03:00Z")]
        with self.assertRaisesRegex(ValueError, "page/asset"):
            BASELINES.baseline_bundle(self.vault, duplicate)

    def test_page_drift_at_publication_edge_blocks_baseline_rename(self):
        content = "---\ntype: note\n---\n\n# Edge\n"
        page_result = self.page_batch(content, content)
        bundle = self.baseline_bundle(page_result, "no-change")
        plan = BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")
        original = BATCH._validate_ledger_operations
        calls = 0
        page = self.vault / "wiki/page.md"
        def drift_on_publish(vault, operations, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                page.write_text(content + "\nInjected drift\n", encoding="utf-8")
            return original(vault, operations, **kwargs)
        BATCH._validate_ledger_operations = drift_on_publish
        try:
            with self.assertRaisesRegex(BATCH.BatchError, "page bytes"):
                BATCH.apply(self.vault, bundle, self.config, plan["planHash"], "wiki-ledger")
        finally:
            BATCH._validate_ledger_operations = original
        self.assertFalse(any(json.loads(path.read_text()).get("kind") == "baseline"
                             for path in (self.vault / "wiki/meta/evidence").glob("*.json")))

    def test_baseline_before_observation_is_rejected_by_generic_batch(self):
        page = "---\ntype: note\n---\n\n# Existing\n"
        page_result = self.page_batch(page, page)
        asset = LEDGER.make_asset("asset-order", "example", "old.txt")
        source = LEDGER.make_source_digest("local:example/old.txt", "sha256:" + "b" * 64)
        observation = LEDGER.make_observation(asset, source, "2025-03-04T05:06:07Z")
        entry = {"observationId": observation["id"], "sourceId": source["id"],
                 "contentHash": source["contentHash"], "pageHash": "sha256:" + hashlib.sha256(page.encode()).hexdigest(),
                 "pageBatchId": page_result["batchId"], "reconciledAt": "2025-03-05T05:06:07Z", "outcome": "no-change"}
        baseline = LEDGER.make_baseline("wiki/page.md", asset["id"], [entry])
        bundle = {"version": 1, "operations": [LEDGER.operation(baseline), LEDGER.operation(asset),
                                                        LEDGER.operation(source), LEDGER.operation(observation)]}
        with self.assertRaises(BATCH.BatchError):
            BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")

    def test_unchanged_observation_refuses_relevant_unfinished_ledger_batch(self):
        asset = LEDGER.make_asset("asset-example", "example", "old.txt")
        self.write_record(asset)
        raw = (self.vault / LEDGER.record_path(asset)).read_bytes()
        operation = LEDGER.operation(asset, raw)
        bundle = {"version": 1, "operations": [operation]}
        review = BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")
        original = BATCH._save_journal
        def interrupt(vault, journal):
            original(vault, journal)
            if journal["authority"] == "wiki-ledger" and journal["entries"][0]["state"] == "published":
                raise RuntimeError("synthetic acknowledgement interruption")
        BATCH._save_journal = interrupt
        try:
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                BATCH.apply(self.vault, bundle, self.config, review["planHash"], "wiki-ledger")
        finally:
            BATCH._save_journal = original
        selection = {"version": 1, "rootId": "example", "assetId": "asset-example",
                     "locator": "old.txt", "observedAt": "2025-03-04T05:06:07Z"}
        with self.assertRaisesRegex(RuntimeError, "unfinished"):
            BASELINES.observation_bundle(self.vault, self.source_root, selection)

    def test_referenced_source_locator_or_content_cannot_change(self):
        _asset, source, _observation = self.seed_observation()
        raw = (self.vault / LEDGER.record_path(source)).read_bytes()
        for changes in ({"locator": "local:example/changed.txt"},
                        {"contentHash": "sha256:" + "d" * 64}):
            changed = dict(source, **changes)
            bundle = {"version": 1, "operations": [LEDGER.operation(changed, raw)]}
            with self.subTest(changes=changes), self.assertRaisesRegex(BATCH.BatchError, "immutable while referenced"):
                BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")

    def test_legacy_source_and_claim_updates_keep_existing_batch_semantics(self):
        source = LEDGER.make_source("https://example.test/doc", b"before")
        source["future"] = {"keep": "old"}
        claim = LEDGER.make_claim("claim-existing", "before")
        claim["future"] = "extension"
        self.write_record(source)
        self.write_record(claim)
        changed_source = dict(source, locator="legacy-updated", contentHash="sha256:" + "c" * 64)
        changed_source.pop("future")
        changed_claim = dict(claim, statement="after")
        changed_claim.pop("future")
        source_raw = (self.vault / LEDGER.record_path(source)).read_bytes()
        claim_raw = (self.vault / LEDGER.record_path(claim)).read_bytes()
        bundle = {"version": 1, "operations": [LEDGER.operation(changed_source, source_raw),
                                                       LEDGER.operation(changed_claim, claim_raw)]}
        # Legacy generic callers were not subject to the new observation/baseline immutability policy.
        self.assertEqual(BATCH.inspect(self.vault, bundle, self.config, "wiki-ledger")["authority"], "wiki-ledger")

    def test_cli_unsafe_ledger_failure_is_structured_json_without_traceback(self):
        config = self.root / "properties.json"
        config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        evidence = self.vault / "wiki/meta/evidence"
        real = evidence.with_name("evidence-real")
        evidence.rename(real)
        outside = self.root / "outside"
        outside.mkdir()
        evidence.symlink_to(outside, target_is_directory=True)
        selection = self.root / "selection.json"
        selection.write_text(json.dumps({"version": 1, "reconciliations": [{}]}), encoding="utf-8")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.root)}
        result = subprocess.run([sys.executable, str(ROOT / "scripts/obsidian-second-brain.py"),
            "baseline-inspect", "--selection", str(selection), "--vault", str(self.vault),
            "--config", str(config), "--json"], text=True, capture_output=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["code"], "command-error")

    def test_history_without_commit_still_validates_root_mapping(self):
        result = BASELINES.history_availability(self.source_root, None)
        self.assertEqual(result["status"], "no-commit-recorded")
        linked = self.root / "linked-root"
        linked.symlink_to(self.source_root, target_is_directory=True)
        with self.assertRaises(ValueError):
            BASELINES.history_availability(linked, None)

    def test_ledger_enumerator_refuses_symlink_fifo_and_noncanonical_entries(self):
        directory = self.vault / "wiki/meta/evidence"
        external = self.root / "external"
        external.mkdir()
        link_target = directory.with_name("evidence-real")
        directory.rename(link_target)
        directory.symlink_to(external, target_is_directory=True)
        with self.assertRaises(BATCH.BatchError):
            BATCH._ledger_files(self.vault)
        directory.unlink()
        link_target.rename(directory)
        fifo = directory / "asset-fifo.json"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(BATCH.BatchError, "regular file"):
            BATCH._ledger_files(self.vault)
        fifo.unlink()
        (directory / "ASSET-wrong.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(BATCH.BatchError, "noncanonical"):
            BATCH._ledger_files(self.vault)

    def test_cli_requires_explicit_move_review(self):
        from subprocess import run
        config = self.root / "properties.json"
        config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        selection = self.root / "selection.json"
        selection.write_text(json.dumps({"version": 1, "rootId": "example", "assetId": "asset-example",
                                         "locator": "new.txt", "observedAt": "2025-03-04T05:06:07Z"}), encoding="utf-8")
        env = {"PATH": __import__("os").environ["PATH"], "HOME": str(self.root),
               "OBSIDIAN_AGENT_CONFIG": str(config)}
        cli = [sys.executable, str(ROOT / "scripts/obsidian-second-brain.py")]
        common = ["--source-root", str(self.source_root), "--vault", str(self.vault),
                  "--config", str(config), "--json"]
        initial = {"version": 1, "rootId": "example", "assetId": "asset-example",
                   "locator": "old.txt", "observedAt": "2025-03-04T05:06:01Z"}
        selection.write_text(json.dumps(initial), encoding="utf-8")
        first_review = run([*cli, "observe-inspect", "--selection", str(selection), *common],
                           text=True, capture_output=True, env=env)
        self.assertEqual(first_review.returncode, 0, first_review.stderr + first_review.stdout)
        first_plan = json.loads(first_review.stdout)
        first_apply = run([*cli, "observe-apply", "--selection", str(selection), *common,
                           "--plan-hash", first_plan["planHash"]], text=True, capture_output=True, env=env)
        self.assertEqual(first_apply.returncode, 0, first_apply.stderr + first_apply.stdout)
        original_observation = len([p for p in (self.vault / "wiki/meta/evidence").glob("*.json")
                                    if json.loads(p.read_text()).get("kind") == "observation"])
        refused = run([*cli, "observe-inspect", "--selection", str(selection), *common],
                      text=True, capture_output=True, env=env)
        # Same observation is an idempotent historical retry, not a move.
        self.assertEqual(refused.returncode, 0, refused.stderr + refused.stdout)
        moved_selection = dict(initial, locator="new.txt", observedAt="2025-03-04T05:06:02Z")
        selection.write_text(json.dumps(moved_selection), encoding="utf-8")
        move_refused = run([*cli, "observe-inspect", "--selection", str(selection), *common],
                           text=True, capture_output=True, env=env)
        self.assertNotEqual(move_refused.returncode, 0)
        move_review = run([*cli, "observe-inspect", "--selection", str(selection), *common, "--allow-move"],
                          text=True, capture_output=True, env=env)
        self.assertEqual(move_review.returncode, 0, move_review.stderr + move_review.stdout)
        move_plan = json.loads(move_review.stdout)
        move_apply = run([*cli, "observe-apply", "--selection", str(selection), *common,
                          "--allow-move", "--plan-hash", move_plan["planHash"]], text=True,
                         capture_output=True, env=env)
        self.assertEqual(move_apply.returncode, 0, move_apply.stderr + move_apply.stdout)
        moved_asset = json.loads((self.vault / LEDGER.record_path(self.asset)).read_text())
        self.assertEqual(moved_asset["locator"], "new.txt")
        self.assertEqual(moved_asset["review"]["status"], "reviewed")
        self.assertEqual(moved_asset["future"], {"keep": True})
        self.assertEqual(original_observation, 1)
        selection.write_text(json.dumps(initial), encoding="utf-8")
        original_replay = run([*cli, "observe-apply", "--selection", str(selection), *common,
                               "--plan-hash", first_plan["planHash"]], text=True, capture_output=True, env=env)
        self.assertEqual(original_replay.returncode, 0, original_replay.stderr + original_replay.stdout)
        self.assertTrue(json.loads(original_replay.stdout)["noop"])
        self.assertEqual(len([p for p in (self.vault / "wiki/meta/evidence").glob("*.json")
                              if json.loads(p.read_text()).get("kind") == "observation"]), 2)


if __name__ == "__main__":
    unittest.main()
