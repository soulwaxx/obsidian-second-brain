#!/usr/bin/env python3
"""The GitHub Copilot CLI and VS Code `connect` adapters, run through the real framework in a temporary HOME.

The registry is patched to the two real adapter modules. Nothing here touches the real home directory, runs
`copilot` or `code`, uses Git or the network.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import connect as connect_module  # noqa: E402
import connect_adapters  # noqa: E402
from connect_adapters import copilot_cli, copilot_vscode  # noqa: E402
from test_connect import ConnectCase, snapshot  # noqa: E402

LAUNCHER = str(ROOT / "scripts/obsidian-second-brain.mjs")
BLOCK_START = "<!-- obsidian-second-brain:start -->"
BLOCK_END = "<!-- obsidian-second-brain:end -->"
OVERRIDES = ("COPILOT_HOME", "APPDATA", "XDG_CONFIG_HOME", "OBSIDIAN_VAULT_PATH")


class CopilotCase(ConnectCase):
    platform = "darwin"

    def setUp(self):
        super().setUp()
        self.registry[:] = ["connect_adapters.copilot_cli", "connect_adapters.copilot_vscode"]
        environ = self.enterContext(mock.patch.dict(os.environ))
        for name in OVERRIDES:
            environ.pop(name, None)
        self.enterContext(mock.patch.object(copilot_vscode, "_platform", return_value=self.platform))
        self.cli_dir = self.home / ".copilot"
        self.vscode_dir = self.home / "Library/Application Support/Code/User"
        self.skills = self.cli_dir / "skills"

    def files_of(self, planned):
        return {item["role"]: item["path"] for item in planned["plan"]["files"] if item["role"] != "skill"}

    def written(self, before):
        """Paths under HOME that differ from a snapshot, as files only."""
        after = snapshot(self.home, self.config.parent)
        return {path for path in after if before.get(path) != after[path] and after[path][0] != "dir"}

    def files_under(self, root):
        """Files left below a directory."""
        return sorted(str(path) for path in root.rglob("*") if path.is_file()) if root.exists() else []

    def skill_paths(self, root):
        return {str(root / rel) for rel in connect_module.skill_sources()}

    def user_json(self, path, entry_key):
        return json.loads(path.read_text(encoding="utf-8"))[entry_key]["obsidian-wiki"]

    def doctor_state(self, name):
        return self.status()["agents"][name]

    def preview_with(self, env, *agents, extra=()):
        code, out, err = self.run_cli(*agents, *extra, "--json", env=env)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def apply_with(self, env, *agents, extra=()):
        planned = self.preview_with(env, *agents, extra=extra)
        code, out, err = self.run_cli(*agents, *extra, "--apply", "--confirm", planned["planHash"], "--json", env=env)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def remove_with(self, env, *agents):
        return self.apply_with(env, *agents, extra=("--remove",))


class CopilotCliTests(CopilotCase):
    def test_detection_is_by_the_configuration_directory_only(self):
        def detected():
            listed = json.loads(self.run_cli("--json")[1])
            return {item["name"]: item["detected"] for item in listed["agents"]}

        self.assertEqual(detected(), {"copilot-cli": False, "copilot-vscode": False})
        self.cli_dir.mkdir()
        self.assertEqual(detected(), {"copilot-cli": True, "copilot-vscode": False})
        self.vscode_dir.mkdir(parents=True)
        self.assertEqual(detected(), {"copilot-cli": True, "copilot-vscode": True})
        # COPILOT_HOME replaces ~/.copilot; a stray ~/.copilot no longer counts.
        elsewhere = self.root / "elsewhere"
        listed = json.loads(self.run_cli("--json", env={"COPILOT_HOME": str(elsewhere)})[1])
        self.assertFalse({item["name"]: item["detected"] for item in listed["agents"]}["copilot-cli"])
        elsewhere.mkdir()
        listed = json.loads(self.run_cli("--json", env={"COPILOT_HOME": str(elsewhere)})[1])
        self.assertTrue({item["name"]: item["detected"] for item in listed["agents"]}["copilot-cli"])
        by_name = {item["name"]: item for item in listed["agents"]}
        self.assertEqual(by_name["copilot-cli"]["verification"], "fixture-tested (live-unverified)")
        self.assertEqual(by_name["copilot-cli"]["mechanism"], "MCP entry + rules block + skill copies")
        self.assertLessEqual({"copilot-cli", "copilot-vscode"}, set(adapter_names()))  # both shipped adapters are registered

    def test_preview_lists_every_file_and_writes_nothing_else(self):
        before = snapshot(self.home, self.config.parent, self.vault)
        planned = self.preview("copilot-cli")
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))
        paths = {item["path"] for item in planned["plan"]["files"]}
        self.assertEqual(paths, {str(self.cli_dir / "mcp-config.json"), str(self.cli_dir / "copilot-instructions.md"),
                                 str(self.manifest), *self.skill_paths(self.skills)})
        self.assertTrue(all(item["op"] == "create" for item in planned["plan"]["files"]))
        self.assertEqual(planned["snippets"], [])  # user-level rules are written, so nothing to print
        self.assertTrue(any("wiki_batch_apply" in note for note in planned["notes"]))
        notes = " ".join(planned["notes"])
        self.assertIn("also covers VS Code", notes)
        self.assertIn("Dock or Finder", notes)
        self.apply("copilot-cli")
        written = {str(path) for path in self.written(before)}
        self.assertEqual(written, paths)  # nothing else under HOME, the config directory or the vault
        self.assertEqual(snapshot(self.vault), {k: v for k, v in before.items() if k.startswith(str(self.vault))})

    def test_entry_rules_and_skills(self):
        self.apply("copilot-cli")
        config = json.loads((self.cli_dir / "mcp-config.json").read_text(encoding="utf-8"))
        self.assertEqual(list(config), ["mcpServers"])
        self.assertEqual(config["mcpServers"]["obsidian-wiki"], {
            "type": "local", "command": str(self.node),
            "args": [LAUNCHER, "mcp", "--tools", "all"],
            "env": {"PYTHON": sys.executable, "OBSIDIAN_AGENT_CONFIG": str(self.config)},
            "tools": ["*"]})
        self.assertTrue(Path(config["mcpServers"]["obsidian-wiki"]["args"][0]).is_absolute())
        rules = (self.cli_dir / "copilot-instructions.md").read_text(encoding="utf-8")
        self.assertTrue(rules.startswith(BLOCK_START + "\n"))  # no front matter: the user-level file is plain Markdown
        self.assertTrue(rules.endswith(BLOCK_END + "\n"))
        self.assertIn("never edit vault files directly", rules)
        sources = connect_module.skill_sources()
        copied = {str(path.relative_to(self.skills)) for path in self.skills.rglob("*") if path.is_file()}
        self.assertEqual(copied, set(sources))
        self.assertTrue(all((self.skills / rel).read_bytes() == data for rel, data in sources.items()))
        self.assertFalse((self.skills / "wiki/scripts").exists())
        self.assertFalse((self.home / "Library").exists())  # the VS Code adapter was not selected

    def test_default_config_entry_has_only_the_interpreter_in_env(self):
        default = self.home / ".config/obsidian-second-brain/properties.json"
        default.parent.mkdir(parents=True)
        default.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "OSB_NODE": str(self.node)}), \
                mock.patch.object(connect_adapters, "REGISTRY", self.registry):
            os.environ.pop("OBSIDIAN_AGENT_CONFIG", None)
            self.assertEqual(connect_module.config_file_path(), default)
            for agent, path, key in (("copilot-cli", self.cli_dir / "mcp-config.json", "mcpServers"),
                                     ("copilot-vscode", self.vscode_dir / "mcp.json", "servers")):
                code, out, err = self.default_run(agent, "--json")
                planned = json.loads(out)
                code, out, err = self.default_run(agent, "--apply", "--confirm", planned["planHash"], "--json")
                self.assertEqual(code, 0, out + err)
                self.assertEqual(self.user_json(path, key)["env"], {"PYTHON": sys.executable})
        self.assertTrue((default.parent / "connect-manifest.json").exists())

    def default_run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = connect_module.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_copilot_home_moves_every_target(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        env = {"COPILOT_HOME": str(elsewhere)}
        planned = self.preview_with(env, "copilot-cli")
        self.assertEqual({item["path"] for item in planned["plan"]["files"]},
                         {str(elsewhere / "mcp-config.json"), str(elsewhere / "copilot-instructions.md"), str(self.manifest),
                          *self.skill_paths(elsewhere / "skills")})
        self.apply_with(env, "copilot-cli")
        self.assertFalse(self.cli_dir.exists())
        self.assertEqual(self.user_json(elsewhere / "mcp-config.json", "mcpServers")["type"], "local")
        self.remove_with(env, "copilot-cli")
        self.assertEqual(list(elsewhere.iterdir()), [])  # connect created only files, and removed them again

    def test_existing_config_is_preserved_and_removal_restores_it(self):
        self.cli_dir.mkdir()
        original = {
            "mcpServers": {
                "playwright": {"type": "local", "command": "npx", "args": ["@playwright/mcp@latest"], "env": {}, "tools": ["*"]},
                "context7": {"type": "http", "url": "https://mcp.context7.com/mcp", "headers": {"CONTEXT7_API_KEY": "$KEY"}, "tools": ["resolve-library-id"]},
            },
        }
        config = self.cli_dir / "mcp-config.json"
        config.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
        instructions = self.cli_dir / "copilot-instructions.md"
        instructions.write_text("# My Copilot rules\n\n- Prefer small diffs.\n", encoding="utf-8")
        (self.cli_dir / "settings.json").write_text('{\n  // my settings\n  "theme": "dark"\n}\n', encoding="utf-8")
        (self.cli_dir / "skills/mine").mkdir(parents=True)
        (self.cli_dir / "skills/mine/SKILL.md").write_text("---\nname: mine\ndescription: x\n---\n", encoding="utf-8")
        before = snapshot(self.cli_dir)
        self.apply("copilot-cli")
        merged = json.loads(config.read_text(encoding="utf-8"))
        for name, value in original["mcpServers"].items():
            self.assertEqual(merged["mcpServers"][name], value)
        self.assertIn("obsidian-wiki", merged["mcpServers"])
        self.assertTrue(instructions.read_text(encoding="utf-8").startswith("# My Copilot rules\n\n- Prefer small diffs.\n\n" + BLOCK_START))
        after = snapshot(self.cli_dir)
        for untouched in ("settings.json", "skills/mine/SKILL.md"):
            self.assertEqual(before[str(self.cli_dir / untouched)], after[str(self.cli_dir / untouched)])
        # A repeat apply plans nothing and writes nothing, not even the manifest.
        settled = snapshot(self.home, self.config.parent)
        again = self.apply("copilot-cli")
        self.assertTrue(all(item["op"] == "unchanged" for item in again["plan"]["files"]))
        self.assertEqual(settled, snapshot(self.home, self.config.parent))
        # Removal restores everything but our entries.
        self.apply("copilot-cli", extra=("--remove",))
        self.assertEqual(json.loads(config.read_text(encoding="utf-8")), original)
        self.assertEqual(instructions.read_text(encoding="utf-8"), "# My Copilot rules\n\n- Prefer small diffs.\n")
        self.assertEqual(snapshot(self.cli_dir, mtimes=False)[str(self.cli_dir / "settings.json")][0], before[str(self.cli_dir / "settings.json")][0])
        self.assertEqual(sorted(path.name for path in self.skills.iterdir()), ["mine"])
        self.assertFalse(self.manifest.exists())

    def test_conflicting_entry_is_refused_with_the_snippet(self):
        self.cli_dir.mkdir()
        (self.cli_dir / "mcp-config.json").write_text('{"mcpServers": {"obsidian-wiki": {"type": "local", "command": "mine", "args": [], "tools": ["*"]}}}\n', encoding="utf-8")
        before = snapshot(self.home, self.config.parent)
        refused = self.refused("copilot-cli")
        refusal = refused["refusals"][0]
        self.assertEqual((refusal["agent"], refusal["code"]), ("copilot-cli", "entry-differs"))
        self.assertIn('"tools"', refusal["snippet"])
        self.assertEqual(before, snapshot(self.home, self.config.parent))

    def test_concurrent_edit_after_the_preview_is_refused(self):
        self.cli_dir.mkdir()
        config = self.cli_dir / "mcp-config.json"
        config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        planned = self.preview("copilot-cli")
        config.write_text('{"mcpServers": {"other": {"command": "x", "args": [], "tools": ["*"]}}}\n', encoding="utf-8")
        before = snapshot(self.home, self.config.parent)
        code, out, err = self.run_cli("copilot-cli", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual(code, 1, out)
        self.assertEqual(json.loads(out)["code"], "stale-plan")
        self.assertEqual(before, snapshot(self.home, self.config.parent))

    def test_doctor_reports_the_connection_and_stale_skill_copies(self):
        self.cli_dir.mkdir()
        self.assertEqual(self.doctor_state("copilot-cli")["state"], "absent")
        self.assertTrue(self.doctor_state("copilot-cli")["detected"])
        self.apply("copilot-cli")
        state = self.doctor_state("copilot-cli")
        self.assertEqual((state["state"], state["reasons"], state["verification"]), ("connected", [], "fixture-tested (live-unverified)"))
        self.assertEqual(self.doctor_state("copilot-vscode")["state"], "absent")
        (self.skills / "wiki-save/SKILL.md").write_text("customized\n", encoding="utf-8")
        (self.skills / "wiki-ingest/SKILL.md").unlink()
        state = self.doctor_state("copilot-cli")
        self.assertEqual(state["state"], "stale")
        self.assertEqual({reason["code"] for reason in state["reasons"]}, {"skill-modified", "skill-missing"})
        # An entry edited after connect is reported too.
        config = self.cli_dir / "mcp-config.json"
        edited = json.loads(config.read_text(encoding="utf-8"))
        edited["mcpServers"]["obsidian-wiki"]["args"][0] = "/old/place/scripts/obsidian-second-brain.mjs"
        config.write_text(json.dumps(edited), encoding="utf-8")
        self.assertIn("mcp-entry-modified", {reason["code"] for reason in self.doctor_state("copilot-cli")["reasons"]})


class CopilotVscodeTests(CopilotCase):
    def test_detection_follows_the_platform_user_data_folder(self):
        def detected():
            listed = json.loads(self.run_cli("--json")[1])
            return {item["name"]: item["detected"] for item in listed["agents"]}["copilot-vscode"]

        self.assertFalse(detected())
        (self.home / ".config/Code/User").mkdir(parents=True)  # the Linux folder does not count on macOS
        self.assertFalse(detected())
        self.vscode_dir.mkdir(parents=True)
        self.assertTrue(detected())
        with mock.patch.object(copilot_vscode, "_platform", return_value="linux"):
            self.assertTrue(detected())
            shutil.rmtree(self.home / ".config")
            self.assertFalse(detected())
        with mock.patch.object(copilot_vscode, "_platform", return_value="win32"):
            appdata = self.root / "AppData"
            (appdata / "Code/User").mkdir(parents=True)
            self.assertFalse(detected())
            listed = json.loads(self.run_cli("--json", env={"APPDATA": str(appdata)})[1])
            self.assertTrue({item["name"]: item["detected"] for item in listed["agents"]}["copilot-vscode"])

    def test_preview_lists_every_file_prints_the_repository_snippet_and_writes_nothing_else(self):
        before = snapshot(self.home, self.config.parent, self.vault)
        planned = self.preview("copilot-vscode")
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))
        instructions = self.cli_dir / "copilot-instructions.md"  # the file the Copilot harness reads in every chat
        paths = {item["path"] for item in planned["plan"]["files"]}
        self.assertEqual(paths, {str(self.vscode_dir / "mcp.json"), str(instructions), str(self.manifest), *self.skill_paths(self.skills)})
        snippets = {item["destination"]: item["content"] for item in planned["snippets"]}
        self.assertEqual(list(snippets), [".github/copilot-instructions.md"])  # no workspace MCP snippet
        self.assertIn("never edit vault files directly", snippets[".github/copilot-instructions.md"])
        self.assertNotIn("{{rules}}", snippets[".github/copilot-instructions.md"])
        self.assertTrue(any("wiki_batch_apply" in note for note in planned["notes"]))
        notes = " ".join(planned["notes"])
        self.assertIn("connect `copilot-cli` instead", notes)  # the Copilot harness needs the CLI adapter, not this one
        self.assertIn("deprecated Local harness", notes)
        self.assertIn("Dock or Finder", notes)  # COPILOT_HOME only reaches VS Code when its environment has it
        self.assertFalse((self.cli_dir / "instructions").exists())
        self.apply("copilot-vscode")
        self.assertEqual({str(path) for path in self.written(before)}, paths)
        self.assertEqual(list(self.root.rglob(".vscode")) + list(self.root.rglob(".github")), [])  # never a project file

    def test_entry_rules_and_skills(self):
        self.apply("copilot-vscode")
        document = json.loads((self.vscode_dir / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(list(document), ["servers"])
        self.assertEqual(document["servers"]["obsidian-wiki"], {
            "type": "stdio", "command": str(self.node),
            "args": [LAUNCHER, "mcp", "--tools", "all"],
            "env": {"PYTHON": sys.executable, "OBSIDIAN_AGENT_CONFIG": str(self.config)}})
        rules = (self.cli_dir / "copilot-instructions.md").read_text(encoding="utf-8")
        self.assertTrue(rules.startswith(BLOCK_START + "\n"))  # plain Markdown: no `applyTo` front matter, so every chat gets it
        self.assertTrue(rules.endswith(BLOCK_END + "\n"))
        self.assertNotIn("applyTo", rules)
        self.assertEqual(json.loads(self.manifest.read_text())["agents"]["copilot-vscode"]["rules"]["header"], "")
        self.assertFalse((self.cli_dir / "instructions").exists())
        sources = connect_module.skill_sources()
        copied = {str(path.relative_to(self.skills)) for path in self.skills.rglob("*") if path.is_file()}
        self.assertEqual(copied, set(sources))
        self.assertFalse((self.cli_dir / "mcp-config.json").exists())  # that file belongs to the CLI adapter

    def test_linux_user_data_folder(self):
        with mock.patch.object(copilot_vscode, "_platform", return_value="linux"):
            planned = self.preview("copilot-vscode")
            self.assertEqual(self.files_of(planned)["mcp"], str(self.home / ".config/Code/User/mcp.json"))
            self.apply("copilot-vscode")
        self.assertEqual(self.user_json(self.home / ".config/Code/User/mcp.json", "servers")["type"], "stdio")
        self.assertFalse((self.home / "Library").exists())

    def test_existing_profile_mcp_json_is_preserved_and_removal_restores_it(self):
        self.vscode_dir.mkdir(parents=True)
        original = {
            "inputs": [{"type": "promptString", "id": "api-key", "description": "API key", "password": True}],
            "servers": {
                "perplexity": {"type": "stdio", "command": "npx", "args": ["-y", "server-perplexity-ask"], "env": {"KEY": "${input:api-key}"}},
                "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
            },
        }
        mcp = self.vscode_dir / "mcp.json"
        mcp.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
        (self.vscode_dir / "settings.json").write_text('{\n  // keep\n  "editor.fontSize": 14\n}\n', encoding="utf-8")
        instructions = self.cli_dir / "copilot-instructions.md"
        before = snapshot(self.vscode_dir)
        self.apply("copilot-vscode")
        merged = json.loads(mcp.read_text(encoding="utf-8"))
        self.assertEqual(merged["inputs"], original["inputs"])
        for name, value in original["servers"].items():
            self.assertEqual(merged["servers"][name], value)
        self.assertEqual(snapshot(self.vscode_dir / "settings.json")[str(self.vscode_dir / "settings.json")],
                         before[str(self.vscode_dir / "settings.json")])
        settled = snapshot(self.home, self.config.parent)
        again = self.apply("copilot-vscode")
        self.assertTrue(all(item["op"] == "unchanged" for item in again["plan"]["files"]))
        self.assertEqual(settled, snapshot(self.home, self.config.parent))
        self.apply("copilot-vscode", extra=("--remove",))
        self.assertEqual(json.loads(mcp.read_text(encoding="utf-8")), original)
        self.assertFalse(instructions.exists())  # connect created the file with only the block, so removal deletes it
        self.assertFalse(self.cli_dir.exists())  # and so is ~/.copilot, which connect created
        self.assertFalse(self.manifest.exists())

    def test_jsonc_profile_config_is_refused_and_the_snippet_printed(self):
        self.vscode_dir.mkdir(parents=True)
        mcp = self.vscode_dir / "mcp.json"
        for content in ('{\n  // my servers\n  "servers": {}\n}\n',
                        '{\n  "servers": {\n    "a": {"type": "stdio", "command": "a"}, /* old */\n  },\n}\n'):
            with self.subTest(content=content):
                mcp.write_text(content, encoding="utf-8")
                before = snapshot(self.home, self.config.parent)
                # The refusal covers the whole selection: the CLI adapter writes nothing either.
                refused = self.refused("copilot-vscode", "copilot-cli")
                self.assertEqual(refused["code"], "connect-refused")
                self.assertEqual([(r["agent"], r["code"], r["path"]) for r in refused["refusals"]],
                                 [("copilot-vscode", "config-unparseable", str(mcp))])
                snippet = json.loads(refused["refusals"][0]["snippet"])
                self.assertEqual(snippet["servers"]["obsidian-wiki"]["type"], "stdio")
                self.assertEqual(before, snapshot(self.home, self.config.parent))
                self.assertFalse(self.cli_dir.exists())
                code, _, err = self.run_cli("copilot-vscode")
                self.assertEqual(code, 1)
                self.assertIn("Add this by hand instead", err)
                self.assertIn('"servers"', err)

    def test_user_text_in_copilot_instructions_is_kept(self):
        rules = self.cli_dir / "copilot-instructions.md"
        # A file the user already has gets only the block and keeps its content on removal.
        rules.parent.mkdir(parents=True)
        rules.write_text("# mine\n", encoding="utf-8")
        self.apply("copilot-vscode")
        self.assertTrue(rules.read_text(encoding="utf-8").startswith("# mine\n\n" + BLOCK_START))
        self.apply("copilot-vscode", extra=("--remove",))
        self.assertEqual(rules.read_text(encoding="utf-8"), "# mine\n")
        self.assertTrue(self.cli_dir.is_dir())  # it existed before connect
        # A file connect created and the user then extended keeps the user's text.
        rules.unlink()
        self.apply("copilot-vscode")
        with rules.open("a", encoding="utf-8") as stream:
            stream.write("\nmy own instruction\n")
        self.apply("copilot-vscode", extra=("--remove",))
        self.assertEqual(rules.read_text(encoding="utf-8"), "\nmy own instruction\n")

    def test_copilot_home_moves_rules_and_skills_but_not_the_profile_file(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        env = {"COPILOT_HOME": str(elsewhere)}
        self.apply_with(env, "copilot-vscode")
        self.assertTrue((elsewhere / "copilot-instructions.md").is_file())
        self.assertTrue((elsewhere / "skills/wiki/SKILL.md").is_file())
        self.assertTrue((self.vscode_dir / "mcp.json").is_file())
        self.assertFalse(self.cli_dir.exists())

    def test_both_adapters_share_the_skills_directory_and_removal_is_per_adapter(self):
        self.apply("copilot-cli", "copilot-vscode")
        records = json.loads(self.manifest.read_text())["agents"]
        self.assertEqual(set(records), {"copilot-cli", "copilot-vscode"})
        again = self.apply("copilot-cli", "copilot-vscode")
        self.assertTrue(all(item["op"] == "unchanged" for item in again["plan"]["files"]))
        self.apply("copilot-vscode", extra=("--remove",))
        self.assertFalse((self.vscode_dir / "mcp.json").exists())
        self.assertTrue((self.cli_dir / "mcp-config.json").exists())  # the other adapter stays connected
        self.assertTrue((self.skills / "wiki/SKILL.md").exists())  # still used by copilot-cli
        self.assertIn("obsidian-second-brain:start", (self.cli_dir / "copilot-instructions.md").read_text(encoding="utf-8"))  # still used by copilot-cli
        self.assertEqual(self.doctor_state("copilot-cli")["state"], "connected")
        self.assertEqual(self.doctor_state("copilot-vscode")["state"], "absent")
        self.apply("copilot-cli", extra=("--remove",))
        self.assertEqual(self.files_under(self.cli_dir), [])
        self.assertFalse(self.manifest.exists())

    def test_the_rules_file_and_skills_are_shared_and_removed_with_the_last_owner_in_either_order(self):
        rules = self.cli_dir / "copilot-instructions.md"
        for sequential in (False, True):
            for removal in (("copilot-cli", "copilot-vscode"), ("copilot-vscode", "copilot-cli")):
                with self.subTest(sequential=sequential, removal=removal):
                    if sequential:
                        self.apply("copilot-cli")
                        self.apply("copilot-vscode")
                    else:
                        self.apply("copilot-cli", "copilot-vscode")
                    self.assertEqual(rules.read_text(encoding="utf-8").count(BLOCK_START), 1)  # written once
                    self.apply(removal[0], extra=("--remove",))
                    self.assertEqual(rules.read_text(encoding="utf-8").count(BLOCK_START), 1)
                    self.assertTrue((self.skills / "wiki/SKILL.md").is_file())
                    self.apply(removal[1], extra=("--remove",))
                    self.assertFalse(rules.exists())
                    self.assertFalse(self.skills.exists())
                    self.assertFalse(self.cli_dir.exists())
                    self.assertFalse(self.manifest.exists())

    def test_preview_warns_about_duplicate_registration_when_both_are_selected_or_connected(self):
        def warnings(planned):
            return [note for note in planned["notes"] if "register the same MCP server" in note]

        self.assertEqual(warnings(self.preview("copilot-cli")), [])
        self.assertEqual(warnings(self.preview("copilot-vscode")), [])
        both = warnings(self.preview("copilot-cli", "copilot-vscode"))
        self.assertEqual(len(both), 1)
        self.assertIn("copilot-cli and copilot-vscode (selected together)", both[0])
        self.apply("copilot-cli")
        self.assertEqual(len(warnings(self.preview("copilot-vscode"))), 1)
        self.assertEqual(warnings(self.preview("copilot-cli", "copilot-vscode", extra=("--remove",))), [])

    def test_removing_the_vscode_adapter_leaves_no_detection_behind(self):
        # Connecting copilot-vscode creates ~/.copilot (rules, skills); after removal neither agent may look detected.
        self.vscode_dir.mkdir(parents=True)
        self.assertEqual(self.doctor_state("copilot-cli")["state"], "absent")
        self.assertFalse(self.doctor_state("copilot-cli")["detected"])
        self.apply("copilot-vscode")
        self.assertTrue(self.doctor_state("copilot-cli")["detected"])
        self.apply("copilot-vscode", extra=("--remove",))
        cli = self.doctor_state("copilot-cli")
        self.assertEqual((cli["state"], cli["detected"]), ("absent", False))
        self.assertFalse(self.cli_dir.exists())
        self.assertTrue(self.vscode_dir.is_dir())  # VS Code's own folder existed before connect and stays

    def test_doctor_reports_connected_and_stale(self):
        self.vscode_dir.mkdir(parents=True)
        self.assertEqual(self.doctor_state("copilot-vscode")["state"], "absent")
        self.apply("copilot-vscode")
        state = self.doctor_state("copilot-vscode")
        self.assertEqual((state["state"], state["reasons"], state["detected"]), ("connected", [], True))
        rules = self.cli_dir / "copilot-instructions.md"
        rules.write_text(rules.read_text(encoding="utf-8").replace("planHash", "changed"), encoding="utf-8")
        (self.skills / "wiki/SKILL.md").write_text("old\n", encoding="utf-8")
        mcp = self.vscode_dir / "mcp.json"
        document = json.loads(mcp.read_text(encoding="utf-8"))
        del document["servers"]["obsidian-wiki"]
        mcp.write_text(json.dumps(document), encoding="utf-8")
        state = self.doctor_state("copilot-vscode")
        self.assertEqual(state["state"], "stale")
        self.assertEqual({reason["code"] for reason in state["reasons"]}, {"rules-block-modified", "skill-modified", "mcp-entry-missing"})


class NoAgentProcessTests(CopilotCase):
    def test_connect_runs_no_copilot_or_code_binary_and_leaves_the_vault_alone(self):
        shims = self.root / "shims"
        shims.mkdir()
        log = self.root / "shim.log"
        for name in ("copilot", "code", "code-insiders", "git", "npx", "curl"):
            shim = shims / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 97\n', encoding="utf-8")
            shim.chmod(0o755)
        vault_before = snapshot(self.vault)
        env = {"PATH": f"{shims}:/usr/bin:/bin"}
        self.assertEqual(self.run_cli("--json", env=env)[0], 0)
        self.apply_with(env, "copilot-cli", "copilot-vscode")
        self.remove_with(env, "copilot-cli", "copilot-vscode")
        self.assertFalse(log.exists(), log.read_text() if log.exists() else "")
        self.assertEqual(vault_before, snapshot(self.vault))


def adapter_names():
    adapters, errors = connect_module.load_adapters(strict=False)
    assert not errors, errors
    return [adapter.name for adapter in adapters]


class AdapterContractTests(unittest.TestCase):
    def test_both_adapters_are_registered_and_valid(self):
        self.assertIn("connect_adapters.copilot_cli", connect_adapters.REGISTRY)
        self.assertIn("connect_adapters.copilot_vscode", connect_adapters.REGISTRY)
        self.assertEqual(copilot_cli.ADAPTER.name, "copilot-cli")
        self.assertEqual(copilot_vscode.ADAPTER.name, "copilot-vscode")
        self.assertEqual(copilot_cli.ADAPTER.mcp.key_path, ("mcpServers",))
        self.assertEqual(copilot_vscode.ADAPTER.mcp.key_path, ("servers",))
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.assertEqual(copilot_cli.copilot_home(home, {}), home / ".copilot")
            self.assertEqual(copilot_cli.copilot_home(home, {"COPILOT_HOME": ""}), home / ".copilot")
            self.assertEqual(copilot_cli.copilot_home(home, {"COPILOT_HOME": "/x"}), Path("/x"))


class CopilotLinuxCase(CopilotCase):
    platform = "linux"


class LinuxEndToEndTests(CopilotLinuxCase):
    def test_connect_both_on_linux_and_remove(self):
        self.vscode_dir = self.home / ".config/Code/User"
        self.apply("copilot-cli", "copilot-vscode")
        self.assertEqual(self.user_json(self.vscode_dir / "mcp.json", "servers")["command"], str(self.node))
        self.apply("copilot-cli", "copilot-vscode", extra=("--remove",))
        self.assertEqual(self.files_under(self.cli_dir) + self.files_under(self.vscode_dir), [])
        self.assertFalse(self.cli_dir.exists())
        self.assertFalse(self.vscode_dir.exists())  # connect created Code/User, so it removes it again


if __name__ == "__main__":
    unittest.main()
