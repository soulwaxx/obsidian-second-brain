#!/usr/bin/env python3
"""Safely scaffold the minimal OKF v0.2 structure in an Obsidian vault."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MIDDLEWARE = ROOT / "skills/wiki/scripts/okf_mw"
QUICKSTART = """---\ntype: note\ntitle: Quickstart\ndescription: Entry point and backlog for this vault.\n---\n# Quickstart\n\nThis wiki is the entry point for knowledge in this Obsidian vault. Add substantive pages as the vault grows; generated indexes provide navigation.\n\n## Backlog\n\n- Add topics and source references as needed.\n"""


def config_path() -> Path:
    return Path(os.environ.get(
        "OBSIDIAN_AGENT_CONFIG",
        str(Path.home() / ".config/obsidian-second-brain/properties.json"),
    )).expanduser().absolute()


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


def plan_for(vault: Path, config: Path) -> dict:
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

    if config.is_symlink():
        raise ValueError(f"refusing symlinked agent config: {config}")
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
        if not d.exists():
            dirs.append(str(d))
    quick = wiki / "quickstart.md"
    if quick.exists() and not quick.is_file():
        raise ValueError(f"collision: {quick} exists and is not a file")
    if not quick.exists():
        writes[str(quick)] = QUICKSTART
    if not (vault / ".obsidian").exists():
        # Creating the directory itself is an intentional Obsidian vault marker.
        pass

    ctext = config_text(vault)
    if not config.parent.exists():
        dirs.append(str(config.parent))
    config_write = not config.exists()
    if config_write:
        writes[str(config)] = ctext
    else:
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"existing agent config is unreadable; preserving it: {config}") from exc
        configured = data.get("vaultPath") if isinstance(data, dict) else None
        if not configured:
            raise ValueError(
                f"existing agent config has no vaultPath; preserving it. Configure {config} "
                f"for this vault with explicit features.autoCommit:false before bootstrapping"
            )
        if Path(configured).expanduser().resolve() != vault.resolve():
            raise ValueError(f"existing agent config points elsewhere; preserving it: {config}")
        features = data.get("features") if isinstance(data.get("features"), dict) else {}
        auto_commit = features.get("autoCommit", True)
        if auto_commit is None:
            auto_commit = True
        config_status = {"configuredForVault": True, "autoCommit": auto_commit}

    if config_write:
        config_status = {"configuredForVault": True, "autoCommit": False}

    # Ask the bundled middleware what it would generate against an isolated copy.
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
        # Generate only in the disposable copy to capture exact index bytes.
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

        # Any existing index must have canonical content from middleware. This
        # fails closed for orphan indexes in empty directories while allowing
        # generated indexes from a prior bootstrap to remain idempotent.
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
    # A quickstart in a pre-existing wiki remains user-owned and is never rewritten.
    return {"vault": str(vault), "directories": dirs,
            "configStatus": config_status,
            "observedState": observed.hexdigest(),
            "writes": [{"path": p, "content": text} for p, text in sorted(writes.items())]}


def digest(plan: dict) -> str:
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path, help="existing vault directory")
    parser.add_argument("--apply", action="store_true", help="apply after confirming the preview hash")
    parser.add_argument("--confirm", help="SHA-256 plan hash shown by preview")
    args = parser.parse_args()
    vault = args.vault.expanduser().resolve()
    config = config_path()
    try:
        plan = plan_for(vault, config)
    except (OSError, ValueError) as exc:
        print(f"bootstrap-vault: {exc}", file=sys.stderr)
        return 1
    plan_hash = digest(plan)
    if not args.apply:
        print(json.dumps({"planHash": plan_hash, **plan}, indent=2))
        return 0
    if args.confirm != plan_hash:
        print(f"bootstrap-vault: stale or unconfirmed plan; preview again (hash {plan_hash})", file=sys.stderr)
        return 1
    try:
        for dirname in plan["directories"]:
            Path(dirname).mkdir(parents=True, exist_ok=True)
        for item in plan["writes"]:
            target = Path(item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.read_text(encoding="utf-8") != item["content"]:
                    raise ValueError(f"collision appeared during apply: {target}")
                continue
            target.write_text(item["content"], encoding="utf-8")
        result = subprocess.run([sys.executable, str(MIDDLEWARE / "sync.py"), str(vault)],
                                text=True, capture_output=True, check=False)
        if result.returncode:
            raise ValueError("index synchronization failed: " + result.stderr.strip())
    except (OSError, ValueError) as exc:
        print(f"bootstrap-vault: {exc}", file=sys.stderr)
        return 1
    print(f"Applied bootstrap plan {plan_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
