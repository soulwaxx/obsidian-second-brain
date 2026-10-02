#!/usr/bin/env python3
"""Verified generated-index ownership journal for OKF lifecycle sync.

The journal lives in Git's private metadata when available, or disposable
.vault-meta in non-Git vaults. It records exact pre/post byte fingerprints for
middleware writes. A marker is not ownership evidence: dirty indexes must
match a recorded post fingerprint.
"""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

JOURNAL_NAME = 'okf-index-ownership.json'
LOCK_NAME = 'okf-index-ownership.lock'
SCHEMA = 1


def fingerprint(content):
    return None if content is None else hashlib.sha256(content).hexdigest()


def _git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=False, text=True).stdout.strip()


def _check_residual_metadata(root, git_root, require_exclusion=False):
    """Fail closed if non-Git journal files become visible to Git."""
    residual = [Path(root, '.vault-meta', name)
                for name in (JOURNAL_NAME, LOCK_NAME)]
    visible = []
    for path in residual:
        exists = os.path.lexists(path)
        if not exists and not require_exclusion:
            continue
        rel = os.path.relpath(path, git_root)
        tracked = exists and subprocess.run(
            ['git', '-C', git_root, 'ls-files', '--error-unmatch', '--', rel],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
        ignored = subprocess.run(
            ['git', '-C', git_root, 'check-ignore', '-q', '--', rel],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
        if tracked or not ignored:
            visible.append((str(path), tracked))
    if visible:
        details = ', '.join(f'{path} ({"tracked ownership metadata" if tracked else "not ignored"})'
                            for path, tracked in visible)
        raise OSError(
            'unignored ownership metadata blocks Git operation: ' + details +
            '; safely preserve the files, then add an exact .vault-meta ignore rule '
            'to the applicable repository .gitignore and untrack any tracked file '
            'before retrying')
    return require_exclusion or any(os.path.lexists(path) for path in residual)


def _vault_metadata_context(root):
    git_dir = os.path.join(root, '.vault-meta')
    try:
        os.mkdir(git_dir, 0o700)
    except FileExistsError:
        pass
    info = os.stat(git_dir, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError('non-Git ownership metadata path is not a directory')
    return root, git_dir


def _git_context(root):
    root = os.path.realpath(root)
    top = _git(root, 'rev-parse', '--show-toplevel')
    if top:
        if os.path.realpath(top) != root:
            residual = _check_residual_metadata(root, top, require_exclusion=True)
            return _vault_metadata_context(root) if residual else None
        git_dir = _git(root, 'rev-parse', '--absolute-git-dir')
        if not git_dir:
            return None
        git_dir = os.path.realpath(git_dir)
        if not os.path.isdir(git_dir):
            raise OSError('Git metadata directory is unavailable')
        residual = _check_residual_metadata(root, top)
        return _vault_metadata_context(root) if residual else (root, git_dir)
    # Non-Git vaults are supported too. Keep the narrowly scoped ownership
    # journal in disposable vault metadata.
    return _vault_metadata_context(root)


def ensure_git_metadata_safe(root):
    """Validate any retained non-Git metadata before a sync can proceed."""
    _git_context(root)


@contextlib.contextmanager
def locked(root):
    """Serialize journal operations using a no-follow lock in Git metadata."""
    context = _git_context(root)
    if context is None:
        yield None
        return
    _, git_dir = context
    before = os.stat(git_dir, follow_symlinks=False)
    dir_fd = os.open(git_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
                     getattr(os, 'O_NOFOLLOW', 0))
    opened_dir = os.fstat(dir_fd)
    if (not stat.S_ISDIR(opened_dir.st_mode) or
            (before.st_dev, before.st_ino) != (opened_dir.st_dev, opened_dir.st_ino)):
        os.close(dir_fd)
        raise OSError('Git metadata directory changed while being opened')
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        lock_fd = os.open(LOCK_NAME, flags, 0o600, dir_fd=dir_fd)
        try:
            opened_lock = os.fstat(lock_fd)
            if not stat.S_ISREG(opened_lock.st_mode):
                raise OSError('ownership lock is not a regular file')
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            public_lock = os.stat(LOCK_NAME, dir_fd=dir_fd, follow_symlinks=False)
            if (public_lock.st_dev, public_lock.st_ino) != (opened_lock.st_dev, opened_lock.st_ino):
                raise OSError('ownership lock path changed while being acquired')
            yield (context[0], git_dir, dir_fd,
                   (opened_dir.st_dev, opened_dir.st_ino),
                   (opened_lock.st_dev, opened_lock.st_ino))
        finally:
            os.close(lock_fd)
    finally:
        os.close(dir_fd)


def _assert_lock(context):
    if context is None:
        return
    _, git_dir, dir_fd, directory_identity, lock_identity = context
    public_dir = os.stat(git_dir, follow_symlinks=False)
    if (not stat.S_ISDIR(public_dir.st_mode) or
            (public_dir.st_dev, public_dir.st_ino) != directory_identity):
        raise OSError('Git metadata directory changed during operation')
    current = os.stat(LOCK_NAME, dir_fd=dir_fd, follow_symlinks=False)
    if (not stat.S_ISREG(current.st_mode) or
            (current.st_dev, current.st_ino) != lock_identity):
        raise OSError('ownership lock path changed during operation')


def _load_locked(context):
    if context is None:
        return {'schema': SCHEMA, 'entries': {}}
    _assert_lock(context)
    root, git_dir, dir_fd, _, _ = context
    try:
        fd = os.open(JOURNAL_NAME, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0),
                     dir_fd=dir_fd)
    except FileNotFoundError:
        return {'schema': SCHEMA, 'entries': {}}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('ownership journal is not a regular file')
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
            state = json.load(stream)
    finally:
        os.close(fd)
    if (not isinstance(state, dict) or state.get('schema') != SCHEMA or
            not isinstance(state.get('entries'), dict)):
        raise OSError('ownership journal has an invalid schema')
    for key, entry in state['entries'].items():
        if (not isinstance(key, str) or not key.startswith('wiki/') or
                not (key == 'wiki/log.md' or
                     key.endswith('/index.md')) or
                not isinstance(entry, dict) or
                entry.get('path') != key or
                not isinstance(entry.get('after'), (str, type(None))) or
                not isinstance(entry.get('before'), (str, type(None)))):
            raise OSError('ownership journal contains an invalid entry')
    return state


def _store_locked(context, state):
    if context is None:
        return
    _assert_lock(context)
    _, _, dir_fd, _, _ = context
    name = f'.{JOURNAL_NAME}.{uuid.uuid4().hex}.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        data = (json.dumps(state, sort_keys=True, separators=(',', ':')) + '\n').encode()
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            view = view[count:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(name, dir_fd=dir_fd)
        raise
    os.close(fd)
    quarantine = f'.{JOURNAL_NAME}.{uuid.uuid4().hex}.old'
    moved_old = False
    try:
        _assert_lock(context)
        try:
            expected = os.stat(JOURNAL_NAME, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            expected = None
        if expected is not None:
            if not stat.S_ISREG(expected.st_mode):
                raise OSError('ownership journal path is not a regular file')
            os.rename(JOURNAL_NAME, quarantine,
                      src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            moved_old = True
            moved = os.stat(quarantine, dir_fd=dir_fd, follow_symlinks=False)
            if (moved.st_dev, moved.st_ino) != (expected.st_dev, expected.st_ino):
                try:
                    os.link(quarantine, JOURNAL_NAME, src_dir_fd=dir_fd,
                            dst_dir_fd=dir_fd, follow_symlinks=False)
                    os.unlink(quarantine, dir_fd=dir_fd)
                    moved_old = False
                except OSError:
                    raise OSError(f'ownership journal changed; recover prior journal at {quarantine}')
                raise OSError('ownership journal changed during update')
        try:
            os.link(name, JOURNAL_NAME, src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            if moved_old:
                try:
                    os.link(quarantine, JOURNAL_NAME, src_dir_fd=dir_fd,
                            dst_dir_fd=dir_fd, follow_symlinks=False)
                    os.unlink(quarantine, dir_fd=dir_fd)
                    moved_old = False
                except OSError:
                    raise OSError(f'ownership journal arrival conflicts; recover prior journal at {quarantine}') from exc
            raise OSError('ownership journal arrival prevented update') from exc
        os.unlink(name, dir_fd=dir_fd)
        if moved_old:
            os.unlink(quarantine, dir_fd=dir_fd)
            moved_old = False
        os.fsync(dir_fd)
    finally:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def _working_tree_clean(root, rel):
    result = subprocess.run(
        ['git', '-C', root, 'status', '--porcelain=v1', '-z',
         '--untracked-files=all', '--', rel],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0 and result.stdout == b''


def _journal_path(root, rel):
    if (not isinstance(rel, str) or not rel.startswith('wiki/') or
            rel.startswith('/') or any(part in ('', '.', '..') for part in rel.split('/')) or
            not (rel == 'wiki/log.md' or rel.endswith('/index.md'))):
        raise OSError('invalid managed wiki path')
    return Path(root, *rel.split('/'))


def _read_public_file(root, rel, with_identity=False):
    """Read a managed public file relative to pinned no-follow directory fds."""
    target = _journal_path(os.path.realpath(root), rel)
    parts = target.relative_to(os.path.realpath(root)).parts
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    root_path = os.path.realpath(root)
    before_root = os.stat(root_path, follow_symlinks=False)
    directory_fd = os.open(root_path, flags)
    opened_root = os.fstat(directory_fd)
    if (not stat.S_ISDIR(opened_root.st_mode) or
            (before_root.st_dev, before_root.st_ino) != (opened_root.st_dev, opened_root.st_ino)):
        os.close(directory_fd)
        raise OSError('vault root changed while being opened')
    root_identity = (opened_root.st_dev, opened_root.st_ino)

    def public_parent_is_pinned(expected):
        current_fd = os.open(root_path, flags)
        try:
            current_root = os.fstat(current_fd)
            if (current_root.st_dev, current_root.st_ino) != root_identity:
                return False
            for part in parts[:-1]:
                child = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = child
            current = os.fstat(current_fd)
            return (current.st_dev, current.st_ino) == expected
        except OSError:
            return False
        finally:
            os.close(current_fd)

    try:
        for part in parts[:-1]:
            child_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        try:
            before = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pinned_parent = os.fstat(directory_fd)
            expected_parent = (pinned_parent.st_dev, pinned_parent.st_ino)
            if not public_parent_is_pinned(expected_parent):
                raise OSError(f'{rel} public directory path changed while being inspected')
            return (None, None) if with_identity else None
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f'{rel} is not a regular file')
        fd = os.open(parts[-1], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0),
                     dir_fd=directory_fd)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError(f'{rel} changed while being inspected')
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b''.join(chunks)
        finally:
            os.close(fd)
        after = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise OSError(f'{rel} public path changed while being inspected')
        pinned_parent = os.fstat(directory_fd)
        if not public_parent_is_pinned((pinned_parent.st_dev, pinned_parent.st_ino)):
            raise OSError(f'{rel} public directory path changed while being inspected')
        identity = (opened.st_dev, opened.st_ino)
        return (data, identity) if with_identity else data
    finally:
        os.close(directory_fd)


def validate_before_sync(root, rel, current_bytes):
    """Reject dirty pre-sync bytes unless their exact postimage is journaled."""
    _journal_path(os.path.realpath(root), rel)
    with locked(root) as context:
        if context is None:
            if current_bytes is not None:
                raise OSError(f'{rel} has unowned dirty bytes in a non-Git vault; preserve and review before sync')
            return None, True
        state = _load_locked(context)
        entry = state['entries'].get(rel)
        current_hash = fingerprint(current_bytes)
        if current_bytes is None:
            return None, True
        if _working_tree_clean(context[0], rel):
            return current_hash, True
        if entry and entry.get('after') == current_hash:
            return entry.get('before'), False
        raise OSError(f'{rel} has unowned dirty bytes; preserve and review before sync')


def record_created(root, rel, expected_identity, expected_bytes):
    """Attribute only a verified index created by the current bootstrap apply."""
    _journal_path(os.path.realpath(root), rel)
    with locked(root) as context:
        current, identity = _read_public_file(root, rel, with_identity=True)
        if (current != expected_bytes or identity != tuple(expected_identity) or
                b'<!-- Generated by OKF middleware; do not edit. -->\n' not in current):
            raise OSError(f'{rel} no longer matches the index created by this bootstrap apply')
        state = _load_locked(context)
        state['entries'][rel] = {
            'path': rel, 'before': None, 'after': fingerprint(current),
        }
        _store_locked(context, state)


def capture_before(root, rel):
    """Read a managed file without following a public symlink and validate provenance."""
    _journal_path(os.path.realpath(root), rel)
    data = _read_public_file(root, rel)
    before, clean = validate_before_sync(root, rel, data)
    return data, before, clean


def record_sync(root, rel, before_hash, after_bytes, baseline_clean=True):
    """Persist verified attribution immediately after a successful write/removal."""
    if _git_context(root) is None:
        return None
    after_hash = fingerprint(after_bytes)
    with locked(root) as context:
        state = _load_locked(context)
        prior = state['entries'].get(rel)
        if not baseline_clean and prior and prior.get('after') == before_hash:
            origin = prior.get('before')
        else:
            origin = before_hash
        state['entries'][rel] = {'path': rel, 'before': origin, 'after': after_hash}
        _store_locked(context, state)
    return {'path': rel, 'before': origin, 'after': after_hash}


def verified_entries(root, rels=None, dirty_only=False):
    """Return only journal entries whose current public bytes still match."""
    root = os.path.realpath(root)
    selected = set(rels) if rels is not None else None
    results = []
    with locked(root) as context:
        if context is None:
            return results
        state = _load_locked(context)
        for rel, entry in sorted(state['entries'].items()):
            if selected is not None and rel not in selected:
                continue
            try:
                current = _read_public_file(root, rel)
            except FileNotFoundError:
                current = None
            if fingerprint(current) != entry['after']:
                continue
            if dirty_only and _working_tree_clean(root, rel):
                continue
            results.append(dict(entry))
    return results


def forget(root, rels):
    """Drop committed/settled ownership records; never touches worktree bytes."""
    with locked(root) as context:
        if context is None:
            return
        state = _load_locked(context)
        for rel in rels:
            state['entries'].pop(rel, None)
        _store_locked(context, state)


def cli():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('list', 'forget', 'capture', 'record-file'))
    parser.add_argument('root')
    parser.add_argument('paths', nargs='*')
    args = parser.parse_args()
    if args.command == 'list':
        print(json.dumps(verified_entries(args.root, args.paths or None, dirty_only=True)))
    elif args.command == 'forget':
        forget(args.root, args.paths)
    elif args.command == 'capture':
        if len(args.paths) != 1:
            parser.error('capture requires one managed path')
        data, before, clean = capture_before(args.root, args.paths[0])
        print(json.dumps({'before': before, 'clean': clean,
                          'current': fingerprint(data)}))
    else:
        if len(args.paths) != 4:
            parser.error('record-file requires path, clean flag, before hash, and postimage file')
        rel, clean, before, post_path = args.paths
        try:
            baseline_clean = clean == 'true'
            before_hash = None if before == 'null' else before
            fd = os.open(post_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError('postimage is not a regular file')
                chunks = []
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(fd)
            record_sync(args.root, rel, before_hash, b''.join(chunks),
                        baseline_clean=baseline_clean)
        except OSError as exc:
            parser.error(str(exc))


if __name__ == '__main__':
    cli()
