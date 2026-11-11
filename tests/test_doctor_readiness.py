#!/usr/bin/env python3
"""Doctor capability/readiness discovery (schema v1): states, safety, and compatibility.

All vaults and configs are disposable synthetic fixtures.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/obsidian-second-brain.py"
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source  # noqa: E402

CLI_MODULE = load_source("doctor_readiness_cli", str(CLI))

LEGACY_KEYS = {"config", "dependencies", "vault", "cacheExclusions", "lifecycle", "retrieval"}
VAULT_KEYS = {"vaultExists", "wikiExists"}
NEW_KEYS = {"schemaVersion", "capabilities", "readiness"}
DEPENDENCY_KEYS = {"python", "pythonMinimum", "pythonSupported", "pythonError", "retrieval", "pyyaml", "git", "jq"}
CAPABILITIES = ["lexicalSearch", "spanReads", "sectionReads", "evidenceReport", "reviewedWrites", "sourceChecks", "semanticRerank"]
STAGES = ["package", "vaultSelected", "scaffold", "workflow", "pendingContent", "pendingRetrieval"]
ACTION_CODES = {
    "select-python", "install-requirements", "install-jq", "choose-vault", "repair-config", "preview-scaffold",
    "inspect-wiki-path", "preview-configure", "review-config-selection", "inspect-obsidian-settings",
    "install-obsidian-git", "install-git", "set-up-vault-git", "review-derived-state", "finish-wiki-session",
    "inspect-lifecycle-state", "build-index", "inspect-retrieval-cache", "inspect-retrieval-state",
}
FORBIDDEN_TOKENS = {"--apply", "--confirm", "git", "ollama", "--semantic-chunks", "-g", "--global", "--user", "rm", "init"}
IGNORE_LINES = ["/.vault-meta/retrieval/", "/.vault-meta/lifecycle/", "/.vault-meta/okf-index-ownership.json",
                "/.vault-meta/okf-index-ownership.lock", ".obsidian/workspace.json", ".obsidian/workspace-mobile.json"]


def tree_snapshot(*roots: Path) -> dict[str, tuple[str, int]]:
    snapshot = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted([root, *root.rglob("*")]):
            if ".git" in path.relative_to(root).parts:
                continue
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else "dir"
            snapshot[str(path)] = (digest, info.st_mtime_ns)
    return snapshot


class DoctorReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault ü"
        self.config = self.root / "cfg" / "properties.json"
        self.config.parent.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(self.root / "empty-home")
        env["OBSIDIAN_AGENT_CONFIG"] = str(self.config)
        env.pop("OBSIDIAN_VAULT_PATH", None)
        for key in list(env):
            if key.startswith("GIT_"):
                env.pop(key)
        self.env = env
        self.outside = self.root / "elsewhere"
        self.outside.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    # -- fixtures -------------------------------------------------------
    def write_config(self, value):
        self.config.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def scaffold(self):
        (self.vault / "wiki").mkdir(parents=True, exist_ok=True)
        (self.vault / "wiki/index.md").write_text("# Index\n", encoding="utf-8")
        (self.vault / "wiki/coffee.md").write_text("---\ntype: note\n---\n# Coffee\n\nEspresso roasting notes.\n", encoding="utf-8")

    def install_plugin(self, version="2.39.0", enabled=True):
        plugin = self.vault / ".obsidian/plugins/obsidian-git"
        plugin.mkdir(parents=True, exist_ok=True)
        (plugin / "manifest.json").write_text(json.dumps({"id": "obsidian-git", "version": version}), encoding="utf-8")
        (self.vault / ".obsidian/community-plugins.json").write_text(json.dumps(["obsidian-git"] if enabled else []), encoding="utf-8")

    def configure(self):
        self.scaffold()
        self.write_config({"vaultPath": str(self.vault), "features": {"autoCommit": False}})
        self.install_plugin()
        subprocess.run(["git", "init", "-q", str(self.vault)], check=True, env=self.env)
        (self.vault / ".gitignore").write_text("\n".join(IGNORE_LINES) + "\n", encoding="utf-8")

    def build(self):
        result = self.run_cli("build", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=cwd or self.outside,
                              env=self.env, text=True, capture_output=True)

    def doctor(self, *args):
        result = self.run_cli("doctor", "--json", *args)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        report = json.loads(result.stdout)
        self.assert_contract(report)
        return report

    def doctor_in_process(self, vault=None):
        config, error = CLI_MODULE.load_config(self.config)
        selected = CLI_MODULE.resolve_vault(str(vault) if vault else None, config)
        with mock.patch.dict(os.environ, self.env, clear=True):
            report = CLI_MODULE.doctor(self.config, config, error, selected)
        self.assert_contract(report)
        return report

    # -- shared contract assertions ---------------------------------------
    def assert_contract(self, report):
        expected = LEGACY_KEYS | NEW_KEYS | (VAULT_KEYS if report["vault"] else set())
        self.assertEqual(set(report), expected)
        self.assertEqual(set(report["dependencies"]), DEPENDENCY_KEYS)
        self.assertEqual(report["schemaVersion"], 1)
        self.assertEqual(sorted(report["capabilities"]), sorted(CAPABILITIES))
        for name, capability in report["capabilities"].items():
            self.assertIn(capability["state"], {"ready", "supported", "unavailable", "unknown"}, name)
            self.assertEqual(capability["reason"] is None, capability["state"] == "ready", (name, capability))
        readiness = report["readiness"]
        self.assertEqual([stage["name"] for stage in readiness["stages"]], STAGES)
        first = next((stage for stage in readiness["stages"] if stage["status"] != "ok"), None)
        self.assertEqual(readiness["stage"], first["name"] if first else "ready")
        self.assertEqual(readiness["reason"], first["reason"] if first else None)
        workflow = readiness["stages"][3]
        self.assertEqual((workflow["syncOwner"], workflow["appState"]), ("obsidian-git", "not-probed"))
        self.assertEqual(readiness["stages"][0]["root"], str(ROOT))
        self.assertLessEqual(len(readiness["nextActions"]), 3)
        for action in readiness["nextActions"]:
            self.assert_safe_action(action)

    def assert_safe_action(self, action):
        self.assertIn(action["code"], ACTION_CODES)
        self.assertTrue(action["doc"].split("#")[0] and (ROOT / action["doc"].split("#")[0]).is_file(), action)
        argv = action["argv"]
        if argv is None:
            self.assertTrue(action["manual"], action)
            return
        self.assertEqual(argv[0], sys.executable)
        self.assertFalse(FORBIDDEN_TOKENS & set(argv), argv)
        if argv[1] == "-m":
            self.assertEqual(argv[1:5], ["-m", "pip", "install", "-r"])
            self.assertEqual(argv[5:], [str(ROOT / "requirements.txt")])
            self.assertTrue(action["manual"] and action["mutates"] and action["requiresReview"])
        else:
            self.assertTrue(Path(argv[1]).is_relative_to(ROOT / "scripts"), argv)
            self.assertIn(Path(argv[1]).name, {"bootstrap-vault.py", "obsidian-second-brain.py"})
            if Path(argv[1]).name == "obsidian-second-brain.py":
                self.assertEqual(argv[2], "build")
                self.assertTrue(action["mutates"] and action["requiresReview"])
            else:
                self.assertFalse(action["mutates"])
                self.assertEqual(action["env"], {"OBSIDIAN_AGENT_CONFIG": str(self.config)})

    def stage(self, report, name):
        return next(stage for stage in report["readiness"]["stages"] if stage["name"] == name)

    def codes(self, report):
        return [action["code"] for action in report["readiness"]["nextActions"]]

    def states(self, report):
        return {name: (cap["state"], cap["reason"]) for name, cap in report["capabilities"].items()}

    # -- selection states ---------------------------------------------------
    def test_missing_config_without_vault(self):
        report = self.doctor()
        self.assertEqual((report["readiness"]["stage"], report["readiness"]["reason"]), ("vaultSelected", "config-missing-no-vault"))
        self.assertEqual(self.codes(report), ["choose-vault"])
        self.assertEqual(set(self.states(report).values()), {("unavailable", "no-vault")})
        self.assertEqual([s["status"] for s in report["readiness"]["stages"][2:]], ["skipped"] * 4)

    def test_invalid_config_and_null_vault_are_distinct(self):
        self.write_config("{broken")
        invalid = self.run_cli("doctor", "--json")
        self.assertEqual(invalid.returncode, 1)  # unchanged legacy exit status
        report = json.loads(invalid.stdout)
        self.assert_contract(report)
        self.assertEqual(report["readiness"]["reason"], "config-invalid")
        self.assertEqual(self.codes(report), ["repair-config"])
        self.write_config({"vaultPath": None})
        report = self.doctor()
        self.assertEqual(report["readiness"]["reason"], "vault-null")
        self.assertEqual(self.codes(report), ["choose-vault"])

    def test_missing_vault_and_file_vault_are_distinct(self):
        self.write_config({"vaultPath": str(self.vault)})
        report = self.doctor()
        self.assertEqual((report["readiness"]["stage"], report["readiness"]["reason"]), ("vaultSelected", "vault-missing"))
        self.assertEqual(self.codes(report), ["preview-scaffold"])
        self.assertEqual(report["readiness"]["nextActions"][0]["argv"][2:], ["--vault", str(self.vault)])
        self.assertEqual(set(self.states(report).values()), {("unavailable", "vault-missing")})
        self.vault.write_text("not a vault")
        report = self.doctor()
        self.assertEqual(report["readiness"]["reason"], "vault-not-directory")

    def test_override_selection_source_and_invalid_config_override(self):
        self.scaffold()
        report = self.doctor("--vault", str(self.vault))
        self.assertEqual(self.stage(report, "vaultSelected")["source"], "override")
        self.write_config({"vaultPath": str(self.vault)})
        self.assertEqual(self.stage(self.doctor(), "vaultSelected")["source"], "config")

    def test_config_conflicts_replace_previews_that_setup_would_refuse(self):
        other = self.root / "other"
        other.mkdir()
        self.write_config({"vaultPath": str(other)})
        self.vault.mkdir()
        report = self.doctor("--vault", str(self.vault))
        self.assertEqual(report["readiness"]["reason"], "wiki-missing")
        self.assertEqual(self.codes(report), ["review-config-selection"])
        self.write_config("{broken")
        report = json.loads(self.run_cli("doctor", "--json", "--vault", str(self.vault)).stdout)
        self.assert_contract(report)
        self.assertEqual(self.codes(report), ["repair-config"])
        self.scaffold()
        report = json.loads(self.run_cli("doctor", "--json", "--vault", str(self.vault)).stdout)
        states = self.states(report)
        # Write commands and source-check refuse an invalid selected config;
        # reads and evidence-report do not consult it.
        self.assertEqual(states["reviewedWrites"], ("unavailable", "config-invalid"))
        self.assertEqual(states["sourceChecks"], ("unavailable", "config-invalid"))
        self.assertEqual(states["spanReads"], ("ready", None))
        self.assertEqual(states["evidenceReport"], ("ready", None))

    def test_suggested_setup_previews_run_and_write_nothing(self):
        def run_suggestion(report, code):
            action = next(action for action in report["readiness"]["nextActions"] if action["code"] == code)
            env = {**self.env, **action["env"]}
            before = tree_snapshot(self.root)
            result = subprocess.run(action["argv"], cwd=self.outside, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("planHash", json.loads(result.stdout))
            self.assertEqual(tree_snapshot(self.root), before)
        self.write_config({"vaultPath": str(self.vault)})
        run_suggestion(self.doctor(), "preview-scaffold")  # vault-missing
        self.vault.mkdir()
        run_suggestion(self.doctor(), "preview-scaffold")  # wiki-missing
        self.scaffold()
        report = self.doctor()
        self.assertEqual(report["readiness"]["reason"], "configure-not-applied")
        run_suggestion(report, "preview-configure")

    def test_symlinked_wiki_and_retrieval_cache_are_reported_as_unsafe(self):
        self.configure()
        real_wiki = self.root / "real-wiki"
        shutil.move(self.vault / "wiki", real_wiki)
        (self.vault / "wiki").symlink_to(real_wiki)
        report = self.doctor()
        self.assertEqual(report["readiness"]["reason"], "wiki-unsafe")
        self.assertEqual(self.states(report)["spanReads"], ("unavailable", "wiki-unsafe"))
        (self.vault / "wiki").unlink()
        shutil.move(real_wiki, self.vault / "wiki")
        self.build()
        shutil.move(self.vault / ".vault-meta", self.root / "real-meta")
        (self.vault / ".vault-meta").symlink_to(self.root / "real-meta")
        report = self.doctor()
        self.assertEqual(self.states(report)["lexicalSearch"], ("unavailable", "index-unsafe"))
        self.assertEqual(self.stage(report, "pendingRetrieval")["reason"], "index-unsafe")
        self.assertEqual(CLI_MODULE._next_actions("index-unsafe", self.vault, self.config)[0]["code"], "inspect-retrieval-cache")

    # -- scaffold and workflow states -----------------------------------------
    def test_wiki_missing_then_scaffold_only_keeps_local_markdown_usable(self):
        self.vault.mkdir()
        report = self.doctor("--vault", str(self.vault))
        self.assertEqual(report["readiness"]["reason"], "wiki-missing")
        self.assertEqual(self.codes(report), ["preview-scaffold"])
        (self.vault / "wiki").mkdir()
        self.assertEqual(self.doctor("--vault", str(self.vault))["readiness"]["reason"], "wiki-index-missing")
        self.scaffold()
        result = self.run_cli("doctor", "--json", "--vault", str(self.vault))
        self.assertEqual(result.returncode, 0)  # not-yet-configured is not an error exit
        report = json.loads(result.stdout)
        self.assertEqual((report["readiness"]["stage"], report["readiness"]["reason"]), ("workflow", "config-missing"))
        self.assertEqual(self.codes(report), ["preview-configure"])
        self.assertEqual(report["readiness"]["nextActions"][0]["argv"][2:], ["--vault", str(self.vault), "--configure"])
        states = self.states(report)
        self.assertEqual(states["spanReads"], ("ready", None))
        self.assertEqual(states["lexicalSearch"], ("unavailable", "index-missing"))
        self.assertEqual(states["reviewedWrites"], ("ready", None))

    def test_workflow_configuration_states(self):
        self.scaffold()
        self.write_config({"vaultPath": None})
        self.assertEqual(self.doctor("--vault", str(self.vault))["readiness"]["reason"], "config-vault-null")
        other = self.root / "other"
        other.mkdir()
        self.write_config({"vaultPath": str(other)})
        report = self.doctor("--vault", str(self.vault))
        self.assertEqual(report["readiness"]["reason"], "config-selects-other-vault")
        self.assertEqual(self.codes(report), ["review-config-selection"])
        for features in ({}, {"autoCommit": True}, None):
            self.write_config({"vaultPath": str(self.vault), **({"features": features} if features is not None else {})})
            self.assertEqual(self.doctor()["readiness"]["reason"], "configure-not-applied")
        self.write_config({"vaultPath": str(self.vault), "features": {"autoCommit": False}})
        report = self.doctor()
        self.assertEqual(report["readiness"]["reason"], "obsidian-git-missing")
        self.assertEqual(self.codes(report), ["install-obsidian-git"])
        self.install_plugin(version="2.38.0")
        self.assertEqual(self.doctor()["readiness"]["reason"], "obsidian-git-incompatible")
        self.install_plugin(enabled=False)
        self.assertEqual(self.doctor()["readiness"]["reason"], "obsidian-git-disabled")
        self.install_plugin()
        report = self.doctor()
        self.assertEqual(report["readiness"]["reason"], "vault-not-git-worktree")
        self.assertEqual(self.codes(report), ["set-up-vault-git"])
        subprocess.run(["git", "init", "-q", str(self.vault)], check=True, env=self.env)
        report = self.doctor()
        self.assertEqual(report["readiness"]["reason"], "cache-exclusions-ineffective")
        self.assertEqual(self.codes(report), ["preview-configure"])

    def test_symlinked_obsidian_settings_are_unknown(self):
        self.configure()
        real = self.root / "real-obsidian"
        shutil.move(self.vault / ".obsidian", real)
        (self.vault / ".obsidian").symlink_to(real)
        report = self.doctor()
        self.assertEqual(self.stage(report, "workflow")["status"], "unknown")
        self.assertEqual(report["readiness"]["reason"], "obsidian-settings-unsafe")

    def test_configured_ready_and_app_state_is_never_probed(self):
        self.configure()
        self.build()
        report = self.doctor()
        self.assertEqual(report["readiness"]["stage"], "ready")
        self.assertEqual(report["readiness"]["nextActions"], [])
        self.assertEqual(self.states(report)["lexicalSearch"], ("ready", None))
        # App-closed and app-open (workspace/plugin data written by a running app)
        # produce identical readiness: doctor does not probe the app.
        (self.vault / ".obsidian/workspace.json").write_text("{}", encoding="utf-8")
        (self.vault / ".obsidian/plugins/obsidian-git/data.json").write_text('{"autoSaveInterval": 5}', encoding="utf-8")
        opened = self.doctor()
        self.assertEqual((opened["readiness"], opened["capabilities"]), (report["readiness"], report["capabilities"]))

    # -- pending content/retrieval and optional features ----------------------
    def test_pending_content_and_stale_index_are_separate_from_source_truth(self):
        self.configure()
        self.build()
        (self.vault / "wiki/coffee.md").write_text("---\ntype: note\n---\n# Coffee\n\nNew grinder notes.\n", encoding="utf-8")
        report = self.doctor()
        self.assertEqual((report["readiness"]["stage"], report["readiness"]["reason"]), ("pendingRetrieval", "index-stale"))
        self.assertEqual(self.codes(report), ["build-index"])
        self.assertEqual(self.states(report)["lexicalSearch"], ("unavailable", "index-stale"))
        self.assertEqual(self.states(report)["spanReads"], ("ready", None))
        state_path = self.vault / ".vault-meta/lifecycle/state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"version": 1, "vault": str(self.vault), "pending": {"wiki/coffee.md": {"changed": True}}}))
        report = self.doctor()
        self.assertEqual((report["readiness"]["stage"], report["readiness"]["reason"]), ("pendingContent", "lifecycle-pending"))
        self.assertEqual(self.codes(report), ["finish-wiki-session"])
        self.assertEqual(self.states(report)["lexicalSearch"], ("unavailable", "content-pending"))
        state_path.write_text(json.dumps({"version": 1, "vault": str(self.vault), "pending": {}, "retrieval": {"token": "t-1", "owner": "o"}}))
        report = self.doctor()
        self.assertEqual((report["readiness"]["stage"], report["readiness"]["reason"]), ("pendingRetrieval", "retrieval-refresh-pending"))
        self.assertEqual(self.codes(report), ["build-index"])
        state_path.write_text(json.dumps({"version": 1, "vault": str(self.vault), "pending": {}, "retrieval": {"token": "t-1"}}))
        report = self.doctor()
        self.assertEqual((report["readiness"]["stage"], report["readiness"]["reason"]), ("pendingRetrieval", "retrieval-state-unknown"))
        self.assertEqual(self.stage(report, "pendingRetrieval")["status"], "unknown")
        self.assertEqual(self.states(report)["lexicalSearch"], ("unknown", "retrieval-state-unknown"))
        state_path.write_text("[]")
        report = self.doctor()
        self.assertEqual((report["readiness"]["stage"], report["readiness"]["reason"]), ("pendingContent", "lifecycle-state-unreadable"))
        self.assertEqual(self.stage(report, "pendingContent")["status"], "unknown")

    def test_semantic_cache_is_supported_but_never_probed(self):
        self.configure()
        self.build()
        self.assertEqual(self.states(self.doctor())["semanticRerank"], ("unavailable", "semantic-cache-missing"))
        (self.vault / ".vault-meta/retrieval/chunks.json").write_text("{}", encoding="utf-8")
        report = self.doctor()
        self.assertEqual(self.states(report)["semanticRerank"], ("supported", "service-not-probed"))
        self.assertEqual(report["readiness"]["stage"], "ready")
        # Hybrid search fuses with BM25, so a cache alone is not enough.
        (self.vault / "wiki/coffee.md").write_text("---\ntype: note\n---\n# Coffee\n\nChanged.\n", encoding="utf-8")
        self.assertEqual(self.states(self.doctor())["semanticRerank"], ("unavailable", "index-stale"))

    def test_missing_optional_parser_does_not_break_local_search(self):
        self.configure()
        self.build()
        real = importlib.util.find_spec
        with mock.patch("importlib.util.find_spec", lambda name, *a: None if name == "markdown_it" else real(name, *a)):
            report = self.doctor_in_process()
        states = self.states(report)
        self.assertEqual(states["sectionReads"], ("unavailable", "markdown-it-missing"))
        self.assertEqual(states["evidenceReport"], ("unavailable", "markdown-it-missing"))
        self.assertEqual(states["lexicalSearch"], ("ready", None))
        self.assertEqual(states["spanReads"], ("ready", None))
        self.assertEqual(report["readiness"]["stage"], "ready")
        self.assertFalse(report["readiness"]["stages"][0]["optional"]["markdownItPy"])

    def test_missing_required_dependency_and_git(self):
        self.configure()
        self.build()
        real = importlib.util.find_spec
        with mock.patch("importlib.util.find_spec", lambda name, *a: None if name == "yaml" else real(name, *a)):
            report = self.doctor_in_process()
        self.assertEqual((report["readiness"]["stage"], report["readiness"]["reason"]), ("package", "pyyaml-missing"))
        self.assertEqual(self.codes(report), ["install-requirements"])
        self.assertEqual(self.states(report)["lexicalSearch"], ("ready", None))
        self.assertEqual(self.states(report)["reviewedWrites"], ("unavailable", "pyyaml-missing"))
        real_which = shutil.which
        with mock.patch("shutil.which", lambda name, *a, **k: None if name == "git" else real_which(name, *a, **k)):
            report = self.doctor_in_process()
        self.assertEqual(report["readiness"]["reason"], "git-missing")
        self.assertEqual(report["capabilities"]["sourceChecks"]["limitations"], ["git-missing"])
        with mock.patch("shutil.which", lambda name, *a, **k: None if name == "jq" else real_which(name, *a, **k)):
            report = self.doctor_in_process()
        self.assertEqual(report["readiness"]["reason"], "jq-missing")

    # -- safety ---------------------------------------------------------------
    def test_discovery_writes_nothing_and_makes_no_network_requests(self):
        self.configure()
        self.build()
        (self.vault / ".vault-meta/retrieval/chunks.json").write_text("{}", encoding="utf-8")
        before = tree_snapshot(self.vault, self.config.parent)
        for args in (("--json",), (), ("--json", "--vault", str(self.vault))):
            self.assertEqual(self.run_cli("doctor", *args).returncode, 0)

        def refuse(*_args, **_kwargs):
            raise AssertionError("doctor attempted a network connection")
        with mock.patch.object(socket, "socket", refuse), mock.patch.object(socket, "create_connection", refuse):
            report = self.doctor_in_process()
        self.assertEqual(report["capabilities"]["semanticRerank"]["state"], "supported")
        self.assertEqual(tree_snapshot(self.vault, self.config.parent), before)

    # -- compatibility and presentation --------------------------------------
    def test_legacy_output_and_human_lines_are_preserved(self):
        self.configure()
        self.build()
        report = self.doctor()
        self.assertEqual(report["config"], {"path": str(self.config), "valid": True, "exists": True, "error": None})
        self.assertEqual((report["vault"], report["vaultExists"], report["wikiExists"]), (str(self.vault), True, True))
        self.assertEqual(report["retrieval"]["freshness"], "current-by-hash")
        self.assertEqual(report["lifecycle"], {"pending": False, "count": 0, "state": "clean"})
        human = self.run_cli("doctor")
        self.assertEqual(human.returncode, 0, human.stderr)
        lines = human.stdout.splitlines()
        self.assertEqual([line.split(":")[0] for line in lines[:5]], ["Vault", "Config", "Retrieval", "Lifecycle", "Cache exclusions"])
        self.assertEqual(lines[5], "Stage: ready")
        self.assertEqual([line.split(":")[0] for line in lines[6:13]], [f"Capability {name}" for name in CAPABILITIES])
        self.assertEqual(len(lines), 13)
        (self.vault / "wiki/coffee.md").write_text("---\ntype: note\n---\n# Coffee\n\nChanged.\n", encoding="utf-8")
        lines = self.run_cli("doctor").stdout.splitlines()
        self.assertEqual(lines[5], "Stage: pendingRetrieval (index-stale)")
        self.assertTrue(lines[13].startswith("Next: Rebuild the derived BM25 cache") and lines[13].endswith("[writes, review first]"))
        self.assertIn("build --vault '" + str(self.vault) + "'", lines[14])

    def test_pi_doctor_bridge_passes_readiness_through(self):
        self.configure()
        bridge = load_source("doctor_readiness_bridge", str(ROOT / "scripts/pi_wiki_tools.py"))
        with mock.patch.dict(os.environ, self.env, clear=True):
            result = bridge.run("doctor", {})
        self.assertTrue(result["ok"])
        self.assert_contract(result["payload"])
        self.assertEqual(result["payload"]["readiness"]["reason"], "index-missing")


if __name__ == "__main__":
    unittest.main()
