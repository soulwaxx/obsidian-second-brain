#!/usr/bin/env python3
"""Safely provision optional retrieval helpers into a vault (preview, then apply)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parent
HELPERS = ("retrieve.py", "bm25-index.py", "contextual-prefix.py")
EXCLUDE = ".vault-meta/retrieval/"


def plan(vault):
    if not vault.is_dir() or vault.is_symlink():
        raise ValueError("vault must be an existing non-symlink directory")
    scripts_dir = vault / "scripts"
    if scripts_dir.is_symlink():
        raise ValueError(f"refusing symlinked scripts directory: {scripts_dir}")
    writes = []
    for name in HELPERS:
        target = scripts_dir / name
        if target.exists() or target.is_symlink():
            raise ValueError(f"refusing to overwrite existing path: {target}")
        writes.append({"path": str(target), "content": (SOURCE / name).read_text(encoding="utf-8")})
    git_dir = vault / ".git"
    if git_dir.is_symlink():
        raise ValueError(f"refusing symlinked Git directory: {git_dir}")
    cache_ignore = "Git local exclude"
    if git_dir.is_file():
        top = subprocess.run(
            ["git", "-C", str(vault), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, check=False,
        )
        if top.returncode or Path(top.stdout.strip()).resolve() != vault.resolve():
            raise ValueError(".git file is not a Git worktree rooted at this vault; refusing provisioning")
        ignored_paths = (".vault-meta/retrieval/", ".vault-meta/retrieval/bm25.json")
        for ignored_path in ignored_paths:
            ignored = subprocess.run(
                ["git", "-C", str(vault), "check-ignore", "--no-index", "-q", "--", ignored_path],
                text=True, capture_output=True, check=False,
            )
            if ignored.returncode != 0:
                raise ValueError(
                    "linked Git worktree retrieval cache is not effectively ignored; the directory and "
                    "BM25 file must both be ignored (filename-only rules and later negations are insufficient); "
                    "add an effective '.vault-meta/retrieval/' rule to the vault .gitignore, then preview again"
                )
        cache_ignore = "effective Git ignore check excludes retrieval directory and BM25 cache"
    if git_dir.is_dir():
        info_dir = git_dir / "info"
        exclude = info_dir / "exclude"
        if info_dir.is_symlink() or exclude.is_symlink():
            raise ValueError(f"refusing symlinked Git exclude path: {exclude}")
        if exclude.exists() and EXCLUDE in exclude.read_text(encoding="utf-8").splitlines():
            exclude_content = None
        else:
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            exclude_content = current + ("" if not current or current.endswith("\n") else "\n") + EXCLUDE + "\n"
            writes.append({"path": str(exclude), "content": exclude_content})
    payload = {"vault": str(vault), "cacheIgnore": cache_ignore, "writes": writes}
    payload["planHash"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path, help="existing vault directory")
    parser.add_argument("--apply", action="store_true", help="apply only with the preview hash")
    parser.add_argument("--confirm", help="planHash printed by preview")
    args = parser.parse_args()
    vault = args.vault.expanduser().absolute()
    try:
        result = plan(vault)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"provision-retrieval: {exc}\n")
    if not args.apply:
        print(json.dumps(result, indent=2))
        return 0
    if args.confirm != result["planHash"]:
        parser.exit(1, "provision-retrieval: unconfirmed or stale plan; preview again\n")

    writes = [(Path(item["path"]), item["content"]) for item in result["writes"]]
    created_files = []
    created_dirs = []
    staged = []
    originals = {}
    try:
        # Preflight all destinations before creating anything.
        for target, content in writes:
            current = vault
            for part in target.relative_to(vault).parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise ValueError(f"refusing symlinked destination parent: {current}")
            if target.is_symlink():
                raise ValueError(f"refusing symlinked destination: {target}")
            if target.exists():
                if target.is_dir():
                    raise ValueError(f"destination is not a file: {target}")
                originals[target] = target.read_bytes()

        for target, content in writes:
            missing = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_dirs.append(directory)
            if target in originals:
                temp = target.with_name(target.name + ".provision.tmp")
                if temp.exists() or temp.is_symlink():
                    raise ValueError(f"temporary path already exists: {temp}")
                with temp.open("x", encoding="utf-8") as stream:
                    staged.append(temp)
                    stream.write(content)
                os.replace(temp, target)
                staged.remove(temp)
            else:
                with target.open("x", encoding="utf-8") as stream:
                    created_files.append(target)
                    stream.write(content)
        print(f"Provisioned retrieval helpers (plan {result['planHash']})")
        return 0
    except (OSError, ValueError) as exc:
        for temp in staged:
            try:
                temp.unlink()
            except OSError:
                pass
        for target in reversed(created_files):
            try:
                target.unlink()
            except OSError:
                pass
        # Existing exclude files are only replaced atomically after successful staging.
        for target, content in writes:
            if target in originals and target.read_bytes() != originals[target]:
                try:
                    restore = target.with_name(target.name + ".restore.tmp")
                    with restore.open("xb") as stream:
                        stream.write(originals[target])
                    os.replace(restore, target)
                except OSError:
                    pass
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        parser.exit(1, f"provision-retrieval: apply failed and new files were rolled back: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
