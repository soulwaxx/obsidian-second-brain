#!/usr/bin/env python3
"""
Write-guard middleware for Obsidian wiki vaults.

Ensures that write operations target only allowed paths under
<wiki-root>/wiki/, rejecting paths that escape the wiki directory
or point to protected system directories.

Usage:
    guard.py <wiki-root> <path-to-write>

Exit codes:
    0: write allowed (path is under wiki-root/wiki/)
    1: write blocked (path outside wiki/)
    2: usage error (wrong number of arguments)
"""

import sys
from pathlib import Path

from okf_paths import RESERVED_WIKI_FILES, in_set

# Directories that are always blocked, relative to wiki root
BLOCKED_DIRS = {
    ".raw",
    ".obsidian",
    ".vault-meta",
    ".git",
}


def _path_contains_traversal(p: Path) -> bool:
    """Check if a path contains '..' traversal components."""
    return ".." in p.parts


def check_write(wiki_root: str, target_path: str) -> tuple[bool, str]:
    """Check if a write to target_path is allowed under wiki_root.

    Returns (allowed, message).
    """
    wiki_root_resolved = Path(wiki_root).resolve()
    target_p = Path(target_path)
    # Resolve relative paths against wiki_root, not cwd
    if not target_p.is_absolute():
        target_resolved = (wiki_root_resolved / target_p).resolve()
    else:
        target_resolved = target_p.resolve()

    # Check for '..' traversal in the unresolved path
    target_raw = Path(target_path)
    if _path_contains_traversal(target_raw):
        return (
            False,
            f"BLOCKED: {target_path} — path contains '..' traversal",
        )

    # Target must be under <wiki-root>/wiki/
    wiki_dir = wiki_root_resolved / "wiki"
    try:
        rel_to_wiki = target_resolved.relative_to(wiki_dir)
    except ValueError:
        return (
            False,
            f"BLOCKED: {target_path} — path is outside wiki/ directory "
            f"(resolved to {target_resolved}, expected under {wiki_dir})",
        )

    # index.md is generated; log.md is owned by the lifecycle hook.
    if in_set(rel_to_wiki.name, RESERVED_WIKI_FILES):
        return (
            False,
            f"BLOCKED: {target_path} — reserved wiki file "
            f"'{rel_to_wiki.as_posix()}'",
        )

    # Even symlinks resolving back inside wiki/ are not writable: the indexer
    # and migration skip them, and the destination could change after checking.
    lexical = target_p if target_p.is_absolute() else wiki_root_resolved / target_p
    try:
        parts = lexical.relative_to(wiki_root_resolved).parts
    except ValueError:
        parts = ()
    current = wiki_root_resolved
    for part in parts:
        current /= part
        if current.is_symlink():
            return False, f"BLOCKED: {target_path} — symlink in wiki path"

    # Reject protected directories
    for blocked in BLOCKED_DIRS:
        try:
            target_resolved.relative_to(wiki_root_resolved / blocked)
            return (
                False,
                f"BLOCKED: {target_path} — path points to blocked "
                f"directory '{blocked}'",
            )
        except ValueError:
            pass

    # Also check if any component of the relative path is a blocked dir
    try:
        rel = target_resolved.relative_to(wiki_root_resolved)
        for part in rel.parts:
            if in_set(part, BLOCKED_DIRS):
                return (
                    False,
                    f"BLOCKED: {target_path} — path contains blocked "
                    f"directory '{part}'",
                )
    except ValueError:
        pass

    return True, f"ALLOWED: {target_path}"


def check_agent_write(wiki_root: str, target_path: str) -> tuple[bool, str]:
    """Leave non-wiki writes alone; apply the strict guard to wiki writes."""
    root = Path(wiki_root).resolve()
    raw = Path(target_path)
    lexical = raw if raw.is_absolute() else root / raw
    wiki = root / "wiki"
    if lexical.is_relative_to(wiki):
        return check_write(wiki_root, target_path)
    if lexical.resolve().is_relative_to(wiki):
        # An OS-level prefix alias (macOS /var -> /private/var) is safe;
        # preserve the path *below* wiki/ so check_write still sees symlinks.
        for ancestor in (lexical, *lexical.parents):
            if ancestor.name == "wiki" and ancestor.parent.resolve() == root:
                return check_write(wiki_root, str(wiki / lexical.relative_to(ancestor)))
        return False, f"BLOCKED: {target_path} — symlink alias into wiki/"
    return True, f"ALLOWED: {target_path} — outside wiki/"


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--if-wiki":
        allowed, message = check_agent_write(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3:
        allowed, message = check_write(sys.argv[1], sys.argv[2])
    else:
        print(
            f"usage: {sys.argv[0]} [--if-wiki] <wiki-root> <path-to-write>",
            file=sys.stderr,
        )
        return 2

    if allowed:
        print(message)
        return 0
    else:
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
