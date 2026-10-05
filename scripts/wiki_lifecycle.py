#!/usr/bin/env python3
"""Small durable lifecycle state machine for the single Obsidian wiki writer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import stat
import string


class LifecycleError(RuntimeError):
    pass


def canonical(p: Path) -> Path:
    try:
        return p.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise LifecycleError(f"unsafe path resolution: {p}") from exc


def secure_dir(path: Path, create: bool = False) -> int:
    """Open every directory component without following symlinks."""
    path = path.absolute()
    fd = os.open(path.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def layout(vault: Path, create: bool) -> tuple[Path, Path, Path]:
    vault = vault.resolve(strict=True)
    life = vault / ".vault-meta" / "lifecycle"
    if not create and not life.exists() and not life.is_symlink():
        return life, life / "state.json", life / "finalize.lock"
    try:
        fd = secure_dir(life, create=create)
    except OSError as exc:
        raise LifecycleError(f"unsafe or unavailable lifecycle state directory: {exc}") from exc
    os.close(fd)
    return life, life / "state.json", life / "finalize.lock"


def locked(lock_path: Path):
    class Lock:
        def __enter__(self):
            parent = secure_dir(lock_path.parent, create=False)
            try:
                fd = os.open(lock_path.name, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
            finally:
                os.close(parent)
            self.fd = fd
            fcntl.flock(fd, fcntl.LOCK_EX)
            return self

        def __exit__(self, *_):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
    return Lock()


def load_state(path: Path, vault: Path) -> dict:
    if path.is_symlink():
        raise LifecycleError("lifecycle state is not a regular file")
    if not path.exists():
        return {"version": 1, "vault": str(vault), "pending": {}}
    if not path.is_file():
        raise LifecycleError("lifecycle state is not a regular file")
    parent = secure_dir(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LifecycleError("lifecycle state is not a regular file")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                state = json.load(stream)
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, ValueError) as exc:
        raise LifecycleError(f"cannot read lifecycle state safely: {exc}") from exc
    finally:
        os.close(parent)
    if state.get("version") != 1 or state.get("vault") != str(vault) or not isinstance(state.get("pending"), dict):
        raise LifecycleError("lifecycle state has an incompatible or changed vault boundary")
    return state


def atomic_json(path: Path, value: dict) -> None:
    parent = secure_dir(path.parent)
    name = f".state-{os.getpid()}-{os.urandom(8).hex()}"
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Replacing a swapped symlink replaces the link itself, never its target.
        os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def resolve_page(vault: Path, cwd: Path, raw: str) -> Path | None:
    candidate = canonical(Path(raw) if Path(raw).is_absolute() else cwd / raw)
    # A missing wiki/ must not make ordinary out-of-vault captures fail.
    # Resolve non-strictly so an intended future wiki destination still maps
    # consistently, while unrelated caller-relative paths return immediately.
    wiki = canonical(vault / "wiki")
    try:
        rel = candidate.relative_to(wiki)
    except ValueError:
        return None
    if not candidate.name.lower().endswith(".md") or candidate.name.lower() in {"index.md", "log.md", "_plan.md"}:
        return None
    return candidate


def digest(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LifecycleError(f"cannot read changed page safely {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LifecycleError(f"changed page is not a regular file: {path}")
        hasher = hashlib.sha256()
        with os.fdopen(fd, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError as exc:
        raise LifecycleError(f"cannot read changed page {path}: {exc}") from exc


def capture(args: argparse.Namespace) -> None:
    vault = Path(args.vault).resolve(strict=True)
    page = resolve_page(vault, Path(args.cwd).resolve(), args.path)
    if page is None:
        return
    owner = args.owner or "manual"
    tool = args.tool or "manual"
    if args.owner and not args.tool:
        raise LifecycleError("owned capture requires a tool identity")
    life, state_path, lock_path = layout(vault, True)
    with locked(lock_path):
        state = load_state(state_path, vault)
        for other_key, other_entry in state["pending"].items():
            other_captures = other_entry.get("captures") or {"manual": {"legacy": "active"}}
            if any(candidate != owner for candidate in other_captures):
                raise LifecycleError(
                    "another writer session owns pending wiki work; refusing a concurrent capture "
                    f"without changing it: {other_key}"
                )
        key = str(page)
        entry = state["pending"].get(key)
        if entry is None:
            before = digest(page)
            entry = {"existed": before is not None, "before": before, "changed": False, "captures": {}}
        captures = entry.setdefault("captures", {})
        owner_tools = captures.setdefault(owner, {})
        # Preserve the first pre-write snapshot through edits and repair retries.
        owner_tools.setdefault(tool, "active")
        entry["path"] = str(page)
        state["pending"][key] = entry
        atomic_json(state_path, state)


def validate(path: Path, validator: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        return f"changed page is not a regular file: {path}"
    result = subprocess.run([sys.executable, str(validator), str(path)], text=True, capture_output=True)
    return None if result.returncode == 0 else (result.stdout + result.stderr).strip()


def record(args: argparse.Namespace) -> None:
    vault = Path(args.vault).resolve(strict=True)
    page = resolve_page(vault, Path(args.cwd).resolve(), args.path)
    if page is None:
        return
    owner = args.owner or "manual"
    tool = args.tool or "manual"
    if args.owner and not args.tool:
        raise LifecycleError("owned record requires a tool identity")
    life, state_path, lock_path = layout(vault, True)
    validator = Path(args.validator).resolve(strict=True)
    with locked(lock_path):
        state = load_state(state_path, vault)
        key = str(page)
        entry = state["pending"].get(key)
        if entry is None:
            if owner != "manual":
                raise LifecycleError("postwrite result has no matching owner capture")
            before = None
            entry = {"existed": False, "before": None, "changed": False, "captures": {"manual": {"manual": "active"}}}
        captures = entry.setdefault("captures", {})
        owner_tools = captures.get(owner)
        if owner_tools is None or tool not in owner_tools:
            if owner != "manual":
                raise LifecycleError("postwrite result does not match a captured owner/tool pair")
            owner_tools = captures.setdefault(owner, {tool: "active"})
        owner_tools[tool] = "settled"
        now = digest(page)
        if now == entry.get("before") and not entry.get("publication_started"):
            active = any(status == "active" for tools in captures.values() for status in tools.values())
            if not active:
                state["pending"].pop(key, None)
            else:
                state["pending"][key] = entry
            atomic_json(state_path, state)
            print("clean")
            return
        entry["changed"] = True
        entry["after"] = now
        entry["path"] = str(page)
        state["pending"][key] = entry
        atomic_json(state_path, state)
        issue = validate(page, validator)
        if issue:
            print(f"invalid changed page; batch retained for repair: {page}\n{issue}", file=sys.stderr)
            raise LifecycleError("changed page failed validation")
    print("pending")


def read_log_at(parent_fd: int) -> tuple[str, tuple[int, int, int, int, str] | None]:
    try:
        fd = os.open("log.md", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except FileNotFoundError:
        return "", None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LifecycleError("refusing non-regular wiki/log.md")
        hasher = hashlib.sha256()
        chunks = []
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8"), (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, hasher.hexdigest())
    finally:
        if fd >= 0:
            os.close(fd)


def same_public_parent(parent_path: Path, pinned_fd: int) -> bool:
    try:
        current = secure_dir(parent_path)
    except OSError:
        return False
    try:
        pinned = os.fstat(pinned_fd)
        candidate = os.fstat(current)
        return (pinned.st_dev, pinned.st_ino) == (candidate.st_dev, candidate.st_ino)
    finally:
        os.close(current)


def create_log_temp(parent_fd: int) -> tuple[int, str]:
    """Create a temporary log inode solely in the already-pinned wiki dir."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(32):
        name = f".log-{os.getpid()}-{os.urandom(12).hex()}"
        try:
            return os.open(name, flags, 0o600, dir_fd=parent_fd), name
        except FileExistsError:
            continue
    raise LifecycleError("could not allocate a unique temporary log file")


def cleanup_temp_at(parent_fd: int, name: str | None, identity: tuple[int, int] | None) -> None:
    if not name or not identity:
        return
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) == identity:
            os.unlink(name, dir_fd=parent_fd)
    except OSError:
        # A missing, replaced, or otherwise inaccessible name is not safe to unlink.
        pass


def safe_log_update(log: Path, entries: list[tuple[str, str]], today: str) -> None:
    title = "# Directory Update Log"
    prose = {"Creation": "Added", "Update": "Revised"}
    bullets = []
    from urllib.parse import quote
    for verb, rel in entries:
        href = "/" + quote(rel)
        label = "".join(("\\n" if ch == "\n" else "\\r" if ch == "\r" else "\\" + ch if ch in string.punctuation else ch) for ch in rel)
        bullets.append(f"* **{verb}**: {prose[verb]} [{label}]({href}).")
    parent_path = log.parent
    try:
        parent_fd = secure_dir(parent_path)
    except OSError as exc:
        raise LifecycleError(f"unsafe wiki/log.md parent: {exc}") from exc
    temp_name = None
    temp_identity = None
    temp_fd = None
    try:
        old, preimage = read_log_at(parent_fd)
        lines = old.lstrip("\ufeff").splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and lines[0].strip() == title:
            lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        heading = f"## {today}"
        section_start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
        if section_start is None:
            lines = [heading, *bullets, "", *lines]
        else:
            section_end = next((i for i in range(section_start + 1, len(lines))
                                if lines[i].startswith("## ")), len(lines))
            existing = set(lines[section_start + 1:section_end])
            add = [bullet for bullet in bullets if bullet not in existing]
            if add:
                insert_at = section_start + 1
                while insert_at < section_end and not lines[insert_at].strip():
                    insert_at += 1
                lines[insert_at:insert_at] = add
        content = title + "\n\n" + "\n".join(lines).rstrip("\n") + "\n"

        fd, temp_name = create_log_temp(parent_fd)
        temp_fd = fd
        temp_stat = os.fstat(fd)
        temp_identity = (temp_stat.st_dev, temp_stat.st_ino)
        if not same_public_parent(parent_path, parent_fd):
            raise LifecycleError("wiki directory mapping changed while preparing wiki/log.md")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            temp_fd = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

        # Recheck both the public parent mapping and the original log preimage at
        # the last possible point before replacing within the pinned directory.
        if not same_public_parent(parent_path, parent_fd):
            raise LifecycleError("wiki directory mapping changed before publishing wiki/log.md")
        current, current_identity = read_log_at(parent_fd)
        if current_identity != preimage or current != old:
            raise LifecycleError("wiki/log.md changed concurrently; preserving the newer log")
        temp_info = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
        if (temp_info.st_dev, temp_info.st_ino) != temp_identity:
            raise LifecycleError("temporary log file changed before publication")
        os.replace(temp_name, log.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        if not same_public_parent(parent_path, parent_fd):
            raise LifecycleError("wiki directory mapping changed during log publication")
    except (OSError, UnicodeError) as exc:
        raise LifecycleError(f"cannot safely publish wiki/log.md: {exc}") from exc
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name:
            cleanup_temp_at(parent_fd, temp_name, temp_identity)
        os.close(parent_fd)


def finalize(args: argparse.Namespace) -> None:
    vault = Path(args.vault).resolve(strict=True)
    life, state_path, lock_path = layout(vault, False)
    if not state_path.exists():
        print("clean")
        return
    middleware = Path(args.middleware).resolve(strict=True)
    validator = middleware / "validate.py"
    sync = middleware / "sync.py"
    owner = args.recover_owner or args.owner or "manual"
    recover_active = args.recover_owner is not None or (args.owner is None and args.recover_owner is None)
    with locked(lock_path):
        state = load_state(state_path, vault)
        all_pending = state["pending"]
        if not all_pending:
            print("clean")
            return

        def captures_for(entry: dict) -> dict[str, dict[str, str]]:
            captures = entry.get("captures")
            # Pre-ownership ledger entries are recoverable only through the
            # explicit manual CLI boundary, never by a frontend session.
            return captures if isinstance(captures, dict) and captures else {"manual": {"legacy": "active"}}

        foreign = sorted({capture_owner for entry in all_pending.values()
                          for capture_owner in captures_for(entry) if capture_owner != owner})
        if foreign:
            raise LifecycleError(
                "pending wiki work belongs to another writer session; it was not changed; "
                f"owner(s): {', '.join(foreign)}"
            )
        owned = {key: entry for key, entry in all_pending.items() if owner in captures_for(entry)}
        if not owned:
            print("clean")
            return

        recovered = False
        for entry in owned.values():
            owner_tools = captures_for(entry).get(owner, {})
            active = [tool for tool, status in owner_tools.items() if status == "active"]
            if active and not recover_active:
                raise LifecycleError(
                    "writer has an active prewrite capture; dependent navigation is blocked until its tool result or explicit owner recovery"
                )
            for tool in active:
                owner_tools[tool] = "settled"
                recovered = True
            entry["captures"] = captures_for(entry)
        if recovered:
            atomic_json(state_path, state)

        reconciled = False
        for key, entry in list(owned.items()):
            path = Path(key)
            current = digest(path)
            if current == entry.get("before") and not entry.get("publication_started"):
                state["pending"].pop(key, None)
                reconciled = True
                continue
            if current != entry.get("after") or not entry.get("changed"):
                entry["changed"] = True
                entry["after"] = current
                entry["path"] = str(path)
                state["pending"][key] = entry
                reconciled = True
        if reconciled:
            atomic_json(state_path, state)
        pending = {k: v for k, v in owned.items() if v.get("changed")}
        if not pending:
            print("clean")
            return
        problems = []
        for raw, entry in pending.items():
            path = Path(raw)
            issue = validate(path, validator)
            if issue:
                problems.append(f"{path}: {issue}")
        if problems:
            raise LifecycleError("pending wiki batch is invalid; changes retained:\n" + "\n".join(problems))
        for entry in pending.values():
            entry["publication_started"] = True
        atomic_json(state_path, state)
        result = subprocess.run([sys.executable, str(sync), str(vault), "--json"], text=True, capture_output=True)
        if result.returncode:
            raise LifecycleError("wiki index synchronization failed; pending batch retained:\n" + (result.stdout + result.stderr))
        today = datetime.now(timezone.utc).date().isoformat()
        log_entries: dict[str, list[tuple[str, str]]] = {}
        log_date_persisted = False
        for raw, entry in sorted(pending.items()):
            page = Path(raw)
            try:
                rel = page.relative_to(vault / "wiki").as_posix()
            except ValueError as exc:
                raise LifecycleError("pending path escaped captured vault boundary") from exc
            current = digest(page)
            if current is None:
                if entry.get("existed"):
                    log_item = ("Update", rel)
                else:
                    log_item = None
            elif current != entry.get("before"):
                log_item = ("Update" if entry.get("existed") else "Creation", rel)
            else:
                log_item = None
            if log_item:
                if not entry.get("log_date"):
                    entry["log_date"] = today
                    log_date_persisted = True
                log_entries.setdefault(entry["log_date"], []).append(log_item)
        if log_date_persisted:
            atomic_json(state_path, state)
        for log_date, entries in sorted(log_entries.items()):
            safe_log_update(vault / "wiki" / "log.md", entries, log_date)
        # The log is lifecycle-owned and excluded from agent writes; sync a second
        # time so generated indexes account for the finalized log contents.
        result = subprocess.run([sys.executable, str(sync), str(vault), "--json"], text=True, capture_output=True)
        if result.returncode:
            raise LifecycleError("index sync after log update failed; batch retained:\n" + (result.stdout + result.stderr))
        for key in pending:
            state["pending"].pop(key, None)
        atomic_json(state_path, state)
    if args.retrieval_script:
        retrieval = Path(args.retrieval_script).resolve(strict=True)
        result = subprocess.run([sys.executable, str(retrieval), "build", "--vault", str(vault)], text=True, capture_output=True)
        if result.returncode:
            print("obsidian lifecycle: warning: retrieval index refresh failed; wiki index navigation remains available", file=sys.stderr)
    print("synced")


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


def exclusion_diagnostic(vault: Path) -> list[str]:
    try:
        selected = vault.resolve(strict=True)
    except OSError:
        return []
    try:
        probe = subprocess.run(["git", "-C", str(selected), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if probe.returncode:
            return []
        repo = Path(probe.stdout.strip()).resolve(strict=True)
        try:
            vault_rel = selected.relative_to(repo).as_posix()
        except ValueError:
            return []
        if vault_rel == ".":
            vault_rel = ""
        warnings = []
        scopes = (
            (
                "lifecycle",
                ".vault-meta/lifecycle/",
                (".vault-meta/lifecycle/state.json", ".vault-meta/lifecycle/finalize.lock", ".vault-meta/lifecycle/.state-diagnostic"),
            ),
            (
                "retrieval",
                ".vault-meta/retrieval/",
                (".vault-meta/retrieval/bm25.json", ".vault-meta/retrieval/bm25.json.tmp"),
            ),
        )
        for label, suffix, outputs in scopes:
            scope = "/".join(part for part in (vault_rel, suffix) if part)
            probes = ["/".join(part for part in (vault_rel, output) if part) for output in outputs]
            visible = [] if _git_directory_ignored(repo, scope) else [scope]
            for probe_path in probes:
                result = subprocess.run(
                    ["git", "-C", str(repo), "check-ignore", "--no-index", "-q", "--", probe_path]
                )
                if result.returncode != 0:
                    visible.append(probe_path)
            if visible:
                warnings.append(
                    f"obsidian lifecycle: setup diagnostic: {label} derived-state scope is not effectively Git-ignored "
                    f"({', '.join(visible)}); add a reviewed directory-scope ignore rule (runtime will not edit ignores)"
                )
            tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "--full-name", "-z", "--", scope], capture_output=True)
            tracked_paths = [p for p in tracked.stdout.split(b"\0") if p]
            if tracked_paths:
                warnings.append(f"obsidian lifecycle: setup diagnostic: {label} contains tracked Git state; review/untrack it explicitly (runtime will not mutate Git)")
        return warnings
    except FileNotFoundError:
        return []


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "record"):
        p = sub.add_parser(name)
        p.add_argument("--vault", required=True)
        p.add_argument("--cwd", required=True)
        p.add_argument("--path", required=True)
        p.add_argument("--owner")
        p.add_argument("--tool")
        if name == "record":
            p.add_argument("--validator", required=True)
    p = sub.add_parser("finalize")
    p.add_argument("--vault", required=True)
    p.add_argument("--middleware", required=True)
    p.add_argument("--retrieval-script")
    p.add_argument("--owner", help="settle only completed work owned by this session")
    p.add_argument("--recover-owner", help="explicitly recover this ended writer session's missing tool results")
    p = sub.add_parser("diagnose")
    p.add_argument("--vault", required=True)
    args = parser.parse_args()
    try:
        if args.command == "capture": capture(args)
        elif args.command == "record": record(args)
        elif args.command == "finalize": finalize(args)
        else:
            for message in exclusion_diagnostic(Path(args.vault)):
                print(message, file=sys.stderr)
        return 0
    except (LifecycleError, OSError, subprocess.SubprocessError) as exc:
        print(f"obsidian lifecycle: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
