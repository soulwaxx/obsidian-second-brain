"""Shared validation for the Obsidian agent integration config."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

KNOWN_FLAGS = ("guard", "toc", "autoCommit", "retrievalRefresh")


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
