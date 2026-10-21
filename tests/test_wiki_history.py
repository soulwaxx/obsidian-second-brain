"""Isolated tests for the explicit destination-only history migration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get("OSB_TEST_PACKAGE_ROOT", Path(__file__).resolve().parents[1])).resolve()
SCRIPT = ROOT / "scripts/wiki_history.py"
sys.path.insert(0, str(ROOT / "scripts"))
import wiki_history

assert Path(wiki_history.__file__).resolve().parent == ROOT / "scripts"
assert Path(wiki_history.LIFE.__file__).resolve().parent == ROOT / "scripts"
assert Path(wiki_history.COMPAT.__file__).resolve().parent == ROOT / "scripts"


def run(*args, ok=True):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], text=True, capture_output=True)
    if (result.returncode == 0) != ok:
        raise AssertionError(f"{result.args}: {result.stdout}\n{result.stderr}")
    return result


class HistoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        (self.vault / "wiki" / "target.md").write_text("---\ntype: note\ntitle: Target\n---\n# Target\n")
        self.log = self.vault / "wiki" / "log.md"
        self.log.write_bytes(b"# History\n\n* **Update**: old label [A \\[label\\]](/target.md#section).\n")
        self.config = Path(self.temp.name) / "config.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}))

    def preview(self):
        return json.loads(run("--vault", self.vault, "--config", self.config, "--json").stdout)

    def test_preview_is_read_only_and_apply_changes_only_destination(self):
        before = self.log.read_bytes()
        mtime = self.log.stat().st_mtime_ns
        preview = self.preview()
        self.assertEqual(before, self.log.read_bytes())
        self.assertEqual(mtime, self.log.stat().st_mtime_ns)
        self.assertEqual(len(preview["operations"]), 1)
        self.assertEqual(preview["operations"][0]["to"], "./target.md#section")
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", preview["planHash"])
        after = self.log.read_bytes()
        self.assertEqual(after.replace(b"./target.md#section", b"/target.md#section"), before)
        repeated = json.loads(run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", preview["planHash"]).stdout)
        self.assertTrue(repeated["alreadyApplied"])
        self.assertEqual(run("--vault", self.vault, "--config", self.config, "--recover", preview["planHash"]).returncode, 0)
        self.assertEqual(self.log.read_bytes(), after)

    def test_bom_crlf_unicode_nested_label_preserves_all_other_bytes(self):
        target_dir = self.vault / "wiki" / "folder"
        target_dir.mkdir()
        (target_dir / "target.md").write_text("---\ntype: note\ntitle: Target\n---\n# Target\n")
        before = "\ufeff# History\r\n\r\n* Keep prose — [Café [detail]](/folder/target.md#part).\r\n".encode("utf-8")
        self.log.write_bytes(before)
        plan = self.preview()
        self.assertEqual(plan["operations"][0]["from"], "/folder/target.md#part")
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"])
        after = self.log.read_bytes()
        expected = before.replace(b"/folder/target.md#part", b"./folder/target.md#part")
        self.assertEqual(after, expected)

    def test_wrong_and_stale_approval_refused(self):
        plan = self.preview()
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", "0" * 64, ok=False)
        self.log.write_bytes(self.log.read_bytes() + b"manual edit\n")
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"], ok=False)
        self.assertTrue(self.log.read_bytes().endswith(b"manual edit\n"))

    def test_recovery_from_before_and_after_publication(self):
        planned = wiki_history.plan(self.vault.resolve(), wiki_history.selected_config(str(self.config), self.vault.resolve()))
        real_replace = os.replace
        def fail_log_replace(src, dst, **kwargs):
            if dst == "log.md":
                raise OSError("injected before publication")
            return real_replace(src, dst, **kwargs)
        with patch.object(wiki_history.os, "replace", side_effect=fail_log_replace):
            with self.assertRaises(OSError):
                wiki_history.apply(self.vault.resolve(), planned["config"], planned["planHash"])
        wiki_history.apply(self.vault.resolve(), planned["config"], planned["planHash"], recovery=True)
        self.assertEqual(self.log.read_bytes(), planned["_after"])

        self.log.write_bytes(planned["_before"])
        planned = wiki_history.plan(self.vault.resolve(), planned["config"])
        def publish_then_crash(src, dst, **kwargs):
            real_replace(src, dst, **kwargs)
            if dst == "log.md":
                raise OSError("injected after publication")
        with patch.object(wiki_history.os, "replace", side_effect=publish_then_crash):
            with self.assertRaises(OSError):
                wiki_history.apply(self.vault.resolve(), planned["config"], planned["planHash"])
        retried = wiki_history.apply(self.vault.resolve(), planned["config"], planned["planHash"], recovery=True)
        self.assertTrue(retried["alreadyApplied"])
        self.assertEqual(self.log.read_bytes(), planned["_after"])

    def test_pending_writer_blocks_and_nested_reserved_untouched(self):
        nested = self.vault / "wiki" / "sub"
        nested.mkdir()
        (nested / "log.md").write_text("/target.md")
        _life, state, _lock = wiki_history.LIFE.layout(self.vault, True)
        wiki_history.LIFE.atomic_json(state, {"version": 1, "vault": str(self.vault.resolve()), "pending": {"writer": {}}})
        plan = self.preview()
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"], ok=False)
        self.assertEqual((nested / "log.md").read_text(), "/target.md")

    def test_config_edit_during_preparation_refuses_publication(self):
        plan = self.preview()
        original = wiki_history.config_still_matches
        calls = 0
        def edit_after_journal(config_id, vault):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.config.write_text(json.dumps({"vaultPath": str(self.vault), "changed": True}))
            return original(config_id, vault)
        with patch.object(wiki_history, "config_still_matches", side_effect=edit_after_journal):
            with self.assertRaises(wiki_history.HistoryError):
                wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"])
        self.assertIn(b"/target.md", self.log.read_bytes())

    def test_temp_file_substitution_is_refused(self):
        plan = self.preview()
        before = self.log.read_bytes()
        original = wiki_history.config_still_matches
        create_temp = wiki_history.LIFE.create_log_temp
        descriptors = []
        def remember_temp(parent):
            fd, name = create_temp(parent)
            descriptors.append(fd)
            return fd, name
        calls = 0
        def replace_temp(config_id, vault):
            nonlocal calls
            calls += 1
            if calls == 2:
                temp = next((p for p in (self.vault / "wiki").iterdir() if p.name.startswith(".log-")), None)
                self.assertIsNotNone(temp)
                self.assertEqual(temp.stat().st_ino, os.fstat(descriptors[0]).st_ino)
                temp.unlink()
                self.assertEqual(os.fstat(descriptors[0]).st_nlink, 0)
                temp.write_bytes(b"unreviewed replacement")
            return original(config_id, vault)
        with patch.object(wiki_history.LIFE, "create_log_temp", side_effect=remember_temp), \
                patch.object(wiki_history, "config_still_matches", side_effect=replace_temp):
            with self.assertRaises(wiki_history.HistoryError):
                wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"])
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertEqual(self.log.read_bytes(), before)

    def test_case_check_runs_on_journal_replay(self):
        plan = self.preview()
        real_replace = os.replace
        def fail_log_replace(src, dst, **kwargs):
            if dst == "log.md":
                raise OSError("injected before publication")
            return real_replace(src, dst, **kwargs)
        with patch.object(wiki_history.os, "replace", side_effect=fail_log_replace):
            with self.assertRaises(OSError):
                wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"])
        real_listdir = os.listdir
        wiki_info = (self.vault / "wiki").stat()
        def collide_log_name(path):
            names = real_listdir(path)
            try:
                info = os.fstat(path)
            except (TypeError, OSError):
                return names
            if (info.st_dev, info.st_ino) == (wiki_info.st_dev, wiki_info.st_ino):
                return ["LOG.md" if name == "log.md" else name for name in names]
            return names
        with patch.object(wiki_history.os, "listdir", side_effect=collide_log_name):
            with self.assertRaises(wiki_history.HistoryError):
                wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"], recovery=True)
        wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"], recovery=True)
        with patch.object(wiki_history.os, "listdir", side_effect=collide_log_name):
            with self.assertRaises(wiki_history.HistoryError):
                wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"])

    def test_lifecycle_directory_replacement_during_journal_creation_refuses(self):
        plan = self.preview()
        lifecycle = self.vault.resolve() / ".vault-meta/lifecycle"
        real_link = os.link
        def replace_lifecycle(src, dst, **kwargs):
            if str(dst).startswith("history-migration-"):
                lifecycle.rename(lifecycle.with_name("lifecycle-detached"))
                lifecycle.mkdir()
            return real_link(src, dst, **kwargs)
        with patch.object(wiki_history.os, "link", side_effect=replace_lifecycle):
            with self.assertRaises(wiki_history.HistoryError):
                wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"])
        self.assertIn(b"/target.md", self.log.read_bytes())
        self.assertFalse(wiki_history.journal_path(self.vault.resolve(), plan["planHash"]).exists())

    def test_public_wiki_replacement_after_publish_reports_recoverable_conflict(self):
        plan = self.preview()
        wiki = self.vault.resolve() / "wiki"
        original = self.log.read_bytes()
        real_replace = os.replace
        switched = False
        def replace_then_switch(src, dst, **kwargs):
            nonlocal switched
            result = real_replace(src, dst, **kwargs)
            if dst == "log.md" and not switched:
                switched = True
                wiki.rename(wiki.with_name("wiki-detached"))
                wiki.mkdir()
                (wiki / "target.md").write_bytes((self.vault / "wiki-detached/target.md").read_bytes())
                (wiki / "log.md").write_bytes(original)
            return result
        with patch.object(wiki_history.os, "replace", side_effect=replace_then_switch):
            with self.assertRaisesRegex(wiki_history.HistoryError, "recoverable"):
                wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"])
        self.assertEqual((wiki / "log.md").read_bytes(), original)
        wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"], recovery=True)
        self.assertEqual((wiki / "log.md").read_bytes(), original.replace(b"/target.md", b"./target.md"))

    def test_concurrent_log_edit_refuses_publication(self):
        plan = self.preview()
        original = wiki_history.LIFE.same_public_parent
        def concurrent_edit(path, fd):
            self.log.write_bytes(self.log.read_bytes() + b"concurrent\n")
            return original(path, fd)
        with patch.object(wiki_history.LIFE, "same_public_parent", side_effect=concurrent_edit):
            with self.assertRaises(wiki_history.HistoryError):
                wiki_history.apply(self.vault.resolve(), plan["config"], plan["planHash"])
        self.assertTrue(self.log.read_bytes().endswith(b"concurrent\n"))

    def test_pending_own_and_foreign_capture_and_state_preservation(self):
        plan = self.preview()
        state_path = self.vault.resolve() / ".vault-meta/lifecycle/state.json"
        state_path.parent.mkdir(parents=True)
        base = {"version": 1, "vault": str(self.vault.resolve()), "pending": {},
                "retrieval": {"owner": "indexer", "token": "keep-me"}, "unrelated": {"keep": True}}
        for owner in ("own", "foreign"):
            state = dict(base)
            state["pending"] = {"page": {"captures": {owner: {"tool": "active"}}}}
            wiki_history.LIFE.atomic_json(state_path, state)
            run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"], ok=False)
        wiki_history.LIFE.atomic_json(state_path, base)
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"])
        saved = json.loads(state_path.read_text())
        self.assertEqual(saved["retrieval"], base["retrieval"])
        self.assertEqual(saved["unrelated"], base["unrelated"])

    def test_existing_malformed_or_symlink_journal_is_never_replaced(self):
        plan = self.preview()
        journal = wiki_history.journal_path(self.vault.resolve(), plan["planHash"])
        journal.parent.mkdir(parents=True)
        journal.write_text("not json")
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"], ok=False)
        self.assertEqual(journal.read_text(), "not json")
        journal.unlink()
        journal.symlink_to(self.log)
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"], ok=False)
        self.assertTrue(journal.is_symlink())

    def test_git_exclusion_required_before_journal_creation(self):
        original_log = self.log.read_bytes()
        def init_git(vault):
            subprocess.run(["git", "-C", str(vault), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(vault), "config", "core.excludesFile", os.devnull], check=True)
            subprocess.run(["git", "-C", str(vault), "config", "user.name", "fixture"], check=True)
            subprocess.run(["git", "-C", str(vault), "config", "user.email", "fixture@example.invalid"], check=True)

        cases = (("", False), ("/.vault-meta/\n!/.vault-meta/\n!/.vault-meta/lifecycle/\n!/.vault-meta/lifecycle/**\n", False),
                 ("/.vault-meta/\n", False), ("/.vault-meta/\n", True))
        for index, (rules, tracked) in enumerate(cases):
            with self.subTest(rules=rules, tracked=tracked):
                vault = Path(self.temp.name) / f"git-{index}"
                (vault / "wiki").mkdir(parents=True)
                (vault / "wiki/target.md").write_text("---\ntype: note\ntitle: Target\n---\n# Target\n")
                log = vault / "wiki/log.md"
                log.write_bytes(original_log)
                config = Path(self.temp.name) / (vault.name + "-config.json")
                config.write_text(json.dumps({"vaultPath": str(vault)}))
                init_git(vault)
                if tracked:
                    existing = vault / ".vault-meta/lifecycle/existing.json"
                    existing.parent.mkdir(parents=True)
                    existing.write_text("existing\n")
                    subprocess.run(["git", "-C", str(vault), "add", str(existing)], check=True)
                    subprocess.run(["git", "-C", str(vault), "commit", "-qm", "tracked state"], check=True)
                    (vault / ".gitignore").write_text(rules)
                    subprocess.run(["git", "-C", str(vault), "add", ".gitignore"], check=True)
                    subprocess.run(["git", "-C", str(vault), "commit", "-qm", "ignore state"], check=True)
                else:
                    (vault / ".gitignore").write_text(rules)
                preview = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(vault), "--config", str(config), "--json"],
                                         text=True, capture_output=True, check=True)
                plan_hash = json.loads(preview.stdout)["planHash"]
                result = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(vault), "--config", str(config),
                                         "--apply", "--confirm", plan_hash], text=True, capture_output=True)
                if rules == "/.vault-meta/\n" and not tracked:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    journal = wiki_history.journal_path(vault.resolve(), plan_hash)
                    self.assertTrue(journal.exists())
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(log.read_bytes(), original_log)
                    journal = wiki_history.journal_path(vault.resolve(), plan_hash)
                    self.assertFalse(journal.exists())
                    if not tracked:
                        self.assertFalse((vault / ".vault-meta/lifecycle").exists())

    def test_ordinary_file_guard_still_rejects_log(self):
        guard = ROOT / "skills/wiki/scripts/okf_mw/guard.py"
        result = subprocess.run([sys.executable, str(guard), str(self.vault), "wiki/log.md"], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_case_colliding_log_refused_when_filesystem_allows_it(self):
        alternate = self.vault / "wiki" / "LOG.md"
        try:
            alternate.write_text("collision")
        except OSError:
            self.skipTest("filesystem cannot create case collision")
        if "LOG.md" not in os.listdir(self.vault / "wiki"):
            self.skipTest("filesystem is case-insensitive")
        run("--vault", self.vault, "--config", self.config, "--json", ok=False)

    def test_wrong_or_changed_config_refused(self):
        plan = self.preview()
        self.config.write_text(json.dumps({"vaultPath": str(self.vault), "extra": True}))
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"], ok=False)
        self.assertIn(b"/target.md", self.log.read_bytes())

    def test_symlinked_log_wiki_state_and_journal_refused(self):
        self.log.unlink()
        self.log.symlink_to(self.vault / "wiki" / "target.md")
        run("--vault", self.vault, "--config", self.config, "--json", ok=False)

        self.log.unlink()
        self.log.write_text("/target.md")
        (self.vault / "wiki").rename(self.vault / "wiki-real")
        (self.vault / "wiki").symlink_to(self.vault / "wiki-real", target_is_directory=True)
        run("--vault", self.vault, "--config", self.config, "--json", ok=False)

        (self.vault / "wiki").unlink()
        (self.vault / "wiki-real").rename(self.vault / "wiki")
        (self.vault / ".vault-meta").mkdir()
        (self.vault / ".vault-meta/lifecycle").mkdir()
        (self.vault / ".vault-meta/lifecycle/state.json").symlink_to(self.vault / "wiki" / "target.md")
        plan = self.preview()
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"], ok=False)

    def test_case_colliding_state_or_journal_refused_when_filesystem_allows_it(self):
        plan = self.preview()
        lifecycle = self.vault.resolve() / ".vault-meta/lifecycle"
        lifecycle.mkdir(parents=True)
        state_case = lifecycle / "STATE.JSON"
        try:
            state_case.write_text("{}");
        except OSError:
            self.skipTest("filesystem cannot create case collision")
        if "STATE.JSON" not in os.listdir(lifecycle):
            self.skipTest("filesystem is case-insensitive")
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"], ok=False)
        state_case.unlink()
        journal_case = lifecycle / f"HISTORY-MIGRATION-{plan['planHash']}.JSON"
        journal_case.write_text("collision")
        run("--vault", self.vault, "--config", self.config, "--apply", "--confirm", plan["planHash"], ok=False)

    def test_case_colliding_wiki_or_log_refused_when_filesystem_allows_it(self):
        alternate = self.vault / "WIKI"
        try:
            alternate.mkdir()
        except FileExistsError:
            self.skipTest("filesystem is case-insensitive")
        run("--vault", self.vault, "--config", self.config, "--json", ok=False)


if __name__ == "__main__":
    unittest.main()
