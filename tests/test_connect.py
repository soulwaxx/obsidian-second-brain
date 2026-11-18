#!/usr/bin/env python3
"""The reviewed `connect` framework, exercised with fixture adapters in a temporary HOME.

Nothing here touches the real home directory, a real agent, Git or the network.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path[:0] = [str(SCRIPTS), str(ROOT / "tests")]
import connect as connect_module  # noqa: E402
import connect_adapters  # noqa: E402
import connect_fixture_adapter  # noqa: E402
from module_loading import load_source  # noqa: E402

WRAPPER = ROOT / "tests/connect_cli.py"
LAUNCHER = SCRIPTS / "obsidian-second-brain.mjs"
SHIMMED = ("git", "gh", "claude", "codex", "copilot", "cursor", "code", "opencode", "pi", "omp", "npx", "npm", "curl", "wget", "ollama", "ssh")


def snapshot(*roots: Path, mtimes: bool = True) -> dict:
    result = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted([root, *root.rglob("*")]):
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else "dir"
            # Directory mtimes change when entries come and go; files carry their own.
            result[str(path)] = (digest, info.st_mtime_ns if mtimes and digest != "dir" else 0, stat.S_IMODE(info.st_mode))
    return result


class ConnectCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.vault = self.root / "vault"
        self.config = self.root / "cfg/properties.json"
        (self.vault / "wiki").mkdir(parents=True)
        self.home.mkdir()
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        self.manifest = self.config.parent / "connect-manifest.json"
        self.registry = []
        connect_fixture_adapter.register(type("R", (), {"REGISTRY": self.registry}))
        self.json_dir = self.home / ".fixture-json"

    def tearDown(self):
        self.temp.cleanup()

    # -- helpers --------------------------------------------------------------------------
    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        values = {"HOME": str(self.home), "OBSIDIAN_AGENT_CONFIG": str(self.config), **(env or {})}
        with mock.patch.dict(os.environ, values), mock.patch.object(connect_adapters, "REGISTRY", self.registry), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            os.environ.pop("OBSIDIAN_VAULT_PATH", None)
            try:
                code = connect_module.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def preview(self, *agents, extra=()):
        code, out, err = self.run_cli(*agents, *extra, "--json")
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def apply(self, *agents, extra=()):
        planned = self.preview(*agents, extra=extra)
        code, out, err = self.run_cli(*agents, *extra, "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def refused(self, *argv):
        code, out, _ = self.run_cli(*argv, "--json")
        self.assertEqual(code, 1, out)
        return json.loads(out)

    def installed_mcp(self):
        return json.loads((self.json_dir / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["obsidian-wiki"]


class FrameworkTests(ConnectCase):
    def test_list_detects_agents_and_writes_nothing(self):
        (self.home / ".fixture-toml").mkdir()
        before = snapshot(self.home, self.config.parent, self.vault)
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        listed = json.loads(out)
        self.assertEqual(listed["mode"], "list")
        by_name = {item["name"]: item for item in listed["agents"]}
        self.assertEqual(list(by_name), [adapter.name for adapter in connect_fixture_adapter.ALL])
        self.assertTrue(by_name["fixture-toml"]["detected"])
        self.assertFalse(by_name["fixture-json"]["detected"])
        self.assertEqual(by_name["fixture-toml"]["state"], "absent")
        self.assertEqual(by_name["fixture-manual"]["state"], "manual-only")
        self.assertEqual(by_name["fixture-json"]["mechanism"], "MCP entry + rules block + skill copies")
        self.assertEqual(by_name["fixture-json"]["verification"], "fixture-tested (live-unverified)")
        for profile in ("read", "all"):
            snippet = listed["genericSnippets"][profile]
            self.assertEqual(snippet["command"], "node")
            self.assertEqual(snippet["args"], [str(ROOT / "scripts/obsidian-second-brain.mjs"), "mcp", "--tools", profile])
            self.assertEqual(snippet["env"]["PYTHON"], sys.executable)
            self.assertEqual(snippet["env"]["OBSIDIAN_AGENT_CONFIG"], str(self.config))
        code, human, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("fixture-json\tFixture JSON agent\tabsent", human)
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))

    def test_preview_shows_exact_changes_and_writes_nothing(self):
        before = snapshot(self.home, self.config.parent, self.vault)
        planned = self.preview("fixture-json")
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))
        self.assertRegex(planned["planHash"], r"^[0-9a-f]{64}$")
        files = {item["path"]: item for item in planned["plan"]["files"]}
        self.assertEqual(files[str(self.json_dir / "mcp.json")]["entry"]["env"]["PYTHON"], sys.executable)
        self.assertIn("never edit vault files directly", files[str(self.json_dir / "RULES.md")]["block"])
        self.assertEqual(files[str(self.manifest)]["role"], "manifest")
        self.assertTrue(all(item["op"] == "create" for item in files.values()))
        self.assertEqual(planned["snippets"][0]["destination"], ".fixture/project-rules.md")
        self.assertIn("Configured Obsidian wiki", planned["snippets"][0]["content"])  # {{rules}} substituted
        self.assertIn("fixture-json: restart the fixture agent", planned["notes"])
        code, human, _ = self.run_cli("fixture-json")
        self.assertEqual(code, 0)
        self.assertIn("Review, then apply exactly this plan:", human)
        self.assertIn(f"Plan hash: {planned['planHash']}", human)
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))

    def test_apply_writes_mcp_rules_skills_and_manifest(self):
        vault_before = snapshot(self.vault)
        applied = self.apply("fixture-json")
        self.assertTrue(applied["applied"])
        entry = self.installed_mcp()
        # Absolute package path, no npx, and the interpreter pinned for the server.
        self.assertEqual(entry["command"], "node")
        self.assertEqual(entry["args"], [str(ROOT / "scripts/obsidian-second-brain.mjs"), "mcp", "--tools", "all"])
        self.assertTrue(Path(entry["args"][0]).is_absolute())
        self.assertEqual(entry["env"]["PYTHON"], sys.executable)
        self.assertEqual(entry["env"]["OBSIDIAN_AGENT_CONFIG"], str(self.config))
        rules = (self.json_dir / "RULES.md").read_text(encoding="utf-8")
        self.assertTrue(rules.startswith("<!-- obsidian-second-brain:start -->\n"))
        self.assertTrue(rules.endswith("<!-- obsidian-second-brain:end -->\n"))
        self.assertIn(f'Configured Obsidian wiki: "{self.vault / "wiki"}"', rules)
        self.assertIn("Use the wiki MCP tools; never edit vault files directly", rules)
        sources = connect_module.skill_sources()
        skills = self.json_dir / "skills"
        copied = {str(path.relative_to(skills)) for path in skills.rglob("*") if path.is_file()}
        self.assertEqual(copied, set(sources))
        self.assertEqual({rel.split("/")[0] for rel in copied}, set(connect_module.SKILL_NAMES))
        self.assertFalse(any(rel.startswith("wiki/scripts") for rel in copied))
        self.assertTrue(all((skills / rel).read_bytes() == data for rel, data in sources.items()))
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        record = manifest["agents"]["fixture-json"]
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(record["packageRoot"], str(ROOT))
        self.assertEqual(record["interpreter"], sys.executable)
        self.assertEqual(record["version"], json.loads((ROOT / "package.json").read_text())["version"])
        self.assertEqual(record["skills"]["files"], {rel: hashlib.sha256(data).hexdigest() for rel, data in sources.items()})
        self.assertEqual(record["mcp"]["entrySha256"], connect_module.entry_hash(entry))
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o600)
        self.assertEqual(vault_before, snapshot(self.vault))
        self.assertFalse(list(self.vault.rglob(".*.osb-*")))

    def test_repeat_apply_is_a_no_op(self):
        self.apply("fixture-json", "fixture-toml")
        before = snapshot(self.home, self.config.parent)
        again = self.apply("fixture-json", "fixture-toml")
        self.assertTrue(all(item["op"] == "unchanged" for item in again["plan"]["files"]))
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        code, human, _ = self.run_cli("fixture-json", "fixture-toml")
        self.assertIn("No changes planned.", human)
        self.assertNotIn("Review, then apply", human)

    def test_plan_hash_mismatch_is_refused_like_bootstrap(self):
        planned = self.preview("fixture-json")
        before = snapshot(self.home, self.config.parent)
        bootstrap = load_source("connect_test_bootstrap", str(SCRIPTS / "bootstrap-vault.py"))
        core = dict(planned["plan"])
        del core["planHash"], core["backupDir"]
        for item in core["files"]:
            item.pop("entry", None)
            item.pop("block", None)
            item.pop("root", None)
        self.assertEqual(bootstrap.digest(core), planned["planHash"])  # one shared hash implementation
        for confirm in ("0" * 64, planned["planHash"].upper()):
            code, out, _ = self.run_cli("fixture-json", "--apply", "--confirm", confirm, "--json")
            self.assertEqual(code, 1)
            payload = json.loads(out)
            self.assertEqual(payload["code"], "stale-plan")
            self.assertEqual(payload["error"], f"stale or unconfirmed plan; preview again (hash {planned['planHash']})")
        code, _, err = self.run_cli("fixture-json", "--apply", "--confirm", "0" * 64)
        self.assertEqual(code, 1)
        self.assertIn("stale or unconfirmed plan; preview again", err)
        self.assertEqual(code, 1)
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        # The hash is bound to the exact agent selection and to --force/--remove.
        self.assertNotEqual(planned["planHash"], self.preview("fixture-json", "fixture-toml")["planHash"])
        code, out, _ = self.run_cli("fixture-toml", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual(json.loads(out)["code"], "stale-plan")
        for argv in (["fixture-json", "--confirm", planned["planHash"]], ["fixture-json", "--apply"]):
            self.assertEqual(self.run_cli(*argv)[0], 2)

    def test_concurrent_edit_after_preview_is_refused(self):
        planned = self.preview("fixture-json")
        (self.json_dir).mkdir()
        (self.json_dir / "mcp.json").write_text('{"mcpServers": {"mine": {"command": "x"}}}\n', encoding="utf-8")
        before = snapshot(self.home, self.config.parent)
        code, out, _ = self.run_cli("fixture-json", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual((code, json.loads(out)["code"]), (1, "stale-plan"))
        self.assertEqual(before, snapshot(self.home, self.config.parent))

    def test_edit_during_apply_is_preserved_and_everything_else_rolled_back(self):
        planned = self.preview("fixture-json")
        target = self.json_dir / "mcp.json"
        real = connect_module.write_atomic
        calls = []

        def racing(path, data, mode):
            real(path, data, mode)
            calls.append(path)
            if len(calls) == 1:  # a user/app edits a not-yet-written file between preview and write
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"edited": true}\n', encoding="utf-8")

        with mock.patch.object(connect_module, "write_atomic", racing):
            code, out, _ = self.run_cli("fixture-json", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["code"], "apply-failed")
        self.assertEqual(target.read_text(encoding="utf-8"), '{"edited": true}\n')
        self.assertFalse((self.json_dir / "RULES.md").exists() or (self.json_dir / "skills").exists())
        self.assertFalse(self.manifest.exists())

    def test_unrelated_config_entries_and_formatting_are_preserved(self):
        self.json_dir.mkdir()
        original = {"theme": "dark", "mcpServers": {"other": {"command": "other", "args": ["--x"], "env": {"TOKEN": "keep"}}}}
        (self.json_dir / "mcp.json").write_text(json.dumps(original, indent=4) + "\n", encoding="utf-8")
        (self.json_dir / "RULES.md").write_text("# My rules\n\nBe kind.\n", encoding="utf-8")
        toml_dir = self.home / ".fixture-toml"
        toml_dir.mkdir()
        toml_original = '# my settings\nmodel = "x"\n\n[mcp_servers.other]\ncommand = "other"  # keep me\nargs = ["--x"]\n\n[profiles.fast]\nmodel = "y"\n'
        (toml_dir / "config.toml").write_text(toml_original, encoding="utf-8")
        yaml_dir = self.home / ".fixture-yaml"
        yaml_dir.mkdir()
        (yaml_dir / "config.yaml").write_text("theme: dark\nmcp:\n  servers:\n    other:\n      type: local\n", encoding="utf-8")
        self.apply("fixture-json", "fixture-toml", "fixture-yaml")
        merged = json.loads((self.json_dir / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["theme"], "dark")
        self.assertEqual(merged["mcpServers"]["other"], original["mcpServers"]["other"])
        self.assertIn("obsidian-wiki", merged["mcpServers"])
        self.assertIn('\n    "theme"', (self.json_dir / "mcp.json").read_text(encoding="utf-8"))  # 4-space indent kept
        rules = (self.json_dir / "RULES.md").read_text(encoding="utf-8")
        self.assertTrue(rules.startswith("# My rules\n\nBe kind.\n\n<!-- obsidian-second-brain:start -->"))
        toml_text = (toml_dir / "config.toml").read_text(encoding="utf-8")
        self.assertTrue(toml_text.startswith(toml_original))  # byte-preserving append, comments included
        parsed = tomllib.loads(toml_text)
        self.assertEqual(parsed["mcp_servers"]["other"], {"command": "other", "args": ["--x"]})
        self.assertEqual(parsed["profiles"], {"fast": {"model": "y"}})
        entry = parsed["mcp_servers"]["obsidian-wiki"]
        self.assertEqual(entry["args"][-2:], ["--tools", "all"])
        self.assertEqual(entry["env"]["PYTHON"], sys.executable)
        import yaml
        merged_yaml = yaml.safe_load((yaml_dir / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(merged_yaml["theme"], "dark")
        self.assertEqual(merged_yaml["mcp"]["servers"]["other"], {"type": "local"})
        self.assertEqual(merged_yaml["mcp"]["servers"]["obsidian-wiki"]["environment"]["PYTHON"], sys.executable)
        # Originals are backed up, outside the agents' directories.
        backups = sorted((self.config.parent / "connect-backups").rglob("*-mcp.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)

    def test_remove_restores_the_previous_state(self):
        self.json_dir.mkdir()
        original = {"mcpServers": {"other": {"command": "other"}}}
        (self.json_dir / "mcp.json").write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
        (self.json_dir / "RULES.md").write_text("# My rules\n", encoding="utf-8")
        toml_dir = self.home / ".fixture-toml"
        toml_dir.mkdir()
        toml_original = '[mcp_servers.other]\ncommand = "other"\n'
        (toml_dir / "config.toml").write_text(toml_original, encoding="utf-8")
        yaml_dir = self.home / ".fixture-yaml"  # created entirely by connect
        self.apply("fixture-json", "fixture-toml", "fixture-yaml")
        self.assertTrue((yaml_dir / "config.yaml").exists())
        planned = self.preview("fixture-json", "fixture-toml", "fixture-yaml", extra=("--remove",))
        self.assertTrue(planned["plan"]["remove"])
        self.apply("fixture-json", "fixture-toml", "fixture-yaml", extra=("--remove",))
        self.assertEqual(json.loads((self.json_dir / "mcp.json").read_text(encoding="utf-8")), original)
        self.assertEqual((self.json_dir / "RULES.md").read_text(encoding="utf-8"), "# My rules\n")
        self.assertEqual((toml_dir / "config.toml").read_text(encoding="utf-8"), toml_original)
        self.assertFalse((self.json_dir / "skills").exists())
        self.assertFalse((yaml_dir / "config.yaml").exists())  # file connect created is deleted
        self.assertFalse(self.manifest.exists())
        again = self.preview("fixture-json", extra=("--remove",))
        self.assertTrue(all(item["op"] == "unchanged" for item in again["plan"]["files"]))
        self.assertTrue(any("not connected" in note for note in again["notes"]))

    def test_remove_deletes_files_connect_created(self):
        self.apply("fixture-json")
        self.apply("fixture-json", extra=("--remove",))
        for name in ("mcp.json", "RULES.md"):
            self.assertFalse((self.json_dir / name).exists(), name)
        self.assertFalse(self.manifest.exists())

    def test_modified_entry_or_rules_block_is_not_removed_or_overwritten(self):
        self.apply("fixture-json")
        mcp = self.json_dir / "mcp.json"
        doc = json.loads(mcp.read_text(encoding="utf-8"))
        doc["mcpServers"]["obsidian-wiki"]["env"]["EXTRA"] = "1"
        mcp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        refused = self.refused("fixture-json", "--remove")
        self.assertEqual(refused["refusals"][0]["code"], "entry-modified")
        refused = self.refused("fixture-json")  # an entry that no longer matches what connect wrote is never overwritten
        self.assertEqual(refused["refusals"][0]["code"], "entry-differs")
        self.assertIn("obsidian-wiki", refused["refusals"][0]["snippet"])
        self.assertEqual(json.loads(mcp.read_text(encoding="utf-8")), doc)

    def test_customized_skill_copy_is_refused_and_force_backs_it_up(self):
        self.apply("fixture-json")
        skill = self.json_dir / "skills/wiki-save/SKILL.md"
        shipped = skill.read_bytes()
        skill.write_bytes(b"my customized skill\n")
        before = snapshot(self.home, self.config.parent)
        refused = self.refused("fixture-json")
        self.assertEqual(refused["code"], "connect-refused")
        self.assertEqual(refused["refusals"][0]["code"], "skill-customized")
        self.assertIn(str(skill), refused["refusals"][0]["message"])
        self.assertIn("--force", refused["refusals"][0]["message"])
        self.assertEqual(self.refused("fixture-json", "--remove")["refusals"][0]["code"], "skill-customized")
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        applied = self.apply("fixture-json", extra=("--force",))
        self.assertEqual(skill.read_bytes(), shipped)
        backup_dir = Path(applied["plan"]["backupDir"])
        self.assertEqual((backup_dir / "001-SKILL.md").read_bytes(), b"my customized skill\n")
        self.assertEqual(json.loads((backup_dir / "backup-index.json").read_text())["001-SKILL.md"], str(skill))
        # A pre-existing different file at a destination is refused even without a manifest.
        shutil.rmtree(self.json_dir)
        self.manifest.unlink()
        (self.json_dir / "skills/wiki").mkdir(parents=True)
        (self.json_dir / "skills/wiki/SKILL.md").write_text("somebody else's wiki skill\n", encoding="utf-8")
        self.assertEqual(self.refused("fixture-json")["refusals"][0]["code"], "skill-customized")

    def test_unmodified_older_skill_copy_is_updated_without_force(self):
        self.apply("fixture-json")
        newer = dict(connect_module.skill_sources())
        newer["wiki-query/SKILL.md"] = b"a newer shipped skill\n"
        newer.pop("wiki/references/plugins.md")
        with mock.patch.object(connect_module, "skill_sources", lambda *_: newer):
            planned = self.preview("fixture-json")
            ops = {Path(item["path"]).relative_to(self.json_dir / "skills").as_posix(): item["op"]
                   for item in planned["plan"]["files"] if item["role"] == "skill"}
            self.assertEqual(ops["wiki/references/plugins.md"], "delete")
            self.assertEqual(ops["wiki-query/SKILL.md"], "update")
            self.assertEqual(ops["wiki-save/SKILL.md"], "unchanged")
            self.apply("fixture-json")
        self.assertEqual((self.json_dir / "skills/wiki-query/SKILL.md").read_bytes(), b"a newer shipped skill\n")
        self.assertFalse((self.json_dir / "skills/wiki/references/plugins.md").exists())

    def test_unmergeable_configs_are_refused_with_the_exact_snippet(self):
        self.json_dir.mkdir()
        cases = [
            ("fixture-json", self.json_dir / "mcp.json", '{\n  // my servers\n  "mcpServers": {}\n}\n', "config-unparseable"),
            ("fixture-json", self.json_dir / "mcp.json", '{"mcpServers": {"obsidian-wiki": {"command": "mine"}}}', "entry-differs"),
            ("fixture-json", self.json_dir / "mcp.json", '{"mcpServers": []}', "config-shape"),
            ("fixture-json", self.json_dir / "mcp.json", '{"a": 1, "a": 2}', "config-unparseable"),
            ("fixture-toml", self.home / ".fixture-toml/config.toml", '[mcp_servers.obsidian-wiki]\ncommand = "mine"\n', "entry-differs"),
            ("fixture-toml", self.home / ".fixture-toml/config.toml", 'mcp_servers = { other = { command = "x" } }\n', "toml-merge-unsafe"),
            ("fixture-toml", self.home / ".fixture-toml/config.toml", "not = [toml\n", "config-unparseable"),
            ("fixture-yaml", self.home / ".fixture-yaml/config.yaml", "# my config\nmcp:\n  servers: {}\n", "config-unparseable"),
        ]
        for agent, path, content, code in cases:
            with self.subTest(agent=agent, code=code, content=content):
                path.parent.mkdir(exist_ok=True)
                path.write_text(content, encoding="utf-8")
                before = snapshot(self.home, self.config.parent)
                refused = self.refused(agent)
                self.assertEqual(refused["code"], "connect-refused")
                refusal = refused["refusals"][0]
                self.assertEqual((refusal["agent"], refusal["code"]), (agent, code))
                self.assertIn("obsidian-wiki", refusal["snippet"])
                self.assertEqual(before, snapshot(self.home, self.config.parent))
                code_, _, err = self.run_cli(agent)
                self.assertEqual(code_, 1)
                self.assertIn("Add this by hand instead", err)
                self.assertIn("obsidian-wiki", err)

    def test_symlinked_targets_and_malformed_markers_are_refused(self):
        self.json_dir.mkdir()
        real = self.root / "dotfiles-mcp.json"
        real.write_text("{}", encoding="utf-8")
        (self.json_dir / "mcp.json").symlink_to(real)
        self.assertEqual(self.refused("fixture-json")["refusals"][0]["code"], "symlink-target")
        (self.json_dir / "mcp.json").unlink()
        (self.json_dir / "RULES.md").write_text("<!-- obsidian-second-brain:start -->\nno end\n", encoding="utf-8")
        self.assertEqual(self.refused("fixture-json")["refusals"][0]["code"], "rules-markers-malformed")

    def test_edited_rules_block_is_refused_and_unrecorded_block_is_replaced(self):
        self.apply("fixture-json")
        rules = self.json_dir / "RULES.md"
        original = rules.read_text(encoding="utf-8")
        rules.write_text(original.replace("Never put credentials", "Do whatever"), encoding="utf-8")
        self.assertEqual(self.refused("fixture-json")["refusals"][0]["code"], "rules-block-modified")
        self.assertEqual(self.refused("fixture-json", "--remove")["refusals"][0]["code"], "rules-block-modified")
        # With no manifest record, the marked region is ours by definition and is upserted.
        self.manifest.unlink()
        self.apply("fixture-json")
        self.assertEqual(rules.read_text(encoding="utf-8"), original)

    def test_rollback_after_injected_failure_restores_everything(self):
        self.json_dir.mkdir()
        (self.json_dir / "mcp.json").write_text('{"keep": 1}\n', encoding="utf-8")
        (self.json_dir / "RULES.md").write_text("# mine\n", encoding="utf-8")
        planned = self.preview("fixture-json", "fixture-toml")
        before = snapshot(self.home, self.config.parent, mtimes=False)  # restored files get new mtimes
        real = connect_module.write_atomic
        for fail_at in (1, 5, 40):  # a backup copy, an early skill file, the final manifest write
            with self.subTest(fail_at=fail_at):
                count = []

                def flaky(path, data, mode):
                    count.append(path)
                    if len(count) == fail_at or (fail_at == 40 and path == self.manifest):
                        raise OSError("injected failure")
                    real(path, data, mode)

                with mock.patch.object(connect_module, "write_atomic", flaky):
                    code, out, _ = self.run_cli("fixture-json", "fixture-toml", "--apply", "--confirm", planned["planHash"], "--json")
                self.assertEqual(code, 1)
                payload = json.loads(out)
                self.assertEqual(payload["code"], "apply-failed")
                self.assertIn("injected failure", payload["error"])
                self.assertIn("rolled back", payload["error"])
                self.assertEqual(before, snapshot(self.home, self.config.parent, mtimes=False))
        applied = self.apply("fixture-json", "fixture-toml")
        self.assertTrue(applied["applied"])

    def test_rollback_does_not_overwrite_a_concurrent_edit(self):
        self.json_dir.mkdir()
        planned = self.preview("fixture-json")
        real = connect_module.write_atomic
        rules = self.json_dir / "RULES.md"

        def failing(path, data, mode):
            if path == self.manifest:
                rules.write_text("edited by the user meanwhile\n", encoding="utf-8")
                raise OSError("injected failure")
            real(path, data, mode)

        with mock.patch.object(connect_module, "write_atomic", failing):
            code, out, _ = self.run_cli("fixture-json", "--apply", "--confirm", planned["planHash"], "--json")
        self.assertEqual(code, 1)
        self.assertIn(str(rules), json.loads(out)["error"])
        self.assertEqual(rules.read_text(encoding="utf-8"), "edited by the user meanwhile\n")
        self.assertFalse((self.json_dir / "mcp.json").exists() or self.manifest.exists())

    def test_shared_skill_directory_survives_removing_one_agent(self):
        self.apply("fixture-json", "fixture-shared")
        skill = self.json_dir / "skills/wiki/SKILL.md"
        shared_plan = self.apply("fixture-shared", extra=("--remove",))
        self.assertTrue(skill.exists())
        self.assertTrue(any("also installed for fixture-json" in note for note in shared_plan["notes"]))
        self.assertFalse((self.home / ".fixture-shared/mcp.json").exists())
        self.apply("fixture-json", extra=("--remove",))
        self.assertFalse((self.json_dir / "skills").exists())

    def test_unknown_agent_and_selection_errors(self):
        code, out, _ = self.run_cli("nope", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["code"], "unknown-agent")
        self.assertIn("fixture-json", payload["error"])
        self.assertEqual(self.run_cli("--apply", "--confirm", "x")[0], 2)
        self.assertEqual(self.run_cli("fixture-json", "--remove", "--python", sys.executable)[0], 2)
        code, out, _ = self.run_cli("--bogus", "--json")
        self.assertEqual((code, json.loads(out)["code"]), (2, "invalid-arguments"))

    def test_manifest_or_targets_inside_the_vault_are_refused(self):
        self.config = self.vault / "properties.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        refused = self.refused("fixture-json")
        self.assertEqual(refused["code"], "inside-vault")
        self.assertFalse((self.vault / "connect-manifest.json").exists())

    def test_interpreter_selection_is_validated_and_pinned(self):
        shim = self.root / "bin/pinned-python"
        shim.parent.mkdir()
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        self.apply("fixture-json", extra=("--python", str(shim)))
        self.assertEqual(self.installed_mcp()["env"]["PYTHON"], str(shim))  # symlinks/shims are not resolved
        self.assertEqual(json.loads(self.manifest.read_text())["agents"]["fixture-json"]["interpreter"], str(shim))
        for body, code in (("echo 1 0 1", "interpreter-unsuitable"), ("echo 0 1 1", "interpreter-unsuitable"), ("exit 3", "interpreter-invalid")):
            bad = self.root / "bin/bad-python"
            bad.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
            bad.chmod(0o755)
            payload = self.refused("fixture-json", "--python", str(bad))
            self.assertEqual(payload["code"], code, body)
        self.assertEqual(self.refused("fixture-json", "--python", str(self.root / "missing"))["code"], "interpreter-invalid")

    def test_connect_makes_no_vault_write_git_network_or_agent_cli_call(self):
        calls = []
        vault_before = snapshot(self.vault)

        def forbid(*args, **kwargs):
            calls.append(args)
            raise AssertionError("connect must not spawn processes or open sockets")

        with mock.patch("subprocess.run", forbid), mock.patch("subprocess.Popen", forbid), \
                mock.patch("socket.socket.connect", forbid), mock.patch("socket.create_connection", forbid), \
                mock.patch("os.system", forbid), mock.patch("os.execv", forbid):
            self.apply("fixture-json", "fixture-toml", "fixture-yaml")
            self.run_cli("--json")
            self.apply("fixture-json", "fixture-toml", "fixture-yaml", extra=("--remove",))
        self.assertEqual(calls, [])
        self.assertEqual(vault_before, snapshot(self.vault))


class CliProcessTests(ConnectCase):
    """The shipped CLI in a real process: HOME-only effects, PATH shims, doctor and a moved package."""

    def setUp(self):
        super().setUp()
        self.shims = self.root / "shims"
        self.shims.mkdir()
        self.log = self.root / "shim.log"
        for name in SHIMMED:
            shim = self.shims / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\nexit 97\n', encoding="utf-8")
            shim.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if k not in ("OBSIDIAN_VAULT_PATH", "OBSIDIAN_AGENT_CONFIG")}
        self.env.update({"HOME": str(self.home), "OBSIDIAN_AGENT_CONFIG": str(self.config), "PATH": f"{self.shims}:/usr/bin:/bin",
                         "CONNECT_FIXTURE_PACKAGE": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                         "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def cli(self, *argv, package=None):
        env = dict(self.env)
        if package:
            env["CONNECT_FIXTURE_PACKAGE"] = str(package)
        return subprocess.run([sys.executable, str(WRAPPER), *argv], cwd=self.root, env=env, text=True, capture_output=True)

    def connect_apply(self, *agents, package=None):
        planned = self.cli("connect", *agents, "--json", package=package)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        applied = self.cli("connect", *agents, "--apply", "--confirm", json.loads(planned.stdout)["planHash"], "--json", package=package)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        return json.loads(applied.stdout)

    def doctor(self, package=None):
        result = self.cli("doctor", "--json", package=package)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_no_agent_cli_git_or_network_tool_runs_and_vault_is_untouched(self):
        vault_before = snapshot(self.vault)
        self.connect_apply("fixture-json", "fixture-toml")
        self.assertEqual(self.cli("connect", "--json").returncode, 0)
        self.assertEqual(self.cli("connect", "fixture-json", "--remove", "--json").returncode, 0)
        self.connect_apply("fixture-json", "fixture-toml")
        self.assertFalse(self.log.exists(), self.log.read_text() if self.log.exists() else "")
        self.assertEqual(vault_before, snapshot(self.vault))
        self.assertFalse((self.vault / ".git").exists())

    def test_connect_must_be_the_first_argument_and_routes_through_the_cli(self):
        result = self.cli("--json", "connect")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid-arguments")
        listed = json.loads(self.cli("connect", "--json").stdout)
        self.assertEqual([item["name"] for item in listed["agents"]], [a.name for a in connect_fixture_adapter.ALL])

    def test_packaged_launcher_lists_no_agents_until_an_adapter_is_registered(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")
        env = dict(self.env, PYTHON=sys.executable, PATH=f"{os.path.dirname(node)}:/usr/bin:/bin")
        result = subprocess.run([node, str(LAUNCHER), "connect", "--json"], cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = json.loads(result.stdout)
        self.assertEqual(listed["mode"], "list")
        self.assertEqual(listed["agents"], [])
        self.assertEqual(sorted(listed["genericSnippets"]), ["all", "read"])
        self.assertFalse(self.manifest.exists())

    def test_doctor_reports_connected_stale_absent_and_manual_only_and_writes_nothing(self):
        (self.home / ".fixture-toml").mkdir()
        (self.home / ".fixture-manual").mkdir()
        before = snapshot(self.home, self.config.parent, self.vault)
        report = self.doctor()["connect"]
        self.assertEqual({name: item["state"] for name, item in report["agents"].items()},
                         {"fixture-json": "absent", "fixture-toml": "absent", "fixture-yaml": "absent",
                          "fixture-shared": "absent", "fixture-manual": "manual-only"})
        self.assertEqual(report["manifest"], {"path": str(self.manifest), "exists": False, "error": None})
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))
        human = self.cli("doctor").stdout
        self.assertIn("Connect fixture-manual: manual-only (no-automatic-target)", human)
        self.assertNotIn("Connect fixture-json", human)  # absent and not detected stays quiet
        self.assertIn("Connect fixture-toml: absent", human)  # detected but not connected

        self.connect_apply("fixture-json", "fixture-toml")
        before = snapshot(self.home, self.config.parent, self.vault)
        report = self.doctor()["connect"]
        self.assertEqual(report["agents"]["fixture-json"]["state"], "connected")
        self.assertEqual(report["agents"]["fixture-json"]["reasons"], [])
        self.assertEqual(report["nextActions"], [])
        human = self.cli("doctor").stdout
        self.assertIn("Connect fixture-json: connected", human)
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))

        # Make each kind of drift in turn; doctor must flag it, never repair it.
        mcp = self.json_dir / "mcp.json"
        original = mcp.read_text(encoding="utf-8")
        edited = json.loads(original)
        edited["mcpServers"]["obsidian-wiki"]["args"][0] = "/old/place/scripts/obsidian-second-brain.mjs"
        mcp.write_text(json.dumps(edited), encoding="utf-8")
        (self.json_dir / "skills/wiki-save/SKILL.md").write_text("customized\n", encoding="utf-8")
        (self.json_dir / "skills/wiki-ingest/SKILL.md").unlink()
        rules = self.json_dir / "RULES.md"
        rules.write_text(rules.read_text(encoding="utf-8").replace("planHash", "changed"), encoding="utf-8")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["agents"]["fixture-json"]["version"] = "0.0.1"
        manifest["agents"]["fixture-json"]["interpreter"] = str(self.root / "gone/python")
        self.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        before = snapshot(self.home, self.config.parent, self.vault)
        report = self.doctor()["connect"]
        stale = report["agents"]["fixture-json"]
        self.assertEqual(stale["state"], "stale")
        self.assertEqual({reason["code"] for reason in stale["reasons"]},
                         {"mcp-entry-modified", "skill-modified", "skill-missing", "rules-block-modified", "version-mismatch", "interpreter-missing"})
        self.assertEqual(report["agents"]["fixture-toml"]["state"], "connected")
        action = report["nextActions"][0]
        self.assertEqual((action["code"], action["mutates"], action["requiresReview"]), ("reconnect-agent", False, True))
        self.assertEqual(action["argv"][-2:], ["connect", "fixture-json"])
        human = self.cli("doctor").stdout
        self.assertIn("Connect fixture-json: stale (", human)
        self.assertIn("mcp-entry-modified", human)
        self.assertIn("Next: Preview the reviewed reconnect for fixture-json", human)
        self.assertEqual(before, snapshot(self.home, self.config.parent, self.vault))

    def test_doctor_flags_an_unmergeable_detected_agent_as_manual_only(self):
        self.json_dir.mkdir()
        (self.json_dir / "mcp.json").write_text('{ // comment\n}\n', encoding="utf-8")
        item = self.doctor()["connect"]["agents"]["fixture-json"]
        self.assertEqual((item["state"], item["reasons"][0]["code"]), ("manual-only", "config-unparseable"))

    def test_doctor_flags_entries_after_the_package_moves(self):
        package = self.root / "pkg-a"
        for name in ("scripts", "skills", "hooks", "package.json", "requirements.txt"):
            source = ROOT / name
            (shutil.copytree if source.is_dir() else shutil.copy)(source, package / name,
                                                                  **({"ignore": shutil.ignore_patterns("__pycache__")} if source.is_dir() else {}))
        self.connect_apply("fixture-json", package=package)
        entry = self.installed_mcp()
        self.assertEqual(entry["args"][0], str(package / "scripts/obsidian-second-brain.mjs"))
        self.assertEqual(entry["env"]["PYTHON"], sys.executable)
        self.assertEqual(self.doctor(package=package)["connect"]["agents"]["fixture-json"]["state"], "connected")
        moved = self.root / "pkg-b"
        shutil.move(package, moved)
        before = snapshot(self.home, self.config.parent)
        stale = self.doctor(package=moved)["connect"]["agents"]["fixture-json"]
        self.assertEqual(stale["state"], "stale")
        self.assertEqual({reason["code"] for reason in stale["reasons"]}, {"package-root-mismatch", "package-root-missing"})
        self.assertEqual(before, snapshot(self.home, self.config.parent))
        # Rerunning connect from the new location repairs the entry (a reviewed update, not a silent one).
        planned = json.loads(self.cli("connect", "fixture-json", "--json", package=moved).stdout)
        self.assertEqual({item["role"] for item in planned["plan"]["files"] if item["op"] == "update"}, {"mcp", "manifest"})
        self.connect_apply("fixture-json", package=moved)
        self.assertEqual(self.installed_mcp()["args"][0], str(moved / "scripts/obsidian-second-brain.mjs"))
        self.assertEqual(self.doctor(package=moved)["connect"]["agents"]["fixture-json"]["state"], "connected")


class AdapterContractTests(unittest.TestCase):
    def test_registry_lists_only_importable_adapters_and_validates_them(self):
        self.assertTrue(all(isinstance(name, str) for name in connect_adapters.REGISTRY))
        adapters, errors = connect_module.load_adapters(strict=False)
        self.assertEqual(errors, [])
        self.assertEqual(len(adapters), len(connect_adapters.REGISTRY))
        bad = type("M", (), {"ADAPTER": object()})
        with mock.patch.dict(sys.modules, {"connect_bad_adapter": bad}), mock.patch.object(connect_adapters, "REGISTRY", ["connect_bad_adapter"]):
            _, errors = connect_module.load_adapters(strict=False)
            self.assertEqual(len(errors), 1)
            with self.assertRaises(connect_module.ConnectError):
                connect_module.load_adapters()

    def test_toml_encoding_round_trips(self):
        entry = {"command": "node", "args": ['/a b/ü"x', "mcp"], "env": {"PYTHON": "/p y", "K-1": "v"}, "enabled": True}
        block = connect_module.toml_block(("mcp_servers",), "obsidian-wiki", entry)
        self.assertEqual(tomllib.loads(block), {"mcp_servers": {"obsidian-wiki": entry}})


if __name__ == "__main__":
    unittest.main()
