#!/usr/bin/env python3
"""okf deterministic index syncer middleware.

Generates and maintains deterministic index.md files for every directory
under <wiki-root>/wiki/ based on OKF frontmatter metadata (title, description).

Idempotent — uses SHA-256 content hashing to avoid rewriting unchanged files.
Inspired by openwiki's index-middleware.ts.

Usage:
    sync.py <wiki-root> [--dry-run] [--dir <specific-dir>]

Exit codes:
    0 — all indexes synced (or dry-run completed)
    1 — error during sync
"""

import argparse
import hashlib
import os
import re
import sys
from urllib.parse import quote

import yaml

# Files to exclude from listings (hidden from generated indexes). Shared with
# guard.py and validate.py so the three agree on what "generated" means.
from okf_paths import LISTING_SKIP_FILES as SKIP_FILES, in_set


def parse_frontmatter(content):
    """Extract *title* and *description* from an OKF YAML frontmatter block.

    Handles ``---`` delimited blocks. Returns ``(title, description)`` where
    either value may be ``None`` if absent from the frontmatter or if no
    frontmatter block exists.

    YAML parsing preserves folded and multiline values, which are flattened
    only when rendered into a single index entry.
    """
    text = content.lstrip('\ufeff')
    opening = re.match(r'\A---[ \t]*\r?\n', text)
    if opening is None:
        return None, None
    rest = text[opening.end():]
    closing = re.search(r'(?m)^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)', rest)
    if closing is None:
        return None, None
    try:
        data = yaml.safe_load(rest[:closing.start()])
    except yaml.YAMLError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    title = data.get('title')
    description = data.get('description')
    return (title if isinstance(title, str) else None,
            description if isinstance(description, str) else None)


def _md_files(dirpath):
    """Return sorted list of .md filenames for *dirpath*, excluding skips.

    Excludes:
    - Dot-prefixed files (e.g. ``.hidden.md``)
    - Reserved filenames: ``index.md``, ``log.md``, ``_plan.md``,
      ``INSTRUCTIONS.md``
    - Non-.md files entirely.
    """
    files = []
    try:
        for entry in os.scandir(dirpath):
            if entry.name.startswith('.'):
                continue
            if entry.is_symlink() or not entry.is_file() or not entry.name.endswith('.md'):
                continue
            if in_set(entry.name, SKIP_FILES):
                continue
            files.append(entry.name)
    except PermissionError:
        pass
    files.sort()
    return files


def _base_files(dirpath):
    """Return sorted list of ``.base`` filenames (Obsidian Bases views).

    Bases views are directory content worth surfacing for progressive
    disclosure, but they are not OKF concept documents: they carry no ``---``
    frontmatter, so there is no title/description to read and they are listed
    by filename stem. A directory that holds only ``.base`` files (e.g. a
    ``meta/`` folder of dashboards) still gets a generated ``index.md`` so the
    parent directory link never dangles.
    """
    files = []
    try:
        for entry in os.scandir(dirpath):
            if entry.name.startswith('.'):
                continue
            if not entry.is_symlink() and entry.is_file() and entry.name.endswith('.base'):
                files.append(entry.name)
    except PermissionError:
        pass
    files.sort()
    return files


def _subdirs(dirpath):
    """Return sorted list of non-dot-prefixed subdirectory names."""
    dirs = []
    try:
        for entry in os.scandir(dirpath):
            if entry.name.startswith('.'):
                continue
            if not entry.is_symlink() and entry.is_dir():
                dirs.append(entry.name)
    except PermissionError:
        pass
    dirs.sort()
    return dirs


def _index_text(value):
    """Keep untrusted frontmatter on one line and out of link syntax."""
    return (re.sub(r'\s+', ' ', value).strip()
            .replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]'))


def build_index_content(dirpath, is_root):
    """Build the markdown content for an ``index.md`` file.

    Returns the full markdown string, or ``None`` when the directory contains
    no .md files (after exclusions), no .base views, and no subdirectories.
    """
    files = _md_files(dirpath)
    bases = _base_files(dirpath)
    subdirs = _subdirs(dirpath)

    if not files and not bases and not subdirs:
        return None

    lines = []

    if is_root:
        lines.append('---')
        lines.append('okf_version: "0.2"')
        lines.append('---')
        lines.append('')

    # Each present content type is one section. Emitting a section header only
    # when it has entries keeps a base-only directory (empty ``# Files``) clean;
    # a directory with .md files renders byte-identically to before.
    sections = []

    if files:
        section = ['# Files', '']
        for fname in files:
            fpath = os.path.join(dirpath, fname)
            title = None
            desc = None
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as fh:
                    content = fh.read()
                title, desc = parse_frontmatter(content)
            except (OSError, IOError):
                pass

            if not title:
                title = os.path.splitext(fname)[0]

            href = quote(fname, safe='')
            if desc:
                section.append(f'- [{_index_text(title)}]({href}) - {_index_text(desc)}')
            else:
                section.append(f'- [{_index_text(title)}]({href})')
        sections.append(section)

    if bases:
        section = ['# Bases', '']
        for bname in bases:
            href = quote(bname, safe='')
            title = os.path.splitext(bname)[0]
            section.append(f'- [{title}]({href})')
        sections.append(section)

    if subdirs:
        section = ['# Directories', '']
        for dname in subdirs:
            href = quote(dname, safe='') + '/'
            section.append(f'- [{dname}/]({href})')
        sections.append(section)

    for section in sections:
        lines.extend(section)
        lines.append('')

    return '\n'.join(lines)


def _content_changed(path, content):
    """Return ``True`` if *content* differs from the existing file at *path*.

    Uses SHA-256 for comparison. Always returns ``True`` when the file does
    not exist or cannot be read.
    """
    if not os.path.exists(path):
        return True
    try:
        with open(path, 'rb') as f:
            existing = hashlib.sha256(f.read()).hexdigest()
    except (OSError, IOError):
        return True
    new = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return existing != new


def sync_directory(wiki_dir, dirpath, dry_run=False):
    """Sync a single directory under *wiki_dir*.

    Parameters
    ----------
    wiki_dir:
        Absolute path to ``<root>/wiki/``.
    dirpath:
        Relative path from *wiki_dir* (``''`` for the wiki root).
    dry_run:
        When ``True``, print what would happen without writing.

    Returns
    -------
    ``(status, rel, detail)`` where *status* is ``'OK'`` | ``'WRITE'`` |
    ``'SKIP'`` | ``'DRY-RUN'``, *rel* is a human-readable relative path,
    and *detail* is a short description.
    """
    full_path = os.path.join(wiki_dir, dirpath) if dirpath else wiki_dir
    is_root = (dirpath == '')

    if not os.path.isdir(full_path):
        return ('SKIP', os.path.join('wiki', dirpath) if dirpath else 'wiki',
                'not a directory')

    content = build_index_content(full_path, is_root)

    if content is None:
        rel = os.path.join('wiki', dirpath) if dirpath else 'wiki'
        return ('SKIP', rel + '/', 'no content')

    rel = os.path.join('wiki', dirpath) if dirpath else 'wiki'
    index_path = os.path.join(full_path, 'index.md')

    if dry_run:
        file_count = len(_md_files(full_path))
        base_count = len(_base_files(full_path))
        dir_count = len(_subdirs(full_path))
        if os.path.exists(index_path) and not _content_changed(index_path, content):
            return ('OK', rel + '/index.md', 'unchanged')
        return ('DRY-RUN', rel + '/index.md',
                f'{file_count} files, {base_count} bases, '
                f'{dir_count} directories')

    if not _content_changed(index_path, content):
        return ('OK', rel + '/index.md', 'unchanged')

    try:
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except (OSError, IOError) as exc:
        return ('ERROR', rel + '/index.md', str(exc))

    file_count = len(_md_files(full_path))
    base_count = len(_base_files(full_path))
    dir_count = len(_subdirs(full_path))
    return ('WRITE', rel + '/index.md',
            f'{file_count} files, {base_count} bases, '
            f'{dir_count} directories')


def main():
    parser = argparse.ArgumentParser(
        description='Deterministic index.md syncer for OKF wiki vaults')
    parser.add_argument('wiki_root',
                        help='Path to the wiki vault root directory')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print what would be written without writing')
    parser.add_argument('--dir', default=None,
                        help='Only sync this specific directory under wiki/ '
                             '(non-recursive)')

    args = parser.parse_args()

    wiki_root = os.path.abspath(args.wiki_root)
    wiki_dir = os.path.join(wiki_root, 'wiki')

    if not os.path.isdir(wiki_dir) or os.path.islink(wiki_dir):
        print(f'error: {wiki_dir} does not exist or is not a directory',
              file=sys.stderr)
        sys.exit(1)

    if args.dir:
        # Sync a single directory (non-recursive)
        target_raw = args.dir.strip('/')
        target = os.path.join(wiki_dir, target_raw)
        parts = target_raw.split('/')
        if (os.path.isabs(args.dir) or '..' in parts or
                not os.path.realpath(target).startswith(os.path.realpath(wiki_dir) + os.sep) or
                any(os.path.islink(os.path.join(wiki_dir, *parts[:i]))
                    for i in range(1, len(parts) + 1)) or
                not os.path.isdir(target)):
            print(f'error: directory not found — wiki/{target_raw}',
                  file=sys.stderr)
            sys.exit(1)
        status, rel, detail = sync_directory(
            wiki_dir, target_raw, dry_run=args.dry_run)
        print(f'[{status}] {rel} ({detail})')
    else:
        # Walk the entire wiki tree
        results = []
        for root, dirnames, _ in os.walk(wiki_dir):
            # Prune hidden directories during traversal
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.') and
                           not os.path.islink(os.path.join(root, d))]

            rel_dir = os.path.relpath(root, wiki_dir)
            dirpath = '' if rel_dir == '.' else rel_dir

            status, rel, detail = sync_directory(
                wiki_dir, dirpath, dry_run=args.dry_run)
            results.append((status, rel, detail))

        for status, rel, detail in results:
            print(f'[{status}] {rel} ({detail})')

    sys.exit(0)


if __name__ == '__main__':
    main()
