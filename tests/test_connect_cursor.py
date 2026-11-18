#!/usr/bin/env python3
"""The Cursor `connect` adapter, exercised through the real framework in a temporary HOME.

Nothing here touches the real home directory, Cursor, Git or the network. Shapes follow
https://cursor.com/docs/context/mcp (~/.cursor/mcp.json, `mcpServers`) and
https://cursor.com/docs/context/skills (~/.cursor/skills).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import connect as connect_module  # noqa: E402
from test_connect import SHIMMED, ConnectCase, snapshot  # noqa: E402

LAUNCHER = str(ROOT / "scripts/obsidian-second-brain.mjs")
# A realistic ~/.cursor/mcp.json: stdio and remote servers, interpolation, static OAuth and an unrelated top-level key.
EXISTING = {
    "mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "${userHome}/projects"],
                       "env": {"LOG_LEVEL": "info", "TOKEN": "${env:FS_TOKEN}"}},
        "github": {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ${env:GITHUB_TOKEN}"}},
        "linear": {"url": "https://mcp.linear.app/mcp", "auth": {"CLIENT_ID": "${env:LINEAR_ID}", "scopes": ["read", "write"]}},
    },
    "unrelated": {"keep": True},
}


def dump(value) -> str:
    return json.dumps(value, indent=2) + "\n"


class CursorCase(ConnectCase):
    def setUp(self):
        super().setUp()
        self.registry[:] = ["connect_adapters.cursor"]  # only the real adapter, not the framework fixtures
        self.cursor = self.home / ".cursor"
        self.mcp = self.cursor / "mcp.json"
        self.skills = self.cursor / "skills"
        self.project = self.root / "project"  # stands in for the user's repository; connect must never write here
        self.project.mkdir()

    def entry(self):
        return json.loads(self.mcp.read_text(encoding="utf-8"))["mcpServers"]["obsidian-wiki"]


class CursorAdapterTests(CursorCase):
    def test_detection_uses_only_the_config_directory(self):
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        agent = json.loads(out)["agents"][0]
        self.assertEqual((agent["name"], agent["detected"], agent["state"]), ("cursor", False, "absent"))
        self.assertEqual(agent["verification"], "fixture-tested (live-unverified)")
        self.assertEqual(agent["mechanism"], "MCP entry + skill copies")
        self.cursor.mkdir()
        before = snapshot(self.home, self.config.parent, self.vault)
        agent = json.loads(self.run_cli("--json")[1])["agents"][0]
        self.assertEqual((agent["detected"], agent["state"]), (True, "absent"))
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))

    def test_preview_lists_every_touched_file_and_writes_nothing(self):
        before = snapshot(self.root)
        planned = self.preview("cursor")
        self.assertEqual(before, snapshot(self.root))
        files = {item["path"]: item for item in planned["plan"]["files"]}
        sources = connect_module.skill_sources()
        expected = {str(self.mcp), str(self.manifest), *(str(self.skills / rel) for rel in sources)}
        self.assertEqual(set(files), expected)  # no rules file and nothing in a project
        self.assertTrue(all(item["op"] == "create" for item in files.values()))
        self.assertEqual(files[str(self.mcp)]["role"], "mcp")
        self.assertEqual(files[str(self.mcp)]["entry"]["type"], "stdio")
        code, human, _ = self.run_cli("cursor")
        self.assertEqual(code, 0)
        for path in (self.mcp, self.manifest, self.skills):  # the human preview groups the skill files under their directory
            self.assertIn(str(path), human)

    def test_apply_with_no_existing_config_writes_the_documented_entry(self):
        applied = self.apply("cursor")
        self.assertTrue(applied["applied"])
        self.assertEqual(json.loads(self.mcp.read_text(encoding="utf-8")),
                         {"mcpServers": {"obsidian-wiki": {
                             "type": "stdio", "command": str(self.node),
                             "args": [LAUNCHER, "mcp", "--tools", "all"],
                             "env": {"PYTHON": sys.executable, "OBSIDIAN_AGENT_CONFIG": str(self.config)}}}})
        entry = self.entry()
        self.assertTrue(Path(entry["command"]).is_absolute() and Path(entry["args"][0]).is_absolute())
        self.assertNotIn("npx", json.dumps(entry))
        # Only the documented files exist: mcp.json, the skills, and the manifest outside the agent directory.
        written = {str(path) for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(written, {str(self.mcp), *(str(self.skills / rel) for rel in connect_module.skill_sources())})
        self.assertTrue(self.manifest.is_file())
        self.assertEqual(os.listdir(self.project), [])
        self.assertFalse((self.cursor / "rules").exists())  # user rules are not file-based
        self.assertFalse((self.skills / "wiki/scripts").exists())

    def test_skill_copies_are_placed_in_the_cursor_skills_directory(self):
        other = self.skills / "my-own-skill"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("---\nname: my-own-skill\ndescription: mine\n---\n", encoding="utf-8")
        self.apply("cursor")
        for name in connect_module.SKILL_NAMES:
            text = (self.skills / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\nname: " + name + "\n"), name)  # Cursor needs name == folder name
            self.assertIn("\ndescription:", text.split("\n---\n", 1)[0])
        self.assertTrue((self.skills / "wiki/references/tool-surface.md").is_file())
        self.apply("cursor", extra=("--remove",))
        self.assertEqual(sorted(path.name for path in self.skills.iterdir()), ["my-own-skill"])

    def test_existing_realistic_config_is_preserved_repeat_is_a_no_op_and_removal_restores_it(self):
        self.cursor.mkdir()
        original = dump(EXISTING)
        self.mcp.write_text(original, encoding="utf-8")
        self.apply("cursor")
        merged = json.loads(self.mcp.read_text(encoding="utf-8"))
        self.assertEqual({key: value for key, value in merged["mcpServers"].items() if key != "obsidian-wiki"}, EXISTING["mcpServers"])
        self.assertEqual(merged["unrelated"], {"keep": True})
        self.assertIn("${userHome}", self.mcp.read_text(encoding="utf-8"))  # interpolation text is untouched
        backups = sorted((self.config.parent / "connect-backups").rglob("*-mcp.json"))
        self.assertEqual([path.read_text(encoding="utf-8") for path in backups], [original])
        before = snapshot(self.home, self.config.parent)
        again = self.apply("cursor")
        self.assertTrue(all(item["op"] == "unchanged" for item in again["plan"]["files"]))
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        self.apply("cursor", extra=("--remove",))
        self.assertEqual(self.mcp.read_text(encoding="utf-8"), original)
        self.assertFalse(self.skills.exists())
        self.assertFalse(self.manifest.exists())

    def test_files_connect_created_are_removed_with_it(self):
        self.apply("cursor")
        self.apply("cursor", extra=("--remove",))
        self.assertFalse(self.mcp.exists())
        self.assertFalse(self.skills.exists())

    def test_unmergeable_or_conflicting_config_is_refused_with_a_snippet(self):
        self.cursor.mkdir()
        cases = [
            ('{\n  // my servers\n  "mcpServers": {}\n}\n', "config-unparseable"),
            ('{"mcpServers": {"a": {"command": "x"},}}\n', "config-unparseable"),
            ('{"mcpServers": []}\n', "config-shape"),
            ('{"mcpServers": {"obsidian-wiki": {"command": "mine"}}}\n', "entry-differs"),
        ]
        for content, code in cases:
            with self.subTest(code=code, content=content):
                self.mcp.write_text(content, encoding="utf-8")
                before = snapshot(self.home, self.config.parent)
                refused = self.refused("cursor")
                refusal = refused["refusals"][0]
                self.assertEqual((refused["code"], refusal["agent"], refusal["code"]), ("connect-refused", "cursor", code))
                self.assertIn('"mcpServers"', refusal["snippet"])
                self.assertIn('"type": "stdio"', refusal["snippet"])
                self.assertEqual(before, snapshot(self.home, self.config.parent))  # nothing written, skills included
        self.assertFalse(self.skills.exists())

    def test_edit_after_preview_is_refused(self):
        self.cursor.mkdir()
        self.mcp.write_text(dump(EXISTING), encoding="utf-8")
        planned = self.preview("cursor")
        edited = dict(EXISTING, theme="dark")
        self.mcp.write_text(dump(edited), encoding="utf-8")
        before = snapshot(self.home, self.config.parent)
        code, out, _ = self.run_cli("cursor", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual((code, json.loads(out)["code"]), (1, "stale-plan"))
        self.assertEqual(before, snapshot(self.home, self.config.parent))

    def test_customized_skill_copy_is_refused_unless_forced(self):
        (self.skills / "wiki-save").mkdir(parents=True)
        mine = self.skills / "wiki-save/SKILL.md"
        mine.write_text("my own wiki-save\n", encoding="utf-8")
        refused = self.refused("cursor")
        self.assertEqual(refused["code"], "connect-refused")
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own wiki-save\n")
        self.assertFalse(self.mcp.exists())

    def test_project_rule_is_printed_and_never_written(self):
        planned = self.preview("cursor")
        self.assertEqual([item["destination"] for item in planned["snippets"]], [".cursor/rules/obsidian-second-brain.mdc"])
        content = planned["snippets"][0]["content"]
        self.assertTrue(content.startswith("---\ndescription: "))
        self.assertIn("\nalwaysApply: true\n---\n", content)
        self.assertIn(f'Configured Obsidian wiki: "{self.vault / "wiki"}"', content)  # {{rules}} expanded
        self.assertNotIn("{{rules}}", content)
        self.assertTrue(any("user rules live in its settings" in note for note in planned["notes"]))
        self.assertEqual(planned["snippets"][0]["agent"], "cursor")
        self.apply("cursor")
        self.assertEqual(os.listdir(self.project), [])
        self.assertFalse(list(self.root.rglob("*.mdc")))
        code, human, _ = self.run_cli("cursor")
        self.assertIn("connect never writes project files", human)


class CursorDoctorTests(CursorCase):
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
        self.env = {key: value for key, value in os.environ.items() if key not in ("OBSIDIAN_VAULT_PATH", "OBSIDIAN_AGENT_CONFIG")}
        self.env.update({"HOME": str(self.home), "OBSIDIAN_AGENT_CONFIG": str(self.config), "OSB_NODE": str(self.node),
                         "PATH": f"{self.shims}:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                         "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def cli(self, *argv):
        return subprocess.run([sys.executable, str(ROOT / "scripts/obsidian-second-brain.py"), *argv], cwd=self.project, env=self.env, text=True, capture_output=True)

    def connect(self, *extra):
        planned = self.cli("connect", "cursor", *extra, "--json")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        applied = self.cli("connect", "cursor", *extra, "--apply", "--confirm", json.loads(planned.stdout)["planHash"], "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr)

    def agent(self):
        result = self.cli("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["connect"]["agents"]["cursor"]

    def test_doctor_reports_absent_detected_connected_and_stale_without_writing_or_running_cursor(self):
        agent = self.agent()
        self.assertEqual((agent["state"], agent["detected"], agent["verification"]), ("absent", False, "fixture-tested (live-unverified)"))
        self.cursor.mkdir()
        self.assertTrue(self.agent()["detected"])
        self.connect()
        before = snapshot(self.home, self.config.parent, self.vault, self.project)
        agent = self.agent()
        self.assertEqual((agent["state"], agent["reasons"], agent["adapterRegistered"]), ("connected", [], True))
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault, self.project))
        self.assertIn("Connect cursor: connected", self.cli("doctor").stdout)

        # Stale skill copies and a stale entry are reported, never repaired.
        (self.skills / "wiki-save/SKILL.md").write_text("old copy\n", encoding="utf-8")
        (self.skills / "wiki-ingest/SKILL.md").unlink()
        edited = json.loads(self.mcp.read_text(encoding="utf-8"))
        edited["mcpServers"]["obsidian-wiki"]["args"][0] = "/old/place/scripts/obsidian-second-brain.mjs"
        self.mcp.write_text(dump(edited), encoding="utf-8")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["agents"]["cursor"]["version"] = "0.0.1"
        self.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        before = snapshot(self.home, self.config.parent, self.vault)
        agent = self.agent()
        self.assertEqual(agent["state"], "stale")
        self.assertEqual({reason["code"] for reason in agent["reasons"]}, {"skill-modified", "skill-missing", "mcp-entry-modified", "version-mismatch"})
        self.assertIn("Connect cursor: stale (", self.cli("doctor").stdout)
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))
        self.assertFalse(self.log.exists(), self.log.read_text() if self.log.exists() else "")


if __name__ == "__main__":
    unittest.main()
