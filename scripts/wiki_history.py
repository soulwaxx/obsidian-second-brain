#!/usr/bin/env python3
"""Review and migrate only destination spans in the root lifecycle history log."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from module_loading import load_source

LIFE = load_source("wiki_history_lifecycle", str(Path(__file__).with_name("wiki_lifecycle.py")))
COMPAT = load_source("wiki_history_compat", str(Path(__file__).with_name("obsidian_compat.py")))
CONFIG = load_source("wiki_history_config", str(Path(__file__).with_name("config_contract.py")))


class HistoryError(RuntimeError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def selected_config(path: str, vault: Path) -> dict:
    config = Path(path).expanduser().absolute()
    value, error = CONFIG.load_config(config)
    if error:
        raise HistoryError("invalid selected config: " + error)
    if value is not None and value.get("vaultPath"):
        configured = Path(value["vaultPath"]).expanduser()
        if not configured.is_absolute():
            configured = config.parent / configured
        if configured.resolve(strict=True) != vault:
            raise HistoryError("selected config does not identify this vault")
    try:
        content = config.read_bytes()
    except FileNotFoundError:
        content = None
    except OSError as exc:
        raise HistoryError(f"cannot bind selected config: {exc}") from exc
    return {"path": str(config), "sha256": sha(content) if content is not None else None}


def read_bytes(parent: int, name: str = "log.md") -> bytes:
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HistoryError("history is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def publish_log(parent: int, temp: str, before: bytes, approval: str) -> None:
    """Publish without replacing arrivals; keep the displaced inode recoverable."""
    quarantine = f".history-log-{approval}.recovery"
    check_case_collision_at(parent, quarantine)
    try:
        os.link("log.md", quarantine, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
    except FileExistsError:
        # Resume only the verified hard-link intermediate state. A distinct
        # recovery artifact may be owner data even when its bytes happen to match.
        public = os.stat("log.md", dir_fd=parent, follow_symlinks=False)
        retained = os.stat(quarantine, dir_fd=parent, follow_symlinks=False)
        if (public.st_dev, public.st_ino) != (retained.st_dev, retained.st_ino):
            raise HistoryError(f"retained history requires review at wiki/{quarantine}")
    except OSError as exc:
        raise HistoryError(f"cannot retain displaced history at wiki/{quarantine}: {exc}") from exc

    try:
        if read_bytes(parent, quarantine) != before:
            raise HistoryError("root history changed during publication")
        public = os.stat("log.md", dir_fd=parent, follow_symlinks=False)
        retained = os.stat(quarantine, dir_fd=parent, follow_symlinks=False)
        if (public.st_dev, public.st_ino) != (retained.st_dev, retained.st_ino):
            raise HistoryError("root history changed during publication")
        displaced = f".history-log-{approval}.displaced"
        check_case_collision_at(parent, displaced)
        try:
            os.stat(displaced, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            # A previous attempt may have retained this name. Never unlink it;
            # select a fresh exclusive destination for this attempt instead.
            displaced = f".history-log-{approval}.displaced-{os.urandom(6).hex()}"
            check_case_collision_at(parent, displaced)
        rename_noreplace(parent, "log.md", displaced)
        os.fsync(parent)
        displaced_stat = os.stat(displaced, dir_fd=parent, follow_symlinks=False)
        retained = os.stat(quarantine, dir_fd=parent, follow_symlinks=False)
        if (read_bytes(parent, displaced) != before
                or (displaced_stat.st_dev, displaced_stat.st_ino) != (retained.st_dev, retained.st_ino)):
            conflict = f".history-log-{approval}.conflict"
            check_case_collision_at(parent, conflict)
            os.link(displaced, conflict, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.link(displaced, "log.md", src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
            raise HistoryError(f"concurrent history preserved at wiki/{conflict} and wiki/{displaced}")
        # Keep the displaced inode as a durable artifact. POSIX has no
        # identity-conditional unlink; deleting this pathname could destroy a
        # concurrent substitution after any preceding stat.
        os.fsync(parent)
        os.link(temp, "log.md", src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
    except (OSError, HistoryError) as exc:
        try:
            os.link(quarantine, "log.md", src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
        except OSError:
            raise HistoryError(f"history publication conflicted; recover prior history at wiki/{quarantine}") from exc
        raise HistoryError("history publication conflicted; preserved concurrent bytes") from exc
    if read_bytes(parent, quarantine) != before:
        raise HistoryError(f"history changed during publication; retained bytes at wiki/{quarantine}")


def secure_wiki(vault: Path) -> int:
    root_fd = LIFE.secure_dir(vault)
    try:
        names = os.listdir(root_fd)
        matches = [name for name in names if name.casefold() == "wiki"]
        if matches != ["wiki"]:
            raise HistoryError("vault wiki directory is missing or case-colliding")
    finally:
        os.close(root_fd)
    return LIFE.secure_dir(vault / "wiki")


def check_root_log_name(parent: int) -> None:
    names = os.listdir(parent)
    if "log.md" not in names or any(n.casefold() == "log.md" and n != "log.md" for n in names):
        raise HistoryError("root wiki/log.md missing or case-colliding")


def plan(vault: Path, config_id: dict) -> dict:
    try:
        parent = secure_wiki(vault)
        check_root_log_name(parent)
        before = read_bytes(parent)
    except (OSError, UnicodeError, LIFE.LifecycleError) as exc:
        raise HistoryError(f"cannot safely inspect root history: {exc}") from exc
    finally:
        if 'parent' in locals():
            os.close(parent)
    report = COMPAT.compatibility_report(vault)
    text = before.decode("utf-8")
    changes = []
    for link in report["links"]:
        if link.get("source") != "wiki/log.md" or link.get("kind") != "lifecycle-history":
            continue
        proposal = link.get("proposal")
        if proposal is None:
            if link.get("classification"):
                raise HistoryError("root history contains an unsupported or unresolved destination change")
            continue
        start, end = proposal["span"]
        raw = text[start:end]
        intended = link.get("intended_path")
        if (raw != link["destination"] or not isinstance(intended, str) or not intended.startswith("wiki/")
                or Path(intended).name.casefold() in {"index.md", "log.md"}):
            raise HistoryError("compatibility source span is unsupported or does not resolve to an existing wiki target")
        if raw == proposal["replacement"]:
            continue
        changes.append({"span": [start, end], "from": raw, "to": proposal["replacement"], "target": link["intended_path"]})
    changes.sort(key=lambda x: x["span"][0])
    if any(a["span"][1] > b["span"][0] for a, b in zip(changes, changes[1:])):
        raise HistoryError("overlapping destination spans")
    after_text = text
    for change in reversed(changes):
        start, end = change["span"]
        after_text = after_text[:start] + change["to"] + after_text[end:]
    after = after_text.encode("utf-8")
    digest_basis = {"version": 1, "vault": str(vault), "config": config_id,
                    "beforeHash": sha(before), "afterHash": sha(after), "replacements": changes}
    return {**digest_basis, "operations": changes, "planHash": sha(canonical(digest_basis)), "_before": before, "_after": after}


def rename_noreplace(parent: int, source: str, destination: str) -> None:
    """Atomically move a name only if the destination is absent; fail closed elsewhere."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        operation = getattr(libc, "renameat2", None)
        if operation is None:
            raise HistoryError("atomic no-replace rename is unavailable on this Linux platform")
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(parent, os.fsencode(source), parent, os.fsencode(destination), 1)
    elif sys.platform == "darwin":
        operation = getattr(libc, "renameatx_np", None)
        if operation is None:
            raise HistoryError("atomic no-replace rename is unavailable on this macOS platform")
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(parent, os.fsencode(source), parent, os.fsencode(destination), 0x00000004)
    else:
        raise HistoryError(f"atomic no-replace rename is unsupported on {sys.platform}")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def check_case_collision_at(parent: int, name: str) -> None:
    matches = [item for item in os.listdir(parent) if item.casefold() == name.casefold()]
    if matches and matches != [name]:
        raise HistoryError(f"case-colliding lifecycle path: {name}")


def journal_path(vault: Path, plan_hash: str) -> Path:
    if not isinstance(plan_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_hash):
        raise HistoryError("plan hash must be exactly 64 lowercase hexadecimal characters")
    return vault / ".vault-meta/lifecycle" / f"history-migration-{plan_hash}.json"


def config_still_matches(config_id: dict, vault: Path) -> None:
    if selected_config(config_id["path"], vault) != config_id:
        raise HistoryError("selected config changed since preview; refusing migration")


def load_journal(path: Path, vault: Path, config_id: dict, approval: str, parent: int) -> tuple[dict, bytes, bytes]:
    fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HistoryError("migration journal is not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            data = json.load(stream)
    finally:
        if fd >= 0:
            os.close(fd)
    fields = {"version", "vault", "config", "planHash", "beforeHash", "afterHash", "beforeHex", "afterHex", "replacements"}
    if not isinstance(data, dict) or set(data) != fields:
        raise HistoryError("migration journal is malformed")
    try:
        before, after = bytes.fromhex(data["beforeHex"]), bytes.fromhex(data["afterHex"])
        basis = {"version": 1, "vault": data["vault"], "config": data["config"],
                 "beforeHash": data["beforeHash"], "afterHash": data["afterHash"], "replacements": data["replacements"]}
        if (data["version"] != 1 or data["vault"] != str(vault) or data["config"] != config_id
                or data["planHash"] != approval or sha(before) != data["beforeHash"]
                or sha(after) != data["afterHash"] or sha(canonical(basis)) != approval):
            raise ValueError("boundary or hashes do not verify")
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoryError(f"migration journal is invalid: {exc}") from exc
    return data, before, after


def create_journal(path: Path, data: dict, parent: int) -> None:
    temp = f".history-{os.getpid()}-{os.urandom(8).hex()}"
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        # Hard-link creation is exclusive: an existing file or symlink is never replaced.
        os.link(temp, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
    finally:
        try: os.unlink(temp, dir_fd=parent)
        except FileNotFoundError: pass


def apply(vault: Path, config_id: dict, approval: str, recovery: bool = False) -> dict:
    journal = journal_path(vault, approval)
    for warning in LIFE.exclusion_diagnostic(vault):
        if "lifecycle derived-state scope" in warning or "lifecycle contains tracked Git state" in warning:
            raise HistoryError("migration journal state is not safely Git-excluded: " + warning)
    life, state_path, lock_path = LIFE.layout(vault, True)
    life_fd = LIFE.secure_dir(life)
    try:
        with LIFE.locked(lock_path):
            if not LIFE.same_public_parent(life, life_fd):
                raise HistoryError("lifecycle directory mapping changed before migration")
            check_case_collision_at(life_fd, state_path.name)
            check_case_collision_at(life_fd, journal.name)
            state = LIFE.load_state(state_path, vault)
            if not LIFE.same_public_parent(life, life_fd):
                raise HistoryError("lifecycle directory mapping changed while reading state")
            if state.get("pending"):
                raise HistoryError("pending lifecycle writer state; refusing history migration")
            config_still_matches(config_id, vault)
            if recovery:
                data, before, after = load_journal(journal, vault, config_id, approval, life_fd)
                expected = data["beforeHash"]
            else:
                try:
                    data, before, after = load_journal(journal, vault, config_id, approval, life_fd)
                    expected = data["beforeHash"]
                except FileNotFoundError:
                    current = plan(vault, config_id)
                    if current["planHash"] != approval:
                        raise HistoryError("approval hash does not match current migration plan")
                    before, after, expected = current["_before"], current["_after"], current["beforeHash"]
                    data = {"version": 1, "vault": str(vault), "config": config_id, "planHash": approval,
                            "beforeHash": expected, "afterHash": sha(after), "beforeHex": before.hex(), "afterHex": after.hex(),
                            "replacements": current["operations"]}
                    if not LIFE.same_public_parent(life, life_fd):
                        raise HistoryError("lifecycle directory mapping changed before journal creation")
                    create_journal(journal, data, life_fd)
                    if not LIFE.same_public_parent(life, life_fd):
                        raise HistoryError("lifecycle directory changed during journal creation")
            parent = secure_wiki(vault)
            try:
                check_case_collision_at(parent, "log.md")
                quarantine = f".history-log-{approval}.recovery"
                check_case_collision_at(parent, quarantine)
                try:
                    os.stat("log.md", dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    # A stopped process may have displaced the log but not yet
                    # published. Restore only the journal's exact preimage and
                    # only into a vacant name before resuming the approved plan.
                    try:
                        retained = read_bytes(parent, quarantine)
                    except FileNotFoundError:
                        pass
                    else:
                        if retained != before:
                            raise HistoryError(f"retained history changed; recover prior history at wiki/{quarantine}")
                        os.link(quarantine, "log.md", src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                        os.fsync(parent)
                check_root_log_name(parent)
                current_bytes = read_bytes(parent)
                try:
                    retained_bytes = read_bytes(parent, quarantine)
                except FileNotFoundError:
                    retained_bytes = None
                if sha(current_bytes) == sha(after):
                    if (not LIFE.same_public_parent(life, life_fd)
                            or not LIFE.same_public_parent(vault / "wiki", parent)):
                        raise HistoryError("public lifecycle/wiki directory changed during migration retry")
                    return {"planHash": approval, "applied": False, "alreadyApplied": True}
                if current_bytes != before or sha(current_bytes) != expected:
                    raise HistoryError("root history changed since approval; preserving current bytes")
                if retained_bytes is not None:
                    public_stat = os.stat("log.md", dir_fd=parent, follow_symlinks=False)
                    retained_stat = os.stat(quarantine, dir_fd=parent, follow_symlinks=False)
                    if ((public_stat.st_dev, public_stat.st_ino) != (retained_stat.st_dev, retained_stat.st_ino)
                            or retained_bytes != before):
                        raise HistoryError(f"retained history requires review at wiki/{quarantine}")
                fd, temp = LIFE.create_log_temp(parent)
                info = os.fstat(fd)
                temp_identity = (info.st_dev, info.st_ino)
                try:
                    with os.fdopen(fd, "wb", closefd=False) as stream:
                        stream.write(after); stream.flush(); os.fsync(stream.fileno())
                    config_still_matches(config_id, vault)
                    if (not LIFE.same_public_parent(life, life_fd)
                            or not LIFE.same_public_parent(vault / "wiki", parent)
                            or read_bytes(parent) != before):
                        raise HistoryError("root history or lifecycle/wiki directory changed before publication")
                    temp_info = os.stat(temp, dir_fd=parent, follow_symlinks=False)
                    if (not stat.S_ISREG(temp_info.st_mode)
                            or (temp_info.st_dev, temp_info.st_ino) != temp_identity):
                        raise HistoryError("temporary history file changed before publication")
                    publish_log(parent, temp, before, approval)
                    if (not LIFE.same_public_parent(life, life_fd)
                            or not LIFE.same_public_parent(vault / "wiki", parent)):
                        raise HistoryError("public lifecycle/wiki directory changed during publication; migration is recoverable")
                finally:
                    try:
                        LIFE.cleanup_temp_at(parent, temp, temp_identity)
                    finally:
                        os.close(fd)
            finally:
                os.close(parent)
        return {"planHash": approval, "applied": True, "alreadyApplied": False}
    finally:
        os.close(life_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--recover", metavar="PLAN_HASH")
    args = parser.parse_args()
    try:
        vault = Path(args.vault).expanduser().resolve(strict=True)
        if args.apply and args.recover or args.recover and args.confirm or args.apply and not args.confirm or not args.apply and args.confirm:
            raise HistoryError("choose preview, --apply --confirm HASH, or --recover HASH")
        config_id = selected_config(args.config, vault)
        if args.recover:
            result = apply(vault, config_id, args.recover, recovery=True)
        elif args.apply:
            result = apply(vault, config_id, args.confirm)
        else:
            item = plan(vault, config_id)
            result = {k: v for k, v in item.items() if not k.startswith("_")}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError, HistoryError, LIFE.LifecycleError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
