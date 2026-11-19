#!/usr/bin/env python3
"""The OpenCode `connect` adapter, exercised through the real framework in a temporary HOME.

Nothing here touches the real home directory, OpenCode, Git or the network. Shapes follow
https://opencode.ai/docs/config/ (~/.config/opencode/opencode.json or opencode.jsonc),
https://opencode.ai/docs/mcp-servers/ (`mcp`, `local`), https://opencode.ai/docs/rules/ (AGENTS.md) and
https://opencode.ai/docs/skills/ (~/.config/opencode/skills) and https://opencode.ai/docs/permissions/ (`permission`, the
last matching rule wins).
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
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
ASK = {"obsidian-wiki_wiki_batch_apply": "ask", "obsidian-wiki_wiki_batch_recover": "ask"}
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
                             "enabled": True}},
                          "permission": ASK})
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
        self.assertEqual({key: value for key, value in merged.items() if key not in ("mcp", "permission")},
                         {key: value for key, value in EXISTING.items() if key not in ("mcp", "permission")})
        # The ask rules come last in the user's permission object: the last matching rule wins.
        self.assertEqual(list(merged["permission"].items()), [*EXISTING["permission"].items(), *ASK.items()])
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

    def test_ask_rules_are_written_listed_in_the_preview_and_removed_with_connect(self):
        planned = self.preview("opencode")
        self.assertEqual(planned["snippets"], [])  # nothing is left for the user to paste
        mcp = next(item for item in planned["plan"]["files"] if item["role"] == "mcp")
        self.assertEqual(mcp["entry"]["type"], "local")
        notes = " ".join(planned["notes"])
        self.assertIn("`ask` rules", notes)
        self.assertNotIn("without asking by default", notes)
        self.apply("opencode")
        written = json.loads(self.mcp.read_text(encoding="utf-8"))
        self.assertEqual(written["permission"], ASK)
        self.assertEqual(os.listdir(self.project), [])
        extras = json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["opencode"]["mcp"]["extras"]
        self.assertEqual([item["name"] for item in extras], list(ASK))
        self.assertTrue(all(item["valueSha256"] and item["createdKeys"] == 1 for item in extras))
        self.apply("opencode", extra=("--remove",))
        self.assertFalse(self.mcp.exists())

    def test_rules_are_appended_after_existing_ones_so_they_take_effect(self):
        # OpenCode: the last matching rule wins. A user's broad allow stays before ours; ours stay last.
        self.dir.mkdir(parents=True)
        mine = {"permission": {"*": "allow", "obsidian-wiki_*": "allow", "bash": {"*": "ask"}}, "model": "a/b"}
        original = dump(mine)
        self.mcp.write_text(original, encoding="utf-8")
        self.apply("opencode")
        permission = json.loads(self.mcp.read_text(encoding="utf-8"))["permission"]
        self.assertEqual(list(permission), ["*", "obsidian-wiki_*", "bash", *ASK])
        self.apply("opencode", extra=("--remove",))
        self.assertEqual(self.mcp.read_text(encoding="utf-8"), original)  # an existing permission object is never removed

    def test_existing_ask_or_deny_for_our_key_is_the_users_and_stays(self):
        self.dir.mkdir(parents=True)
        mine = {"permission": {"obsidian-wiki_wiki_batch_apply": "deny", "obsidian-wiki_wiki_batch_recover": "ask"}}
        original = dump(mine)
        self.mcp.write_text(original, encoding="utf-8")
        self.apply("opencode")
        self.assertEqual(json.loads(self.mcp.read_text(encoding="utf-8"))["permission"], mine["permission"])
        self.assertNotIn("extras", json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["opencode"]["mcp"])
        self.apply("opencode", extra=("--remove",))
        self.assertEqual(self.mcp.read_text(encoding="utf-8"), original)

    def test_an_allow_for_our_key_or_a_permission_string_is_refused_with_the_snippet(self):
        self.dir.mkdir(parents=True)
        cases = [
            ({"permission": {"obsidian-wiki_wiki_batch_apply": "allow"}}, "extra-differs", "obsidian-wiki_wiki_batch_apply"),
            ({"permission": {"obsidian-wiki_wiki_batch_recover": {"*": "allow"}}}, "extra-differs", "obsidian-wiki_wiki_batch_recover"),
            ({"permission": "allow"}, "config-shape", "permission"),
            ({"permission": ["ask"]}, "config-shape", "permission"),
        ]
        for config, code, where in cases:
            with self.subTest(config=config):
                self.mcp.write_text(dump(config), encoding="utf-8")
                before = snapshot(self.home, self.config.parent)
                refusal = self.refused("opencode")["refusals"][0]
                self.assertEqual(refusal["code"], code)
                self.assertIn(where, refusal["message"])
                self.assertEqual(json.loads(refusal["snippet"])["permission"], ASK)  # what to merge by hand
                self.assertIn('"obsidian-wiki"', refusal["snippet"])
                self.assertEqual(before, snapshot(self.home, self.config.parent))  # no MCP entry without the approval rules

    def test_a_permission_string_of_ask_or_deny_already_covers_our_tools(self):
        self.dir.mkdir(parents=True)
        for value in ("ask", "deny"):
            with self.subTest(value=value):
                self.mcp.write_text(dump({"permission": value}), encoding="utf-8")
                self.apply("opencode")
                self.assertEqual(json.loads(self.mcp.read_text(encoding="utf-8"))["permission"], value)
                self.apply("opencode", extra=("--remove",))
                self.assertEqual(json.loads(self.mcp.read_text(encoding="utf-8")), {"permission": value})

    def test_changed_rule_is_left_on_removal_and_a_later_allow_is_refused(self):
        self.apply("opencode")
        edited = json.loads(self.mcp.read_text(encoding="utf-8"))
        edited["permission"]["obsidian-wiki_wiki_batch_apply"] = "deny"
        self.mcp.write_text(dump(edited), encoding="utf-8")
        again = self.apply("opencode")  # deny is as safe as ask: kept, no longer owned
        self.assertTrue(all(item["op"] == "unchanged" for item in again["plan"]["files"] if item["role"] == "mcp"))
        extras = json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["opencode"]["mcp"]["extras"]
        self.assertEqual([item["name"] for item in extras], ["obsidian-wiki_wiki_batch_recover"])
        self.apply("opencode", extra=("--remove",))
        self.assertEqual(json.loads(self.mcp.read_text(encoding="utf-8")), {"permission": {"obsidian-wiki_wiki_batch_apply": "deny"}})
        edited["permission"]["obsidian-wiki_wiki_batch_apply"] = "allow"
        self.mcp.write_text(dump(edited), encoding="utf-8")
        self.assertEqual(self.refused("opencode")["refusals"][0]["code"], "extra-differs")

    def test_removal_keeps_a_rule_edited_after_connect_and_says_so(self):
        self.apply("opencode")
        edited = json.loads(self.mcp.read_text(encoding="utf-8"))
        edited["permission"]["obsidian-wiki_wiki_batch_recover"] = {"*": "allow"}
        self.mcp.write_text(dump(edited), encoding="utf-8")
        removed = self.apply("opencode", extra=("--remove",))
        self.assertEqual(json.loads(self.mcp.read_text(encoding="utf-8")), {"permission": {"obsidian-wiki_wiki_batch_recover": {"*": "allow"}}})
        self.assertTrue(any("obsidian-wiki_wiki_batch_recover" in note and "it stays" in note for note in removed["notes"]), removed["notes"])

    def test_default_opencode_jsonc_with_only_the_schema_is_edited_and_restored(self):
        # OpenCode seeds ~/.config/opencode/opencode.jsonc with just the schema (JSON.stringify(..., null, 2), no trailing newline).
        self.dir.mkdir(parents=True)
        original = json.dumps({"$schema": "https://opencode.ai/config.json"}, indent=2)
        self.jsonc.write_text(original, encoding="utf-8")
        self.apply("opencode")
        edited = json.loads(self.jsonc.read_text(encoding="utf-8"))
        self.assertEqual(list(edited), ["$schema", "mcp", "permission"])
        self.assertEqual(edited["permission"], ASK)
        self.assertEqual(self.entry(self.jsonc)["type"], "local")
        self.assertFalse(self.mcp.exists())
        self.apply("opencode", extra=("--remove",))
        self.assertEqual(self.jsonc.read_text(encoding="utf-8"), original)  # the empty mcp/permission objects connect added are gone too

    def test_a_skill_name_another_directory_opencode_reads_already_provides_is_not_installed_again(self):
        # Codex installs the same six names in ~/.agents/skills; OpenCode reads that directory, ~/.claude/skills and its own.
        agents = self.home / ".agents/skills"
        claude = self.home / ".claude/skills"
        sources = connect_module.skill_sources()
        for rel, data in sources.items():
            if rel.split("/")[0] != "wiki-health":
                (agents / rel).parent.mkdir(parents=True, exist_ok=True)
                (agents / rel).write_bytes(data)
        (claude / "wiki-health").mkdir(parents=True)
        (claude / "wiki-health/SKILL.md").write_text("my own wiki-health\n", encoding="utf-8")  # not ours, still a duplicate name
        before = snapshot(agents, claude)
        planned = self.preview("opencode")
        self.assertEqual(planned["plan"]["skillsElsewhere"],
                         {"opencode": {**{name: str(agents) for name in connect_module.SKILL_NAMES if name != "wiki-health"}, "wiki-health": str(claude)}})
        self.assertFalse([item for item in planned["plan"]["files"] if item["role"] == "skill"])
        self.assertFalse(any(self.skills.name in item["path"] for item in planned["plan"]["files"]))
        code, human, _ = self.run_cli("opencode")
        self.assertEqual(code, 0)
        self.assertIn(f"Skip skills for opencode: wiki, wiki-query, wiki-save, wiki-ingest, wiki-research already in {agents}", human)
        self.assertIn(f"wiki-health already in {claude}", human)
        self.apply("opencode")
        self.assertFalse(self.skills.exists())  # no second copy, and no empty directory
        self.assertEqual(before, snapshot(agents, claude))  # the other directories are never written
        record = json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["opencode"]["skills"]
        self.assertEqual(record["files"], {})
        self.assertEqual(record["skillsElsewhere"]["wiki-save"], str(agents))
        self.assertEqual(record["skillsElsewhere"]["wiki-health"], str(claude))
        # Once a copy is gone, a rerun installs that skill itself and the record forgets it.
        shutil.rmtree(agents / "wiki-query")
        again = self.apply("opencode")
        self.assertEqual({item["path"] for item in again["plan"]["files"] if item["role"] == "skill"}, {str(self.skills / "wiki-query/SKILL.md")})
        record = json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["opencode"]["skills"]
        self.assertEqual(list(record["files"]), ["wiki-query/SKILL.md"])
        self.assertNotIn("wiki-query", record["skillsElsewhere"])
        self.apply("opencode", extra=("--remove",))
        self.assertFalse(self.skills.exists())
        self.assertEqual(before.keys() - snapshot(agents, claude).keys(), {str(agents / "wiki-query"), str(agents / "wiki-query/SKILL.md")})

    def codex_installs(self):
        self.registry[:] = ["connect_adapters.codex", "connect_adapters.opencode"]
        (self.home / ".codex").mkdir(exist_ok=True)

    def test_an_existing_connect_copy_is_replaced_by_the_shared_one_when_the_name_now_also_exists_elsewhere(self):
        self.apply("opencode")
        agents = self.home / ".agents/skills/wiki-save"
        agents.mkdir(parents=True)
        (agents / "SKILL.md").write_bytes(connect_module.skill_sources()["wiki-save/SKILL.md"])
        planned = self.preview("opencode")
        self.assertEqual(planned["plan"]["skillsElsewhere"], {"opencode": {"wiki-save": str(self.home / ".agents/skills")}})
        self.assertEqual([item["path"] for item in planned["plan"]["files"] if item["op"] == "delete"], [str(self.skills / "wiki-save/SKILL.md")])
        self.apply("opencode")
        self.assertFalse((self.skills / "wiki-save").exists())
        self.assertTrue((self.skills / "wiki-query/SKILL.md").is_file())
        record = json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["opencode"]["skills"]
        self.assertNotIn("wiki-save/SKILL.md", record["files"])
        self.assertEqual(record["skillsElsewhere"], {"wiki-save": str(self.home / ".agents/skills")})

    def test_opencode_first_then_codex_warns_and_a_second_connect_opencode_converges_to_one_copy(self):
        self.codex_installs()
        self.apply("opencode")
        own = snapshot(self.skills)
        planned = self.preview("codex")
        note = next(note for note in planned["notes"] if "lists them twice" in note)
        for text in ("OpenCode", str(self.skills), ", ".join(connect_module.SKILL_NAMES), "connect opencode"):
            self.assertIn(text, note)
        self.apply("codex")
        self.assertEqual(own, snapshot(self.skills))  # codex never touches OpenCode's copies
        shared = self.home / ".agents/skills"
        self.assertTrue((shared / "wiki-save/SKILL.md").is_file())
        # The advice: reconnect OpenCode. Its own copies go, Codex's are used, and the directory connect created is gone.
        rerun = self.preview("opencode")
        self.assertEqual(set(rerun["plan"]["skillsElsewhere"]["opencode"]), set(connect_module.SKILL_NAMES))
        self.apply("opencode")
        self.assertFalse(self.skills.exists())
        self.assertTrue((shared / "wiki-save/SKILL.md").is_file())
        record = json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["opencode"]["skills"]
        self.assertEqual(record["files"], {})
        self.assertEqual(set(record["skillsElsewhere"].values()), {str(shared)})
        self.assertTrue(all(item["op"] == "unchanged" for item in self.apply("opencode")["plan"]["files"]))
        # L1: removing Codex's copies names the agent that now depends on them.
        removal = self.preview("codex", extra=("--remove",))
        note = next(note for note in removal["notes"] if "uses instead of its own" in note)
        for text in ("opencode", str(shared), "connect opencode"):
            self.assertIn(text, note)

    def test_a_copy_the_user_deletes_as_advised_stays_deleted_and_the_run_converges(self):
        self.codex_installs()
        self.apply("opencode")
        self.apply("codex")
        shutil.rmtree(self.skills / "wiki-query")  # what the preview note advised ("remove one set by hand")
        planned = self.preview("opencode")
        self.assertNotIn(str(self.skills / "wiki-query/SKILL.md"), {item["path"] for item in planned["plan"]["files"] if item["op"] != "unchanged"})
        self.apply("opencode")
        self.assertFalse(self.skills.exists())
        self.assertIn("wiki-query", json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["opencode"]["skills"]["skillsElsewhere"])

    def test_a_customized_opencode_copy_is_not_removed_silently_when_codex_provides_the_name(self):
        self.codex_installs()
        self.apply("opencode")
        self.apply("codex")
        (self.skills / "wiki-save/SKILL.md").write_text("my wiki-save\n", encoding="utf-8")
        refusal = self.refused("opencode")["refusals"][0]
        self.assertEqual(refusal["code"], "skill-customized")
        self.assertEqual((self.skills / "wiki-save/SKILL.md").read_text(encoding="utf-8"), "my wiki-save\n")
        self.apply("opencode", extra=("--force",))
        self.assertFalse((self.skills / "wiki-save").exists())

    def test_selected_together_with_an_agent_that_installs_the_same_skills_they_are_installed_once(self):
        self.registry[:] = ["connect_adapters.codex", "connect_adapters.opencode"]
        (self.home / ".codex").mkdir()
        self.apply("codex", "opencode")
        self.assertTrue((self.home / ".agents/skills/wiki-save/SKILL.md").is_file())
        self.assertFalse(self.skills.exists())
        record = json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]
        self.assertEqual(set(record["opencode"]["skills"]["skillsElsewhere"]), set(connect_module.SKILL_NAMES))
        self.assertEqual(set(record["opencode"]["skills"]["skillsElsewhere"].values()), {str(self.home / ".agents/skills")})

    def test_rules_file_is_not_created_while_claude_md_exists_and_the_block_is_printed(self):
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "CLAUDE.md").write_text("claude rules\n", encoding="utf-8")
        before_claude = snapshot(claude)
        planned = self.preview("opencode")
        self.assertNotIn(str(self.rules), {item["path"] for item in planned["plan"]["files"]})
        snippet = next(item for item in planned["snippets"] if "rules block" in item["title"])
        self.assertIn(START, snippet["content"])
        self.assertIn(f'Configured Obsidian wiki: "{self.vault / "wiki"}"', snippet["content"])
        self.assertIn(str(claude / "CLAUDE.md"), snippet["destination"])
        note = next(note for note in planned["notes"] if "was not created" in note)
        for text in (str(self.rules), str(claude / "CLAUDE.md"), "would hide it", "MCP server's instructions", "paste the printed block"):
            self.assertIn(text, note)
        code, human, _ = self.run_cli("opencode")
        self.assertIn("Snippet for opencode: Wiki rules block, not written", human)
        self.apply("opencode")
        self.assertFalse(self.rules.exists())
        self.assertEqual(before_claude, snapshot(claude))
        self.assertIsNone(json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["opencode"]["rules"])
        # Removal has nothing to undo for the rules and leaves CLAUDE.md alone.
        self.apply("opencode", extra=("--remove",))
        self.assertEqual(before_claude, snapshot(claude))

    def test_an_existing_rules_file_gets_the_block_even_when_claude_md_exists(self):
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "CLAUDE.md").write_text("claude rules\n", encoding="utf-8")
        self.dir.mkdir(parents=True)
        self.rules.write_text("# mine\n", encoding="utf-8")
        planned = self.preview("opencode")
        self.assertEqual({item["role"]: item["op"] for item in planned["plan"]["files"] if item["path"] == str(self.rules)}, {"rules": "update"})
        self.assertEqual([item for item in planned["snippets"] if "rules block" in item["title"]], [])
        self.apply("opencode")
        self.assertIn(START, self.rules.read_text(encoding="utf-8"))

    def test_opencode_config_dir_replaces_the_global_directory_for_every_location(self):
        custom = self.root / "custom-opencode"
        xdg = self.root / "xdg"
        env = {"OPENCODE_CONFIG_DIR": str(custom), "XDG_CONFIG_HOME": str(xdg)}
        agent = json.loads(self.run_cli("--json", env=env)[1])["agents"][0]
        self.assertFalse(agent["detected"])
        custom.mkdir()
        self.assertTrue(json.loads(self.run_cli("--json", env=env)[1])["agents"][0]["detected"])
        planned = self.preview_with(env, "opencode")
        paths = {item["path"] for item in planned["plan"]["files"]}
        self.assertEqual({str(custom / "opencode.json"), str(custom / "AGENTS.md"), str(custom / "skills/wiki/SKILL.md")} - paths, set())
        self.assertFalse([path for path in paths if path.startswith((str(xdg), str(self.home / ".config")))])
        self.apply_with(env, "opencode")
        self.assertEqual(json.loads((custom / "opencode.json").read_text(encoding="utf-8"))["permission"], ASK)
        self.assertTrue((custom / "AGENTS.md").is_file())
        self.assertFalse(xdg.exists() or (self.home / ".config").exists())
        self.apply_with(env, "opencode", "--remove")
        self.assertFalse((custom / "opencode.json").exists())
        self.assertFalse((custom / "AGENTS.md").exists())

    def test_an_empty_opencode_config_dir_counts_as_unset(self):
        planned = self.preview_with({"OPENCODE_CONFIG_DIR": ""}, "opencode")
        self.assertIn(str(self.mcp), {item["path"] for item in planned["plan"]["files"]})


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

    def connect_all(self, *agents, extra=()):
        planned = self.cli("connect", *agents, *extra, "--json")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        applied = self.cli("connect", *agents, *extra, "--apply", "--confirm", json.loads(planned.stdout)["planHash"], "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr)

    def test_doctor_reports_stale_when_the_copy_that_made_connect_skip_a_skill_disappears(self):
        # Codex and OpenCode connected together: Codex owns ~/.agents/skills, OpenCode skipped the same names.
        self.connect_all("codex", "opencode")
        agent = self.agent()
        self.assertEqual((agent["state"], agent["reasons"]), ("connected", []))
        self.assertFalse(self.skills.exists())
        self.connect_all("codex", extra=("--remove",))  # the shared copies go away with the last Codex connection
        agent = self.agent()
        self.assertEqual(agent["state"], "stale")
        self.assertEqual([reason["code"] for reason in agent["reasons"]], ["skill-elsewhere-missing"])
        detail = agent["reasons"][0]["detail"]
        self.assertIn(str(self.home / ".agents/skills"), detail)
        self.assertIn("wiki-save", detail)
        self.assertIn("run connect for this agent again", detail)
        before = snapshot(self.home, self.config.parent, self.vault)
        self.assertIn("Connect opencode: stale (", self.cli("doctor").stdout)
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))  # reported, never repaired
        self.connect_all("opencode")  # the rerun installs OpenCode's own copies
        agent = self.agent()
        self.assertEqual((agent["state"], agent["reasons"]), ("connected", []))
        self.assertTrue((self.skills / "wiki-save/SKILL.md").is_file())

    def test_doctor_reports_a_duplicate_when_codex_is_connected_after_opencode_and_clears_after_the_advised_rerun(self):
        self.connect_all("opencode")
        self.connect_all("codex")
        agent = self.agent()
        self.assertEqual(agent["state"], "stale")
        self.assertEqual([reason["code"] for reason in agent["reasons"]], ["skill-duplicated-elsewhere"])
        detail = agent["reasons"][0]["detail"]
        for text in ("wiki-save", str(self.home / ".agents/skills"), str(self.skills), "run connect opencode again"):
            self.assertIn(text, detail)
        before = snapshot(self.home, self.config.parent, self.vault)
        self.assertIn("Connect opencode: stale (", self.cli("doctor").stdout)
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))  # reported, never repaired
        self.connect_all("opencode")
        agent = self.agent()
        self.assertEqual((agent["state"], agent["reasons"]), ("connected", []))
        self.assertFalse(self.skills.exists())

    def test_doctor_reports_the_ask_rules_when_they_go_missing_or_change(self):
        self.connect()
        edited = json.loads(self.mcp.read_text(encoding="utf-8"))
        edited["permission"]["obsidian-wiki_wiki_batch_apply"] = "deny"
        del edited["permission"]["obsidian-wiki_wiki_batch_recover"]
        self.mcp.write_text(dump(edited), encoding="utf-8")
        agent = self.agent()
        self.assertEqual(agent["state"], "stale")
        self.assertEqual(sorted(reason["code"] for reason in agent["reasons"]), ["mcp-extra-missing", "mcp-extra-modified"])
        self.assertTrue(all("permission.obsidian-wiki_wiki_batch_" in reason["detail"] for reason in agent["reasons"]))

    def test_doctor_does_not_report_a_rules_block_that_connect_did_not_write_for_claude_md(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude/CLAUDE.md").write_text("claude rules\n", encoding="utf-8")
        self.connect()
        self.assertFalse(self.rules.exists())
        agent = self.agent()
        self.assertEqual((agent["state"], agent["reasons"]), ("connected", []))


if __name__ == "__main__":
    unittest.main()
