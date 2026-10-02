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
    # Bootstrap-created generated indexes must be attributable so the first
    # ordinary page write can refresh navigation in non-Git vaults.
    (vault / "wiki/first.md").write_text('---\ntype: note\ntitle: First\n---\n')
    sync = subprocess.run([sys.executable, str(SCRIPT.parents[0] / ".." / "skills/wiki/scripts/okf_mw/sync.py"), str(vault)],
                          text=True, capture_output=True)
    assert sync.returncode == 0, sync.stdout + sync.stderr
    assert "(first.md)" in (vault / "wiki/index.md").read_text()
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
    assert "[First nested](first.md)" in (git_vault / "wiki/nested/index.md").read_text()
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
            assert "collision" in str(exc)
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

print("vault bootstrap regressions PASS")
