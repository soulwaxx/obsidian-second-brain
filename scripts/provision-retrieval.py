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


def open_parent(vault, target, created_dirs):
    """Open a destination parent by descriptor, refusing symlinks and swaps."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(vault, flags)
    current = vault
    try:
        for part in target.relative_to(vault).parts[:-1]:
            current = current / part
            try:
                os.mkdir(part, dir_fd=fd)
                created_dirs.append((os.dup(fd), part))
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        actual, opened = os.stat(current, follow_symlinks=False), os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"destination parent changed during provisioning: {current}")
        return fd, current
    except BaseException:
        os.close(fd)
        raise


def read_at(parent_fd, name):
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        stat = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read()
        return content, (stat.st_dev, stat.st_ino)
    finally:
        os.close(fd)


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
    replaced = []
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
                info = target.stat(follow_symlinks=False)
                originals[target] = (target.read_bytes(), (info.st_dev, info.st_ino))

        for target, content in writes:
            parent_fd, parent_path = open_parent(vault, target, created_dirs)
            name = target.name
            try:
                if target in originals:
                    previous_bytes, previous_identity = originals[target]
                    try:
                        current_bytes, current_identity = read_at(parent_fd, name)
                    except OSError as exc:
                        raise ValueError(f"destination changed since preview: {target}") from exc
                    if current_identity != previous_identity or current_bytes != previous_bytes:
                        raise ValueError(f"destination changed since preview: {target}")
                    temp_name = name + ".provision.tmp"
                    temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666, dir_fd=parent_fd)
                    temp_stat = os.fstat(temp_fd)
                    staged.append((os.dup(parent_fd), temp_name, temp_stat.st_dev, temp_stat.st_ino))
                    with os.fdopen(temp_fd, "w", encoding="utf-8") as stream:
                        stream.write(content)
                    actual, opened = os.stat(parent_path, follow_symlinks=False), os.fstat(parent_fd)
                    if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
                        raise ValueError(f"destination parent changed during provisioning: {parent_path}")
                    os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    installed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    replaced.append((os.dup(parent_fd), name, installed.st_dev, installed.st_ino, content.encode("utf-8"), previous_bytes))
                    staged_fd, _, _, _ = staged.pop()
                    os.close(staged_fd)
                else:
                    file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666, dir_fd=parent_fd)
                    file_stat = os.fstat(file_fd)
                    created_files.append((os.dup(parent_fd), name, file_stat.st_dev, file_stat.st_ino))
                    with os.fdopen(file_fd, "w", encoding="utf-8") as stream:
                        stream.write(content)
                actual, opened = os.stat(parent_path, follow_symlinks=False), os.fstat(parent_fd)
                if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError(f"destination parent changed during provisioning: {parent_path}")
            finally:
                os.close(parent_fd)
        for parent_fd, *_ in created_files + created_dirs:
            os.close(parent_fd)
        for parent_fd, *_ in replaced:
            os.close(parent_fd)
        print(f"Provisioned retrieval helpers (plan {result['planHash']})")
        return 0
    except (OSError, ValueError) as exc:
        for parent_fd, temp_name, device, inode in staged:
            try:
                current = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (device, inode):
                    os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
            os.close(parent_fd)
        for parent_fd, name, device, inode in reversed(created_files):
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (device, inode):
                    os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
            os.close(parent_fd)
        # Restore only our replacement, never a concurrent writer's file.
        for parent_fd, name, device, inode, installed_bytes, original_bytes in reversed(replaced):
            try:
                current_bytes, current_identity = read_at(parent_fd, name)
                if current_identity == (device, inode) and current_bytes == installed_bytes:
                    restore_name = name + ".restore.tmp"
                    restore_fd = os.open(restore_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
                    with os.fdopen(restore_fd, "wb") as stream:
                        stream.write(original_bytes)
                    os.replace(restore_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except OSError:
                pass
            os.close(parent_fd)
        for parent_fd, name in reversed(created_dirs):
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            os.close(parent_fd)
        parser.exit(1, f"provision-retrieval: apply failed and new files were rolled back: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
