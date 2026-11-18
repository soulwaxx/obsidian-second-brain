#!/usr/bin/env python3
"""The OpenCode `connect` adapter, exercised through the real framework in a temporary HOME.

Nothing here touches the real home directory, OpenCode, Git or the network. Shapes follow
https://opencode.ai/docs/config/ (~/.config/opencode/opencode.json or opencode.jsonc),
https://opencode.ai/docs/mcp-servers/ (`mcp`, `local`), https://opencode.ai/docs/rules/ (AGENTS.md) and
https://opencode.ai/docs/skills/ (~/.config/opencode/skills).
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import connect as connect_module  # noqa: E402
import connect_adapters  # noqa: E402
from test_connect import SHIMMED, ConnectCase, snapshot  # noqa: E402

LAUNCHER = str(ROOT / "scripts/obsidian-second-brain.mjs")
START, END = "<!-- obsidian-second-brain:start", "<!-- obsidian-second-brain:end"
# A realistic ~/.config/opencode/opencode.json: schema, model, provider, other MCP servers, permission, instructions.
EXISTING = {
    "$schema": "https://opencode.ai/config.json",
    "model": "anthropic/claude-sonnet-4-5",
    "autoupdate": True,
    "provider": {"anthropic": {"options": {"apiKey": "{env:ANTHROPIC_API_KEY}"}}},
    "mcp": {
        "context7": {"type": "remote", "url": "https://mcp.context7.com/mcp", "headers": {"CONTEXT7_API_KEY": "{env:CONTEXT7_API_KEY}"}, "enabled": True},
        "everything": {"type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server-everything"],
                       "environment": {"LOG_LEVEL": "info"}, "enabled": False},
    },
    "permission": {"*": "ask", "bash": {"*": "ask", "git *": "allow"}, "edit": "deny"},
    "instructions": ["CONTRIBUTING.md"],
}


def dump(value) -> str:
    return json.dumps(value, indent=2) + "\n"


class OpenCodeCase(ConnectCase):
    def setUp(self):
        super().setUp()
        self.registry[:] = ["connect_adapters.opencode"]  # only the real adapter, not the framework fixtures
        self.dir = self.home / ".config/opencode"
        self.mcp = self.dir / "opencode.json"
        self.jsonc = self.dir / "opencode.jsonc"
        self.rules = self.dir / "AGENTS.md"
        self.skills = self.dir / "skills"
        self.project = self.root / "project"  # stands in for the user's repository; connect must never write here
        self.project.mkdir()

    def entry(self, path=None):
        return json.loads((path or self.mcp).read_text(encoding="utf-8"))["mcp"]["obsidian-wiki"]

    def preview_with(self, env, *agents):
        code, out, err = self.run_cli(*agents, "--json", env=env)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def apply_with(self, env, *agents):
        planned = self.preview_with(env, *agents)
        code, out, err = self.run_cli(*agents, "--apply", "--confirm", planned["planHash"], "--json", env=env)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def written(self):
        return {str(path) for path in self.home.rglob("*") if path.is_file()}


class OpenCodeAdapterTests(OpenCodeCase):
    def test_detection_uses_only_the_config_directory(self):
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        agent = json.loads(out)["agents"][0]
        self.assertEqual((agent["name"], agent["detected"], agent["state"]), ("opencode", False, "absent"))
        self.assertEqual(agent["verification"], "fixture-tested (live-unverified)")
        self.assertEqual(agent["mechanism"], "MCP entry + rules block + skill copies")
        self.dir.mkdir(parents=True)
        before = snapshot(self.home, self.config.parent, self.vault)
        agent = json.loads(self.run_cli("--json")[1])["agents"][0]
        self.assertEqual((agent["detected"], agent["state"]), (True, "absent"))
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))

    def test_preview_lists_every_touched_file_and_writes_nothing(self):
        before = snapshot(self.root)
        planned = self.preview("opencode")
        self.assertEqual(before, snapshot(self.root))
        files = {item["path"]: item for item in planned["plan"]["files"]}
        expected = {str(self.mcp), str(self.rules), str(self.manifest), *(str(self.skills / rel) for rel in connect_module.skill_sources())}
        self.assertEqual(set(files), expected)  # nothing in a project, no second config file
        self.assertTrue(all(item["op"] == "create" for item in files.values()))
        self.assertEqual((files[str(self.mcp)]["role"], files[str(self.rules)]["role"]), ("mcp", "rules"))
        self.assertEqual(files[str(self.mcp)]["entry"]["type"], "local")
        code, human, _ = self.run_cli("opencode")
        self.assertEqual(code, 0)
        for path in (self.mcp, self.rules, self.manifest, self.skills):
            self.assertIn(str(path), human)

    def test_apply_with_no_existing_config_writes_the_documented_entry(self):
        applied = self.apply("opencode")
        self.assertTrue(applied["applied"])
        self.assertEqual(json.loads(self.mcp.read_text(encoding="utf-8")),
                         {"mcp": {"obsidian-wiki": {
                             "type": "local",
                             "command": [str(self.node), LAUNCHER, "mcp", "--tools", "all"],
                             "environment": {"PYTHON": sys.executable, "OBSIDIAN_AGENT_CONFIG": str(self.config)},
                             "enabled": True}}})
        command = self.entry()["command"]
        self.assertTrue(Path(command[0]).is_absolute() and Path(command[1]).is_absolute())
        self.assertNotIn("npx", json.dumps(self.entry()))
        self.assertEqual(self.written(), {str(self.mcp), str(self.rules), *(str(self.skills / rel) for rel in connect_module.skill_sources())})
        self.assertTrue(self.manifest.is_file())
        self.assertFalse(self.jsonc.exists())  # never a second global config
        self.assertEqual(os.listdir(self.project), [])

    def test_default_config_entry_carries_only_the_interpreter(self):
        # Without OBSIDIAN_AGENT_CONFIG the default config is selected and the entry pins nothing else.
        default = self.home / ".config/obsidian-second-brain/properties.json"
        default.parent.mkdir(parents=True)
        default.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        values = {"HOME": str(self.home), "OSB_NODE": str(self.node)}

        def run(*argv):
            out = io.StringIO()
            with mock.patch.dict(os.environ, values), mock.patch.object(connect_adapters, "REGISTRY", self.registry), \
                    contextlib.redirect_stdout(out):
                os.environ.pop("OBSIDIAN_AGENT_CONFIG", None)
                os.environ.pop("OBSIDIAN_VAULT_PATH", None)
                self.assertEqual(connect_module.main(list(argv)), 0)
            return json.loads(out.getvalue())

        planned = run("opencode", "--json")
        run("opencode", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual(self.entry()["environment"], {"PYTHON": sys.executable})
        self.assertTrue((default.parent / "connect-manifest.json").is_file())

    def test_rules_block_and_skills_are_placed_in_the_opencode_directories(self):
        self.dir.mkdir(parents=True)
        mine = "# My OpenCode rules\n\nBe brief.\n"
        self.rules.write_text(mine, encoding="utf-8")
        other = self.skills / "my-own-skill"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("---\nname: my-own-skill\ndescription: mine\n---\n", encoding="utf-8")
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "CLAUDE.md").write_text("claude rules\n", encoding="utf-8")
        before_claude = snapshot(claude)
        self.apply("opencode")
        text = self.rules.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(mine))  # the user's text stays first and untouched
        self.assertEqual((text.count(START), text.count(END)), (1, 1))
        self.assertIn(f'Configured Obsidian wiki: "{self.vault / "wiki"}"', text)
        for name in connect_module.SKILL_NAMES:
            body = (self.skills / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(body.startswith("---\nname: " + name + "\n"), name)
        self.assertTrue((self.skills / "wiki/references/tool-surface.md").is_file())
        self.assertFalse((self.skills / "wiki/scripts").exists())
        self.assertEqual(before_claude, snapshot(claude))  # ~/.claude is never written
        self.assertFalse((self.home / ".agents").exists())
        self.apply("opencode", extra=("--remove",))
        self.assertEqual(self.rules.read_text(encoding="utf-8"), mine)
        self.assertEqual(sorted(path.name for path in self.skills.iterdir()), ["my-own-skill"])

    def test_existing_realistic_config_is_preserved_repeat_is_a_no_op_and_removal_restores_it(self):
        self.dir.mkdir(parents=True)
        original = dump(EXISTING)
        self.mcp.write_text(original, encoding="utf-8")
        self.apply("opencode")
        merged = json.loads(self.mcp.read_text(encoding="utf-8"))
        self.assertEqual({key: value for key, value in merged["mcp"].items() if key != "obsidian-wiki"}, EXISTING["mcp"])
        self.assertEqual({key: value for key, value in merged.items() if key != "mcp"}, {key: value for key, value in EXISTING.items() if key != "mcp"})
        self.assertIn("{env:ANTHROPIC_API_KEY}", self.mcp.read_text(encoding="utf-8"))  # substitution text is untouched
        self.assertEqual(self.entry()["type"], "local")
        backups = sorted((self.config.parent / "connect-backups").rglob("*-opencode.json"))
        self.assertEqual([path.read_text(encoding="utf-8") for path in backups], [original])
        before = snapshot(self.home, self.config.parent)
        again = self.apply("opencode")
        self.assertTrue(all(item["op"] == "unchanged" for item in again["plan"]["files"]))
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        self.apply("opencode", extra=("--remove",))
        self.assertEqual(self.mcp.read_text(encoding="utf-8"), original)
        self.assertFalse(self.skills.exists())
        self.assertFalse(self.rules.exists())
        self.assertFalse(self.manifest.exists())

    def test_files_connect_created_are_removed_with_it(self):
        self.apply("opencode")
        self.apply("opencode", extra=("--remove",))
        for path in (self.mcp, self.rules, self.skills, self.manifest):
            self.assertFalse(path.exists(), path)

    def test_unmergeable_or_conflicting_config_is_refused_with_a_snippet(self):
        self.dir.mkdir(parents=True)
        cases = [
            ('{\n  // my servers\n  "mcp": {}\n}\n', "config-unparseable"),
            ('{"mcp": {"a": {"type": "local", "command": ["x"]},}}\n', "config-unparseable"),
            ('{"mcp": []}\n', "config-shape"),
            ('{"mcp": {"obsidian-wiki": {"type": "local", "command": ["mine"]}}}\n', "entry-differs"),
        ]
        for content, code in cases:
            with self.subTest(code=code, content=content):
                self.mcp.write_text(content, encoding="utf-8")
                before = snapshot(self.home, self.config.parent)
                refused = self.refused("opencode")
                refusal = refused["refusals"][0]
                self.assertEqual((refused["code"], refusal["agent"], refusal["code"]), ("connect-refused", "opencode", code))
                self.assertIn('"mcp"', refusal["snippet"])
                self.assertIn('"type": "local"', refusal["snippet"])
                self.assertIn('"environment"', refusal["snippet"])
                self.assertEqual(before, snapshot(self.home, self.config.parent))  # nothing written, rules and skills included
        self.assertFalse(self.skills.exists() or self.rules.exists())

    def test_jsonc_with_comments_is_refused_and_no_second_config_is_created(self):
        self.dir.mkdir(parents=True)
        commented = '{\n  "$schema": "https://opencode.ai/config.json",\n  // keep my model\n  "model": "anthropic/claude-sonnet-4-5",\n}\n'
        self.jsonc.write_text(commented, encoding="utf-8")
        before = snapshot(self.home, self.config.parent)
        refused = self.refused("opencode")
        refusal = refused["refusals"][0]
        self.assertEqual((refusal["code"], refusal["path"]), ("config-unparseable", str(self.jsonc)))
        self.assertIn('"obsidian-wiki"', refusal["snippet"])
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        self.assertFalse(self.mcp.exists())

    def test_strict_json_in_opencode_jsonc_is_edited_in_place(self):
        self.dir.mkdir(parents=True)
        original = dump(EXISTING)
        self.jsonc.write_text(original, encoding="utf-8")
        planned = self.preview("opencode")
        self.assertIn(str(self.jsonc), {item["path"] for item in planned["plan"]["files"]})
        self.assertNotIn(str(self.mcp), {item["path"] for item in planned["plan"]["files"]})
        self.apply("opencode")
        self.assertFalse(self.mcp.exists())  # no second global config next to the existing one
        self.assertEqual(self.entry(self.jsonc)["type"], "local")
        self.apply("opencode", extra=("--remove",))
        self.assertEqual(self.jsonc.read_text(encoding="utf-8"), original)
        self.assertFalse(self.mcp.exists())

    def test_both_config_files_existing_targets_opencode_jsonc_like_opencode_itself(self):
        self.dir.mkdir(parents=True)
        self.mcp.write_text(dump({"model": "a/b"}), encoding="utf-8")
        self.jsonc.write_text(dump({"mcp": {}}), encoding="utf-8")
        planned = self.preview("opencode")
        paths = {item["path"] for item in planned["plan"]["files"]}
        self.assertIn(str(self.jsonc), paths)
        self.assertNotIn(str(self.mcp), paths)

    def test_edit_after_preview_is_refused(self):
        self.dir.mkdir(parents=True)
        self.mcp.write_text(dump(EXISTING), encoding="utf-8")
        planned = self.preview("opencode")
        self.mcp.write_text(dump(dict(EXISTING, theme="dark")), encoding="utf-8")
        before = snapshot(self.home, self.config.parent)
        code, out, _ = self.run_cli("opencode", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual((code, json.loads(out)["code"]), (1, "stale-plan"))
        self.assertEqual(before, snapshot(self.home, self.config.parent))

    def test_customized_skill_copy_is_refused_unless_forced(self):
        (self.skills / "wiki-save").mkdir(parents=True)
        mine = self.skills / "wiki-save/SKILL.md"
        mine.write_text("my own wiki-save\n", encoding="utf-8")
        refused = self.refused("opencode")
        self.assertEqual(refused["code"], "connect-refused")
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own wiki-save\n")
        self.assertFalse(self.mcp.exists() or self.rules.exists())

    def test_xdg_config_home_moves_every_location(self):
        xdg = self.root / "xdg"
        env = {"XDG_CONFIG_HOME": str(xdg)}
        agent = json.loads(self.run_cli("--json", env=env)[1])["agents"][0]
        self.assertFalse(agent["detected"])
        (xdg / "opencode").mkdir(parents=True)
        self.assertTrue(json.loads(self.run_cli("--json", env=env)[1])["agents"][0]["detected"])
        planned = self.preview_with(env, "opencode")
        paths = {item["path"] for item in planned["plan"]["files"]}
        self.assertEqual({str(xdg / "opencode/opencode.json"), str(xdg / "opencode/AGENTS.md")} - paths, set())
        self.assertTrue(all(not path.startswith(str(self.home / ".config")) for path in paths))
        self.apply_with(env, "opencode")
        self.assertTrue((xdg / "opencode/opencode.json").is_file())
        self.assertTrue((xdg / "opencode/skills/wiki/SKILL.md").is_file())
        self.assertFalse((self.home / ".config").exists())

    def test_write_tools_ask_snippet_and_notes_are_printed_never_written(self):
        planned = self.preview("opencode")
        snippet = planned["snippets"][0]
        self.assertEqual(snippet["agent"], "opencode")
        self.assertEqual(json.loads(snippet["content"]),
                         {"permission": {"obsidian-wiki_wiki_batch_apply": "ask", "obsidian-wiki_wiki_batch_recover": "ask"}})
        notes = " ".join(planned["notes"])
        for text in ("without asking by default", "~/.claude/CLAUDE.md", "~/.claude/skills and ~/.agents/skills"):
            self.assertIn(text, notes)
        self.apply("opencode")
        self.assertNotIn("permission", json.loads(self.mcp.read_text(encoding="utf-8")))
        self.assertEqual(os.listdir(self.project), [])


class OpenCodeDoctorTests(OpenCodeCase):
    """Doctor reads the manifest; this runs the shipped CLI in a process with the real registry and agent shims."""

    def setUp(self):
        super().setUp()
        self.shims = self.root / "shims"
        self.shims.mkdir()
        self.log = self.root / "shim.log"
        for name in SHIMMED:
            shim = self.shims / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\nexit 97\n', encoding="utf-8")
            shim.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items() if key not in ("OBSIDIAN_VAULT_PATH", "OBSIDIAN_AGENT_CONFIG", "XDG_CONFIG_HOME")}
        self.env.update({"HOME": str(self.home), "OBSIDIAN_AGENT_CONFIG": str(self.config), "OSB_NODE": str(self.node),
                         "PATH": f"{self.shims}:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                         "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def cli(self, *argv):
        return subprocess.run([sys.executable, str(ROOT / "scripts/obsidian-second-brain.py"), *argv], cwd=self.project, env=self.env, text=True, capture_output=True)

    def connect(self):
        planned = self.cli("connect", "opencode", "--json")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        applied = self.cli("connect", "opencode", "--apply", "--confirm", json.loads(planned.stdout)["planHash"], "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr)

    def agent(self):
        result = self.cli("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["connect"]["agents"]["opencode"]

    def test_doctor_reports_absent_detected_connected_and_stale_without_writing_or_running_opencode(self):
        agent = self.agent()
        self.assertEqual((agent["state"], agent["detected"], agent["verification"]), ("absent", False, "fixture-tested (live-unverified)"))
        self.dir.mkdir(parents=True)
        self.assertTrue(self.agent()["detected"])
        self.connect()
        before = snapshot(self.home, self.config.parent, self.vault, self.project)
        agent = self.agent()
        self.assertEqual((agent["state"], agent["reasons"], agent["adapterRegistered"]), ("connected", [], True))
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault, self.project))
        self.assertIn("Connect opencode: connected", self.cli("doctor").stdout)

        # Stale skill copies and a stale entry are reported, never repaired.
        (self.skills / "wiki-save/SKILL.md").write_text("old copy\n", encoding="utf-8")
        (self.skills / "wiki-ingest/SKILL.md").unlink()
        edited = json.loads(self.mcp.read_text(encoding="utf-8"))
        edited["mcp"]["obsidian-wiki"]["command"][1] = "/old/place/scripts/obsidian-second-brain.mjs"
        self.mcp.write_text(dump(edited), encoding="utf-8")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["agents"]["opencode"]["version"] = "0.0.1"
        self.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        before = snapshot(self.home, self.config.parent, self.vault)
        agent = self.agent()
        self.assertEqual(agent["state"], "stale")
        self.assertEqual({reason["code"] for reason in agent["reasons"]}, {"skill-modified", "skill-missing", "mcp-entry-modified", "version-mismatch"})
        self.assertIn("Connect opencode: stale (", self.cli("doctor").stdout)
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))
        self.assertFalse(self.log.exists(), self.log.read_text() if self.log.exists() else "")


if __name__ == "__main__":
    unittest.main()
