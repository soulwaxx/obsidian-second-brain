#!/usr/bin/env python3
"""Shared reserved-path vocabulary for the OKF middleware.

Single source of truth for "which files under wiki/ are special". guard.py,
validate.py, and sync.py each render this one list so they cannot disagree.

OKF v0.2 reserves exactly two filenames at any level: index.md (directory
listing) and log.md (update history). Everything else is a concept document.
The former toc.md whole-vault section map was a non-conformant extra file and
was retired in the v0.2 migration.

All comparisons casefold. The vault lives on APFS, which is case-insensitive:
wiki/Index.md and wiki/index.md are the same inode, so a case-sensitive
membership test is a one-character bypass of the write guard.
"""

# Produced deterministically by sync.py. The agent must never create or
# overwrite these — a direct write is clobbered on the next sync.
GENERATED_WIKI_FILES = frozenset({"index.md"})

# Written by the lifecycle hook, append-at-top. Agent writes are guard-blocked;
# only the lifecycle hook may create or update the file.
APPEND_ONLY_WIKI_FILES = frozenset({"log.md"})

# Carry no concept frontmatter, so validate.py exempts them from the OKF
# page rules.
RESERVED_WIKI_FILES = GENERATED_WIKI_FILES | APPEND_ONLY_WIKI_FILES

# Hidden from generated index listings: the reserved files above plus
# scratch/authoring artifacts.
LISTING_SKIP_FILES = RESERVED_WIKI_FILES | {"_plan.md", "INSTRUCTIONS.md"}


def in_set(name: str, names) -> bool:
    """Case-insensitive membership test for a filename or wiki-relative path."""
    return name.casefold() in {n.casefold() for n in names}
