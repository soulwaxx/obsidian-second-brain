#!/usr/bin/env python3
"""The package `setup` command is a reviewed route to bootstrap-vault.py.

Plans and hashes must agree with a direct bootstrap invocation for the same
inputs; refusals, stale approvals, and owner data handling are unchanged.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "obsidian-second-brain.py"
BOOTSTRAP = SCRIPTS / "bootstrap-vault.py"
LAUNCHER = SCRIPTS / "obsidian-second-brain.mjs"
sys.path.insert(0, str(SCRIPTS))
from module_loading import load_source  # noqa: E402


def tree_snapshot(*roots: Path) -> dict[str, tuple[str, int]]:
    snapshot = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted([root, *root.rglob("*")]):
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else "dir"
            snapshot[str(path)] = (digest, info.st_mtime_ns)
    return snapshot


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault ü space"
        self.config = self.root / "config ü" / "properties.json"
        self.outside = self.root / "caller cwd"
        self.outside.mkdir()
        env = os.environ.copy()
        env.update({"HOME": str(self.root / "home"), "OBSIDIAN_AGENT_CONFIG": str(self.config),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        env.pop("OBSIDIAN_VAULT_PATH", None)
        self.env = env

    def tearDown(self):
        self.temp.cleanup()

    # -- helpers -------------------------------------------------------------
    def call(self, argv, cwd=None):
        return subprocess.run(argv, cwd=cwd or self.outside, env=self.env, text=True, capture_output=True)

    def direct(self, *args, vault=None):
        return self.call([sys.executable, str(BOOTSTRAP), "--vault", str(vault or self.vault), *args])

    def setup_cli(self, *args, vault=None, json_output=True):
        return self.call([sys.executable, str(CLI), "setup", "--vault", str(vault or self.vault), *args,
                         *(["--json"] if json_output else [])])

    def write_config(self, value):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def install_plugin(self, settings=None):
        plugin = self.vault / ".obsidian/plugins/obsidian-git"
        plugin.mkdir(parents=True, exist_ok=True)
        (plugin / "manifest.json").write_text(json.dumps({"id": "obsidian-git", "version": "2.39.0"}), encoding="utf-8")
        (self.vault / ".obsidian/community-plugins.json").write_text('["obsidian-git", "other-plugin"]', encoding="utf-8")
        if settings is not None:
            (plugin / "data.json").write_text(json.dumps(settings), encoding="utf-8")
        return plugin / "data.json"

    def assert_parity(self, *args, vault=None):
        """Both routes agree on the plan/hash or on the refusal; neither writes."""
        roots = (self.vault, self.config.parent)
        before = tree_snapshot(*roots)
        direct = self.direct(*args, vault=vault)
        routed = self.setup_cli(*args, vault=vault)
        self.assertEqual(tree_snapshot(*roots), before, "previews and refusals must not write")
        if direct.returncode == 0:
            self.assertEqual(routed.returncode, 0, routed.stdout + routed.stderr)
            expected = json.loads(direct.stdout)
            output = json.loads(routed.stdout)
            self.assertEqual(output["planHash"], expected.pop("planHash"))
            self.assertEqual(output["plan"], expected)
            self.assertEqual((output["operation"], output["mode"], output["applied"]), ("setup", "preview", False))
            self.assertEqual(output["phase"], "configure" if "--configure" in args else "scaffold")
            return output
        self.assertEqual((direct.returncode, routed.returncode), (1, 1), routed.stdout + routed.stderr)
        error = json.loads(routed.stdout)
        self.assertEqual(error["code"], "setup-refused")
        self.assertEqual(direct.stderr.strip(), f"bootstrap-vault: {error['error']}".strip())
        return error

    def apply(self, output, *args):
        result = self.setup_cli(*args, "--apply", "--confirm", output["planHash"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual((applied["mode"], applied["applied"], applied["planHash"]), ("apply", True, output["planHash"]))
        return applied

    # -- parity across the bootstrap scenarios -----------------------------
    def test_new_vault_scaffold_parity_apply_and_repeat_noop(self):
        preview = self.assert_parity()
        self.assertFalse(self.vault.exists())
        self.assertIn(str(self.vault), preview["plan"]["directories"])
        applied = self.apply(preview)
        self.assertEqual(applied["plan"], preview["plan"])
        self.assertTrue((self.vault / "wiki/quickstart.md").is_file())
        self.assertTrue((self.vault / "wiki/index.md").is_file())
        self.assertFalse(self.config.exists(), "scaffold never creates the integration config")
        self.assertFalse((self.vault / ".git").exists(), "setup never initializes Git")
        # Readiness reflects the post-apply state; the next step is never executed.
        self.assertEqual((applied["readiness"]["stage"], applied["readiness"]["reason"]), ("workflow", "obsidian-git-missing"))
        self.assertEqual([action["code"] for action in applied["nextActions"]], ["install-obsidian-git"])
        repeat = self.assert_parity()
        self.assertEqual((repeat["plan"]["directories"], repeat["plan"]["writes"]), ([], []))
        before = tree_snapshot(self.vault)
        self.apply(repeat)
        self.assertEqual({k: v[0] for k, v in tree_snapshot(self.vault).items()}, {k: v[0] for k, v in before.items()})

    def test_existing_vault_scaffold_only_preserves_notes_and_config(self):
        (self.vault / "wiki").mkdir(parents=True)
        note = self.vault / "wiki/coffee.md"
        note.write_text("---\ntype: note\n---\n# Coffee\n", encoding="utf-8")
        self.write_config({"vaultPath": str(self.vault), "custom": {"keep": 1}})
        config_bytes = self.config.read_bytes()
        preview = self.assert_parity()
        applied = self.apply(preview)
        self.assertEqual(note.read_text(encoding="utf-8"), "---\ntype: note\n---\n# Coffee\n")
        self.assertEqual(self.config.read_bytes(), config_bytes, "scaffold-only preserves existing config")
        self.assertIn("(./coffee.md)", (self.vault / "wiki/index.md").read_text(encoding="utf-8"))
        self.install_plugin()
        after_plugin = json.loads(self.setup_cli().stdout)
        self.assertEqual(after_plugin["readiness"]["reason"], "configure-not-applied")
        configure = after_plugin["nextActions"][0]
        self.assertEqual(configure["code"], "preview-configure")
        self.assertEqual(configure["argv"][1:], [str(CLI), "setup", "--vault", str(self.vault), "--configure"])
        self.assertEqual(applied["phase"], "scaffold")

    def test_relative_vault_resolves_from_caller_cwd_like_bootstrap(self):
        self.vault.mkdir()
        relative = os.path.relpath(self.vault, self.outside)
        self.assertEqual(self.assert_parity(vault=relative)["plan"]["vault"], str(self.vault))

    def test_vault_override_environment_is_not_consulted(self):
        other = self.root / "other vault"
        (other / "wiki").mkdir(parents=True)
        self.vault.mkdir()
        self.env["OBSIDIAN_VAULT_PATH"] = str(other)
        before = tree_snapshot(other)
        output = self.assert_parity()
        self.assertEqual(output["plan"]["vault"], str(self.vault))
        self.assertEqual(output["readiness"]["reason"], "wiki-missing")
        self.assertEqual(output["nextActions"][0]["argv"][2:], ["setup", "--vault", str(self.vault)])
        self.apply(output)
        self.assertTrue((self.vault / "wiki/index.md").is_file())
        self.assertEqual(tree_snapshot(other), before)

    def test_null_selection_configure_parity_and_apply(self):
        self.vault.mkdir()
        self.write_config({"vaultPath": None, "custom": 1, "features": {"mystery": 7}})
        settings = self.install_plugin({"unknown": "kept", "autoSaveInterval": 12, "autoPullInterval": 3,
                                        "disablePush": True, "squashCommitsBeforePush": True})
        scaffold = self.assert_parity()
        self.assertEqual(scaffold["plan"]["configDifferences"], {})
        preview = self.assert_parity("--configure")
        plan = preview["plan"]
        self.assertTrue(plan["pluginReady"])
        self.assertEqual(plan["configDifferences"]["features.autoCommit"], {"from": "<missing>", "to": False})
        self.assertEqual(plan["configDifferences"]["vaultPath"], {"from": None, "to": str(self.vault)})
        serialized = json.dumps(preview)
        self.assertNotIn("mystery", serialized)
        self.assertNotIn('"unknown"', serialized)
        self.apply(preview, "--configure")
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual((config["vaultPath"], config["features"], config["custom"]),
                         (str(self.vault), {"mystery": 7, "autoCommit": False}, 1))
        profile = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual((profile["unknown"], profile["autoSaveInterval"], profile["autoPullInterval"], profile["disablePush"]),
                         ("kept", 12, 3, True), "schedules and disabled-push intent are preserved")
        self.assertIs(profile["squashCommitsBeforePush"], False)
        self.assertEqual(json.loads((self.vault / ".obsidian/community-plugins.json").read_text()), ["obsidian-git", "other-plugin"])
        self.assertFalse((self.vault / ".git").exists(), "configure never initializes Git")
        self.assertFalse((self.vault / "wiki").exists(), "configure never scaffolds pages")
        repeat = self.assert_parity("--configure")
        self.assertEqual(repeat["plan"]["writes"], [])

    def test_configure_in_git_vault_performs_no_git_writes(self):
        self.vault.mkdir()
        subprocess.run(["git", "init", "-q", str(self.vault)], check=True, env=self.env)
        self.install_plugin()
        git_dir = tree_snapshot(self.vault / ".git")
        preview = self.assert_parity("--configure")
        self.apply(preview, "--configure")
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["features"], {"autoCommit": False})
        self.assertEqual(tree_snapshot(self.vault / ".git"), git_dir, "no staging, commit, or Git config writes")
        status = subprocess.run(["git", "-C", str(self.vault), "status", "--porcelain"], text=True,
                                capture_output=True, check=True, env=self.env).stdout
        self.assertNotIn("A ", status)
        self.assertIn("/.vault-meta/retrieval/", (self.vault / ".gitignore").read_text(encoding="utf-8"))

    def test_configure_apply_requires_enabled_obsidian_git(self):
        self.vault.mkdir()
        preview = self.assert_parity("--configure")
        self.assertFalse(preview["plan"]["pluginReady"])
        result = self.setup_cli("--configure", "--apply", "--confirm", preview["planHash"])
        self.assertEqual(result.returncode, 1)
        error = json.loads(result.stdout)
        self.assertEqual(error["code"], "setup-refused")
        self.assertIn("requires Obsidian Git", error["error"])
        self.assertFalse(self.config.exists())
        self.assertFalse((self.vault / ".gitignore").exists())

    def test_refusals_match_bootstrap(self):
        cases = {
            "malformed config": lambda: self.write_config("{bad"),
            "invalid feature type": lambda: self.write_config({"vaultPath": str(self.vault), "features": []}),
            "wrong selected vault": lambda: self.write_config({"vaultPath": str(self.root / "other")}),
            "user-owned log": lambda: (self.vault / "wiki/log.md").write_text("# My log\n", encoding="utf-8"),
            "user-owned index": lambda: (self.vault / "wiki/index.md").write_text("# Mine\n", encoding="utf-8"),
            "wiki is a file": lambda: (shutil.rmtree(self.vault / "wiki"), (self.vault / "wiki").write_text("x")),
            "vault is a file": lambda: (shutil.rmtree(self.vault), self.vault.write_text("x")),
        }
        for name, arrange in cases.items():
            with self.subTest(name):
                if self.vault.is_dir():
                    shutil.rmtree(self.vault)
                elif self.vault.exists():
                    self.vault.unlink()
                if self.config.exists():
                    self.config.unlink()
                (self.vault / "wiki").mkdir(parents=True)
                arrange()
                owned = tree_snapshot(self.vault, self.config.parent)
                for args in ((), ("--configure",)):
                    error = self.assert_parity(*args)
                    if "code" not in error:
                        # Configuration does not inspect scaffold-only collisions; parity is still asserted.
                        continue
                    applied = self.setup_cli(*args, "--apply", "--confirm", "0" * 64)
                    self.assertEqual(json.loads(applied.stdout)["code"], "setup-refused")
                self.assertEqual(tree_snapshot(self.vault, self.config.parent), owned)

    def test_symlinked_config_path_is_refused_like_bootstrap(self):
        real = self.root / "real-config"
        real.mkdir()
        self.config.parent.symlink_to(real, target_is_directory=True)
        self.vault.mkdir()
        self.assertEqual(self.assert_parity("--configure")["code"], "setup-refused")

    # -- approval binding ----------------------------------------------------
    def test_stale_or_missing_approval_is_refused_without_writes(self):
        (self.vault / "wiki").mkdir(parents=True)
        preview = self.assert_parity()
        (self.vault / "wiki/new.md").write_text("---\ntype: note\n---\n# New\n", encoding="utf-8")
        before = tree_snapshot(self.vault)
        for extra in (["--confirm", preview["planHash"]], [], ["--confirm", "wrong"]):
            with self.subTest(extra=extra):
                result = self.setup_cli("--apply", *extra)
                self.assertEqual(result.returncode, 1)
                error = json.loads(result.stdout)
                self.assertEqual(error["code"], "stale-plan")
                self.assertIn("preview again", error["error"])
                direct = self.direct("--apply", *extra)
                self.assertEqual(direct.returncode, 1)
                self.assertIn("stale or unconfirmed plan", direct.stderr)
                self.assertEqual(tree_snapshot(self.vault), before)

    def test_hashes_are_interchangeable_between_routes(self):
        self.vault.mkdir()
        preview = json.loads(self.direct().stdout)
        result = self.setup_cli("--apply", "--confirm", preview["planHash"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.vault / "wiki/quickstart.md").is_file())
        self.install_plugin()
        routed = json.loads(self.setup_cli("--configure").stdout)
        direct = self.direct("--configure", "--apply", "--confirm", routed["planHash"])
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertIs(json.loads(self.config.read_text(encoding="utf-8"))["features"]["autoCommit"], False)

    def test_concurrent_owner_settings_edit_survives_failed_apply(self):
        self.vault.mkdir()
        self.write_config({"vaultPath": str(self.vault), "features": {"autoCommit": True}})
        settings = self.install_plugin({"custom": "before"})
        config_bytes = self.config.read_bytes()
        preview = json.loads(self.setup_cli("--configure").stdout)
        sys.modules.pop("obsidian_cli_bootstrap", None)
        with mock.patch.dict(os.environ, {"OBSIDIAN_AGENT_CONFIG": str(self.config)}):
            cli = load_source("osb_setup_test_cli", str(CLI))
            bootstrap = load_source("osb_setup_test_bootstrap", str(BOOTSTRAP))
            planned = json.loads(next(item["content"] for item in bootstrap.plan_for(self.vault, self.config, configure=True)["writes"]
                                      if item["path"].endswith("data.json")))
            real_matches = bootstrap._public_parent_matches
            injected = []

            def owner_edits_after_replacement(path, identity):
                # The Obsidian app rewrites settings after bootstrap replaced them.
                if path == settings.parent and not injected and json.loads(settings.read_text()) == planned:
                    injected.append(True)
                    settings.write_text('{"external": "edit"}', encoding="utf-8")
                    return False
                return real_matches(path, identity)

            with mock.patch.object(bootstrap, "_public_parent_matches", side_effect=owner_edits_after_replacement), \
                    mock.patch.object(cli, "load_source", side_effect=lambda name, path: bootstrap if name == "obsidian_cli_bootstrap" else load_source(name, path)):
                with self.assertRaises(cli.SetupError) as raised:
                    cli.setup(str(self.vault), True, True, preview["planHash"])
        self.assertTrue(injected)
        self.assertEqual(raised.exception.code, "setup-refused")
        self.assertEqual(settings.read_text(encoding="utf-8"), '{"external": "edit"}', "owner edit survives rollback")
        self.assertEqual(self.config.read_bytes(), config_bytes, "owned config replacement is rolled back")
        self.assertFalse((self.vault / ".gitignore").exists())

    # -- interface -----------------------------------------------------------
    def test_invalid_arguments_are_structured_and_write_nothing(self):
        self.vault.mkdir()
        before = tree_snapshot(self.vault)
        cases = {
            ("setup", "--vault", str(self.vault), "--config", str(self.config)): "OBSIDIAN_AGENT_CONFIG",
            ("setup",): "requires --vault",
            ("setup", "terms", "--vault", str(self.vault)): "query terms",
            ("setup", "--vault", str(self.vault), "--limit", "3"): "--limit",
            ("setup", "--vault", str(self.vault), "--plan-hash", "x"): "--plan-hash",
            ("setup", "--vault", str(self.vault), "--confirm", "x"): "--confirm requires --apply",
            ("doctor", "--apply"): "require setup",
            ("search", "x", "--configure"): "require setup",
        }
        for argv, message in cases.items():
            with self.subTest(argv=argv):
                result = self.call([sys.executable, str(CLI), *argv, "--json"])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                error = json.loads(result.stdout)
                self.assertEqual(error["code"], "invalid-arguments")
                self.assertIn(message, error["error"])
                text = self.call([sys.executable, str(CLI), *argv])
                self.assertEqual(text.returncode, 2)
                self.assertEqual(text.stdout, "")
                self.assertIn(message, text.stderr)
        self.assertEqual(tree_snapshot(self.vault), before)
        self.assertFalse(self.config.exists())

    def test_human_output_previews_applies_and_reports_errors(self):
        self.vault.mkdir()
        preview = self.setup_cli(json_output=False)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        plan_hash = json.loads(self.setup_cli().stdout)["planHash"]
        self.assertIn(f"Plan hash: {plan_hash}\n", preview.stdout)
        self.assertIn(f"Write: {self.vault / 'wiki/quickstart.md'}\n", preview.stdout)
        self.assertIn(f"OBSIDIAN_AGENT_CONFIG={shlex.quote(str(self.config))} obsidian-second-brain setup", preview.stdout)
        self.assertIn(f"--apply --confirm {plan_hash}", preview.stdout)
        unplugged = self.setup_cli("--configure", json_output=False)
        self.assertIn("requires Obsidian Git installed and enabled", unplugged.stdout)
        self.assertNotIn("--apply --confirm", unplugged.stdout)
        self.assertIn("Stage: ", preview.stdout)
        self.assertFalse((self.vault / "wiki").exists())
        stale = self.setup_cli("--apply", "--confirm", "wrong", json_output=False)
        self.assertEqual((stale.returncode, stale.stdout), (1, ""))
        self.assertIn("obsidian-second-brain: stale or unconfirmed plan", stale.stderr)
        applied = self.setup_cli("--apply", "--confirm", plan_hash, json_output=False)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn(f"Applied scaffold plan {plan_hash}\n", applied.stdout)
        self.install_plugin({"disablePush": True})
        self.write_config({"vaultPath": str(self.vault)})
        configure = self.setup_cli("--configure", json_output=False)
        self.assertIn("Config features.autoCommit: \"<missing>\" -> false\n", configure.stdout)
        self.assertIn("(private content bound to the plan hash)", configure.stdout)
        self.assertNotIn("disablePush", configure.stdout, "preserved settings are not proposed changes")
        self.assertIn("--configure --apply --confirm", configure.stdout)
        noop = self.setup_cli(json_output=False)
        self.assertIn("No changes planned.", noop.stdout)
        self.assertNotIn("--apply --confirm", noop.stdout)

    def test_human_apply_hint_binds_absolute_config_selection(self):
        self.vault.mkdir()
        self.env["OBSIDIAN_AGENT_CONFIG"] = "relative cfg/p.json"
        absolute = self.outside / "relative cfg/p.json"
        preview = self.setup_cli(json_output=False)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn(f"OBSIDIAN_AGENT_CONFIG={shlex.quote(str(absolute))} obsidian-second-brain setup", preview.stdout)
        plan_hash = json.loads(self.setup_cli().stdout)["planHash"]
        self.env["OBSIDIAN_AGENT_CONFIG"] = str(absolute)
        applied = self.call([sys.executable, str(CLI), "setup", "--vault", str(self.vault), "--apply", "--confirm", plan_hash],
                            cwd=self.root)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)

    def test_launcher_reports_unstartable_interpreter(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for the package launcher")
        env = {**self.env, "PYTHON": str(self.root / "missing python")}
        result = subprocess.run([node, str(LAUNCHER), "setup", "--vault", str(self.vault)], cwd=self.outside,
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("cannot start Python interpreter", result.stderr)
        self.assertIn("Python 3.14+", result.stderr)
        self.assertIn("PYTHON=", result.stderr)
        self.assertFalse(self.vault.exists())
        routed = subprocess.run([node, str(LAUNCHER), "setup", "--vault", str(self.vault), "--json"], cwd=self.outside,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(routed.returncode, 0, routed.stderr)
        self.assertEqual(json.loads(routed.stdout)["planHash"], json.loads(self.direct().stdout)["planHash"])


if __name__ == "__main__":
    unittest.main()
