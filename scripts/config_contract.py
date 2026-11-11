"""Shared validation for the Obsidian agent integration config."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

KNOWN_FLAGS = ("guard", "toc", "autoCommit", "retrievalRefresh")
OBSIDIAN_GIT_ID = "obsidian-git"
OBSIDIAN_GIT_MINIMUM = (2, 39, 0)


def system_path_alias(path: Path) -> bool:
    """OS-owned path aliases that setup accepts in an otherwise symlink-free config path."""
    return sys.platform == "darwin" and path == Path("/var")


def symlinked_config_component(config: Path) -> Path | None:
    """First symlinked component of the agent config path that reviewed setup refuses."""
    return next((part for part in (config, *config.parents) if part.is_symlink() and not system_path_alias(part)), None)


def obsidian_git_diagnostics(vault: Path) -> list[tuple[str, str]]:
    """Statically check that operator-installed Obsidian Git is compatible and enabled.

    Shared by reviewed setup and read-only doctor; it reads only the plugin
    manifest and enabled-plugin list and never contacts the Obsidian app.
    Returns ``(code, message)`` pairs; an empty list means ready.
    """
    obsidian = vault / ".obsidian"
    manifest_path = obsidian / "plugins" / OBSIDIAN_GIT_ID / "manifest.json"
    errors = []
    manifest = None
    manifest_loaded = False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_loaded = True
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append(("obsidian-git-missing", "Obsidian Git plugin is missing or has an unreadable manifest; install it in Obsidian"))
    if manifest_loaded:
        if not isinstance(manifest, dict):
            errors.append(("obsidian-git-invalid", "Obsidian Git manifest is invalid; expected a JSON object"))
        else:
            version = manifest.get("version")
            match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version) if isinstance(version, str) else None
            numbers = tuple(int(part) for part in match.groups()) if match else ()
            if manifest.get("id") != OBSIDIAN_GIT_ID or not match or numbers < OBSIDIAN_GIT_MINIMUM:
                errors.append(("obsidian-git-incompatible", "Obsidian Git manifest id/version is incompatible (requires obsidian-git 2.39.0 or newer stable semver)"))
    enabled = []
    try:
        enabled = json.loads((obsidian / "community-plugins.json").read_text(encoding="utf-8"))
        if not isinstance(enabled, list):
            enabled = []
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    if OBSIDIAN_GIT_ID not in enabled:
        errors.append(("obsidian-git-disabled", "Obsidian Git is not enabled in .obsidian/community-plugins.json"))
    return errors


def load_config(path: str | Path) -> tuple[dict[str, Any] | None, str | None]:
    config_path = Path(path)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"cannot load {config_path}: {exc}"
    if not isinstance(value, dict):
        return None, f"invalid {config_path}: config must be a JSON object"
    vault = value.get("vaultPath")
    if vault is not None and not isinstance(vault, str):
        return value, f"invalid {config_path}: vaultPath must be a string or null"
    features = value.get("features")
    if features is not None and not isinstance(features, dict):
        return value, f"invalid {config_path}: features must be an object or null"
    for name in KNOWN_FLAGS:
        flag = (features or {}).get(name)
        if flag is not None and not isinstance(flag, bool):
            return value, f"invalid {config_path}: features.{name} must be a boolean or null"
    return value, None


def repair_target(config_path: str | Path, target_path: str, cwd: str) -> bool:
    """Accept only the selected, existing regular config file, without symlinks."""
    import os
    import stat

    selected = Path(config_path).expanduser()
    target = Path(target_path).expanduser()
    if not selected.is_absolute():
        selected = Path(cwd) / selected
    if not target.is_absolute():
        target = Path(cwd) / target
    selected = Path(os.path.abspath(selected))
    target = Path(os.path.abspath(target))
    if target != selected:
        return False
    try:
        info = target.lstat()
        return stat.S_ISREG(info.st_mode) and target.resolve(strict=True) == selected.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--repair-target")
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args()
    value, error = load_config(args.config)
    result = {"config": value, "error": error}
    if args.repair_target is not None:
        result["repairTarget"] = repair_target(args.config, args.repair_target, args.cwd)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
