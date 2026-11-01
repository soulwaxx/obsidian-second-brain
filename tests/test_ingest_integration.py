from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source
CLI = ROOT / "scripts/obsidian-second-brain.py"
PAGE_A = "---\ntype: note\n---\n\n# Alpha\n"
PAGE_B = "---\ntype: note\n---\n\n# Beta\n"


class IngestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        self.config = self.root / "properties.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        self.source = self.root / "selected.txt"
        self.source.write_bytes(b"Selected local text; preserve these bytes.\n")
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "OBSIDIAN_AGENT_CONFIG": str(self.config)}

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.root, env=self.env,
                              text=True, capture_output=True)

    def test_reviewed_page_batch_then_capture_replay_preserves_separate_authorities(self):
        bundle = {"version": 1, "operations": [
            {"path": "wiki/alpha.md", "expectedHash": None, "content": PAGE_A},
            {"path": "wiki/beta.md", "expectedHash": None, "content": PAGE_B},
        ]}
        bundle_path = self.root / "bundle.json"
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        inspected = self.cli("batch-inspect", "--bundle", bundle_path, "--vault", self.vault,
                             "--config", self.config, "--json")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        batch_plan = json.loads(inspected.stdout)
        self.assertEqual(batch_plan["targets"], ["wiki/alpha.md", "wiki/beta.md"])
        applied = self.cli("batch-apply", "--bundle", bundle_path, "--vault", self.vault,
                           "--config", self.config, "--plan-hash", batch_plan["planHash"], "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((self.vault / "wiki/alpha.md").read_text(), PAGE_A)
        self.assertEqual((self.vault / "wiki/beta.md").read_text(), PAGE_B)
        self.assertIn("alpha.md", (self.vault / "wiki/index.md").read_text())
        self.assertIn("beta.md", (self.vault / "wiki/index.md").read_text())
        original_log = (self.vault / "wiki/log.md").read_bytes()
        self.assertIn(b"alpha.md", original_log)
        self.assertIn(b"beta.md", original_log)

        capture_review = self.cli("capture-inspect", "--source", self.source,
                                  "--page", "wiki/alpha.md", "--json")
        self.assertEqual(capture_review.returncode, 0, capture_review.stderr)
        capture_plan = json.loads(capture_review.stdout)
        self.assertNotIn(str(self.source), capture_review.stdout)
        capture_apply = self.cli("capture-apply", "--source", self.source, "--page", "wiki/alpha.md",
                                 "--plan-hash", capture_plan["planHash"], "--json")
        self.assertEqual(capture_apply.returncode, 0, capture_apply.stderr)
        capture_result = json.loads(capture_apply.stdout)
        self.assertFalse(capture_result["noop"])
        payload = self.vault / capture_result["targets"][0]
        self.assertEqual(payload.read_bytes(), self.source.read_bytes())
        self.assertEqual(payload.stem, __import__("hashlib").sha256(self.source.read_bytes()).hexdigest())
        record = self.vault / capture_result["targets"][1]
        self.assertIn("[[wiki/alpha]]", record.read_text(encoding="utf-8"))

        # Raw-source capture is not a wiki-page lifecycle write: it preserves the
        # wiki log and its retry remains an exact no-op.
        self.assertEqual((self.vault / "wiki/log.md").read_bytes(), original_log)
        retry_review = self.cli("capture-inspect", "--source", self.source, "--page", "wiki/alpha.md", "--json")
        self.assertEqual(retry_review.returncode, 0, retry_review.stderr)
        self.assertTrue(json.loads(retry_review.stdout)["noop"])
        retry_apply = self.cli("capture-apply", "--source", self.source, "--page", "wiki/alpha.md",
                               "--plan-hash", capture_plan["planHash"], "--json")
        self.assertEqual(retry_apply.returncode, 0, retry_apply.stderr)
        self.assertTrue(json.loads(retry_apply.stdout)["noop"])
        self.assertEqual((self.vault / "wiki/log.md").read_bytes(), original_log)

        # The ordinary page authority cannot be widened to capture destinations.
        raw_bundle = {"version": 1, "operations": [
            {"path": ".raw/agent-captures/not-authorized.txt", "expectedHash": None, "content": "no\n"}
        ]}
        raw_path = self.root / "raw-bundle.json"
        raw_path.write_text(json.dumps(raw_bundle), encoding="utf-8")
        denied = self.cli("batch-inspect", "--bundle", raw_path, "--vault", self.vault,
                          "--config", self.config, "--authority", "wiki-page", "--json")
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse((self.vault / ".raw/agent-captures/not-authorized.txt").exists())

    def test_observation_cli_inspects_and_publishes_selected_source_records(self):
        selection = self.root / "selection.json"
        selection.write_text(json.dumps({"version": 1, "rootId": "research", "assetId": "asset-notes",
                                         "locator": "selected.txt", "observedAt": "2025-01-02T03:04:05Z"}), encoding="utf-8")
        inspected = self.cli("observe-inspect", "--selection", selection, "--source-root", self.root,
                             "--vault", self.vault, "--config", self.config, "--json")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        plan = json.loads(inspected.stdout)
        self.assertEqual(plan["authority"], "wiki-ledger")
        self.assertFalse((self.vault / ".vault-meta").exists())
        applied = self.cli("observe-apply", "--selection", selection, "--source-root", self.root,
                           "--vault", self.vault, "--config", self.config,
                           "--plan-hash", plan["planHash"], "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        records = list((self.vault / "wiki/meta/evidence").glob("*.json"))
        self.assertEqual(len(records), 3)
        self.assertNotIn(str(self.root), " ".join(path.read_text(encoding="utf-8") for path in records))
        journal_dir = self.vault / ".vault-meta/lifecycle/batches"
        journal_count = len(list(journal_dir.glob("*.json")))
        exact_replay = self.cli("observe-apply", "--selection", selection, "--source-root", self.root,
                                "--vault", self.vault, "--config", self.config,
                                "--plan-hash", plan["planHash"], "--json")
        self.assertEqual(exact_replay.returncode, 0, exact_replay.stderr + exact_replay.stdout)
        self.assertTrue(json.loads(exact_replay.stdout)["noop"])
        self.assertEqual(len(list(journal_dir.glob("*.json"))), journal_count)
        retry = self.cli("observe-inspect", "--selection", selection, "--source-root", self.root,
                         "--vault", self.vault, "--config", self.config, "--json")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        retry_plan = json.loads(retry.stdout)
        self.assertTrue(retry_plan["noop"])
        retry_apply = self.cli("observe-apply", "--selection", selection, "--source-root", self.root,
                               "--vault", self.vault, "--config", self.config,
                               "--plan-hash", retry_plan["planHash"], "--json")
        self.assertEqual(retry_apply.returncode, 0, retry_apply.stderr)
        self.assertTrue(json.loads(retry_apply.stdout)["noop"])
        other_root = self.root / "other-root"
        other_root.mkdir()
        (other_root / "selected.txt").write_bytes(self.source.read_bytes())
        refused = self.cli("observe-apply", "--selection", selection, "--source-root", other_root,
                           "--vault", self.vault, "--config", self.config,
                           "--plan-hash", plan["planHash"], "--json")
        self.assertNotEqual(refused.returncode, 0)

    def test_baseline_requires_completed_page_batch_and_accepts_no_change_receipt(self):
        page = self.vault / "wiki/alpha.md"
        page_content = PAGE_A
        page.write_text(page_content, encoding="utf-8")
        page_bundle = {"version": 1, "operations": [{"path": "wiki/alpha.md",
                       "expectedHash": __import__("hashlib").sha256(page_content.encode()).hexdigest(),
                       "content": page_content}]}
        bundle_path = self.root / "page-batch.json"
        bundle_path.write_text(json.dumps(page_bundle), encoding="utf-8")
        page_review = self.cli("batch-inspect", "--bundle", bundle_path, "--vault", self.vault,
                               "--config", self.config, "--json")
        self.assertEqual(page_review.returncode, 0, page_review.stderr)
        page_result = self.cli("batch-apply", "--bundle", bundle_path, "--vault", self.vault,
                               "--config", self.config, "--plan-hash", json.loads(page_review.stdout)["planHash"], "--json")
        self.assertEqual(page_result.returncode, 0, page_result.stderr)
        observation_selection = self.root / "observation.json"
        observation_selection.write_text(json.dumps({"version": 1, "rootId": "research", "assetId": "asset-notes",
            "locator": "selected.txt", "observedAt": "2025-01-02T03:04:05Z"}), encoding="utf-8")
        observation_review = self.cli("observe-inspect", "--selection", observation_selection, "--source-root", self.root,
                                      "--vault", self.vault, "--config", self.config, "--json")
        self.assertEqual(observation_review.returncode, 0, observation_review.stderr)
        observation_result = self.cli("observe-apply", "--selection", observation_selection, "--source-root", self.root,
            "--vault", self.vault, "--config", self.config, "--plan-hash", json.loads(observation_review.stdout)["planHash"], "--json")
        self.assertEqual(observation_result.returncode, 0, observation_result.stderr + observation_result.stdout)
        observation = next(json.loads(path.read_text()) for path in (self.vault / "wiki/meta/evidence").glob("*.json")
                           if json.loads(path.read_text()).get("kind") == "observation")
        ledger = load_source("baseline_bypass_test_ledger", str(ROOT / "scripts/evidence_ledger.py"))
        invalid_entry = {"observationId": observation["id"], "sourceId": observation["sourceId"],
                         "contentHash": observation["contentHash"], "pageHash": "sha256:" + "0" * 64,
                         "pageBatchId": "00000000-0000-0000-0000-000000000000",
                         "reconciledAt": "2025-01-03T03:04:05Z", "outcome": "no-change"}
        bypass = {"version": 1, "operations": [ledger.operation(ledger.make_baseline("wiki/alpha.md",
                  observation["assetId"], [invalid_entry]))]}
        bypass_path = self.root / "bypass.json"
        bypass_path.write_text(json.dumps(bypass), encoding="utf-8")
        denied = self.cli("batch-inspect", "--bundle", bypass_path, "--vault", self.vault,
                          "--config", self.config, "--authority", "wiki-ledger", "--json")
        self.assertNotEqual(denied.returncode, 0)
        selection = self.root / "baseline.json"
        selection.write_text(json.dumps({"version": 1, "reconciliations": [{"page": "wiki/alpha.md",
            "observationId": observation["id"], "pageBatchId": json.loads(page_result.stdout)["batchId"],
            "reconciledAt": "2025-01-03T03:04:05Z", "outcome": "no-change"}]}), encoding="utf-8")
        reviewed = self.cli("baseline-inspect", "--selection", selection, "--vault", self.vault,
                            "--config", self.config, "--json")
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr + reviewed.stdout)
        published = self.cli("baseline-apply", "--selection", selection, "--vault", self.vault,
            "--config", self.config, "--plan-hash", json.loads(reviewed.stdout)["planHash"], "--json")
        self.assertEqual(published.returncode, 0, published.stderr)
        ledger_batch_count = len(list((self.vault / ".vault-meta/lifecycle/batches").glob("*.json")))
        exact_replay = self.cli("baseline-apply", "--selection", selection, "--vault", self.vault,
            "--config", self.config, "--plan-hash", json.loads(reviewed.stdout)["planHash"], "--json")
        self.assertEqual(exact_replay.returncode, 0, exact_replay.stderr + exact_replay.stdout)
        self.assertTrue(json.loads(exact_replay.stdout)["noop"])
        self.assertEqual(len(list((self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))), ledger_batch_count)
        noop_review = self.cli("baseline-inspect", "--selection", selection, "--vault", self.vault,
                               "--config", self.config, "--json")
        self.assertEqual(noop_review.returncode, 0, noop_review.stderr + noop_review.stdout)
        self.assertTrue(json.loads(noop_review.stdout)["noop"])
        noop_apply = self.cli("baseline-apply", "--selection", selection, "--vault", self.vault,
            "--config", self.config, "--plan-hash", json.loads(noop_review.stdout)["planHash"], "--json")
        self.assertEqual(noop_apply.returncode, 0, noop_apply.stderr + noop_apply.stdout)
        self.assertTrue(json.loads(noop_apply.stdout)["noop"])
        self.assertEqual(len(list((self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))), ledger_batch_count)
        source_path = next(path for path in (self.vault / "wiki/meta/evidence").glob("*.json")
                           if json.loads(path.read_text()).get("kind") == "source")
        source_preimage = source_path.read_bytes()
        selected_source = json.loads(source_preimage)
        selected_source["contentHash"] = "sha256:" + "0" * 64
        source_path.write_text(json.dumps(selected_source), encoding="utf-8")
        corrupted_review = self.cli("baseline-inspect", "--selection", selection, "--vault", self.vault,
                                    "--config", self.config, "--json")
        self.assertNotEqual(corrupted_review.returncode, 0, corrupted_review.stdout)
        source_path.write_bytes(source_preimage)
        baseline = next(json.loads(path.read_text()) for path in (self.vault / "wiki/meta/evidence").glob("*.json")
                        if json.loads(path.read_text()).get("kind") == "baseline")
        self.assertEqual(baseline["history"][0]["outcome"], "no-change")
        evidence_dir = self.vault / "wiki/meta/evidence"
        source_path = next(path for path in evidence_dir.glob("*.json") if json.loads(path.read_text()).get("kind") == "source")
        source_preimage = source_path.read_bytes()
        changed_source = json.loads(source_preimage)
        changed_source["locator"] = "local:research/elsewhere.txt"
        source_path.write_text(json.dumps(changed_source), encoding="utf-8")
        source_replay = self.cli("batch-recover", "--batch-id", json.loads(published.stdout)["batchId"],
                                "--vault", self.vault, "--config", self.config, "--json")
        self.assertNotEqual(source_replay.returncode, 0, source_replay.stdout)
        source_path.write_bytes(source_preimage)
        observation_path = next(path for path in evidence_dir.glob("*.json")
                               if json.loads(path.read_text()).get("kind") == "observation")
        observation_preimage = observation_path.read_bytes()
        changed_observation = json.loads(observation_preimage)
        changed_observation["locator"] = "elsewhere.txt"
        observation_path.write_text(json.dumps(changed_observation), encoding="utf-8")
        observation_replay = self.cli("batch-recover", "--batch-id", json.loads(published.stdout)["batchId"],
                                      "--vault", self.vault, "--config", self.config, "--json")
        self.assertNotEqual(observation_replay.returncode, 0, observation_replay.stdout)
        observation_path.write_bytes(observation_preimage)
        page.write_text(PAGE_A + "\nDrift after reconciliation.\n", encoding="utf-8")
        drifted_review = self.cli("baseline-inspect", "--selection", selection, "--vault", self.vault,
                                  "--config", self.config, "--json")
        self.assertNotEqual(drifted_review.returncode, 0)
        self.assertEqual(drifted_review.stderr, "")
        self.assertEqual(json.loads(drifted_review.stdout)["code"], "command-error")
        replay = self.cli("batch-recover", "--batch-id", json.loads(published.stdout)["batchId"],
                          "--vault", self.vault, "--config", self.config, "--json")
        self.assertNotEqual(replay.returncode, 0, replay.stdout)

    def test_observation_and_baseline_invalid_config_fail_without_vault_writes(self):
        source_selection = self.root / "observation-selection.json"
        source_selection.write_text(json.dumps({"version": 1, "rootId": "research", "assetId": "asset-notes",
            "locator": "selected.txt", "observedAt": "2025-01-02T03:04:05Z"}), encoding="utf-8")
        baseline_selection = self.root / "baseline-selection.json"
        baseline_selection.write_text(json.dumps({"version": 1, "reconciliations": []}), encoding="utf-8")
        before = {path.relative_to(self.vault).as_posix(): path.read_bytes()
                  for path in self.vault.rglob("*") if path.is_file()}
        missing = self.cli("observe-inspect", "--source-root", self.root, "--vault", self.vault,
                           "--config", self.config, "--json")
        self.assertNotEqual(missing.returncode, 0)
        after_missing = {path.relative_to(self.vault).as_posix(): path.read_bytes()
                         for path in self.vault.rglob("*") if path.is_file()}
        self.assertEqual(after_missing, before)
        self.config.write_text("{malformed", encoding="utf-8")
        for command, selection in (("observe-inspect", source_selection), ("baseline-inspect", baseline_selection)):
            result = self.cli(command, "--selection", selection, "--source-root", self.root,
                              "--vault", self.vault, "--config", self.config, "--json")
            self.assertNotEqual(result.returncode, 0)
        after = {path.relative_to(self.vault).as_posix(): path.read_bytes()
                 for path in self.vault.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((self.vault / ".vault-meta").exists())

    def test_batch_and_capture_cli_stop_on_invalid_selected_config(self):
        bundle = {"version": 1, "operations": [
            {"path": "wiki/blocked.md", "expectedHash": None, "content": PAGE_A},
        ]}
        bundle_path = self.root / "invalid-config-bundle.json"
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        batch_preview = self.cli("batch-inspect", "--bundle", bundle_path, "--vault", self.vault,
                                 "--config", self.config, "--json")
        capture_preview = self.cli("capture-inspect", "--source", self.source, "--json")
        self.assertEqual(batch_preview.returncode, 0, batch_preview.stderr)
        self.assertEqual(capture_preview.returncode, 0, capture_preview.stderr)
        batch_hash = json.loads(batch_preview.stdout)["planHash"]
        capture_hash = json.loads(capture_preview.stdout)["planHash"]

        for invalid in (
            '{"vaultPath":null,"features":{"guard":"bad"}}',
            '{malformed json',
            '[]',
        ):
            with self.subTest(config=invalid):
                self.config.write_text(invalid, encoding="utf-8")
                doctor = self.cli("doctor", "--vault", self.vault, "--config", self.config, "--json")
                self.assertEqual(doctor.returncode, 1)
                self.assertFalse(json.loads(doctor.stdout)["config"]["valid"])
                search = self.cli("search", "anything", "--vault", self.vault, "--config", self.config, "--json")
                self.assertEqual(search.returncode, 0, search.stderr)
                commands = (
                    ("batch-inspect", "--bundle", bundle_path, "--vault", self.vault,
                     "--config", self.config, "--json"),
                    ("batch-apply", "--bundle", bundle_path, "--vault", self.vault,
                     "--config", self.config, "--plan-hash", batch_hash, "--json"),
                    ("capture-inspect", "--source", self.source, "--json"),
                    ("batch-recover", "--batch-id", "00000000-0000-0000-0000-000000000000",
                     "--vault", self.vault, "--json"),
                    ("batch-rollback", "--batch-id", "00000000-0000-0000-0000-000000000000",
                     "--vault", self.vault, "--json"),
                    ("capture-apply", "--source", self.source, "--plan-hash", capture_hash, "--json"),
                )
                for command in commands:
                    result = self.cli(*command)
                    self.assertNotEqual(result.returncode, 0, command)
                    self.assertEqual(result.stderr, "")
                    error = json.loads(result.stdout)
                    self.assertEqual(error["code"], "command-error")
                    self.assertIn("config", error["error"].lower())
                self.assertFalse((self.vault / "wiki/blocked.md").exists())
                self.assertFalse((self.vault / ".raw").exists())
                self.assertFalse((self.vault / ".vault-meta").exists())

    def test_cli_recovers_interrupted_reviewed_batch_without_overwriting_app_edit(self):
        batch = load_source("ingest_integration_batch", str(ROOT / "scripts/wiki_batch.py"))
        bundle = {"version": 1, "operations": [
            {"path": "wiki/alpha.md", "expectedHash": None, "content": PAGE_A},
            {"path": "wiki/beta.md", "expectedHash": None, "content": PAGE_B},
        ]}
        bundle_path = self.root / "recover-bundle.json"
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        plan = batch.inspect(self.vault, bundle, self.config)
        original_call = batch._call_lifecycle
        failed = {"once": False}

        def interrupt_finalize(vault, path, batch_id, action, config=None, expected_config=None):
            if action == "finalize" and not failed["once"]:
                failed["once"] = True
                raise batch.BatchError("simulated interruption before lifecycle finalization")
            return original_call(vault, path, batch_id, action, config, expected_config)

        batch._call_lifecycle = interrupt_finalize
        try:
            with self.assertRaises(batch.BatchError):
                batch.apply(self.vault, bundle, self.config, plan["planHash"])
        finally:
            batch._call_lifecycle = original_call
        journal = next(json.loads(path.read_text(encoding="utf-8"))
                       for path in (self.vault / ".vault-meta/lifecycle/batches").glob("*.json"))
        self.assertEqual(journal["phase"], "finalizing")

        # An app edit between interruption and recovery is retained and reported.
        beta = self.vault / "wiki/beta.md"
        beta.write_text("concurrent Obsidian edit\n", encoding="utf-8")
        blocked = self.cli("batch-recover", "--batch-id", journal["batchId"], "--vault", self.vault, "--json")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(blocked.stderr, "")
        error = json.loads(blocked.stdout)
        self.assertEqual(error["code"], "command-error")
        self.assertIn("conflict", error["error"])
        self.assertEqual(beta.read_text(encoding="utf-8"), "concurrent Obsidian edit\n")

        beta.write_text(PAGE_B, encoding="utf-8")
        recovered = self.cli("batch-recover", "--batch-id", journal["batchId"], "--vault", self.vault, "--json")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["phase"], "complete")
        finalized_log = (self.vault / "wiki/log.md").read_bytes()
        replay = self.cli("batch-apply", "--bundle", bundle_path, "--vault", self.vault,
                          "--config", self.config, "--plan-hash", plan["planHash"], "--json")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertTrue(json.loads(replay.stdout)["noop"])
        self.assertEqual((self.vault / "wiki/log.md").read_bytes(), finalized_log)
        self.assertIn(b"alpha.md", finalized_log)
        self.assertIn(b"beta.md", finalized_log)


if __name__ == "__main__":
    unittest.main()
