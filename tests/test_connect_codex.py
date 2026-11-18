#!/usr/bin/env python3
"""The Codex CLI `connect` adapter (#66), run against synthetic files in a temporary HOME.

Nothing here touches the real home directory, a real Codex install, the `codex` binary or the network.
The shared helpers come from tests/test_connect.py; only the real adapter is registered.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import tomllib
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parent)]
from test_connect import ROOT, ConnectCase, connect_module, snapshot  # noqa: E402

RULES_START = "<!-- obsidian-second-brain:start -->"
RULES_END = "<!-- obsidian-second-brain:end -->"
TOML_START = "# obsidian-second-brain:start"

# A realistic ~/.codex/config.toml: comments, floats, other MCP servers (stdio with an env table, remote with a
# bearer variable), profiles, trusted projects and the `[[skills.config]]` array of tables Codex itself writes.
# Documented at https://developers.openai.com/codex/config-basic and https://developers.openai.com/codex/mcp.
REAL_CONFIG = '''# Codex user configuration
model = "gpt-5-codex"
model_reasoning_effort = "high"
approval_policy = "on-request"
sandbox_mode = "workspace-write"
web_search = "cached"
project_doc_max_bytes = 65536
project_doc_fallback_filenames = ["TEAM_GUIDE.md", ".agents.md"]
mcp_optional_startup_grace_ms = 1000

[sandbox_workspace_write]
network_access = false
writable_roots = ["/tmp/scratch"]

[shell_environment_policy]
inherit = "core"
ignore_default_excludes = false

[shell_environment_policy.filters]
"PATH" = "include"
"HOME" = "include"

[features]
web_search_request = false
unified_exec = true

[tui]
notifications = true

[projects."/Users/me/work/app"]
trust_level = "trusted"

[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
startup_timeout_sec = 20.0  # slow npx start
tool_timeout_sec = 90.5

[mcp_servers.context7.env]
CONTEXT7_API_KEY = "keep-me"

[mcp_servers.docs]
url = "https://docs.example.com/mcp"
bearer_token_env_var = "DOCS_TOKEN"
enabled = false

[[skills.config]]
path = "/Users/me/.agents/skills/old/SKILL.md"
enabled = false
'''


class CodexCase(ConnectCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CODEX_HOME", None)
        self.registry[:] = ["connect_adapters.codex"]
        self.codex = self.home / ".codex"
        self.skills = self.home / ".agents/skills"
        self.config_toml = self.codex / "config.toml"
        self.agents_md = self.codex / "AGENTS.md"

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def entry(self):
        return tomllib.loads(self.config_toml.read_text(encoding="utf-8"))["mcp_servers"]["obsidian-wiki"]

    def files(self, root):
        return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())

    def codex_status(self, **env):
        with mock.patch.dict(os.environ, env):
            return self.status()["agents"]["codex"]


class DetectionAndPreviewTests(CodexCase):
    def test_detection_uses_only_the_codex_home_directory_and_never_runs_the_binary(self):
        shim = self.root / "shim"
        shim.mkdir()
        log = self.root / "codex-ran"
        (shim / "codex").write_text(f"#!/bin/sh\necho ran >> {log}\n", encoding="utf-8")
        (shim / "codex").chmod(0o755)
        values = {"PATH": f"{shim}:{os.environ['PATH']}"}
        agent = json.loads(self.run_cli("--json", env=values)[1])["agents"][0]
        self.assertEqual((agent["name"], agent["detected"], agent["state"]), ("codex", False, "absent"))
        self.assertEqual(agent["verification"], "fixture-tested (live-unverified)")
        self.assertEqual(agent["mechanism"], "MCP entry + rules block + skill copies")
        self.codex.mkdir()
        self.assertTrue(json.loads(self.run_cli("--json", env=values)[1])["agents"][0]["detected"])
        custom = self.root / "elsewhere"
        self.codex.rmdir()
        custom.mkdir()
        self.assertTrue(json.loads(self.run_cli("--json", env={"CODEX_HOME": str(custom)})[1])["agents"][0]["detected"])
        # An empty CODEX_HOME counts as unset, as in Codex.
        self.assertFalse(json.loads(self.run_cli("--json", env={"CODEX_HOME": ""})[1])["agents"][0]["detected"])
        self.assertFalse(log.exists())

    def test_preview_lists_every_file_and_writes_nothing_including_the_project(self):
        project = self.root / "project"
        project.mkdir()
        before = snapshot(self.home, self.config.parent, self.vault, project)
        with contextlib.chdir(project):
            planned = self.preview("codex")
            code, human, _ = self.run_cli("codex")
        self.assertEqual(code, 0)
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault, project))
        files = {item["path"]: item for item in planned["plan"]["files"]}
        sources = connect_module.skill_sources()
        expected = {str(self.config_toml), str(self.agents_md), str(self.manifest),
                    *(str(self.skills / rel) for rel in sources)}
        self.assertEqual(set(files), expected)
        self.assertTrue(all(item["op"] == "create" for item in files.values()))
        self.assertEqual(files[str(self.config_toml)]["entry"]["args"][-2:], ["--tools", "all"])
        self.assertIn(str(self.config_toml), human)
        # Project-level rules are printed for the user to paste, never written.
        snippet = planned["snippets"][0]
        self.assertIn("AGENTS.md", snippet["destination"])
        self.assertIn("Configured Obsidian wiki", snippet["content"])
        self.assertNotIn("{{rules}}", snippet["content"])
        notes = " ".join(planned["notes"])
        self.assertIn("AGENTS.override.md", notes)
        self.assertIn("~/.agents/skills", notes)
        self.assertIn("Restart Codex", notes)
        self.assertIn("Pi also reads ~/.agents/skills", notes)


class ApplyTests(CodexCase):
    def test_apply_without_existing_config_writes_exactly_the_documented_entry(self):
        vault_before = snapshot(self.vault)
        self.assertTrue(self.apply("codex")["applied"])
        self.assertEqual(tomllib.loads(self.config_toml.read_text(encoding="utf-8")), {"mcp_servers": {"obsidian-wiki": {
            "command": str(self.node),  # pinned absolute node
            "args": [str(ROOT / "scripts/obsidian-second-brain.mjs"), "mcp", "--tools", "all"],  # absolute package root
            "env": {"PYTHON": sys.executable, "OBSIDIAN_AGENT_CONFIG": str(self.config)},
        }}})
        text = self.config_toml.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(TOML_START + "\n[mcp_servers.obsidian-wiki]\n"))
        self.assertNotIn("npx", text)
        rules = self.agents_md.read_text(encoding="utf-8")
        self.assertTrue(rules.startswith(RULES_START + "\n") and rules.endswith(RULES_END + "\n"))
        self.assertIn("never edit vault files directly", rules)
        sources = connect_module.skill_sources()
        self.assertEqual(self.files(self.skills), sorted(sources))
        self.assertTrue(all((self.skills / rel).read_bytes() == data for rel, data in sources.items()))
        # Nothing outside the previewed files: the two Codex files, the skill copies and nothing else under HOME.
        self.assertEqual(self.files(self.home), sorted([".codex/AGENTS.md", ".codex/config.toml",
                                                        *(f".agents/skills/{rel}" for rel in sources)]))
        self.assertEqual(vault_before, snapshot(self.vault))
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))["agents"]["codex"]
        self.assertEqual((manifest["mcp"]["format"], manifest["mcp"]["keyPath"]), ("toml", ["mcp_servers"]))
        self.assertEqual(manifest["skills"]["root"], str(self.skills))

    def test_codex_home_moves_the_config_and_rules_but_not_the_skills(self):
        custom = self.root / "profile/codex-home"
        custom.mkdir(parents=True)
        env = {"CODEX_HOME": str(custom)}
        planned = json.loads(self.run_cli("codex", "--json", env=env)[1])
        code, out, err = self.run_cli("codex", "--apply", "--confirm", planned["planHash"], "--json", env=env)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.files(custom), ["AGENTS.md", "config.toml"])
        self.assertFalse(self.codex.exists())
        self.assertTrue((self.skills / "wiki/SKILL.md").is_file())  # $HOME/.agents/skills, not under CODEX_HOME
        self.assertEqual(self.codex_status(CODEX_HOME=str(custom))["state"], "connected")
        self.assertEqual(self.run_cli("codex", "--remove", "--apply", "--confirm",
                                      json.loads(self.run_cli("codex", "--remove", "--json", env=env)[1])["planHash"], "--json", env=env)[0], 0)
        self.assertEqual(self.files(custom), [])

    def test_existing_realistic_config_is_preserved_byte_for_byte_and_floats_do_not_block_the_append(self):
        self.write(self.config_toml, REAL_CONFIG)
        self.write(self.agents_md, "# My global rules\n\n- Prefer small diffs.\n")
        user_skill = self.skills / "my-skill/SKILL.md"
        self.write(user_skill, "---\nname: my-skill\ndescription: mine\n---\n")
        self.apply("codex")
        text = self.config_toml.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(REAL_CONFIG + "\n" + TOML_START + "\n"))  # byte-preserving append
        parsed = tomllib.loads(text)
        original = tomllib.loads(REAL_CONFIG)
        self.assertEqual(parsed["mcp_servers"]["context7"]["startup_timeout_sec"], 20.0)
        self.assertEqual(parsed["mcp_servers"]["context7"], original["mcp_servers"]["context7"])
        self.assertEqual(parsed["mcp_servers"]["docs"], original["mcp_servers"]["docs"])
        self.assertEqual(parsed["skills"], original["skills"])
        self.assertEqual({key: value for key, value in parsed.items() if key != "mcp_servers"},
                         {key: value for key, value in original.items() if key != "mcp_servers"})
        self.assertEqual(self.entry()["args"][-2:], ["--tools", "all"])
        rules = self.agents_md.read_text(encoding="utf-8")
        self.assertTrue(rules.startswith("# My global rules\n\n- Prefer small diffs.\n\n" + RULES_START))
        self.assertEqual(user_skill.read_text(encoding="utf-8"), "---\nname: my-skill\ndescription: mine\n---\n")
        backups = list((self.config.parent / "connect-backups").rglob("*-config.toml"))
        self.assertEqual([path.read_text(encoding="utf-8") for path in backups], [REAL_CONFIG])

    def test_a_config_without_a_final_newline_gets_a_clean_append(self):
        original = REAL_CONFIG.rstrip("\n")
        self.write(self.config_toml, original)
        self.apply("codex")
        self.assertTrue(self.config_toml.read_text(encoding="utf-8").startswith(original + "\n\n" + TOML_START))
        self.apply("codex", extra=("--remove",))
        self.assertEqual(self.config_toml.read_text(encoding="utf-8"), original + "\n")  # the missing newline is the only difference

    def test_repeat_apply_is_a_no_op(self):
        self.write(self.config_toml, REAL_CONFIG)
        self.apply("codex")
        before = snapshot(self.home, self.config.parent)
        again = self.apply("codex")
        self.assertTrue(all(item["op"] == "unchanged" for item in again["plan"]["files"]))
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        self.assertIn("No changes planned.", self.run_cli("codex")[1])

    def test_removal_restores_the_original_files(self):
        self.write(self.config_toml, REAL_CONFIG)
        self.write(self.agents_md, "# My global rules\n")
        self.apply("codex")
        self.apply("codex", extra=("--remove",))
        self.assertEqual(self.config_toml.read_text(encoding="utf-8"), REAL_CONFIG)
        self.assertEqual(self.agents_md.read_text(encoding="utf-8"), "# My global rules\n")
        self.assertFalse(self.skills.exists())
        self.assertFalse(self.manifest.exists())

    def test_removal_deletes_the_files_connect_created(self):
        self.apply("codex")
        self.apply("codex", extra=("--remove",))
        self.assertEqual(self.files(self.home), [])
        self.assertFalse(self.manifest.exists())

    def test_removal_keeps_user_edits_outside_our_block_and_refuses_an_edited_block(self):
        self.write(self.config_toml, REAL_CONFIG)
        self.apply("codex")
        text = self.config_toml.read_text(encoding="utf-8")
        edited = text + '\n[mcp_servers.later]\ncommand = "later"\n'  # added by the user (or `codex mcp add`) afterwards
        self.config_toml.write_text(edited, encoding="utf-8")
        self.apply("codex", extra=("--remove",))
        kept = self.config_toml.read_text(encoding="utf-8")
        self.assertTrue(kept.startswith(REAL_CONFIG.rstrip("\n") + "\n"))  # the blank lines around our block may collapse
        self.assertEqual(tomllib.loads(kept), {**tomllib.loads(REAL_CONFIG), "mcp_servers": {
            **tomllib.loads(REAL_CONFIG)["mcp_servers"], "later": {"command": "later"}}})
        self.assertNotIn(TOML_START, kept)
        self.apply("codex")
        self.config_toml.write_text(self.config_toml.read_text(encoding="utf-8").replace('"all"', '"read"'), encoding="utf-8")
        before = snapshot(self.home)
        refused = self.refused("codex", "--remove")
        self.assertEqual(refused["refusals"][0]["code"], "entry-modified")
        self.assertEqual(self.refused("codex")["refusals"][0]["code"], "entry-differs")
        self.assertEqual(before, snapshot(self.home))


class RefusalTests(CodexCase):
    def test_a_conflicting_existing_entry_is_refused_with_the_snippet(self):
        mine = REAL_CONFIG + '\n[mcp_servers.obsidian-wiki]\ncommand = "node"\nargs = ["/my/own/server.mjs"]\n'
        self.write(self.config_toml, mine)
        before = snapshot(self.home, self.config.parent)
        refused = self.refused("codex")
        refusal = refused["refusals"][0]
        self.assertEqual(refusal["code"], "entry-differs")
        self.assertIn("[mcp_servers.obsidian-wiki]", refusal["snippet"])
        self.assertIn("--tools", refusal["snippet"])
        self.assertEqual(before, snapshot(self.home, self.config.parent))  # nothing else was written either
        code, _, err = self.run_cli("codex")
        self.assertEqual(code, 1)
        self.assertIn("Add this by hand instead", err)

    def test_configs_that_cannot_take_an_appended_table_are_refused(self):
        cases = [
            ("not valid = [toml\n", "config-unparseable"),
            ('mcp_servers = { other = { command = "x" } }\n', "toml-merge-unsafe"),  # inline table cannot be extended
        ]
        for content, code in cases:
            with self.subTest(content=content):
                self.write(self.config_toml, content)
                before = snapshot(self.home, self.config.parent)
                refusal = self.refused("codex")["refusals"][0]
                self.assertEqual(refusal["code"], code)
                self.assertIn("obsidian-wiki", refusal["snippet"])
                self.assertEqual(before, snapshot(self.home, self.config.parent))

    def test_a_symlinked_config_file_is_refused(self):
        target = self.root / "dotfiles/config.toml"
        self.write(target, REAL_CONFIG)
        self.codex.mkdir()
        (self.config_toml).symlink_to(target)
        refusal = self.refused("codex")["refusals"][0]
        self.assertIn("symlink", refusal["message"])
        self.assertEqual(target.read_text(encoding="utf-8"), REAL_CONFIG)

    def test_a_config_edited_after_the_preview_is_refused(self):
        self.write(self.config_toml, REAL_CONFIG)
        planned = self.preview("codex")
        edited = REAL_CONFIG + '\n[mcp_servers.added-meanwhile]\ncommand = "x"\n'
        self.config_toml.write_text(edited, encoding="utf-8")
        before = snapshot(self.home, self.config.parent)
        code, out, _ = self.run_cli("codex", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual((code, json.loads(out)["code"]), (1, "stale-plan"))
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        self.assertEqual(self.config_toml.read_text(encoding="utf-8"), edited)

    def test_an_edit_during_apply_is_preserved_and_everything_else_rolled_back(self):
        self.write(self.config_toml, REAL_CONFIG)
        planned = self.preview("codex")
        real = connect_module.write_atomic
        calls = []
        edited = REAL_CONFIG + "# edited during apply\n"

        def racing(path, data, mode):
            real(path, data, mode)
            calls.append(path)
            if len(calls) == 1:
                self.config_toml.write_text(edited, encoding="utf-8")

        with mock.patch.object(connect_module, "write_atomic", racing):
            code, out, _ = self.run_cli("codex", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual((code, json.loads(out)["code"]), (1, "apply-failed"))
        self.assertEqual(self.config_toml.read_text(encoding="utf-8"), edited)
        self.assertFalse(self.agents_md.exists() or self.skills.exists())
        self.assertFalse(self.manifest.exists())

    def test_customized_skill_copy_is_refused_unless_forced(self):
        self.apply("codex")
        skill = self.skills / "wiki-save/SKILL.md"
        skill.write_text("my own version\n", encoding="utf-8")
        refusal = self.refused("codex")["refusals"][0]
        self.assertEqual(refusal["code"], "skill-customized")
        self.assertEqual(skill.read_text(encoding="utf-8"), "my own version\n")
        self.assertIn("customized skill copies", refusal["message"])
        self.assertNotIn("not installed by connect", refusal["message"])

    def test_a_same_named_skill_connect_never_wrote_is_described_as_such(self):
        other = self.skills / "wiki/SKILL.md"
        self.write(other, "---\nname: wiki\ndescription: another tool's wiki\n---\n")
        before = snapshot(self.home, self.config.parent)
        refusal = self.refused("codex")["refusals"][0]
        self.assertEqual(refusal["code"], "skill-customized")
        self.assertIn("exists and was not installed by connect", refusal["message"])
        self.assertNotIn("customized", refusal["message"])
        self.assertIn("--force", refusal["message"])
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        self.apply("codex", extra=("--force",))  # --force backs it up and replaces it
        self.assertEqual((self.skills / "wiki/SKILL.md").read_bytes(), connect_module.skill_sources()["wiki/SKILL.md"])
        self.assertTrue(list((self.config.parent / "connect-backups").rglob("*SKILL.md")))


class ForeignEditTests(CodexCase):
    """Codex rewrites config.toml with toml_edit: our markers can move or vanish, our table stays findable.

    The texts below are hand-written after toml_edit's behavior: a new top-level table is inserted after the
    last existing one (before a trailing comment), and `codex mcp add|remove` re-renders every server table
    without comments.
    """

    def connected(self, base=REAL_CONFIG):
        self.write(self.config_toml, base)
        self.apply("codex")
        return self.config_toml.read_text(encoding="utf-8")

    def server_tables(self, name="obsidian-wiki"):
        mine = self.entry()
        env = "".join(f"{key} = {json.dumps(value)}\n" for key, value in mine["env"].items())
        return (f"[mcp_servers.{name}]\ncommand = {json.dumps(mine['command'])}\nargs = {json.dumps(mine['args'])}\n",
                f"[mcp_servers.{name}.env]\n{env}")

    def assert_removed(self, expected_text, expected_settings):
        self.assertEqual(self.codex_status()["state"], "connected")
        self.apply("codex", extra=("--remove",))
        kept = self.config_toml.read_text(encoding="utf-8")
        self.assertNotIn("obsidian-second-brain:", kept)
        self.assertEqual(tomllib.loads(kept), expected_settings)
        if expected_text is not None:
            self.assertEqual(kept, expected_text)
        self.assertFalse(self.manifest.exists())

    def test_a_table_codex_adds_before_our_end_marker_does_not_block_removal(self):
        text = self.connected()
        end = "# obsidian-second-brain:end\n"
        self.assertTrue(text.endswith(end))
        # `[notice]` and a trusted project, appended by Codex: toml_edit puts them before the trailing comment.
        added = '\n[notice]\nhide_full_access_warning = true\n\n[projects."/Users/me/new"]\ntrust_level = "trusted"\n'
        self.config_toml.write_text(text[:-len(end)] + added + end, encoding="utf-8")
        original = tomllib.loads(REAL_CONFIG)
        original["notice"] = {"hide_full_access_warning": True}
        original["projects"]["/Users/me/new"] = {"trust_level": "trusted"}
        self.assert_removed(REAL_CONFIG + added, original)

    def test_codex_mcp_add_that_drops_the_start_marker_still_allows_removal(self):
        self.connected()
        main, env = self.server_tables()
        # `codex mcp add later` re-renders the servers: comments (our start marker) are gone, tables are in place.
        rendered = (REAL_CONFIG.split("[mcp_servers.context7]")[0]
                    + '[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp"]\n'
                      "startup_timeout_sec = 20.0\ntool_timeout_sec = 90.5\n\n"
                      '[mcp_servers.context7.env]\nCONTEXT7_API_KEY = "keep-me"\n\n'
                      '[mcp_servers.docs]\nurl = "https://docs.example.com/mcp"\nbearer_token_env_var = "DOCS_TOKEN"\n'
                      "enabled = false\n\n"
                      + main + "\n" + env + '\n[mcp_servers.later]\ncommand = "later"\n\n'
                      '[[skills.config]]\npath = "/Users/me/.agents/skills/old/SKILL.md"\nenabled = false\n'
                      "# obsidian-second-brain:end\n")
        self.config_toml.write_text(rendered, encoding="utf-8")
        original = tomllib.loads(REAL_CONFIG)
        original["mcp_servers"]["later"] = {"command": "later"}
        self.assert_removed(None, original)

    def test_codex_mcp_remove_of_another_server_that_drops_both_markers_still_allows_removal(self):
        self.connected()
        main, env = self.server_tables()
        base = REAL_CONFIG.split("[mcp_servers.context7]")[0]
        rendered = (base + main + "\n" + env + '\n[mcp_servers.docs]\nurl = "https://docs.example.com/mcp"\n'
                    'bearer_token_env_var = "DOCS_TOKEN"\nenabled = false\n\n'
                    '[[skills.config]]\npath = "/Users/me/.agents/skills/old/SKILL.md"\nenabled = false\n')
        self.config_toml.write_text(rendered, encoding="utf-8")
        original = tomllib.loads(REAL_CONFIG)
        del original["mcp_servers"]["context7"]
        self.assert_removed(None, original)

    def test_reordered_tables_with_our_subtable_detached_are_removed_whole(self):
        self.connected()
        main, env = self.server_tables()
        rendered = (REAL_CONFIG + "\n" + main + '\n[mcp_servers.later]\ncommand = "later"\n\n' + env
                    + "\n[notice]\nhide_rate_limit_model_nudge = true\n")
        self.config_toml.write_text(rendered, encoding="utf-8")
        original = tomllib.loads(REAL_CONFIG)
        original["mcp_servers"]["later"] = {"command": "later"}
        original["notice"] = {"hide_rate_limit_model_nudge": True}
        self.assert_removed(None, original)
        kept = self.config_toml.read_text(encoding="utf-8")
        self.assertIn('[mcp_servers.later]\ncommand = "later"\n', kept)
        self.assertNotIn("obsidian-wiki", kept)

    def test_an_edited_entry_is_still_refused_when_the_markers_are_gone(self):
        self.connected()
        main, env = self.server_tables()
        self.config_toml.write_text(REAL_CONFIG + "\n" + main.replace('"all"', '"read"') + "\n" + env + "# obsidian-second-brain:end\n",
                                    encoding="utf-8")
        before = snapshot(self.home)
        self.assertEqual(self.refused("codex", "--remove")["refusals"][0]["code"], "entry-modified")
        self.assertEqual(before, snapshot(self.home))

    def test_an_always_allow_approval_codex_saved_blocks_update_and_removal_with_an_explanation(self):
        text = self.connected()
        end = "# obsidian-second-brain:end\n"
        # Codex "Always allow" persists a per-tool approval as a subtable of our server (mcp_tool_call.rs).
        saved = '\n[mcp_servers.obsidian-wiki.tools.wiki_batch_apply]\napproval_mode = "approve"\n'
        self.config_toml.write_text(text[:-len(end)] + saved + end, encoding="utf-8")
        before = snapshot(self.home)
        self.assertEqual(self.refused("codex", "--remove")["refusals"][0]["code"], "entry-modified")
        other = self.root / "other-node"
        other.write_text("#!/bin/sh\necho v24.1.0\n", encoding="utf-8")
        other.chmod(0o755)
        refusal = self.refused("codex", "--node", str(other))["refusals"][0]
        self.assertEqual(refusal["code"], "entry-differs")
        self.assertIn("that connect wrote was changed afterwards", refusal["message"])
        self.assertEqual(before, snapshot(self.home))

    def test_update_replaces_a_table_that_codex_moved_and_leaves_its_neighbours(self):
        text = self.connected()
        end = "# obsidian-second-brain:end\n"
        added = '\n[notice]\nhide_full_access_warning = true\n'
        self.config_toml.write_text(text[:-len(end)] + added + end, encoding="utf-8")
        other = self.root / "other-node"
        other.write_text("#!/bin/sh\necho v24.1.0\n", encoding="utf-8")
        other.chmod(0o755)
        self.apply("codex", extra=("--node", str(other)))
        updated = self.config_toml.read_text(encoding="utf-8")
        self.assertEqual(self.entry()["command"], str(other))
        self.assertEqual(updated.count("obsidian-second-brain:start"), 1)
        self.assertEqual(updated.count("obsidian-second-brain:end"), 1)
        self.assertEqual(tomllib.loads(updated)["notice"], {"hide_full_access_warning": True})
        self.assertEqual(self.codex_status()["state"], "connected")

    def test_an_entry_written_as_dotted_keys_or_inline_is_refused_with_nothing_changed(self):
        self.connected()
        mine = self.entry()
        dotted = ("[mcp_servers]\nobsidian-wiki.command = " + json.dumps(mine["command"]) + "\nobsidian-wiki.args = " + json.dumps(mine["args"])
                  + "\nobsidian-wiki.env.PYTHON = " + json.dumps(mine["env"]["PYTHON"])
                  + "\nobsidian-wiki.env.OBSIDIAN_AGENT_CONFIG = " + json.dumps(mine["env"]["OBSIDIAN_AGENT_CONFIG"]) + "\n")
        inline = "[mcp_servers]\nobsidian-wiki = " + json.dumps({"command": mine["command"], "args": mine["args"], "env": mine["env"]}).replace(": ", " = ") + "\n"
        for label, content in (("dotted", dotted), ("inline", inline)):
            with self.subTest(label):
                self.config_toml.write_text(REAL_CONFIG.split("[mcp_servers.context7]")[0] + content, encoding="utf-8")
                self.assertEqual(self.entry(), mine)
                self.assertEqual(self.codex_status()["state"], "connected")
                before = snapshot(self.home)
                refusal = self.refused("codex", "--remove")["refusals"][0]
                self.assertEqual(refusal["code"], "toml-merge-unsafe")
                self.assertIn("[mcp_servers.obsidian-wiki]", refusal["message"])
                self.assertEqual(before, snapshot(self.home))
        # An update is refused the same way, and prints the table to paste.
        other = self.root / "other-node"
        other.write_text("#!/bin/sh\necho v24.1.0\n", encoding="utf-8")
        other.chmod(0o755)
        refusal = self.refused("codex", "--node", str(other))["refusals"][0]
        self.assertEqual(refusal["code"], "toml-merge-unsafe")
        self.assertIn("[mcp_servers.obsidian-wiki]", refusal["snippet"])


class DoctorTests(CodexCase):
    def test_doctor_reports_absent_detected_connected_and_stale_states(self):
        self.assertEqual(self.codex_status()["state"], "absent")
        self.assertFalse(self.codex_status()["detected"])
        self.codex.mkdir()
        self.assertTrue(self.codex_status()["detected"])
        self.apply("codex")
        connected = self.codex_status()
        self.assertEqual((connected["state"], connected["reasons"]), ("connected", []))
        self.assertEqual(connected["verification"], "fixture-tested (live-unverified)")
        before = snapshot(self.home, self.config.parent)

        text = self.config_toml.read_text(encoding="utf-8")
        self.config_toml.write_text(text.replace(str(ROOT / "scripts"), "/old/place/scripts"), encoding="utf-8")
        (self.skills / "wiki-save/SKILL.md").write_text("customized\n", encoding="utf-8")
        (self.skills / "wiki-ingest/SKILL.md").unlink()
        rules = self.agents_md.read_text(encoding="utf-8")
        self.agents_md.write_text(rules.replace("planHash", "changed"), encoding="utf-8")
        stale = self.codex_status()
        self.assertEqual(stale["state"], "stale")
        self.assertEqual({reason["code"] for reason in stale["reasons"]},
                         {"mcp-entry-modified", "skill-modified", "skill-missing", "rules-block-modified"})
        self.assertNotEqual(before, snapshot(self.home, self.config.parent))  # only because this test edited the files
        after = snapshot(self.home, self.config.parent)
        self.codex_status()
        self.assertEqual(after, snapshot(self.home, self.config.parent))  # doctor never repairs

    def test_doctor_flags_an_outdated_skill_copy_from_an_older_package(self):
        self.apply("codex")
        older = "an older shipped copy\n"
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["agents"]["codex"]["skills"]["files"]["wiki/SKILL.md"] = hashlib.sha256(older.encode()).hexdigest()
        self.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (self.skills / "wiki/SKILL.md").write_text(older, encoding="utf-8")  # unmodified since connect, but this package ships newer text
        stale = self.codex_status()
        self.assertEqual(stale["state"], "stale")
        self.assertEqual([reason["code"] for reason in stale["reasons"]], ["skill-outdated"])

    def test_doctor_flags_a_missing_package_root_and_node(self):
        self.apply("codex")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["agents"]["codex"]["packageRoot"] = str(self.root / "gone")
        self.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.node.unlink()
        stale = self.codex_status()
        self.assertEqual(stale["state"], "stale")
        self.assertTrue({"package-root-missing", "node-missing"} <= {reason["code"] for reason in stale["reasons"]})


class ShippedAdapterTests(unittest.TestCase):
    def test_the_adapter_is_registered_and_labelled_fixture_tested(self):
        from connect_adapters import REGISTRY, codex
        self.assertIn("connect_adapters.codex", REGISTRY)
        self.assertEqual(codex.ADAPTER.name, "codex")
        self.assertEqual(codex.ADAPTER.verification, "fixture-tested (live-unverified)")
        self.assertEqual(codex.ADAPTER.mcp.key_path, ("mcp_servers",))
        self.assertEqual(codex.ADAPTER.mcp.name, "obsidian-wiki")


if __name__ == "__main__":
    unittest.main()
