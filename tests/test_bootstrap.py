#!/usr/bin/env python3
"""Integration tests for the preview/confirm/apply vault bootstrapper."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/bootstrap-vault.py"


def invoke(vault, config, *args, ok=True):
    env = os.environ.copy()
    env["OBSIDIAN_AGENT_CONFIG"] = str(config)
    result = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(vault), *args],
                            text=True, capture_output=True, env=env)
    assert (result.returncode == 0) == ok, result.stderr
    return result


def preview(vault, config):
    data = json.loads(invoke(vault, config).stdout)
    return data, data["planHash"]


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    config = root / "config" / "properties.json"
    vault = root / "empty"
    vault.mkdir()
    plan, token = preview(vault, config)
    writes = {Path(item["path"]).relative_to(vault.resolve()).as_posix(): item["content"]
              for item in plan["writes"] if Path(item["path"]).is_relative_to(vault.resolve())}
    assert "wiki/quickstart.md" in writes
    assert "wiki/index.md" in writes
    assert "autoCommit\": false" in next(item["content"] for item in plan["writes"]
                                         if Path(item["path"]) == config)
    assert not (vault / "wiki").exists() and not config.exists()  # preview is read-only
    assert plan["configStatus"] == {"configuredForVault": True, "autoCommit": False}
    assert invoke(vault, config, "--apply", "--confirm", "wrong", ok=False)
    assert not (vault / "wiki").exists()
    invoke(vault, config, "--apply", "--confirm", token)
    assert (vault / ".obsidian").is_dir()
    assert (vault / "wiki/index.md").is_file()
    assert "type: note" in (vault / "wiki/quickstart.md").read_text()
    assert (vault / "wiki/index.md").read_text().startswith('---\nokf_version: "0.2"')
    before = {p: p.read_bytes() for p in (vault / "wiki").rglob("*") if p.is_file()}
    second, second_hash = preview(vault, config)
    assert second["writes"] == []
    invoke(vault, config, "--apply", "--confirm", second_hash)
    assert before == {p: p.read_bytes() for p in before}

    # Stale plan: a newly appearing quickstart invalidates confirmation.
    stale_vault = root / "stale"
    stale_vault.mkdir()
    _, stale = preview(stale_vault, root / "stale-config.json")
    (stale_vault / "wiki").mkdir()
    (stale_vault / "wiki/quickstart.md").write_text("user content")
    invoke(stale_vault, root / "stale-config.json", "--apply", "--confirm", stale, ok=False)
    assert not (stale_vault / ".obsidian").exists()

    # Preserve populated vault content and existing config, including omitted defaults.
    populated = root / "populated"
    populated.mkdir()
    note = populated / "user-note.md"
    note.write_text("user data stays untouched\n")
    cfg = root / "existing-config.json"
    original = '{"vaultPath": "' + str(populated) + '", "custom": 7}\n'
    cfg.write_text(original)
    p, h = preview(populated, cfg)
    assert note.read_text() == "user data stays untouched\n"
    assert cfg.read_text() == original
    assert p["configStatus"] == {"configuredForVault": True, "autoCommit": True}
    assert any(Path(item["path"]) == (populated / "wiki/index.md").resolve() for item in p["writes"])
    invoke(populated, cfg, "--apply", "--confirm", h)
    assert note.read_text() == "user data stays untouched\n"
    assert cfg.read_text() == original
    assert (populated / "wiki/index.md").is_file()

    # User-owned indexes and nested indexes middleware would overwrite are collisions.
    user_index = root / "user-index"
    (user_index / "wiki").mkdir(parents=True)
    (user_index / "wiki/quickstart.md").write_text('---\ntype: note\n---\n')
    (user_index / "wiki/index.md").write_text("handwritten root index\n")
    invoke(user_index, root / "user-index-config.json", ok=False)

    collision = root / "collision"
    (collision / "wiki/topic").mkdir(parents=True)
    (collision / "wiki/topic/note.md").write_text('---\ntype: note\n---\n')
    (collision / "wiki/topic/index.md").write_text("handwritten\n")
    invoke(collision, root / "collision-config.json", ok=False)

    # Orphan nested indexes and nested logs collide even when sync has no output for them.
    orphan_index = root / "orphan-index"
    (orphan_index / "wiki/topic").mkdir(parents=True)
    (orphan_index / "wiki/topic/index.md").write_text("handwritten empty-dir index\n")
    invoke(orphan_index, root / "orphan-index-config.json", ok=False)
    nested_log = root / "nested-log"
    (nested_log / "wiki/topic").mkdir(parents=True)
    (nested_log / "wiki/topic/log.md").write_text("user data\n")
    invoke(nested_log, root / "nested-log-config.json", ok=False)

    # Paths containing parentheses survive middleware dry-run output parsing, and
    # previously generated nested indexes are accepted unchanged on repeat preview.
    projects = root / "projects"
    project_dir = projects / "wiki/Projects (Active)"
    project_dir.mkdir(parents=True)
    (project_dir / "overview.md").write_text('---\ntype: note\ntitle: Overview\n---\n')
    projects_cfg = root / "projects-config.json"
    project_plan, project_hash = preview(projects, projects_cfg)
    nested_index = (project_dir / "index.md").resolve()
    assert any(Path(item["path"]) == nested_index for item in project_plan["writes"])
    invoke(projects, projects_cfg, "--apply", "--confirm", project_hash)
    repeated_plan, repeated_hash = preview(projects, projects_cfg)
    assert repeated_plan["writes"] == []
    invoke(projects, projects_cfg, "--apply", "--confirm", repeated_hash)

    # Existing logs are accepted only when recognizable as hook-generated; never rewritten.
    user_log = root / "user-log"
    (user_log / "wiki").mkdir(parents=True)
    (user_log / "wiki/log.md").write_text("My personal log, not hook-owned.\n")
    invoke(user_log, root / "user-log-config.json", ok=False)
    malformed_log = root / "malformed-log"
    (malformed_log / "wiki").mkdir(parents=True)
    (malformed_log / "wiki/log.md").write_text("# Directory Update Log\n\nnot a hook entry\n")
    invoke(malformed_log, root / "malformed-log-config.json", ok=False)

    owned_log = root / "owned-log"
    (owned_log / "wiki").mkdir(parents=True)
    log_text = "# Directory Update Log\n\n## 2026-09-27\n* **Creation**: Added [page.md](/page.md).\n"
    (owned_log / "wiki/log.md").write_text(log_text)
    owned_plan, owned_hash = preview(owned_log, root / "owned-log-config.json")
    assert all(Path(item["path"]) != (owned_log / "wiki/log.md").resolve() for item in owned_plan["writes"])
    invoke(owned_log, root / "owned-log-config.json", "--apply", "--confirm", owned_hash)
    assert (owned_log / "wiki/log.md").read_text() == log_text

    # Existing configs without a vault target are preserved and require explicit opt-in setup.
    for config_value in ('{"vaultPath": null, "custom": 1}', '{"custom": 1}'):
        disabled_cfg = root / ("disabled-" + str(len(config_value)) + ".json")
        disabled_cfg.write_text(config_value)
        disabled_vault = root / ("disabled-vault-" + str(len(config_value)))
        disabled_vault.mkdir()
        failed = invoke(disabled_vault, disabled_cfg, ok=False)
        assert "explicit features.autoCommit:false" in failed.stderr
        assert disabled_cfg.read_text() == config_value
        assert not (disabled_vault / "wiki").exists()

    # A nonexistent fresh vault is previewed with its root directory planned, then created on apply.
    fresh = root / "brand-new" / "vault"
    fresh_config = root / "fresh-config.json"
    fresh_plan, fresh_hash = preview(fresh, fresh_config)
    assert str(fresh.resolve()) in fresh_plan["directories"]
    assert str(fresh.parent.resolve()) in fresh_plan["directories"]
    assert not fresh.exists()
    invoke(fresh, fresh_config, "--apply", "--confirm", fresh_hash)
    assert (fresh / ".obsidian").is_dir() and (fresh / "wiki/index.md").is_file()

    # Config targeting another vault must never be overwritten.
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    wrong_config = root / "wrong-config.json"
    wrong_config.write_text('{"vaultPath": "' + str(elsewhere) + '"}')
    invoke(root / "other", wrong_config, ok=False)

print("vault bootstrap regressions PASS")
