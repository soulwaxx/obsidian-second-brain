#!/usr/bin/env python3
"""Integration tests for the preview/confirm/apply vault bootstrapper."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/bootstrap-vault.py"
sys.path.insert(0, str(SCRIPT.parent))
from config_contract import load_config, repair_target

# Shared config contract rejects malformed feature containers and known flags,
# while treating null as omitted and preserving unknown fields.
with tempfile.TemporaryDirectory() as contract_temp:
    cfg = Path(contract_temp) / "config.json"
    for payload in (
        {"features": []},
        {"features": {"guard": "false"}},
        {"features": {"autoCommit": 0}},
        {"features": {"retrievalRefresh": "yes"}},
    ):
        target = Path(contract_temp) / "vault"
        target.mkdir(exist_ok=True)
        cfg.write_text(json.dumps({**payload, "vaultPath": str(target)}))
        _, error = load_config(cfg)
        assert error
        rejected = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(target)],
                                  text=True, capture_output=True,
                                  env={**os.environ, "OBSIDIAN_AGENT_CONFIG": str(cfg)})
        assert rejected.returncode != 0, payload
    cfg.write_text(json.dumps({"vaultPath": None, "features": {"guard": None, "unknown": "kept"}}))
    loaded, error = load_config(cfg)
    assert error is None and loaded["features"]["unknown"] == "kept"
    selected = Path(contract_temp) / "repair.json"
    selected.write_text("{invalid")
    assert repair_target(selected, str(selected), contract_temp)
    alias = Path(contract_temp) / "repair-alias.json"
    alias.symlink_to(selected)
    assert not repair_target(selected, str(alias), contract_temp)
    symlink_config = Path(contract_temp) / "selected-symlink.json"
    symlink_config.symlink_to(selected)
    assert not repair_target(symlink_config, str(symlink_config), contract_temp)


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
    assert plan["pluginReady"] is False
    assert "wiki/quickstart.md" in writes
    assert not any(Path(item["path"]) == config for item in plan["writes"])
    assert not (vault / "wiki").exists() and not config.exists()  # preview is read-only
    assert plan["configStatus"]["configuredForVault"] is False
    assert invoke(vault, config, "--apply", "--confirm", "wrong", ok=False)
    assert not (vault / "wiki").exists()
    invoke(vault, config, "--apply", "--confirm", token)
    assert (vault / ".obsidian").is_dir()
    assert (vault / "wiki/index.md").is_file()
    assert "type: note" in (vault / "wiki/quickstart.md").read_text()
    assert (vault / "wiki/index.md").read_text().startswith('---\nokf_version: "0.2"')
    # Bootstrap-created generated indexes must be attributable so the first
    # ordinary page write can refresh navigation in non-Git vaults.
    (vault / "wiki/first.md").write_text('---\ntype: note\ntitle: First\n---\n')
    sync = subprocess.run([sys.executable, str(SCRIPT.parents[0] / ".." / "skills/wiki/scripts/okf_mw/sync.py"), str(vault)],
                          text=True, capture_output=True)
    assert sync.returncode == 0, sync.stdout + sync.stderr
    assert "(./first.md)" in (vault / "wiki/index.md").read_text()
    before = {p: p.read_bytes() for p in (vault / "wiki").rglob("*") if p.is_file()}
    second, second_hash = preview(vault, config)
    assert second["writes"] == []
    invoke(vault, config, "--apply", "--confirm", second_hash)
    assert before == {p: p.read_bytes() for p in before}

    # Same provenance seam in a Git vault using bootstrap's explicit
    # autoCommit:false configuration; nested generated indexes stay attributable.
    git_vault = root / "git-bootstrap"
    (git_vault / "wiki/nested").mkdir(parents=True)
    (git_vault / "wiki/nested/seed.md").write_text('---\ntype: note\ntitle: Seed\n---\n')
    git_config = root / "git-bootstrap-config.json"
    git_config.write_text(json.dumps({"vaultPath": str(git_vault), "features": {"autoCommit": False}}))
    subprocess.run(["git", "init", "-q", str(git_vault)], check=True)
    git_plan, git_hash = preview(git_vault, git_config)
    invoke(git_vault, git_config, "--apply", "--confirm", git_hash)
    (git_vault / "wiki/nested/first.md").write_text('---\ntype: note\ntitle: First nested\n---\n')
    git_sync = subprocess.run([sys.executable, str(SCRIPT.parents[0] / ".." / "skills/wiki/scripts/okf_mw/sync.py"), str(git_vault)], text=True, capture_output=True)
    assert git_sync.returncode == 0, git_sync.stdout + git_sync.stderr
    assert "[First nested](./first.md)" in (git_vault / "wiki/nested/index.md").read_text()
    repeat, repeat_hash = preview(git_vault, git_config)
    assert repeat["writes"] == []
    invoke(git_vault, git_config, "--apply", "--confirm", repeat_hash)

    # A canonical-looking pre-existing/legacy index is never adopted merely
    # because bootstrap can recognize its generated bytes.
    legacy = root / "legacy-index"
    (legacy / "wiki").mkdir(parents=True)
    legacy_config = root / "legacy-index-config.json"
    legacy_preview, _ = preview(legacy, legacy_config)
    generated_index = next(item["content"] for item in legacy_preview["writes"]
                           if Path(item["path"]) == (legacy / "wiki/index.md").resolve())
    quickstart_content = next(item["content"] for item in legacy_preview["writes"]
                              if Path(item["path"]) == (legacy / "wiki/quickstart.md").resolve())
    (legacy / "wiki/quickstart.md").write_text(quickstart_content)
    (legacy / "wiki/index.md").write_text(generated_index)
    legacy_plan, legacy_hash = preview(legacy, legacy_config)
    invoke(legacy, legacy_config, "--apply", "--confirm", legacy_hash)
    legacy_bytes = (legacy / "wiki/index.md").read_bytes()
    (legacy / "wiki/new.md").write_text('---\ntype: note\ntitle: New\n---\n')
    legacy_sync = subprocess.run([sys.executable, str(SCRIPT.parents[0] / ".." / "skills/wiki/scripts/okf_mw/sync.py"), str(legacy)], text=True, capture_output=True)
    assert legacy_sync.returncode != 0 and "unowned dirty bytes" in legacy_sync.stdout
    assert (legacy / "wiki/index.md").read_bytes() == legacy_bytes

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

    # Existing config is unchanged by default; explicit configure requires the
    # installed/enabled plugin and is bound to its inspected preimages.
    disabled_cfg = root / "existing-migration.json"
    disabled_cfg.write_text('{"vaultPath": null, "custom": 1, "features": {"mystery": 7}}')
    disabled_vault = root / "existing-migration-vault"
    disabled_vault.mkdir()
    original_config = disabled_cfg.read_text()
    default_plan, default_hash = preview(disabled_vault, disabled_cfg)
    invoke(disabled_vault, disabled_cfg, "--apply", "--confirm", default_hash)
    assert disabled_cfg.read_text() == original_config
    assert default_plan["pluginDiagnostics"]
    missing = invoke(disabled_vault, disabled_cfg, "--configure", ok=True)
    missing_plan = json.loads(missing.stdout)
    assert "missing" in " ".join(missing_plan["pluginDiagnostics"])
    failed_apply = invoke(disabled_vault, disabled_cfg, "--configure", "--apply", "--confirm", missing_plan["planHash"], ok=False)
    assert "requires Obsidian Git" in failed_apply.stderr
    assert disabled_cfg.read_text() == original_config
    plugin_dir = disabled_vault / ".obsidian/plugins/obsidian-git"
    plugin_dir.mkdir(parents=True)
    for manifest_value in ([], None, {"id": "obsidian-git", "version": "2.39.0-beta"},
                           {"id": "obsidian-git", "version": "v2.39.0"}):
        (plugin_dir / "manifest.json").write_text(json.dumps(manifest_value))
        incompatible = json.loads(invoke(disabled_vault, disabled_cfg, "--configure").stdout)
        assert incompatible["pluginDiagnostics"]
        assert "incompatible" in " ".join(incompatible["pluginDiagnostics"]) or "invalid" in " ".join(incompatible["pluginDiagnostics"])
    (plugin_dir / "manifest.json").write_text(json.dumps({"id": "wrong-id", "version": "1.0.0"}))
    incompatible = json.loads(invoke(disabled_vault, disabled_cfg, "--configure").stdout)
    assert "incompatible" in " ".join(incompatible["pluginDiagnostics"])
    (plugin_dir / "manifest.json").write_text(json.dumps({"id": "obsidian-git", "version": "2.39.0"}))
    (plugin_dir / "data.json").write_text(json.dumps({"unknown": "kept", "autoSaveInterval": 12, "autoPullInterval": 3, "disablePush": True, "squashCommitsBeforePush": True}))
    (disabled_vault / ".obsidian/community-plugins.json").write_text('["other-plugin"]')
    disabled_plugin = json.loads(invoke(disabled_vault, disabled_cfg, "--configure").stdout)
    assert "not enabled" in " ".join(disabled_plugin["pluginDiagnostics"])
    (disabled_vault / ".obsidian/community-plugins.json").write_text('["obsidian-git", "other-plugin"]')
    config_plan_result = invoke(disabled_vault, disabled_cfg, "--configure")
    config_plan = json.loads(config_plan_result.stdout)
    assert config_plan["pluginReady"]
    assert config_plan["configDifferences"]["vaultPath"] == {"from": None, "to": str(disabled_vault.resolve())}
    assert config_plan["configDifferences"]["features.autoCommit"] == {"from": "<missing>", "to": False}
    assert config_plan["profileDiffs"]["squashCommitsBeforePush"] == {"from": True, "to": False}
    assert "autoSaveInterval" not in config_plan["profileDiffs"] and "autoPullInterval" not in config_plan["profileDiffs"]
    assert "privateContentBoundToPlanHash" in next(item for item in config_plan["writes"] if item["path"].endswith("data.json"))
    assert "unknown" not in config_plan_result.stdout and "kept" not in config_plan_result.stdout
    assert "mystery" not in config_plan_result.stdout and '"custom"' not in config_plan_result.stdout
    invoke(disabled_vault, disabled_cfg, "--configure", "--apply", "--confirm", "wrong", ok=False)
    assert disabled_cfg.read_text() == original_config
    invoke(disabled_vault, disabled_cfg, "--configure", "--apply", "--confirm", config_plan["planHash"])
    migrated = json.loads(disabled_cfg.read_text())
    assert migrated["features"]["autoCommit"] is False and migrated["features"]["mystery"] == 7
    profile = json.loads((plugin_dir / "data.json").read_text())
    assert profile["unknown"] == "kept" and profile["autoSaveInterval"] == 12
    assert profile["autoPullInterval"] == 3 and profile["disablePush"] is True
    assert profile["squashCommitsBeforePush"] is False
    assert json.loads((disabled_vault / ".obsidian/community-plugins.json").read_text()) == ["obsidian-git", "other-plugin"]
    assert ".vault-meta/retrieval/" in (disabled_vault / ".gitignore").read_text()
    assert ".vault-meta/lifecycle/" in (disabled_vault / ".gitignore").read_text()
    assert not (disabled_vault / ".vault-meta/retrieval").exists()
    repeat_profile_result = invoke(disabled_vault, disabled_cfg, "--configure")
    repeat_profile = json.loads(repeat_profile_result.stdout)
    assert repeat_profile["writes"] == []
    assert repeat_profile["profileDiffs"] == {} and repeat_profile["configDifferences"] == {}
    assert repeat_profile["ignoreDiffs"] == []
    invoke(disabled_vault, disabled_cfg, "--configure", "--apply", "--confirm", repeat_profile["planHash"])

    # Missing plugin settings means first-time defaults; malformed existing
    # settings still fail closed.
    no_data_vault = root / "no-data-vault"
    plugin = no_data_vault / ".obsidian/plugins/obsidian-git"
    plugin.mkdir(parents=True)
    (plugin / "manifest.json").write_text('{"id":"obsidian-git","version":"2.39.0"}')
    (no_data_vault / ".obsidian/community-plugins.json").write_text('["obsidian-git"]')
    no_data_config = root / "no-data-config.json"
    no_data_config.write_text(json.dumps({"vaultPath": str(no_data_vault), "features": {"autoCommit": False}}))
    no_data_plan = json.loads(invoke(no_data_vault, no_data_config, "--configure").stdout)
    assert no_data_plan["pluginReady"]
    invoke(no_data_vault, no_data_config, "--configure", "--apply", "--confirm", no_data_plan["planHash"])
    default_profile = json.loads((plugin / "data.json").read_text())
    assert default_profile["autoSaveInterval"] == 5 and default_profile["squashCommitsBeforePush"] is False
    (plugin / "data.json").write_text("[]")
    malformed_settings = invoke(no_data_vault, no_data_config, "--configure", ok=False)
    assert "settings must be a JSON object" in malformed_settings.stderr

    # Ignore rules must be effective in Git despite later negations; preexisting
    # tracked state is diagnosed and never staged or removed.
    ignore_vault = root / "ignore-git-vault"
    ignore_vault.mkdir()
    subprocess.run(["git", "init", "-q", str(ignore_vault)], check=True)
    (ignore_vault / ".gitignore").write_text(
        "/.vault-meta/retrieval/\n!/.vault-meta/retrieval/\n/.vault-meta/retrieval/*\n!/.vault-meta/retrieval/bm25.json\n"
        "/.vault-meta/lifecycle/\n!/.vault-meta/lifecycle/\n/.vault-meta/lifecycle/*\n!/.vault-meta/lifecycle/finalize.lock\n!/.vault-meta/lifecycle/.state-*\n")
    for directory_probe, actual_temp in ((".vault-meta/lifecycle/", ".vault-meta/lifecycle/.state-123-abcd"),
                                         (".vault-meta/retrieval/", ".vault-meta/retrieval/bm25.json")):
        winning = subprocess.run(["git", "-C", str(ignore_vault), "check-ignore", "--no-index", "--verbose",
                                  "--non-matching", "--", directory_probe], check=True,
                                 text=True, capture_output=True).stdout
        assert "/.vault-meta/" in winning and "*" in winning
        assert subprocess.run(["git", "-C", str(ignore_vault), "check-ignore", "--no-index", "-q", "--", actual_temp]).returncode != 0
    (ignore_vault / ".vault-meta/lifecycle").mkdir(parents=True)
    tracked_state = ignore_vault / ".vault-meta/lifecycle/tracked.json"
    tracked_state.write_text("fixture tracked state\n")
    subprocess.run(["git", "-C", str(ignore_vault), "add", "-f", ".vault-meta/lifecycle/tracked.json"], check=True)
    staged_fixture_before = subprocess.run(["git", "-C", str(ignore_vault), "diff", "--cached", "--binary"],
                                          check=True, capture_output=True).stdout
    ignore_plugin = ignore_vault / ".obsidian/plugins/obsidian-git"
    ignore_plugin.mkdir(parents=True)
    (ignore_plugin / "manifest.json").write_text('{"id":"obsidian-git","version":"2.39.0"}')
    (ignore_plugin / "data.json").write_text("{}")
    (ignore_vault / ".obsidian/community-plugins.json").write_text('["obsidian-git"]')
    ignore_config = root / "ignore-config.json"
    ignore_config.write_text(json.dumps({"vaultPath": str(ignore_vault), "features": {"autoCommit": False}}))
    ignore_plan = json.loads(invoke(ignore_vault, ignore_config, "--configure").stdout)
    assert ignore_plan["ignoreDiagnostics"] and "already tracked" in ignore_plan["ignoreDiagnostics"][0]
    assert "/.vault-meta/retrieval/" in ignore_plan["ignoreDiffs"]
    assert "/.vault-meta/lifecycle/" in ignore_plan["ignoreDiffs"]
    invoke(ignore_vault, ignore_config, "--configure", "--apply", "--confirm", ignore_plan["planHash"])
    for probe in (".vault-meta/retrieval/", ".vault-meta/retrieval/bm25.json",
                  ".vault-meta/retrieval/bm25.json.tmp", ".vault-meta/retrieval/index.json",
                  ".vault-meta/lifecycle/", ".vault-meta/lifecycle/finalize.lock",
                  ".vault-meta/lifecycle/finalize.lock.tmp", ".vault-meta/lifecycle/.state-123-abcd",
                  ".vault-meta/lifecycle/state.json"):
        assert subprocess.run(["git", "-C", str(ignore_vault), "check-ignore", "--no-index", "-q", "--", probe]).returncode == 0, probe
    assert subprocess.run(["git", "-C", str(ignore_vault), "ls-files", "--error-unmatch", ".vault-meta/lifecycle/tracked.json"],
                          text=True, capture_output=True).returncode == 0
    staged_fixture_after = subprocess.run(["git", "-C", str(ignore_vault), "diff", "--cached", "--binary"],
                                          check=True, capture_output=True).stdout
    assert staged_fixture_after == staged_fixture_before

    # Git evaluates .gitignore from linked-worktree context too; selective child
    # exceptions must be closed by reviewed final directory exclusions.
    main_repo = root / "ignore-worktree-main"
    main_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(main_repo)], check=True)
    subprocess.run(["git", "-C", str(main_repo), "config", "user.name", "Bootstrap Test"], check=True)
    subprocess.run(["git", "-C", str(main_repo), "config", "user.email", "bootstrap@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(main_repo), "commit", "--allow-empty", "-qm", "fixture"], check=True)
    linked_vault = root / "ignore-linked-worktree"
    subprocess.run(["git", "-C", str(main_repo), "worktree", "add", "-q", "-b", "setup-ignore-linked", str(linked_vault)], check=True)
    (linked_vault / ".gitignore").write_text(
        "/.vault-meta/retrieval/\n!/.vault-meta/retrieval/\n/.vault-meta/retrieval/*\n!/.vault-meta/retrieval/bm25.json\n"
        "/.vault-meta/lifecycle/\n!/.vault-meta/lifecycle/\n/.vault-meta/lifecycle/*\n!/.vault-meta/lifecycle/finalize.lock\n!/.vault-meta/lifecycle/.state-*\n")
    linked_plugin = linked_vault / ".obsidian/plugins/obsidian-git"
    linked_plugin.mkdir(parents=True)
    (linked_plugin / "manifest.json").write_text('{"id":"obsidian-git","version":"2.39.0"}')
    (linked_plugin / "data.json").write_text("{}")
    (linked_vault / ".obsidian/community-plugins.json").write_text('["obsidian-git"]')
    linked_config = root / "linked-config.json"
    linked_config.write_text(json.dumps({"vaultPath": str(linked_vault), "features": {"autoCommit": False}}))
    linked_plan = json.loads(invoke(linked_vault, linked_config, "--configure").stdout)
    assert "/.vault-meta/retrieval/" in linked_plan["ignoreDiffs"]
    assert "/.vault-meta/lifecycle/" in linked_plan["ignoreDiffs"]
    invoke(linked_vault, linked_config, "--configure", "--apply", "--confirm", linked_plan["planHash"])
    for probe in (".vault-meta/retrieval/", ".vault-meta/retrieval/bm25.json",
                  ".vault-meta/retrieval/bm25.json.tmp", ".vault-meta/lifecycle/",
                  ".vault-meta/lifecycle/finalize.lock", ".vault-meta/lifecycle/.state-123-abcd",
                  ".vault-meta/lifecycle/state.json"):
        assert subprocess.run(["git", "-C", str(linked_vault), "check-ignore", "--no-index", "-q", "--", probe]).returncode == 0, probe
    linked_repeat = json.loads(invoke(linked_vault, linked_config, "--configure").stdout)
    assert linked_repeat["writes"] == [] and linked_repeat["ignoreDiffs"] == []

    # A non-Git scaffold retains its ownership journal/lock when its operator
    # later initializes Git; reviewed configure adds exact exclusions without
    # moving or rewriting either ownership file.
    ownership_vault = root / "ownership-transition-vault"
    ownership_config = root / "ownership-transition-config.json"
    ownership_plan, ownership_hash = preview(ownership_vault, ownership_config)
    invoke(ownership_vault, ownership_config, "--apply", "--confirm", ownership_hash)
    journal = ownership_vault / ".vault-meta/okf-index-ownership.json"
    ownership_lock = ownership_vault / ".vault-meta/okf-index-ownership.lock"
    assert journal.is_file() and ownership_lock.is_file()
    journal_before = journal.read_bytes()
    lock_before = ownership_lock.read_bytes()
    subprocess.run(["git", "init", "-q", str(ownership_vault)], check=True)
    ownership_plugin = ownership_vault / ".obsidian/plugins/obsidian-git"
    ownership_plugin.mkdir(parents=True)
    (ownership_plugin / "manifest.json").write_text('{"id":"obsidian-git","version":"2.39.0"}')
    (ownership_plugin / "data.json").write_text("{}")
    (ownership_vault / ".obsidian/community-plugins.json").write_text('["obsidian-git"]')
    ownership_plan_result = invoke(ownership_vault, ownership_config, "--configure")
    ownership_configure = json.loads(ownership_plan_result.stdout)
    assert "/.vault-meta/okf-index-ownership.json" in ownership_configure["ignoreDiffs"]
    assert "/.vault-meta/okf-index-ownership.lock" in ownership_configure["ignoreDiffs"]
    invoke(ownership_vault, ownership_config, "--configure", "--apply", "--confirm", ownership_configure["planHash"])
    assert journal.read_bytes() == journal_before and ownership_lock.read_bytes() == lock_before
    for entry in (".vault-meta/okf-index-ownership.json", ".vault-meta/okf-index-ownership.lock"):
        assert subprocess.run(["git", "-C", str(ownership_vault), "check-ignore", "-q", "--", entry]).returncode == 0
    # Existing journals work with the newly excluded residual metadata.
    ownership_sync = subprocess.run([sys.executable, str(SCRIPT.parents[0] / ".." / "skills/wiki/scripts/okf_mw/sync.py"), str(ownership_vault)],
                                    text=True, capture_output=True)
    assert ownership_sync.returncode == 0, ownership_sync.stdout + ownership_sync.stderr

    # Settings-only migration preserves an existing wiki/WIKI.md convention;
    # the standalone scaffold remains responsible for Quickstart creation.
    convention_vault = root / "wiki-convention-vault"
    convention_wiki = convention_vault / "wiki"
    convention_wiki.mkdir(parents=True)
    (convention_wiki / "WIKI.md").write_text("# Existing entry point\n")
    (convention_wiki / "seed.md").write_text("---\ntype: note\ntitle: Seed\n---\n# Seed\n")
    convention_sync = subprocess.run([sys.executable, str(SCRIPT.parents[0] / ".." / "skills/wiki/scripts/okf_mw/sync.py"), str(convention_vault)],
                                     text=True, capture_output=True)
    assert convention_sync.returncode == 0, convention_sync.stdout + convention_sync.stderr
    # Settings-only migration must also preserve legacy/user-owned reserved
    # files that would be refused by the independent scaffold operation.
    historical_log = convention_wiki / "log.md"
    historical_log.write_text("# Historical changelog\n\n- Existing fixture history.\n")
    nested_history = convention_wiki / "meta"
    nested_history.mkdir()
    (nested_history / "Log.MD").write_text("Nested historical fixture log.\n")
    (nested_history / "Index.MD").write_text("Handwritten fixture navigation.\n")
    convention_before = {path.relative_to(convention_vault): path.read_bytes()
                         for path in convention_wiki.rglob("*") if path.is_file()}
    convention_plugin = convention_vault / ".obsidian/plugins/obsidian-git"
    convention_plugin.mkdir(parents=True)
    (convention_plugin / "manifest.json").write_text('{"id":"obsidian-git","version":"2.39.0"}')
    (convention_plugin / "data.json").write_text("{}")
    (convention_vault / ".obsidian/community-plugins.json").write_text('["obsidian-git"]')
    convention_config = root / "wiki-convention-config.json"
    convention_config.write_text(json.dumps({"vaultPath": str(convention_vault), "features": {"autoCommit": True}, "custom": "keep"}))
    convention_plan_result = invoke(convention_vault, convention_config, "--configure")
    convention_plan = json.loads(convention_plan_result.stdout)
    wiki_writes = [entry for entry in convention_plan["writes"]
                   if Path(entry["path"]).is_relative_to(convention_wiki)]
    assert wiki_writes == []
    assert not any(Path(entry["path"]) == convention_wiki / "quickstart.md" for entry in convention_plan["writes"])
    assert Path("wiki/index.md") in convention_before
    assert convention_before == {path.relative_to(convention_vault): path.read_bytes()
                                 for path in convention_wiki.rglob("*") if path.is_file()}
    original_config = convention_config.read_bytes()
    historical_log.write_text(historical_log.read_text() + "- Later fixture history.\n")
    stale_configuration = invoke(convention_vault, convention_config, "--configure", "--apply",
                                 "--confirm", convention_plan["planHash"], ok=False)
    assert "stale or unconfirmed" in stale_configuration.stderr
    assert convention_config.read_bytes() == original_config
    historical_log.write_bytes(convention_before[Path("wiki/log.md")])
    invoke(convention_vault, convention_config, "--configure", "--apply", "--confirm", convention_plan["planHash"])
    convention_after = {path.relative_to(convention_vault): path.read_bytes()
                        for path in convention_wiki.rglob("*") if path.is_file()}
    assert convention_after == convention_before
    assert not (convention_wiki / "quickstart.md").exists()
    assert json.loads(convention_config.read_text())["features"]["autoCommit"] is False
    repeated_configuration = json.loads(invoke(convention_vault, convention_config, "--configure").stdout)
    assert repeated_configuration["writes"] == []
    invoke(convention_vault, convention_config, ok=False)  # scaffold collisions still refused

    # A nonexistent fresh vault is previewed with its root directory planned, then created on apply.
    fresh = root / "brand-new" / "vault"
    fresh_config = root / "fresh-config.json"
    fresh_plan, fresh_hash = preview(fresh, fresh_config)
    assert str(fresh.resolve()) in fresh_plan["directories"]
    assert str(fresh.parent.resolve()) in fresh_plan["directories"]
    assert not fresh.exists()
    invoke(fresh, fresh_config, "--apply", "--confirm", fresh_hash)
    assert (fresh / ".obsidian").is_dir() and (fresh / "wiki/index.md").is_file()

    # A symlink in the config path's ancestors must be rejected before preview/apply.
    real_config_parent = root / "real-config-parent"
    real_config_parent.mkdir()
    config_link = root / "config-link"
    config_link.symlink_to(real_config_parent, target_is_directory=True)
    symlink_config = config_link / "properties.json"
    rejected = invoke(root / "symlink-config-vault", symlink_config, ok=False)
    assert "symlink" in rejected.stderr.lower()
    assert not (root / "symlink-config-vault").exists()

    # A destination that appears after preflight causes a late conflict; rollback
    # removes only this invocation's creations and preserves the appearing file.
    spec = importlib.util.spec_from_file_location("bootstrap_vault", SCRIPT)
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    # Ancestor symlinks to the same pinned inode are still unsafe: the public
    # path chain must remain symlink-free for both create success and rollback.
    create_ancestor_root = root / "ancestor-symlink-create"
    create_ancestor_settings = create_ancestor_root / "base/settings"
    create_ancestor_settings.mkdir(parents=True)
    (create_ancestor_settings / "foreign.txt").write_text("keep create peer")
    moved_create_ancestor = root / "moved-ancestor-create"
    create_ancestor_target = create_ancestor_settings / "profile.json"
    create_ancestor_plan = {"vault": str(create_ancestor_root), "directories": [],
                            "configure": True, "pluginReady": True,
                            "writes": [{"path": str(create_ancestor_target), "content": "{}\\n"}]}
    real_read_at = bootstrap._read_at
    create_ancestor_swap = [False]

    def swap_create_ancestor(parent_fd, name):
        if not create_ancestor_swap[0]:
            create_ancestor_swap[0] = True
            (create_ancestor_root / "base").rename(moved_create_ancestor)
            (create_ancestor_root / "base").symlink_to(moved_create_ancestor, target_is_directory=True)
        return real_read_at(parent_fd, name)

    with mock.patch.object(bootstrap, "_read_at", side_effect=swap_create_ancestor):
        try:
            bootstrap.apply_plan(create_ancestor_plan)
            raise AssertionError("symlinked ancestor after file creation must refuse success")
        except ValueError as exc:
            assert "destination parent changed" in str(exc) or "destination changed during apply" in str(exc)
    assert create_ancestor_swap[0]
    assert (moved_create_ancestor / "settings/profile.json").read_text() == "{}\\n"
    assert (moved_create_ancestor / "settings/foreign.txt").read_text() == "keep create peer"

    replace_ancestor_root = root / "ancestor-symlink-replace"
    replace_ancestor_settings = replace_ancestor_root / "base/settings"
    replace_ancestor_settings.mkdir(parents=True)
    ancestor_profile = replace_ancestor_settings / "profile.json"
    ancestor_profile.write_text('{"old":true}')
    (replace_ancestor_settings / "foreign.txt").write_text("keep replacement peer")
    moved_replace_ancestor = root / "moved-ancestor-replace"
    replace_ancestor_plan = {"vault": str(replace_ancestor_root), "directories": [],
                             "configure": True, "pluginReady": True,
                             "writes": [{"path": str(ancestor_profile), "content": '{"new":true}',
                                         "preimage": '{"old":true}'}]}
    replace_ancestor_swap = [0]

    def swap_replace_ancestor_on_final_read(parent_fd, name):
        replace_ancestor_swap[0] += 1
        if replace_ancestor_swap[0] == 5:
            (replace_ancestor_root / "base").rename(moved_replace_ancestor)
            (replace_ancestor_root / "base").symlink_to(moved_replace_ancestor, target_is_directory=True)
        return real_read_at(parent_fd, name)

    with mock.patch.object(bootstrap, "_read_at", side_effect=swap_replace_ancestor_on_final_read):
        try:
            bootstrap.apply_plan(replace_ancestor_plan)
            raise AssertionError("symlinked ancestor must prevent replacement success")
        except ValueError as exc:
            assert "destination changed during apply" in str(exc)
    assert replace_ancestor_swap[0] == 5
    assert json.loads((moved_replace_ancestor / "settings/profile.json").read_text()) == {"new": True}
    assert (moved_replace_ancestor / "settings/foreign.txt").read_text() == "keep replacement peer"

    # A failed mixed migration rolls back owned config/scaffold changes while
    # preserving a concurrent user edit to the settings postimage.
    rollback_vault = root / "rollback-vault"
    rollback_vault.mkdir()
    rollback_plugin = rollback_vault / ".obsidian/plugins/obsidian-git"
    rollback_plugin.mkdir(parents=True)
    (rollback_plugin / "manifest.json").write_text(json.dumps({"id": "obsidian-git", "version": "2.39.0"}))
    rollback_settings = rollback_plugin / "data.json"
    rollback_settings.write_text('{"custom": "before"}')
    (rollback_vault / ".obsidian/community-plugins.json").write_text('["obsidian-git"]')
    rollback_config = root / "rollback-config.json"
    rollback_config.write_text(json.dumps({"vaultPath": str(rollback_vault), "features": {"autoCommit": True}}))
    rollback_plan = bootstrap.plan_for(rollback_vault, rollback_config, configure=True)
    rollback_marker = rollback_vault / ".vault-meta/setup-marker"
    rollback_plan["directories"].append(str(rollback_marker.parent))
    rollback_plan["writes"].append({"path": str(rollback_marker), "content": "created marker\\n"})
    real_parent_matches = bootstrap._public_parent_matches
    injected_rollback_edit = [False]

    def fail_after_external_settings_edit(path, identity):
        if path == rollback_marker.parent and not injected_rollback_edit[0]:
            injected_rollback_edit[0] = True
            rollback_settings.write_text('{"external": "edit"}')
            rollback_marker.write_text("user edit to newly-created marker\\n")
            return False
        return real_parent_matches(path, identity)

    with mock.patch.object(bootstrap, "_public_parent_matches", side_effect=fail_after_external_settings_edit):
        try:
            bootstrap.apply_plan(rollback_plan)
            raise AssertionError("injected parent ownership failure should abort setup")
        except ValueError as exc:
            assert "parent changed" in str(exc)
    assert injected_rollback_edit[0]
    assert json.loads(rollback_config.read_text())["features"]["autoCommit"] is True
    assert rollback_settings.read_text() == '{"external": "edit"}'
    assert rollback_marker.read_text() == "user edit to newly-created marker\\n"
    assert not (rollback_vault / "wiki").exists()
    assert not (rollback_vault / ".gitignore").exists()

    # A settings parent swapped after file creation/replacement cannot make the
    # apply report success based on a freshly reopened, unrelated directory.
    def parent_swap_fixture(name, existing_settings):
        swap_root = root / name
        swap_plugin = swap_root / ".obsidian/plugins/obsidian-git"
        swap_plugin.mkdir(parents=True)
        (swap_plugin / "manifest.json").write_text('{"id":"obsidian-git","version":"2.39.0"}')
        if existing_settings:
            (swap_plugin / "data.json").write_text('{"squashCommitsBeforePush":true}')
        (swap_root / ".obsidian/community-plugins.json").write_text('["obsidian-git"]')
        swap_config = root / f"{name}-config.json"
        swap_config.write_text(json.dumps({"vaultPath": str(swap_root), "features": {"autoCommit": False}}))
        return swap_root, swap_plugin, swap_config, bootstrap.plan_for(swap_root, swap_config, configure=True)

    # Reproduce the old final-check bug exactly: the second parent open used
    # to see a replaced directory and compare that new identity to itself.
    for case_name, has_settings in (("second-open-create", False), ("second-open-replace", True)):
        second_open_vault, second_open_dir, _, second_open_plan = parent_swap_fixture(case_name, has_settings)
        real_directory_fd = bootstrap._directory_fd
        directory_open_count = [0]
        moved_second_open = root / f"{case_name}-moved"

        def swap_on_reopened_parent(path, created):
            if path == second_open_dir:
                directory_open_count[0] += 1
                if directory_open_count[0] == 2:
                    second_open_dir.rename(moved_second_open)
                    second_open_dir.mkdir()
                    (second_open_dir / "foreign.txt").write_text("preserve second-open peer")
            return real_directory_fd(path, created)

        with mock.patch.object(bootstrap, "_directory_fd", side_effect=swap_on_reopened_parent):
            bootstrap.apply_plan(second_open_plan)
        assert directory_open_count[0] == 1
        assert json.loads((second_open_dir / "data.json").read_text())["squashCommitsBeforePush"] is (False if has_settings else False)

    create_parent_vault, create_parent_dir, create_parent_config, create_parent_plan = parent_swap_fixture(
        "create-parent-swap-vault", False)
    moved_create_parent = root / "moved-create-plugin"
    create_parent_foreign = root / "create-parent-foreign.txt"
    create_swapped = [False]
    real_parent_matches = bootstrap._public_parent_matches

    def swap_create_parent(path, identity):
        if path == create_parent_dir and not create_swapped[0]:
            create_swapped[0] = True
            create_parent_dir.rename(moved_create_parent)
            create_parent_dir.mkdir()
            (create_parent_dir / "foreign.txt").write_text("preserve me")
        return real_parent_matches(path, identity)

    with mock.patch.object(bootstrap, "_public_parent_matches", side_effect=swap_create_parent):
        try:
            bootstrap.apply_plan(create_parent_plan)
            raise AssertionError("created profile parent swap must refuse apply")
        except ValueError as exc:
            assert "parent changed" in str(exc)
    assert create_swapped[0] and not (create_parent_dir / "data.json").exists()
    assert (create_parent_dir / "foreign.txt").read_text() == "preserve me"
    assert (moved_create_parent / "data.json").is_file()

    replace_parent_vault, replace_parent_dir, replace_parent_config, replace_parent_plan = parent_swap_fixture(
        "replace-parent-swap-vault", True)
    moved_replace_parent = root / "moved-replace-plugin"
    replace_swapped = [0]
    real_parent_matches = bootstrap._public_parent_matches

    def swap_replace_parent(path, identity):
        if path == replace_parent_dir:
            replace_swapped[0] += 1
            if replace_swapped[0] == 2:
                replace_parent_dir.rename(moved_replace_parent)
                replace_parent_dir.mkdir()
                (replace_parent_dir / "foreign.txt").write_text("preserve replacement peer")
        return real_parent_matches(path, identity)

    with mock.patch.object(bootstrap, "_public_parent_matches", side_effect=swap_replace_parent):
        try:
            bootstrap.apply_plan(replace_parent_plan)
            raise AssertionError("replaced profile parent swap must refuse apply")
        except ValueError as exc:
            assert "parent changed" in str(exc)
    assert replace_swapped[0] >= 2
    assert (replace_parent_dir / "foreign.txt").read_text() == "preserve replacement peer"
    assert not (replace_parent_dir / "data.json").exists()
    assert json.loads((moved_replace_parent / "data.json").read_text())["squashCommitsBeforePush"] is False

    # In-place edits and identity swaps after the initial preimage read are
    # caught before atomic replacement.
    def replacement_fixture(name):
        race_vault = root / name
        race_plugin = race_vault / ".obsidian/plugins/obsidian-git"
        race_plugin.mkdir(parents=True)
        (race_plugin / "manifest.json").write_text('{"id":"obsidian-git","version":"2.39.0"}')
        (race_plugin / "data.json").write_text('{"squashCommitsBeforePush":true}')
        (race_vault / ".obsidian/community-plugins.json").write_text('["obsidian-git"]')
        race_config = root / f"{name}-config.json"
        race_config.write_text(json.dumps({"vaultPath": str(race_vault), "features": {"autoCommit": False}}))
        return race_vault, race_plugin / "data.json", race_config, bootstrap.plan_for(race_vault, race_config, configure=True)

    rollback_temp_vault, rollback_temp_settings, _, rollback_temp_plan = replacement_fixture("rollback-temp-vault")
    rollback_temp_marker = rollback_temp_vault / ".trigger"
    rollback_temp_plan["writes"].append({"path": str(rollback_temp_marker), "content": "trigger\\n"})
    parent_checks = [0]
    real_parent_matches = bootstrap._public_parent_matches

    def fail_after_all_writes(path, identity):
        if path == rollback_temp_vault:
            parent_checks[0] += 1
            if parent_checks[0] == 3:
                return False
        return real_parent_matches(path, identity)

    real_open = os.open
    rollback_race_injected = [False]

    def edit_during_rollback_temp_open(path, flags, *args, **kwargs):
        if isinstance(path, str) and ".data.json.rollback-" in path and not rollback_race_injected[0]:
            rollback_race_injected[0] = True
            rollback_temp_settings.write_text('{"external":"rollback race"}')
        return real_open(path, flags, *args, **kwargs)

    with mock.patch.object(bootstrap, "_public_parent_matches", side_effect=fail_after_all_writes), \
            mock.patch.object(bootstrap.os, "open", side_effect=edit_during_rollback_temp_open):
        try:
            bootstrap.apply_plan(rollback_temp_plan)
            raise AssertionError("injected final parent failure should initiate rollback")
        except ValueError as exc:
            assert "parent changed" in str(exc)
    assert rollback_race_injected[0]
    assert rollback_temp_settings.read_text() == '{"external":"rollback race"}'
    assert not rollback_temp_marker.exists()
    assert not (rollback_temp_vault / ".gitignore").exists()
    assert not list(rollback_temp_settings.parent.glob("*.rollback-*"))

    race_vault, race_settings, race_config, race_plan = replacement_fixture("content-race-vault")
    real_stat = os.stat
    injected = [False]

    def inject_in_place_edit(path, *args, **kwargs):
        if path == "data.json" and kwargs.get("dir_fd") is not None and not injected[0]:
            injected[0] = True
            race_settings.write_text('{"external":"must survive"}')
        return real_stat(path, *args, **kwargs)

    with mock.patch.object(bootstrap.os, "stat", side_effect=inject_in_place_edit):
        try:
            bootstrap.apply_plan(race_plan)
            raise AssertionError("concurrent in-place settings edit must abort")
        except (OSError, ValueError):
            pass
    assert injected[0] and race_settings.read_text() == '{"external":"must survive"}'

    identity_vault, identity_settings, identity_config, identity_plan = replacement_fixture("identity-race-vault")
    outside_settings = root / "external-settings.json"
    outside_settings.write_text('{"outside":"stay"}')
    displaced_settings = root / "displaced-settings.json"
    real_stat = os.stat
    swapped_identity = [False]

    def inject_identity_swap(path, *args, **kwargs):
        if path == "data.json" and kwargs.get("dir_fd") is not None and not swapped_identity[0]:
            swapped_identity[0] = True
            identity_settings.rename(displaced_settings)
            identity_settings.symlink_to(outside_settings)
        return real_stat(path, *args, **kwargs)

    with mock.patch.object(bootstrap.os, "stat", side_effect=inject_identity_swap):
        try:
            bootstrap.apply_plan(identity_plan)
            raise AssertionError("replacement identity swap must abort")
        except (OSError, ValueError):
            pass
    assert swapped_identity[0] and outside_settings.read_text() == '{"outside":"stay"}'
    identity_settings.unlink()
    displaced_settings.rename(identity_settings)

    late_vault = root / "late-vault"
    late_config = root / "late-config" / "properties.json"
    late_plan, _ = preview(late_vault, late_config)
    late_target = late_vault.resolve() / "wiki/quickstart.md"
    real_open = os.open
    injected = [False]

    def inject_late_conflict(path, flags, *args, **kwargs):
        parent_fd = kwargs.get("dir_fd")
        target_open = (
            path == late_target.name and parent_fd is not None
            and os.fstat(parent_fd).st_ino == (late_vault.resolve() / "wiki").stat().st_ino
        )
        if (Path(path) == late_target or target_open) and not injected[0]:
            injected[0] = True
            late_target.write_text("appeared concurrently\n")
        return real_open(path, flags, *args, **kwargs)

    with mock.patch.object(bootstrap.os, "open", side_effect=inject_late_conflict):
        try:
            bootstrap.apply_plan(late_plan)
            raise AssertionError("late destination conflict should fail")
        except ValueError as exc:
            assert "collision" in str(exc) or "stale" in str(exc)
    assert injected[0] and late_target.read_text() == "appeared concurrently\n"
    assert not late_config.exists()
    assert not (late_vault / ".obsidian").exists()
    assert not (late_vault / "wiki/index.md").exists()

    # A parent swapped for a symlink after preflight must not redirect creation.
    swap_vault = root / "swap-vault"
    swap_vault.mkdir()
    swap_config = root / "swap-config.json"
    swap_plan, _ = preview(swap_vault, swap_config)
    outside = root / "outside"
    outside.mkdir()
    swap_wiki = swap_vault / "wiki"
    displaced_wiki = root / "displaced-wiki"
    swapped = [False]
    escaped = [False]
    swap_real_open = os.open
    real_fdopen = os.fdopen

    def inject_parent_swap(path, flags, *args, **kwargs):
        parent_fd = kwargs.get("dir_fd")
        watched_destination_open = (
            path == "index.md" and parent_fd is not None
            and os.fstat(parent_fd).st_ino == swap_wiki.stat().st_ino
        )
        path_based_open = Path(path) == swap_vault.resolve() / "wiki/index.md"
        if (watched_destination_open or path_based_open) and not swapped[0]:
            swapped[0] = True
            swap_wiki.rename(displaced_wiki)
            swap_wiki.symlink_to(outside, target_is_directory=True)
        return swap_real_open(path, flags, *args, **kwargs)

    class WatchingFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, content):
            result = self.stream.write(content)
            if (outside / "index.md").exists():
                escaped[0] = True
            return result

    with mock.patch.object(bootstrap.os, "open", side_effect=inject_parent_swap), \
            mock.patch.object(bootstrap.os, "fdopen",
                              side_effect=lambda *args, **kwargs: WatchingFile(real_fdopen(*args, **kwargs))):
        try:
            bootstrap.apply_plan(swap_plan)
        except (OSError, ValueError):
            pass
        else:
            raise AssertionError(f"swapped parent should fail apply (swap={swapped[0]})")
    assert swapped[0] and not escaped[0], "destination parent swap redirected a write outside the vault"
    swap_wiki.unlink()
    displaced_wiki.rename(swap_wiki)
    assert not list(outside.iterdir())

    # Config targeting another vault must never be overwritten.
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    wrong_config = root / "wrong-config.json"
    wrong_config.write_text('{"vaultPath": "' + str(elsewhere) + '"}')
    invoke(root / "other", wrong_config, ok=False)

# Whole-directory proof must accept broader exclusions without rewriting them,
# and reject child wildcards even when all diagnostic samples stay ignored.
with tempfile.TemporaryDirectory() as scope_temp:
    for existing in (False, True):
        for number, (rules, expected) in enumerate((
            ("/.vault-meta/\n", True),
            ("/.vault-meta\n", True),
            ("/.vault-meta/lifecycle/\n/.vault-meta/retrieval/\n", True),
            ("/.vault-meta/lifecycle/\n!/.vault-meta/lifecycle/\n"
             "/.vault-meta/lifecycle/*\n!/.vault-meta/lifecycle/.state-[0-9]*\n"
             "/.vault-meta/retrieval/\n", False),
            ("/.vault-meta/lifecycle/\n!/.vault-meta/lifecycle/\n"
             "/.vault-meta/lifecycle/*\n/.vault-meta/lifecycle/*/\n"
             "!/.vault-meta/lifecycle/.state-[0-9]*\n"
             "/.vault-meta/retrieval/\n", False),
        )):
            vault = Path(scope_temp).resolve() / f"vault-{existing}-{number}"
            vault.mkdir()
            subprocess.run(["git", "-C", str(vault), "init", "-q"], check=True)
            plugin = vault / ".obsidian/plugins/obsidian-git"
            plugin.mkdir(parents=True)
            (plugin / "manifest.json").write_text('{"id":"obsidian-git","version":"2.39.0"}')
            (vault / ".obsidian/community-plugins.json").write_text('["obsidian-git"]')
            config = vault.parent / f"config-{existing}-{number}.json"
            config.write_text(json.dumps({"vaultPath": str(vault), "features": {"autoCommit": False}}))
            if existing:
                (vault / ".vault-meta/lifecycle").mkdir(parents=True)
                (vault / ".vault-meta/retrieval").mkdir()
            (vault / ".gitignore").write_text(rules)
            assert bootstrap._git_directory_ignored(vault, ".vault-meta/lifecycle/") == expected
            plan = bootstrap.plan_for(vault, config, configure=True)
            if not expected:
                assert "/.vault-meta/lifecycle/" in plan["ignoreDiffs"]
            bootstrap.apply_plan(plan)
            assert not bootstrap.plan_for(vault, config, configure=True)["writes"]

print("vault bootstrap regressions PASS")
