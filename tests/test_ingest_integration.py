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
                    self.assertIn("config", result.stderr.lower())
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
        self.assertIn("conflict", blocked.stderr)
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
