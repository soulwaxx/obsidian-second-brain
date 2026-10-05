#!/usr/bin/env python3
"""Safely scaffold the minimal OKF v0.2 structure in an Obsidian vault."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from config_contract import load_config

ROOT = Path(__file__).resolve().parents[1]
MIDDLEWARE = ROOT / "skills/wiki/scripts/okf_mw"
sys.path.insert(0, str(MIDDLEWARE))
from ownership import record_created
QUICKSTART = """---\ntype: note\ntitle: Quickstart\ndescription: Entry point and backlog for this vault.\n---\n# Quickstart\n\nThis wiki is the entry point for knowledge in this Obsidian vault. Add substantive pages as the vault grows; generated indexes provide navigation.\n\n## Backlog\n\n- Add topics and source references as needed.\n"""

OBSIDIAN_GIT_ID = "obsidian-git"
OBSIDIAN_GIT_PROFILE = {
    "autoSaveInterval": 5,
    "autoBackupAfterFileChange": True,
    "differentIntervalCommitAndPush": False,
    "autoPushInterval": 0,
    "autoPullInterval": 8,
    "autoPullOnBoot": True,
    "autoCommitOnlyStaged": False,
    "disablePush": False,
    "pullBeforePush": True,
    "squashCommitsBeforePush": False,
}


def config_path() -> Path:
    path = Path(os.environ.get(
        "OBSIDIAN_AGENT_CONFIG",
        str(Path.home() / ".config/obsidian-second-brain/properties.json"),
    )).expanduser().absolute()
    return path


def config_text(vault: Path) -> str:
    return json.dumps({
        "vaultPath": str(vault),
        "features": {"autoCommit": False},
    }, indent=2) + "\n"


def hook_owned_log(path: Path) -> bool:
    """Recognize the lifecycle hook's dated update-log format conservatively."""
    try:
        lines = path.read_text(encoding="utf-8").lstrip("\ufeff").splitlines()
    except (OSError, UnicodeError):
        return False
    lines = [line for line in lines if line.strip()]
    if not lines or lines[0] != "# Directory Update Log":
        return False
    saw_date = False
    for line in lines[1:]:
        if re.fullmatch(r"## \d{4}-\d{2}-\d{2}", line):
            saw_date = True
        elif not re.match(r"\* \*\*(?:Creation|Update)\*\*:", line):
            return False
    return saw_date and any(re.match(r"\* \*\*(?:Creation|Update)\*\*:", line) for line in lines[1:])


def plan_for(vault: Path, config: Path, configure: bool = False) -> dict:
    if vault.exists() and (not vault.is_dir() or vault.is_symlink()):
        raise ValueError("vault must be a directory, not a symlink or file")
    if not vault.exists():
        ancestor = next((p for p in (vault, *vault.parents) if p.exists()), None)
        if ancestor is None or not ancestor.is_dir():
            raise ValueError("vault parent must be an existing directory")
    for entry in (vault / "wiki", vault / ".obsidian"):
        if entry.is_symlink():
            raise ValueError(f"refusing symlinked vault path: {entry}")
        if entry.is_dir() and any(child.is_symlink() for child in entry.rglob("*")):
            raise ValueError(f"refusing symlink in vault settings/wiki tree: {entry}")
    wiki = vault / "wiki"
    obsidian = vault / ".obsidian"
    if obsidian.exists() and not obsidian.is_dir():
        raise ValueError(f"collision: {obsidian} exists and is not a directory")
    if wiki.exists() and not wiki.is_dir():
        raise ValueError(f"collision: {wiki} exists and is not a directory")
    if wiki.exists():
        for path in wiki.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"refusing symlink in wiki tree: {path}")

    for component in (config, *config.parents):
        if component.is_symlink() and not _system_path_alias(component):
            raise ValueError(f"refusing symlinked agent config path: {component}")
    if wiki.exists():
        for path in wiki.rglob("*"):
            if path.name.casefold() == "log.md":
                if not path.is_file() or path.parent != wiki or not hook_owned_log(path):
                    raise ValueError(f"collision: existing user-owned log is not safe for bootstrap; preserving it: {path}")

    writes = {}
    dirs = []
    if not vault.exists():
        missing = []
        parent = vault
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        dirs.extend(str(path) for path in reversed(missing))
    for d in (vault / ".obsidian", wiki):
        if not d.exists() and (not configure or d != wiki):
            dirs.append(str(d))
    quick = wiki / "quickstart.md"
    if quick.exists() and not quick.is_file():
        raise ValueError(f"collision: {quick} exists and is not a file")
    if not configure and not quick.exists():
        writes[str(quick)] = QUICKSTART
    if not (vault / ".obsidian").exists():
        # Creating the directory itself is an intentional Obsidian vault marker.
        pass

    ctext = config_text(vault)
    if not config.parent.exists() and configure:
        dirs.append(str(config.parent))
    config_status = {"configuredForVault": False, "migrationRequired": bool(config.exists())}
    config_differences = {}
    if config.exists():
        data, config_error = load_config(config)
        if config_error:
            raise ValueError(f"existing agent config is invalid; preserving it: {config_error}")
        data = data or {}
        configured = data.get("vaultPath")
        if configured and Path(configured).expanduser().resolve() != vault.resolve():
            raise ValueError(f"existing agent config points elsewhere; preserving it: {config}")
        if configured:
            features = data.get("features") or {}
            config_status = {"configuredForVault": True, "autoCommit": features.get("autoCommit", True)}
        if configure:
            migrated = dict(data)
            if migrated.get("vaultPath") != str(vault):
                config_differences["vaultPath"] = {
                    "from": migrated.get("vaultPath", "<missing>"), "to": str(vault)}
                migrated["vaultPath"] = str(vault)
            features = dict(migrated.get("features") or {})
            if features.get("autoCommit") is not False:
                config_differences["features.autoCommit"] = {
                    "from": features.get("autoCommit", "<missing>"), "to": False}
                features["autoCommit"] = False
            if "features" in migrated or features:
                migrated["features"] = features
            if config_differences:
                writes[str(config)] = {"content": json.dumps(migrated, indent=2) + "\n",
                                       "preimage": config.read_text(encoding="utf-8")}
            config_status = {"configuredForVault": True, "autoCommit": False, "migration": True}
    elif configure:
        config_differences = {"vaultPath": {"from": "<missing>", "to": str(vault)},
                              "features.autoCommit": {"from": "<missing>", "to": False}}
        writes[str(config)] = ctext
        config_status = {"configuredForVault": True, "autoCommit": False, "migration": False}

    # Obsidian Git is operator-installed. Preview readiness without requiring it
    # for the independent wiki scaffold.
    plugin_dir = obsidian / "plugins" / OBSIDIAN_GIT_ID
    manifest_path = plugin_dir / "manifest.json"
    settings_path = plugin_dir / "data.json"
    enabled_path = obsidian / "community-plugins.json"
    plugin_errors = []
    manifest = None
    manifest_loaded = False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_loaded = True
    except (OSError, UnicodeError, json.JSONDecodeError):
        plugin_errors.append("Obsidian Git plugin is missing or has an unreadable manifest; install it in Obsidian")
    if manifest_loaded:
        if not isinstance(manifest, dict):
            plugin_errors.append("Obsidian Git manifest is invalid; expected a JSON object")
        else:
            version = manifest.get("version")
            match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version) if isinstance(version, str) else None
            numbers = tuple(int(part) for part in match.groups()) if match else ()
            if manifest.get("id") != OBSIDIAN_GIT_ID or not match or numbers < (2, 39, 0):
                plugin_errors.append("Obsidian Git manifest id/version is incompatible (requires obsidian-git 2.39.0 or newer stable semver)")
    enabled = []
    try:
        enabled = json.loads(enabled_path.read_text(encoding="utf-8"))
        if not isinstance(enabled, list):
            enabled = []
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    if OBSIDIAN_GIT_ID not in enabled:
        plugin_errors.append("Obsidian Git is not enabled in .obsidian/community-plugins.json")
    profile_diffs = {}
    if configure and not plugin_errors:
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                if not isinstance(settings, dict):
                    raise ValueError("settings must be a JSON object")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"cannot read Obsidian Git settings: {exc}")
        else:
            settings = {}
        updated = dict(settings)
        for key, default in OBSIDIAN_GIT_PROFILE.items():
            # Preserve schedules and disabled-push intent; expose the proposed
            # squash preference even where an existing value differs.
            if key not in settings:
                updated[key] = default
            elif key == "squashCommitsBeforePush" and settings[key] != default:
                updated[key] = default
            elif key == "disablePush" and settings[key] is True:
                updated[key] = True
            if updated.get(key) != settings.get(key):
                profile_diffs[key] = {"from": settings.get(key, "<missing>"), "to": updated[key]}
        if profile_diffs:
            settings_write = {"content": json.dumps(updated, indent=2) + "\n"}
            if settings_path.exists():
                settings_write["preimage"] = settings_path.read_text(encoding="utf-8")
            writes[str(settings_path)] = settings_write

    ignore_path = vault / ".gitignore"
    ignore_lines = ["/.vault-meta/retrieval/", "/.vault-meta/lifecycle/",
                    "/.vault-meta/okf-index-ownership.json", "/.vault-meta/okf-index-ownership.lock",
                    ".obsidian/workspace.json", ".obsidian/workspace-mobile.json"]
    ignore_diffs = []
    ignore_diagnostics = []
    if configure:
        if ignore_path.is_symlink():
            raise ValueError(f"refusing symlinked ignore file: {ignore_path}")
        existing_ignore = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
        git_root = subprocess.run(["git", "-C", str(vault), "rev-parse", "--show-toplevel"],
                                  text=True, capture_output=True, check=False)
        is_git = git_root.returncode == 0
        missing_ignore = [line for line in ignore_lines if line not in existing_ignore.splitlines()]
        if is_git:
            scopes = (
                ("/.vault-meta/retrieval/", ".vault-meta/retrieval/",
                 (".vault-meta/retrieval/bm25.json",
                  ".vault-meta/retrieval/bm25.json.tmp",
                  ".vault-meta/retrieval/index.json")),
                ("/.vault-meta/lifecycle/", ".vault-meta/lifecycle/",
                 (".vault-meta/lifecycle/finalize.lock",
                  ".vault-meta/lifecycle/.state-123-abcd",
                  ".vault-meta/lifecycle/state.json")),
            )
            for rule, directory_probe, output_probes in scopes:
                if not _git_directory_ignored(vault, directory_probe):
                    if rule not in missing_ignore:
                        missing_ignore.append(rule)
                    continue
                for probe in output_probes:
                    winner = _git_ignore_winner(vault, probe)
                    if not winner or winner.startswith("!"):
                        if rule not in missing_ignore:
                            missing_ignore.append(rule)
                        break
            for rule, probe in (("/.vault-meta/okf-index-ownership.json", ".vault-meta/okf-index-ownership.json"),
                                ("/.vault-meta/okf-index-ownership.lock", ".vault-meta/okf-index-ownership.lock")):
                winner = _git_ignore_winner(vault, probe)
                if not winner or winner.startswith("!"):
                    if rule not in missing_ignore:
                        missing_ignore.append(rule)
            tracked = subprocess.run(["git", "-C", str(vault), "ls-files", "--",
                                      ".vault-meta/retrieval", ".vault-meta/lifecycle",
                                      ".vault-meta/okf-index-ownership.json", ".vault-meta/okf-index-ownership.lock"],
                                    text=True, capture_output=True, check=False)
            if tracked.stdout.strip():
                ignore_diagnostics.append("derived retrieval/lifecycle/ownership state is already tracked; setup will not untrack or stage it")
        ignore_diffs = missing_ignore
        if missing_ignore:
            ignore_content = existing_ignore + ("" if not existing_ignore or existing_ignore.endswith("\n") else "\n") + "\n".join(missing_ignore) + "\n"
            if ignore_path.exists():
                writes[str(ignore_path)] = {"content": ignore_content, "preimage": existing_ignore}
            else:
                writes[str(ignore_path)] = ignore_content

    # Only the separate scaffold operation plans pages/indexes. Configuration
    # migration must not synthesize a quickstart or converge existing navigation.
    if not configure:
        with tempfile.TemporaryDirectory(prefix="okf-bootstrap-") as temp:
            trial = Path(temp) / "vault"
            trial.mkdir()
            trialwiki = trial / "wiki"
            if wiki.exists():
                shutil.copytree(wiki, trialwiki)
            else:
                trialwiki.mkdir()
            if not (trialwiki / "quickstart.md").exists():
                (trialwiki / "quickstart.md").write_text(QUICKSTART, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(MIDDLEWARE / "sync.py"), str(trial), "--dry-run"],
                text=True, capture_output=True, check=False,
            )
            if result.returncode:
                raise ValueError("bundled index middleware failed: " + result.stderr.strip())
            generated = subprocess.run(
                [sys.executable, str(MIDDLEWARE / "sync.py"), str(trial)],
                text=True, capture_output=True, check=False,
            )
            if generated.returncode:
                raise ValueError("bundled index middleware failed: " + generated.stderr.strip())
            middleware_indexes = {}
            for line in result.stdout.splitlines():
                if not line.startswith("[") or "] " not in line:
                    continue
                status, remainder = line[1:].split("] ", 1)
                rel = remainder.rsplit(" (", 1)[0]
                if not rel.endswith("index.md") or status not in {"DRY-RUN", "OK"}:
                    continue
                middleware_indexes[rel] = status
                content = (trial / rel).read_text(encoding="utf-8")
                target = wiki.parent / rel
                if target.exists() and target.read_text(encoding="utf-8") != content:
                    raise ValueError(f"collision: middleware would overwrite existing {target}")
                if status == "DRY-RUN" and not target.exists():
                    writes[str(target)] = content

            if wiki.exists():
                for target in wiki.rglob("*"):
                    if target.is_file() and target.name.casefold() == "index.md":
                        rel = target.relative_to(vault).as_posix()
                        if rel not in middleware_indexes:
                            raise ValueError(f"collision: existing index has no canonical middleware content: {target}")
    # Bind confirmation to all inputs used by the plan, not merely its output:
    # edits to existing wiki/config/settings invalidate a previously reviewed hash.
    observed = hashlib.sha256()
    for base in (wiki, vault / ".obsidian"):
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                observed.update(str(path.relative_to(vault)).encode())
                if path.is_file():
                    observed.update(path.read_bytes())
    if config.exists():
        observed.update(str(config).encode())
        observed.update(config.read_bytes())
    if ignore_path.exists():
        observed.update(str(ignore_path).encode())
        observed.update(ignore_path.read_bytes())
    # A quickstart in a pre-existing wiki remains user-owned and is never rewritten.
    write_items = []
    for path, value in sorted(writes.items()):
        item = {"path": path, **(value if isinstance(value, dict) else {"content": value})}
        write_items.append(item)
    return {"vault": str(vault), "directories": dirs,
            "configure": configure,
            "pluginReady": not plugin_errors,
            "pluginDiagnostics": plugin_errors,
            "profileDiffs": profile_diffs,
            "ignoreDiffs": ignore_diffs,
            "configDifferences": config_differences,
            "ignoreDiagnostics": ignore_diagnostics,
            "configStatus": config_status,
            "observedState": observed.hexdigest(),
            "writes": write_items}


def digest(plan: dict) -> str:
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _system_path_alias(path: Path) -> bool:
    return sys.platform == "darwin" and path == Path("/var")


def _git_ignore_record(vault: Path, path: str) -> tuple[bytes, bytes, bytes] | None:
    result = subprocess.run(
        ["git", "-C", str(vault), "check-ignore", "--no-index", "--verbose",
         "--non-matching", "-z", "--stdin"],
        input=os.fsencode(path) + b"\0", capture_output=True, check=False,
    )
    fields = result.stdout.split(b"\0")
    if len(fields) < 4 or not fields[2]:
        return None
    return tuple(fields[:3])


def _git_directory_ignored(vault: Path, path: str) -> bool:
    directory = _git_ignore_record(vault, path.rstrip("/") + "/")
    if not directory or directory[2].startswith(b"!"):
        return False
    # A directory-only winner excludes descendants. Otherwise require the same
    # rule for the bare path, so a child wildcard cannot match an empty suffix.
    if directory[2].endswith(b"/"):
        return directory == _git_ignore_record(vault, path.rstrip("/") + "/.directory-scope-probe")
    return directory == _git_ignore_record(vault, path.rstrip("/"))


def _git_ignore_winner(vault: Path, path: str) -> str | None:
    record = _git_ignore_record(vault, path)
    return os.fsdecode(record[2]) if record else None


def _check_path(path: Path, expect_dir: bool):
    """Reject symlinked ancestors and incompatible existing destinations."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink() and not _system_path_alias(current):
            raise ValueError(f"refusing symlinked destination: {current}")
        if current.exists() and current != absolute and not current.is_dir():
            raise ValueError(f"collision: parent is not a directory: {current}")
    if absolute.exists():
        if expect_dir and not absolute.is_dir():
            raise ValueError(f"collision: destination is not a directory: {absolute}")
        if not expect_dir and not absolute.is_file():
            raise ValueError(f"collision: destination is not a regular file: {absolute}")


def _directory_fd(path: Path, created_dirs: list) -> int:
    """Open/create a directory chain without following any user-controlled link."""
    absolute = path.absolute()
    if sys.platform == "darwin" and absolute.parts[:2] == ("/", "var"):
        absolute = Path("/private", *absolute.parts[1:])
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor, flags)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        try:
            child_fd = os.open(part, flags, dir_fd=fd)
        except FileNotFoundError:
            try:
                os.mkdir(part, dir_fd=fd)
            except FileExistsError:
                pass
            else:
                created_dirs.append((os.dup(fd), part, None))
            child_fd = os.open(part, flags, dir_fd=fd)
            if created_dirs and created_dirs[-1][1] == part and created_dirs[-1][2] is None:
                parent_fd, name, _ = created_dirs[-1]
                stat = os.fstat(child_fd)
                created_dirs[-1] = (parent_fd, name, (stat.st_dev, stat.st_ino))
        os.close(fd)
        fd = child_fd
        current = current / part
    return fd


def _read_at(parent_fd: int, name: str):
    """Read a regular no-follow file and return its bytes and opened identity."""
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"destination is not a regular file: {name}")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), (info.st_dev, info.st_ino), info
    finally:
        os.close(fd)


def _public_parent_matches(path: Path, identity: tuple[int, int]) -> bool:
    try:
        # lstat on the leaf alone still follows symlinked ancestors. Restore
        # the full destination-chain guard before comparing the pinned inode.
        _check_path(path, True)
        info = path.stat(follow_symlinks=False)
        return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity
    except (OSError, ValueError):
        return False


def apply_plan(plan: dict) -> None:
    """Apply a reviewed plan, guarding races and rolling back owned creations."""
    directories = [Path(value) for value in plan["directories"]]
    writes = [(Path(item["path"]), item["content"], item.get("preimage")) for item in plan["writes"]]
    # Preflight the complete write set before making any changes.
    for directory in directories:
        _check_path(directory, True)
    for target, content, preimage in writes:
        _check_path(target.parent, True)
        _check_path(target, False)
        if target.exists():
            actual = target.read_text(encoding="utf-8")
            if actual != content and (preimage is None or actual != preimage):
                raise ValueError(f"stale or conflicting destination: {target}")
        elif preimage is not None:
            raise ValueError(f"stale or missing replacement destination: {target}")

    created_files = []
    replaced_files = []
    write_checks = {}
    created_indexes = []
    created_dirs = []
    try:
        if plan.get("configure") and not plan.get("pluginReady"):
            raise ValueError("configuration/profile apply requires Obsidian Git installed and enabled: " + "; ".join(plan.get("pluginDiagnostics", [])))
        for directory in sorted(directories, key=lambda item: len(item.parts)):
            fd = _directory_fd(directory, created_dirs)
            os.close(fd)
        for target, content, preimage in writes:
            parent_fd = _directory_fd(target.parent, created_dirs)
            name = target.name
            try:
                parent_stat = os.fstat(parent_fd)
                parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
                try:
                    existing_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                          dir_fd=parent_fd)
                except FileNotFoundError:
                    existing_fd = None
                if existing_fd is not None:
                    os.close(existing_fd)
                    original_bytes, original_identity, original_stat = _read_at(parent_fd, name)
                    if original_bytes.decode("utf-8") == content:
                        write_checks[target] = (os.dup(parent_fd), parent_identity,
                                                content.encode("utf-8"), original_identity)
                        continue
                    if preimage is None or original_bytes.decode("utf-8") != preimage:
                        raise ValueError(f"stale or conflicting destination: {target}")
                    public_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    current_bytes, current_identity, _ = _read_at(parent_fd, name)
                    if ((public_info.st_dev, public_info.st_ino) != original_identity
                            or current_identity != original_identity or current_bytes != original_bytes):
                        raise ValueError(f"destination changed during replacement: {target}")
                    temp_name = f".{name}.bootstrap-{os.getpid()}-{len(replaced_files)}"
                    temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                      original_stat.st_mode & 0o777, dir_fd=parent_fd)
                    try:
                        with os.fdopen(temp_fd, "w", encoding="utf-8") as stream:
                            stream.write(content)
                        latest_bytes, latest_identity, _ = _read_at(parent_fd, name)
                        latest_public = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                        if (latest_identity != original_identity or latest_bytes != original_bytes
                                or (latest_public.st_dev, latest_public.st_ino) != original_identity):
                            raise ValueError(f"destination changed during replacement: {target}")
                        if not _public_parent_matches(target.parent, parent_identity):
                            raise ValueError(f"destination parent changed during replacement: {target.parent}")
                        os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    finally:
                        try:
                            os.unlink(temp_name, dir_fd=parent_fd)
                        except FileNotFoundError:
                            pass
                    post_bytes, post_identity, _ = _read_at(parent_fd, name)
                    if post_bytes != content.encode("utf-8"):
                        raise ValueError(f"destination changed after replacement: {target}")
                    replaced_files.append((os.dup(parent_fd), name, post_identity,
                                           preimage, content, original_stat.st_mode & 0o777,
                                           target.parent, parent_identity))
                    write_checks[target] = (os.dup(parent_fd), parent_identity,
                                            content.encode("utf-8"), post_identity)
                    continue
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    fd = os.open(name, flags, 0o666, dir_fd=parent_fd)
                except FileExistsError as exc:
                    raise ValueError(f"collision appeared during apply: {target}") from exc
                file_stat = os.fstat(fd)
                identity = (file_stat.st_dev, file_stat.st_ino)
                created_files.append((os.dup(parent_fd), name, identity, content.encode("utf-8"),
                                      target.parent, parent_identity))
                if target.name.casefold() == "index.md" and target.is_relative_to(Path(plan["vault"]) / "wiki"):
                    created_indexes.append((target.relative_to(Path(plan["vault"])).as_posix(), identity, content.encode("utf-8")))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(content)
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
                write_checks[target] = (os.dup(parent_fd), parent_identity,
                                        content.encode("utf-8"), identity)
            finally:
                os.close(parent_fd)
        for target, _, _ in writes:
            pinned_fd, parent_identity, expected_content, expected_identity = write_checks[target]
            if not _public_parent_matches(target.parent, parent_identity):
                raise ValueError(f"destination parent changed during apply: {target.parent}")
            current_bytes, current_identity, _ = _read_at(pinned_fd, target.name)
            public_info = os.stat(target.name, dir_fd=pinned_fd, follow_symlinks=False)
            target_info = target.stat(follow_symlinks=False)
            if (current_bytes != expected_content or current_identity != expected_identity
                    or (public_info.st_dev, public_info.st_ino) != expected_identity
                    or (target_info.st_dev, target_info.st_ino) != expected_identity
                    or not _public_parent_matches(target.parent, parent_identity)):
                raise ValueError(f"destination changed during apply: {target}")
        if not plan.get("configure"):
            result = subprocess.run([sys.executable, str(MIDDLEWARE / "sync.py"), plan["vault"]],
                                    text=True, capture_output=True, check=False)
            if result.returncode:
                raise ValueError("index synchronization failed: " + result.stderr.strip())
            for rel, identity, content in created_indexes:
                record_created(plan["vault"], rel, identity, content)
    except (OSError, ValueError):
        for parent_fd, name, identity, old_content, new_content, old_mode, parent_path, parent_identity in reversed(replaced_files):
            try:
                if not _public_parent_matches(parent_path, parent_identity):
                    continue
                stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (stat.st_dev, stat.st_ino) == identity:
                    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                    try:
                        current = os.read(fd, os.fstat(fd).st_size).decode("utf-8")
                    finally:
                        os.close(fd)
                    if current == new_content:
                        rollback_name = f".{name}.rollback-{os.getpid()}"
                        fd = os.open(rollback_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), old_mode, dir_fd=parent_fd)
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                                stream.write(old_content)
                            # The temp write can take time. Revalidate the target
                            # and its public parent immediately before restoration.
                            latest_bytes, latest_identity, _ = _read_at(parent_fd, name)
                            latest_public = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                            if (latest_identity == identity and latest_bytes == new_content.encode("utf-8")
                                    and (latest_public.st_dev, latest_public.st_ino) == identity
                                    and _public_parent_matches(parent_path, parent_identity)):
                                os.replace(rollback_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                        finally:
                            try:
                                os.unlink(rollback_name, dir_fd=parent_fd)
                            except FileNotFoundError:
                                pass
            except OSError:
                pass
        for parent_fd, name, identity, expected_content, parent_path, parent_identity in reversed(created_files):
            try:
                if not _public_parent_matches(parent_path, parent_identity):
                    continue
                current, current_identity, _ = _read_at(parent_fd, name)
                if current_identity == identity and current == expected_content:
                    os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
        for parent_fd, name, identity in reversed(created_dirs):
            try:
                stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if identity is not None and (stat.st_dev, stat.st_ino) == identity:
                    os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        for parent_fd, _, _, _, _, _, _, _ in replaced_files:
            os.close(parent_fd)
        for parent_fd, _, _, _, _, _ in created_files:
            os.close(parent_fd)
        for parent_fd, _, _ in created_dirs:
            os.close(parent_fd)
        for pinned_fd, _, _, _ in write_checks.values():
            os.close(pinned_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path, help="existing vault directory")
    parser.add_argument("--apply", action="store_true", help="apply after confirming the preview hash")
    parser.add_argument("--confirm", help="SHA-256 plan hash shown by preview")
    parser.add_argument("--configure", action="store_true", help="explicitly review/migrate integration config and Obsidian Git settings")
    args = parser.parse_args()
    vault = args.vault.expanduser().resolve()
    config = config_path()
    try:
        plan = plan_for(vault, config, configure=args.configure)
    except (OSError, ValueError) as exc:
        print(f"bootstrap-vault: {exc}", file=sys.stderr)
        return 1
    plan_hash = digest(plan)
    if not args.apply:
        public_plan = dict(plan)
        safe_writes = []
        for write in plan["writes"]:
            shown = dict(write)
            if (Path(write["path"]) == config or Path(write["path"]).name == "data.json"
                    or (Path(write["path"]) == Path(plan["vault"]) / ".gitignore" and "preimage" in write)):
                shown.pop("content", None)
                shown.pop("preimage", None)
                shown["privateContentBoundToPlanHash"] = True
            safe_writes.append(shown)
        public_plan["writes"] = safe_writes
        print(json.dumps({"planHash": plan_hash, **public_plan}, indent=2))
        return 0
    if args.confirm != plan_hash:
        print(f"bootstrap-vault: stale or unconfirmed plan; preview again (hash {plan_hash})", file=sys.stderr)
        return 1
    try:
        apply_plan(plan)
    except (OSError, ValueError) as exc:
        print(f"bootstrap-vault: {exc}", file=sys.stderr)
        return 1
    print(f"Applied bootstrap plan {plan_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
