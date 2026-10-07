from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import stat

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source
MODULE = load_source(
    "wiki_batch_test", str(ROOT / "scripts/wiki_batch.py")
)
INDEX = load_source("wiki_batch_test_index", str(ROOT / "scripts/bm25-index.py"))
EVIDENCE = load_source("wiki_batch_test_evidence", str(ROOT / "scripts/evidence_ledger.py"))
CLI = ROOT / "scripts/obsidian-second-brain.py"
PAGE_A = "---\ntype: note\n---\n\n# Alpha\n"
PAGE_B = "---\ntype: note\n---\n\n# Beta\n"


class WikiBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        self.config = Path(self.temp.name) / "properties.json"

    @staticmethod
    def bundle(*operations):
        return {"version": 1, "operations": list(operations)}

    @staticmethod
    def operation(path="wiki/alpha.md", content=PAGE_A, expected=None):
        return {"path": path, "expectedHash": expected, "content": content}

    def inspect(self, bundle, authority="wiki-page"):
        return MODULE.inspect(self.vault, bundle, self.config, authority)

    def vault_snapshot(self):
        result = {}
        for path in [self.vault, *self.vault.rglob("*")]:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                content = path.read_bytes()
            elif stat.S_ISLNK(info.st_mode):
                content = path.readlink().as_posix()
            else:
                content = None
            result[path.relative_to(self.vault).as_posix() if path != self.vault else "."] = (
                info.st_mode, info.st_mtime_ns, content)
        return result

    def test_source_capture_history_is_read_only_and_returns_validated_journals(self):
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        self.assertEqual(MODULE.source_capture_history(self.vault), [])
        self.assertFalse((self.vault / ".vault-meta").exists())
        bundle = self.bundle({"path": ".raw/agent-captures/reference.txt", "expectedHash": None,
                              "content": "immutable payload\\n"})
        preview = MODULE.inspect(self.vault, bundle, self.config, "source-capture")
        self.assertEqual(MODULE.source_capture_history(self.vault), [])
        self.assertFalse((self.vault / ".vault-meta").exists())
        applied = MODULE.apply(self.vault, bundle, self.config, preview["planHash"], "source-capture")
        history = MODULE.source_capture_history(self.vault)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["batchId"], applied["batchId"])
        self.assertEqual(history[0]["phase"], "complete")
        self.assertEqual(history[0]["targets"], {
            ".raw/agent-captures/reference.txt": MODULE.sha(b"immutable payload\\n")})

    def test_two_page_batch_one_approval_navigation_and_log(self):
        bundle = self.bundle(self.operation(), self.operation("wiki/beta.md", PAGE_B))
        plan = self.inspect(bundle)
        self.assertEqual(plan["targets"], ["wiki/alpha.md", "wiki/beta.md"])
        result = MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        self.assertEqual(result["phase"], "complete")
        self.assertEqual((self.vault / "wiki/alpha.md").read_text(), PAGE_A)
        self.assertEqual((self.vault / "wiki/beta.md").read_text(), PAGE_B)
        index = (self.vault / "wiki/index.md").read_text()
        log = (self.vault / "wiki/log.md").read_text()
        self.assertIn("alpha.md", index)
        self.assertIn("beta.md", index)
        self.assertIn("Creation", log)
        self.assertIn("alpha.md", log)
        self.assertIn("beta.md", log)
        before = log
        replay = MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        self.assertTrue(replay["noop"])
        self.assertEqual((self.vault / "wiki/log.md").read_text(), before)

    def test_invalid_or_conflicting_second_target_rejects_before_first_write(self):
        first = self.operation()
        invalid = self.operation("wiki/beta.md", "not frontmatter")
        bad_bundle = self.bundle(first, invalid)
        with self.assertRaises(MODULE.BatchError):
            self.inspect(bad_bundle)
        self.assertFalse((self.vault / "wiki/alpha.md").exists())
        (self.vault / "wiki/Beta.md").write_text(PAGE_B)
        conflict = self.bundle(first, self.operation("wiki/beta.md", PAGE_B))
        with self.assertRaises(MODULE.BatchError):
            self.inspect(conflict)
        self.assertFalse((self.vault / "wiki/alpha.md").exists())

    def test_duplicate_case_and_ancestor_destinations_rejected(self):
        with self.assertRaises(MODULE.BatchError):
            self.inspect(self.bundle(self.operation(), self.operation("wiki/ALPHA.md", PAGE_B)))
        with self.assertRaises(MODULE.BatchError):
            self.inspect(self.bundle(self.operation("wiki/node", PAGE_A), self.operation("wiki/node/child.md", PAGE_B)))

    def test_rejects_unapproved_content_and_user_preimage_drift(self):
        bundle = self.bundle(self.operation())
        plan = self.inspect(bundle)
        changed = self.bundle(self.operation(content=PAGE_A + "changed\n"))
        with self.assertRaises(MODULE.BatchError):
            MODULE.apply(self.vault, changed, self.config, plan["planHash"])
        target = self.vault / "wiki/alpha.md"
        target.write_text("---\ntype: note\n---\n\n# User\n")
        with self.assertRaises(MODULE.BatchError):
            MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        self.assertIn("User", target.read_text())

    def test_completed_apply_and_recover_verify_every_postimage(self):
        bundle = self.bundle(self.operation(), self.operation("wiki/beta.md", PAGE_B))
        plan = self.inspect(bundle)
        result = MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        self.assertTrue(MODULE.apply(self.vault, bundle, self.config, plan["planHash"])["noop"])
        self.assertTrue(MODULE.recover(self.vault, result["batchId"])["noop"])
        (self.vault / "wiki/beta.md").write_text("app edit\n")
        with self.assertRaises(MODULE.BatchError):
            MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        with self.assertRaises(MODULE.BatchError):
            MODULE.recover(self.vault, result["batchId"])
        self.assertEqual((self.vault / "wiki/beta.md").read_text(), "app edit\n")

    def test_recovery_after_first_publication_and_journal_interruption(self):
        bundle = self.bundle(self.operation(), self.operation("wiki/beta.md", PAGE_B))
        plan = self.inspect(bundle)
        save = MODULE._save_journal
        raised = {"done": False}
        def interrupt(vault, journal):
            if not raised["done"] and journal["entries"][0]["state"] == "published" and journal["entries"][1]["state"] == "pending":
                raised["done"] = True
                raise OSError("simulated journal interruption after first publication")
            return save(vault, journal)
        MODULE._save_journal = interrupt
        try:
            with self.assertRaises(OSError):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._save_journal = save
        self.assertTrue((self.vault / "wiki/alpha.md").exists())
        self.assertFalse((self.vault / "wiki/beta.md").exists())
        journal_path = next((self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        journal = json.loads(journal_path.read_text())
        result = MODULE.recover(self.vault, journal["batchId"])
        self.assertEqual(result["phase"], "complete")
        self.assertEqual((self.vault / "wiki/beta.md").read_text(), PAGE_B)

    def test_interrupted_record_and_finalization_recover_only_remaining_work(self):
        bundle = self.bundle(self.operation(), self.operation("wiki/beta.md", PAGE_B))
        plan = self.inspect(bundle)
        lifecycle = MODULE._call_lifecycle
        failed = {"once": False}
        def interrupt(vault, path, batch_id, action, config=None, expected_config=None):
            if action == "record" and path.name == "beta.md" and not failed["once"]:
                failed["once"] = True
                raise MODULE.BatchError("simulated record interruption")
            return lifecycle(vault, path, batch_id, action, config, expected_config)
        MODULE._call_lifecycle = interrupt
        try:
            with self.assertRaises(MODULE.BatchError):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._call_lifecycle = lifecycle
        journal_path = next((self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        journal = json.loads(journal_path.read_text())
        self.assertTrue(journal["entries"][0]["recorded"])
        self.assertFalse(journal["entries"][1]["recorded"])
        failed = {"once": False}
        def interrupt_finalize(vault, path, batch_id, action, config=None, expected_config=None):
            if action == "finalize" and not failed["once"]:
                failed["once"] = True
                raise MODULE.BatchError("simulated finalize interruption")
            return lifecycle(vault, path, batch_id, action, config, expected_config)
        MODULE._call_lifecycle = interrupt_finalize
        try:
            with self.assertRaises(MODULE.BatchError):
                MODULE.recover(self.vault, journal["batchId"])
        finally:
            MODULE._call_lifecycle = lifecycle
        self.assertEqual(MODULE.recover(self.vault, journal["batchId"])["phase"], "complete")

    def test_finalization_boundary_prevents_stale_navigation_rollback(self):
        self.config.write_text(json.dumps({"vaultPath": str(self.vault), "features": {"retrievalRefresh": False}}), encoding="utf-8")
        bundle = self.bundle(self.operation("wiki/new.md", PAGE_A))
        plan = self.inspect(bundle)
        save = MODULE._save_journal

        def interrupt_complete(vault, journal):
            if journal["phase"] == "complete":
                raise OSError("simulated interruption after lifecycle finalization")
            return save(vault, journal)

        MODULE._save_journal = interrupt_complete
        try:
            with self.assertRaisesRegex(OSError, "after lifecycle finalization"):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._save_journal = save
        journal = next(json.loads(path.read_text(encoding="utf-8"))
                       for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        self.assertEqual(journal["phase"], "finalizing")
        index = (self.vault / "wiki/index.md").read_text(encoding="utf-8")
        log = (self.vault / "wiki/log.md").read_bytes()
        self.assertIn("new.md", index)
        with self.assertRaisesRegex(MODULE.BatchError, "finalization may have published"):
            MODULE.rollback(self.vault, journal["batchId"])
        self.assertEqual((self.vault / "wiki/new.md").read_text(encoding="utf-8"), PAGE_A)
        recovered = MODULE.recover(self.vault, journal["batchId"])
        self.assertEqual(recovered["phase"], "complete")
        self.assertEqual((self.vault / "wiki/index.md").read_text(encoding="utf-8"), index)
        self.assertEqual((self.vault / "wiki/log.md").read_bytes(), log)

    def test_recovery_refuses_valid_config_identity_drift(self):
        self.config.write_text(json.dumps({"vaultPath": str(self.vault), "features": {"guard": True}}), encoding="utf-8")
        bundle = self.bundle(self.operation())
        plan = self.inspect(bundle)
        save = MODULE._save_journal

        def persist_then_interrupt(vault, journal):
            save(vault, journal)
            if journal["phase"] == "approved":
                raise OSError("simulated stop after approval journal")

        MODULE._save_journal = persist_then_interrupt
        try:
            with self.assertRaises(OSError):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._save_journal = save
        journal = next(json.loads(path.read_text(encoding="utf-8"))
                       for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        alternate = Path(self.temp.name) / "alternate-properties.json"
        alternate.write_text(json.dumps({"vaultPath": str(self.vault), "features": {"guard": True}}), encoding="utf-8")
        cli = subprocess.run([sys.executable, str(CLI), "batch-recover", "--batch-id", journal["batchId"],
                              "--vault", str(self.vault), "--config", str(alternate), "--json"],
                             text=True, capture_output=True)
        self.assertNotEqual(cli.returncode, 0)
        self.assertIn("config identity changed", cli.stderr)
        with self.assertRaisesRegex(MODULE.BatchError, "config identity changed"):
            MODULE.recover(self.vault, journal["batchId"], alternate)
        self.config.write_text(json.dumps({"vaultPath": str(self.vault), "features": {"guard": False}}), encoding="utf-8")
        with self.assertRaisesRegex(MODULE.BatchError, "config identity changed"):
            MODULE.recover(self.vault, journal["batchId"], self.config)
        self.assertFalse((self.vault / "wiki/alpha.md").exists())
        saved = json.loads(next((self.vault / ".vault-meta/lifecycle/batches").glob("*.json")).read_text())
        self.assertEqual(saved["phase"], "approved")

    def test_recovery_rechecks_config_under_lock_before_publication(self):
        self.config.write_text(json.dumps({"vaultPath": str(self.vault), "features": {"guard": True}}), encoding="utf-8")
        bundle = self.bundle(self.operation())
        plan = self.inspect(bundle)
        save = MODULE._save_journal

        def persist_then_interrupt(vault, journal):
            save(vault, journal)
            if journal["phase"] == "approved":
                raise OSError("simulated stop after approved journal")

        MODULE._save_journal = persist_then_interrupt
        try:
            with self.assertRaises(OSError):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._save_journal = save
        journal = next(json.loads(path.read_text(encoding="utf-8"))
                       for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        preflight = MODULE._preflight_recovery

        def change_config_after_preflight(vault, data):
            preflight(vault, data)
            self.config.write_text(json.dumps({"vaultPath": str(self.vault), "features": {"guard": False}}), encoding="utf-8")

        MODULE._preflight_recovery = change_config_after_preflight
        try:
            with self.assertRaisesRegex(MODULE.BatchError, "config identity changed"):
                MODULE.recover(self.vault, journal["batchId"], self.config)
        finally:
            MODULE._preflight_recovery = preflight
        self.assertFalse((self.vault / "wiki/alpha.md").exists())

    def test_batch_finalization_refreshes_bm25_by_config_default_and_replay_is_noop(self):
        modes = ("missing", "omitted", "null", "true", "false")
        for mode in modes:
            with self.subTest(retrievalRefresh=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                vault = root / "vault"
                (vault / "wiki").mkdir(parents=True)
                (vault / "wiki/existing.md").write_text("---\ntype: note\n---\n\n# Existing\n", encoding="utf-8")
                config = root / "properties.json"
                if mode != "missing":
                    settings = {"vaultPath": str(vault)}
                    if mode == "omitted":
                        settings["features"] = {}
                    elif mode == "null":
                        settings["features"] = {"retrievalRefresh": None}
                    else:
                        settings["features"] = {"retrievalRefresh": mode == "true"}
                    config.write_text(json.dumps(settings), encoding="utf-8")
                INDEX.build(vault)
                index = vault / ".vault-meta/retrieval/bm25.json"
                before = index.read_bytes()
                content = "---\ntype: note\n---\n\n# New Entry\n\nrefreshedbatchterm\n"
                bundle = self.bundle(self.operation("wiki/new.md", content))
                plan = MODULE.inspect(vault, bundle, config)
                result = MODULE.apply(vault, bundle, config, plan["planHash"])
                self.assertEqual(result["phase"], "complete")
                refreshed = index.read_bytes()
                enabled = mode != "false"
                if enabled:
                    self.assertNotEqual(refreshed, before)
                    env = {"PATH": __import__("os").environ["PATH"], "HOME": temp}
                    searched = subprocess.run([sys.executable, str(CLI), "search", "refreshedbatchterm",
                                               "--vault", str(vault), "--config", str(config), "--json"],
                                              cwd=temp, env=env, text=True, capture_output=True)
                    self.assertEqual(searched.returncode, 0, searched.stderr)
                    response = json.loads(searched.stdout)
                    self.assertEqual(response["freshness"], "current-by-mtime")
                    self.assertTrue(any(item["path"] == "wiki/new.md" for item in response["results"]))
                else:
                    self.assertEqual(refreshed, before)
                    env = {"PATH": __import__("os").environ["PATH"], "HOME": temp}
                    searched = subprocess.run([sys.executable, str(CLI), "search", "refreshedbatchterm",
                                               "--vault", str(vault), "--config", str(config), "--json"],
                                              cwd=temp, env=env, text=True, capture_output=True)
                    self.assertEqual(searched.returncode, 0, searched.stderr)
                    self.assertEqual(json.loads(searched.stdout)["freshness"], "stale")
                log = (vault / "wiki/log.md").read_bytes()
                index_stat = index.stat().st_mtime_ns
                replay = MODULE.apply(vault, bundle, config, plan["planHash"])
                self.assertTrue(replay["noop"])
                self.assertEqual(index.read_bytes(), refreshed)
                self.assertEqual(index.stat().st_mtime_ns, index_stat)
                self.assertEqual((vault / "wiki/log.md").read_bytes(), log)

    def test_partial_rollback_preserves_concurrent_edit_then_resumes(self):
        bundle = self.bundle(self.operation(), self.operation("wiki/beta.md", PAGE_B))
        plan = self.inspect(bundle)
        lifecycle = MODULE._call_lifecycle
        fail = {"once": False}

        def interrupt_record(vault, path, batch_id, action, config=None, expected_config=None):
            if action == "record" and path.name == "beta.md" and not fail["once"]:
                fail["once"] = True
                raise MODULE.BatchError("leave reviewed batch recoverable before finalization")
            return lifecycle(vault, path, batch_id, action, config, expected_config)

        MODULE._call_lifecycle = interrupt_record
        try:
            with self.assertRaises(MODULE.BatchError):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._call_lifecycle = lifecycle
        journal = next(json.loads(path.read_text()) for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        beta = self.vault / "wiki/beta.md"
        beta.write_text("concurrent app edit\n")
        partial = MODULE.rollback(self.vault, journal["batchId"])
        self.assertFalse(partial["rolledBack"])
        self.assertFalse((self.vault / "wiki/alpha.md").exists())
        self.assertEqual(beta.read_text(), "concurrent app edit\n")
        beta.write_text(PAGE_B)
        final = MODULE.rollback(self.vault, journal["batchId"])
        self.assertTrue(final["rolledBack"])
        self.assertFalse((self.vault / "wiki/alpha.md").exists())
        self.assertFalse(beta.exists())

    def test_rollback_clean_record_acknowledgement_retries_for_preimage_and_absence(self):
        original = "---\ntype: note\n---\n\n# Original\n"
        cases = (("wiki/previous.md", original), ("wiki/created.md", None))
        for raw_path, before in cases:
            with self.subTest(path=raw_path):
                target = self.vault / raw_path
                if before is not None:
                    target.write_text(before, encoding="utf-8")
                after = PAGE_B
                bundle = self.bundle(self.operation(raw_path, after,
                                                    MODULE.sha(before.encode()) if before is not None else None))
                plan = self.inspect(bundle)
                original_call = MODULE._call_lifecycle
                failed = {"once": False}

                def interrupt_apply_record(vault, path, batch_id, action, config=None, expected_config=None):
                    if action == "record" and not failed["once"]:
                        failed["once"] = True
                        raise MODULE.BatchError("simulated stop before forward record")
                    return original_call(vault, path, batch_id, action, config, expected_config)

                MODULE._call_lifecycle = interrupt_apply_record
                try:
                    with self.assertRaises(MODULE.BatchError):
                        MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
                finally:
                    MODULE._call_lifecycle = original_call
                journal = next(json.loads(path.read_text(encoding="utf-8"))
                               for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json")
                               if json.loads(path.read_text(encoding="utf-8"))["bundle"]["operations"][0]["path"] == raw_path)
                fail_save = {"once": False}
                save = MODULE._save_journal

                def interrupt_rollback_ack(vault, data):
                    if data["entries"][0]["state"] == "rolled-back" and data["entries"][0]["recorded"] and not fail_save["once"]:
                        fail_save["once"] = True
                        raise OSError("simulated stop after rollback clean record")
                    return save(vault, data)

                MODULE._save_journal = interrupt_rollback_ack
                try:
                    with self.assertRaisesRegex(OSError, "after rollback clean record"):
                        MODULE.rollback(self.vault, journal["batchId"])
                finally:
                    MODULE._save_journal = save
                journal_file = next(path for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json")
                                    if json.loads(path.read_text(encoding="utf-8"))["batchId"] == journal["batchId"])
                saved = json.loads(journal_file.read_text(encoding="utf-8"))
                self.assertEqual(saved["entries"][0]["state"], "rolled-back")
                self.assertFalse(saved["entries"][0]["recorded"])
                state_path = self.vault / ".vault-meta/lifecycle/state.json"
                self.assertNotIn(str(target), json.loads(state_path.read_text(encoding="utf-8"))["pending"])
                recovered = MODULE.rollback(self.vault, journal["batchId"])
                self.assertTrue(recovered["rolledBack"])
                self.assertEqual(target.read_text(encoding="utf-8") if before is not None else None, before)

    def test_ledger_authority_requires_flat_canonical_paths_before_any_state_or_target_write(self):
        source = EVIDENCE.make_source("https://example.test/source", b"evidence")
        claim = EVIDENCE.make_claim("claim-nested", "Claim", support=[source["id"]])
        for record in (source, claim):
            nested_path = f"wiki/meta/evidence/selected/{record['id']}.json"
            nested = self.bundle(self.operation(nested_path, json.dumps(record, sort_keys=True)))
            before = self.vault_snapshot()
            with self.subTest(kind=record["kind"], phase="inspect"):
                with self.assertRaisesRegex(MODULE.BatchError, "canonical evidence ledger path"):
                    self.inspect(nested, "wiki-ledger")
                self.assertEqual(self.vault_snapshot(), before)
            with self.subTest(kind=record["kind"], phase="apply"):
                with self.assertRaisesRegex(MODULE.BatchError, "canonical evidence ledger path"):
                    MODULE.apply(self.vault, nested, self.config, "a" * 64, "wiki-ledger")
                self.assertEqual(self.vault_snapshot(), before)

        mixed = self.bundle(
            self.operation(EVIDENCE.record_path(source), json.dumps(source, sort_keys=True)),
            self.operation(f"wiki/meta/evidence/selected/{claim['id']}.json", json.dumps(claim, sort_keys=True)),
        )
        before = self.vault_snapshot()
        with self.assertRaisesRegex(MODULE.BatchError, "canonical evidence ledger path"):
            self.inspect(mixed, "wiki-ledger")
        with self.assertRaisesRegex(MODULE.BatchError, "canonical evidence ledger path"):
            MODULE.apply(self.vault, mixed, self.config, "b" * 64, "wiki-ledger")
        self.assertEqual(self.vault_snapshot(), before)
        self.assertFalse((self.vault / "wiki/meta").exists())
        self.assertFalse((self.vault / ".vault-meta").exists())

    def test_authorities_are_fixed_and_scoped(self):
        capture = self.bundle(self.operation(".raw/agent-captures/" + "a" * 64 + ".md", "# captured\n"))
        plan = self.inspect(capture, "source-capture")
        MODULE.apply(self.vault, capture, self.config, plan["planHash"], "source-capture")
        text_record = self.bundle(self.operation(".raw/agent-captures/" + "b" * 64 + ".txt", "plain text\n"))
        text_plan = self.inspect(text_record, "source-capture")
        MODULE.apply(self.vault, text_record, self.config, text_plan["planHash"], "source-capture")
        with self.assertRaises(MODULE.BatchError):
            self.inspect(self.bundle(self.operation(".raw/other.md", "x")), "source-capture")
        with self.assertRaises(MODULE.BatchError):
            self.inspect(self.bundle(self.operation(".raw/agent-captures/x.md", "changed", "0" * 64)), "source-capture")
        source = EVIDENCE.make_source("https://example.test/source", b"evidence")
        ledger = self.bundle(self.operation(EVIDENCE.record_path(source), json.dumps(source, sort_keys=True)))
        plan = self.inspect(ledger, "wiki-ledger")
        MODULE.apply(self.vault, ledger, self.config, plan["planHash"], "wiki-ledger")

    def test_symlink_and_case_collision_refused(self):
        (self.vault / "wiki/Elsewhere.md").write_text("existing")
        with self.assertRaises(MODULE.BatchError):
            self.inspect(self.bundle(self.operation("wiki/elsewhere.md")))
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.vault / "wiki/linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(MODULE.BatchError):
            self.inspect(self.bundle(self.operation("wiki/linked/a.md")))

    def test_rollback_refuses_to_clobber_concurrent_edit_and_restores_preimage(self):
        target = self.vault / "wiki/alpha.md"
        original = "---\ntype: note\n---\n\n# Before\n"
        target.write_text(original)
        bundle = self.bundle(self.operation(expected=MODULE.sha(original.encode()), content=PAGE_A))
        plan = self.inspect(bundle)
        result = MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        target.write_text("app edit\n")
        with self.assertRaises(MODULE.BatchError):
            MODULE.rollback(self.vault, result["batchId"])
        self.assertEqual(target.read_text(), "app edit\n")

    def test_config_identity_and_cli_exact_approval(self):
        bundle = self.bundle(self.operation())
        plan = self.inspect(bundle)
        self.config.write_text('{"vaultPath":"elsewhere"}')
        with self.assertRaises(MODULE.BatchError):
            MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        self.config.unlink()
        path = Path(self.temp.name) / "bundle.json"
        path.write_text(json.dumps(bundle))
        cli = Path(__file__).resolve().parents[1] / "scripts/obsidian-second-brain.py"
        inspected = subprocess.run([sys.executable, str(cli), "batch-inspect", "--vault", str(self.vault),
                                    "--config", str(self.config), "--bundle", str(path), "--json"], text=True, capture_output=True)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        plan_hash = json.loads(inspected.stdout)["planHash"]
        applied = subprocess.run([sys.executable, str(cli), "batch-apply", "--vault", str(self.vault),
                                  "--config", str(self.config), "--bundle", str(path), "--plan-hash", plan_hash, "--json"], text=True, capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)

    def test_invalid_config_fails_closed_for_direct_planning_and_apply(self):
        bundle = self.bundle(self.operation())
        plan = self.inspect(bundle)
        invalid_configs = (
            '{"vaultPath":null,"features":{"guard":"bad"}}',
            '{invalid json',
            '[]',
        )
        for invalid in invalid_configs:
            with self.subTest(config=invalid):
                self.config.write_text(invalid, encoding="utf-8")
                with self.assertRaises(MODULE.BatchError):
                    MODULE.inspect(self.vault, bundle, self.config)
                with self.assertRaises(MODULE.BatchError):
                    MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
                self.assertFalse((self.vault / "wiki/alpha.md").exists())
                self.assertFalse((self.vault / ".vault-meta").exists())
        self.config.unlink()
        # Missing config plus an explicit vault remains a supported boundary.
        fresh = self.inspect(bundle)
        self.assertEqual(MODULE.apply(self.vault, bundle, self.config, fresh["planHash"])["phase"], "complete")

    def test_invalid_config_blocks_direct_recovery_and_rollback(self):
        bundle = self.bundle(self.operation())
        plan = self.inspect(bundle)
        save = MODULE._save_journal
        def persist_then_interrupt(vault, journal):
            save(vault, journal)
            if journal["phase"] == "approved":
                raise OSError("simulated stop before first publication")
        MODULE._save_journal = persist_then_interrupt
        try:
            with self.assertRaises(OSError):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._save_journal = save
        journal = next(json.loads(path.read_text(encoding="utf-8"))
                       for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        self.config.write_text('{"vaultPath":null,"features":{"guard":"bad"}}', encoding="utf-8")
        with self.assertRaisesRegex(MODULE.BatchError, "invalid selected integration config"):
            MODULE.recover(self.vault, journal["batchId"])
        with self.assertRaisesRegex(MODULE.BatchError, "invalid selected integration config"):
            MODULE.rollback(self.vault, journal["batchId"])
        self.assertFalse((self.vault / "wiki/alpha.md").exists())
        saved = json.loads(next((self.vault / ".vault-meta/lifecycle/batches").glob("*.json")).read_text())
        self.assertEqual(saved["phase"], "approved")

    def test_recovery_preflights_all_targets_before_any_publication(self):
        bundle = self.bundle(self.operation(), self.operation("wiki/beta.md", PAGE_B))
        plan = self.inspect(bundle)
        save = MODULE._save_journal
        interrupted = {"once": False}

        def save_then_interrupt(vault, journal):
            save(vault, journal)
            if journal["phase"] == "approved" and not interrupted["once"]:
                interrupted["once"] = True
                raise OSError("simulated interruption after approved journal persisted")

        MODULE._save_journal = save_then_interrupt
        try:
            with self.assertRaises(OSError):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._save_journal = save
        journal = next(json.loads(path.read_text(encoding="utf-8"))
                       for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        (self.vault / "wiki/beta.md").write_text("concurrent edit\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.BatchError, "recovery preflight"):
            MODULE.recover(self.vault, journal["batchId"])
        self.assertFalse((self.vault / "wiki/alpha.md").exists())
        self.assertEqual((self.vault / "wiki/beta.md").read_text(encoding="utf-8"), "concurrent edit\n")
        self.assertFalse((self.vault / ".vault-meta/lifecycle/state.json").exists())

    def test_clean_lifecycle_record_acknowledgement_can_be_recovered(self):
        target = self.vault / "wiki/alpha.md"
        target.write_text(PAGE_A, encoding="utf-8")
        log = self.vault / "wiki/log.md"
        log.write_bytes(b"existing log\n")
        bundle = self.bundle(self.operation(expected=MODULE.sha(PAGE_A.encode())))
        plan = self.inspect(bundle)
        save = MODULE._save_journal
        interrupted = {"once": False}

        def interrupt_record_ack(vault, journal):
            if journal["entries"][0]["recorded"] and not interrupted["once"]:
                interrupted["once"] = True
                raise OSError("simulated interruption after clean record")
            return save(vault, journal)

        MODULE._save_journal = interrupt_record_ack
        try:
            with self.assertRaisesRegex(OSError, "after clean record"):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._save_journal = save
        journal = next(json.loads(path.read_text(encoding="utf-8"))
                       for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        self.assertFalse(journal["entries"][0]["recorded"])
        state_path = self.vault / ".vault-meta/lifecycle/state.json"
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["pending"], {})
        recovered = MODULE.recover(self.vault, journal["batchId"])
        self.assertEqual(recovered["phase"], "complete")
        self.assertEqual(target.read_text(encoding="utf-8"), PAGE_A)
        self.assertEqual(log.read_bytes(), b"existing log\n")

    def test_clean_record_retry_does_not_acknowledge_foreign_owner_state(self):
        target = self.vault / "wiki/alpha.md"
        target.write_text(PAGE_A, encoding="utf-8")
        bundle = self.bundle(self.operation(expected=MODULE.sha(PAGE_A.encode())))
        plan = self.inspect(bundle)
        save = MODULE._save_journal
        interrupted = {"once": False}

        def interrupt_record_ack(vault, journal):
            if journal["entries"][0]["recorded"] and not interrupted["once"]:
                interrupted["once"] = True
                raise OSError("simulated interruption after clean record")
            return save(vault, journal)

        MODULE._save_journal = interrupt_record_ack
        try:
            with self.assertRaises(OSError):
                MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            MODULE._save_journal = save
        journal = next(json.loads(path.read_text(encoding="utf-8"))
                       for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        MODULE.LIFECYCLE.capture(argparse.Namespace(vault=str(self.vault), cwd=str(self.vault),
                                                    path=str(target), owner="foreign", tool="other"))
        with self.assertRaisesRegex(MODULE.BatchError, "does not match a captured owner/tool pair"):
            MODULE.recover(self.vault, journal["batchId"])
        self.assertFalse(journal["entries"][0]["recorded"])
        self.assertEqual(target.read_text(encoding="utf-8"), PAGE_A)

    def test_git_state_must_be_effectively_ignored_before_journaling(self):
        subprocess.run(["git", "init", "-q", str(self.vault)], check=True)
        bundle = self.bundle(self.operation())
        plan = self.inspect(bundle)
        with self.assertRaises(MODULE.BatchError):
            MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        self.assertFalse((self.vault / "wiki/alpha.md").exists())
        (self.vault / ".gitignore").write_text("/.vault-meta/lifecycle/\n")
        applied = MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        self.assertEqual(applied["phase"], "complete")

    def test_foreign_lifecycle_owner_blocks_before_any_batch_write(self):
        page = self.vault / "wiki/alpha.md"
        MODULE.LIFECYCLE.capture(argparse.Namespace(vault=str(self.vault), cwd=str(self.vault), path=str(page), owner="foreign", tool="other"))
        bundle = self.bundle(self.operation(), self.operation("wiki/beta.md", PAGE_B))
        plan = self.inspect(bundle)
        with self.assertRaises(MODULE.BatchError):
            MODULE.apply(self.vault, bundle, self.config, plan["planHash"])
        self.assertFalse(page.exists())
        self.assertFalse((self.vault / "wiki/beta.md").exists())


if __name__ == "__main__":
    unittest.main()
