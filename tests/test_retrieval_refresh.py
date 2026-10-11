"""Durable refresh regression tests, using disposable vaults only."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import wiki_lifecycle as L
from module_loading import load_source
B = load_source("refresh_bm25", str(ROOT / "scripts/bm25-index.py"))
CLI = load_source("refresh_cli", str(ROOT / "scripts/obsidian-second-brain.py"))
MW = ROOT / "skills/wiki/scripts/okf_mw"


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / "wiki").mkdir()
        self.page = self.vault / "wiki/note.md"
        self.args = argparse.Namespace(vault=str(self.vault), middleware=str(MW),
                                      retrieval_script=str(ROOT / "scripts/bm25-index.py"),
                                      owner="writer", recover_owner=None)

    def write(self, text="Fresh value", tool="one"):
        args = argparse.Namespace(vault=str(self.vault), cwd=str(self.vault), path=str(self.page),
                                  owner="writer", tool=tool, validator=str(MW / "validate.py"))
        L.capture(args)
        self.page.write_text("---\ntype: note\n---\n# Note\n\n" + text + "\n")
        L.record(args)

    def state(self):
        return json.loads((self.vault / ".vault-meta/lifecycle/state.json").read_text())

    def finalize(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            L.finalize(self.args)
        return out.getvalue().strip()

    def failing_build(self):
        original = subprocess.run
        def run(command, **kwargs):
            if command[1] == self.args.retrieval_script:
                return subprocess.CompletedProcess(command, 1, "", "fixture failure")
            return original(command, **kwargs)
        return run

    def test_failure_retains_retry_then_explicit_build_acknowledges_without_log_rewrite(self):
        self.write()
        with patch.object(L.subprocess, "run", side_effect=self.failing_build()):
            self.assertEqual(self.finalize(), "synced-retrieval-pending")
        state = self.state()
        self.assertFalse(state["pending"])
        self.assertIn("retrieval", state)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.vault / "wiki").rglob("*.md")}
        # Simulate restart: retry reads only persisted state, not in-memory work.
        self.assertEqual(self.finalize(), "synced")
        self.assertNotIn("retrieval", self.state())
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.vault / "wiki").rglob("*.md")})
        self.write("Second value", "two")
        with patch.object(L.subprocess, "run", side_effect=self.failing_build()):
            self.finalize()
        subprocess.run([sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault)], check=True, capture_output=True)
        self.assertNotIn("retrieval", self.state())

    def test_obligation_persisted_before_build_and_noop_coalesced(self):
        self.write("First")
        self.write("Second", "two")
        original = subprocess.run
        calls = []
        def run(command, **kwargs):
            if command[1] == self.args.retrieval_script:
                state = self.state()
                self.assertFalse(state["pending"])
                self.assertTrue(state["retrieval"]["token"])
                calls.append(command)
            return original(command, **kwargs)
        with patch.object(L.subprocess, "run", side_effect=run):
            self.assertEqual(self.finalize(), "synced")
            self.assertEqual(self.finalize(), "clean")
        self.assertEqual(len(calls), 1)

    def test_disabled_changed_batch_and_tracked_rename_deletion(self):
        self.write()
        self.args.retrieval_script = None
        with patch.object(L, "refresh_retrieval", side_effect=AssertionError("disabled refresh")):
            self.assertEqual(self.finalize(), "synced")
        self.assertFalse((self.vault / B.DEFAULT_INDEX).exists())
        self.assertNotIn("retrieval", self.state())
        self.args.retrieval_script = str(ROOT / "scripts/bm25-index.py")
        moved = self.vault / "wiki/renamed.md"
        captures = []
        for path, tool in [(self.page, "remove-old"), (moved, "create-new")]:
            args = argparse.Namespace(vault=str(self.vault), cwd=str(self.vault), path=str(path),
                                      owner="writer", tool=tool, validator=str(MW / "validate.py"))
            L.capture(args)
            captures.append(args)
        self.page.rename(moved)
        for args in captures:
            L.record(args)
        self.assertEqual(self.finalize(), "synced")
        self.assertEqual(set(B.load_index(self.vault / B.DEFAULT_INDEX)["lengths"]), {"wiki/renamed.md"})
        deletion = argparse.Namespace(**{**vars(captures[1]), "tool": "delete-new"})
        L.capture(deletion)
        moved.unlink()
        L.record(deletion)
        self.assertEqual(self.finalize(), "synced")
        self.assertFalse(B.load_index(self.vault / B.DEFAULT_INDEX)["lengths"])

    def test_newer_generation_or_active_capture_cannot_be_acknowledged(self):
        self.write()
        with patch.object(L.subprocess, "run", side_effect=self.failing_build()):
            self.finalize()
        old = self.state()["retrieval"]["token"]
        B.build(self.vault)
        self.write("New generation", "two")
        self.assertFalse(L.acknowledge_retrieval(self.vault, old))
        with patch.object(L.subprocess, "run", side_effect=self.failing_build()):
            self.finalize()
        new = self.state()["retrieval"]["token"]
        self.assertNotEqual(old, new)
        B.build(self.vault)
        self.assertFalse(L.acknowledge_retrieval(self.vault, old))
        self.assertTrue(L.acknowledge_retrieval(self.vault, new))

    def test_unrelated_owner_disabled_refresh_and_read_only_diagnostics(self):
        self.write()
        with patch.object(L.subprocess, "run", side_effect=self.failing_build()):
            self.finalize()
        before = self.state()
        self.args.owner = "reader"
        with patch.object(L.subprocess, "run", side_effect=AssertionError("read-only retry")):
            self.assertEqual(self.finalize(), "clean")
        self.assertEqual(self.state(), before)
        self.args.owner = "writer"
        self.args.retrieval_script = None
        with patch.object(L.subprocess, "run", side_effect=AssertionError("disabled retry")):
            self.assertEqual(self.finalize(), "clean")
        B.build(self.vault)
        result = CLI.search(self.vault, "value", 10)
        self.assertTrue(result["retrievalPending"])
        self.assertEqual(result["freshness"], "pending-refresh")
        self.assertEqual(self.state(), before)

    def test_recovered_noop_reports_existing_retry_without_republishing(self):
        self.write()
        with patch.object(L.subprocess, "run", side_effect=self.failing_build()):
            self.finalize()
        before = (self.vault / "wiki/log.md").stat().st_mtime_ns
        args = argparse.Namespace(vault=str(self.vault), cwd=str(self.vault), path=str(self.page),
                                  owner="writer", tool="noop")
        L.capture(args)
        self.args.recover_owner = "writer"
        with patch.object(L.subprocess, "run", side_effect=AssertionError("no-op publication/build")):
            self.assertEqual(self.finalize(), "synced-retrieval-pending")
        self.assertEqual((self.vault / "wiki/log.md").stat().st_mtime_ns, before)
        self.assertFalse(self.state()["pending"])
        self.assertEqual(self.finalize(), "synced")

    def test_source_race_during_navigation_cannot_be_published_as_valid_batch(self):
        self.write()
        original = subprocess.run
        altered = False
        def race(command, **kwargs):
            nonlocal altered
            result = original(command, **kwargs)
            if command[1] == str(MW / "sync.py") and not altered:
                self.page.write_text("invalid concurrent edit without frontmatter\n")
                altered = True
            return result
        with patch.object(L.subprocess, "run", side_effect=race):
            with self.assertRaisesRegex(L.LifecycleError, "changed during finalization"):
                self.finalize()
        self.assertTrue(self.state()["pending"])
        self.assertFalse((self.vault / B.DEFAULT_INDEX).exists())

    def test_interruption_invalid_output_timeout_and_newer_cache_refusal(self):
        self.write()
        original = subprocess.run
        def interrupted(command, **kwargs):
            if command[1] == self.args.retrieval_script:
                raise SystemExit("interrupted before refresh")
            return original(command, **kwargs)
        with patch.object(L.subprocess, "run", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.finalize()
        self.assertFalse(self.state()["pending"])
        self.assertTrue(self.state()["retrieval"])
        for outcome in [subprocess.CompletedProcess([], 0, "pretend success", ""),
                        subprocess.TimeoutExpired([], 10)]:
            with patch.object(L.subprocess, "run", side_effect=outcome if isinstance(outcome, Exception) else None,
                              return_value=None if isinstance(outcome, Exception) else outcome):
                self.assertEqual(self.finalize(), "synced-retrieval-pending")
            self.assertTrue(self.state()["retrieval"])
        B.build(self.vault)
        index = self.vault / B.DEFAULT_INDEX
        original_parent = B.open_output_parent
        def concurrent_cache(*args):
            replacement = index.with_suffix(".replacement")
            replacement.write_text(index.read_text())
            replacement.replace(index)
            return original_parent(*args)
        with patch.object(B, "open_output_parent", side_effect=concurrent_cache):
            with self.assertRaisesRegex(ValueError, "concurrent update"):
                B.build(self.vault)
        self.assertTrue(self.state()["retrieval"])

    def test_claude_warns_about_retrieval_not_unpublished_content(self):
        import os
        self.args.owner = "claude:writer"
        args = argparse.Namespace(vault=str(self.vault), cwd=str(self.vault), path=str(self.page),
                                  owner=self.args.owner, tool="one", validator=str(MW / "validate.py"))
        L.capture(args)
        self.page.write_text("---\ntype: note\n---\n# Note\nCurrent value\n")
        L.record(args)
        cache = self.vault / ".vault-meta/retrieval"
        cache.mkdir()
        (cache / "bm25.json.tmp").write_text("occupied")
        config = self.vault / "config.json"
        config.write_text(json.dumps({"vaultPath": str(self.vault)}))
        env = {**os.environ, "OBSIDIAN_AGENT_CONFIG": str(config), "OBSIDIAN_VAULT_PATH": str(self.vault),
               "WIKI_MIDDLEWARE_DIR": str(MW)}
        stop = subprocess.run(["bash", str(ROOT / "hooks/obsidian-session.sh"), "stop"],
                              input=json.dumps({"session_id": "writer"}), text=True, capture_output=True, env=env)
        self.assertEqual(stop.returncode, 0, stop.stderr)
        message = json.loads(stop.stdout)["systemMessage"]
        self.assertIn("were published", message)
        self.assertIn("BM25 refresh is pending", message)
        self.assertNotIn("not finalized", message)
        before = (self.vault / "wiki/log.md").stat().st_mtime_ns
        (cache / "bm25.json.tmp").unlink()
        stop = subprocess.run(["bash", str(ROOT / "hooks/obsidian-session.sh"), "stop"],
                              input=json.dumps({"session_id": "writer"}), text=True, capture_output=True, env=env)
        self.assertEqual(stop.stdout.strip(), "synced")
        self.assertEqual((self.vault / "wiki/log.md").stat().st_mtime_ns, before)

    def test_external_same_mtime_edits_and_build_race_are_not_fresh(self):
        self.write()
        self.finalize()
        old_mtime = self.page.stat().st_mtime_ns
        self.page.write_text(self.page.read_text().replace("Fresh", "Older"))
        import os
        os.utime(self.page, ns=(old_mtime, old_mtime))
        self.assertEqual(CLI.search(self.vault, "value", 10)["freshness"], "stale")
        before = (self.vault / B.DEFAULT_INDEX).read_bytes()
        original = B.open_output_parent
        def race(*args):
            self.page.write_text(self.page.read_text() + "concurrent change\n")
            return original(*args)
        with patch.object(B, "open_output_parent", side_effect=race):
            with self.assertRaisesRegex(ValueError, "source.*changed"):
                B.build(self.vault)
        self.assertEqual(before, (self.vault / B.DEFAULT_INDEX).read_bytes())


if __name__ == "__main__":
    unittest.main()
