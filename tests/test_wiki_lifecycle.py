#!/usr/bin/env python3
"""Disposable regression suite for the durable wiki batch/log state machine."""

import json
import os
from datetime import datetime as RealDateTime
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/wiki_lifecycle.py"
MIDDLEWARE = ROOT / "skills/wiki/scripts/okf_mw"
BM25 = ROOT / "scripts/bm25-index.py"
RETRIEVE = ROOT / "scripts/retrieve.py"
sys.path.insert(0, str(ROOT / "scripts"))
import wiki_lifecycle


def run(*args: object, ok: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run([sys.executable, *map(str, args)], cwd=cwd, text=True, capture_output=True)
    if ok and result.returncode:
        raise AssertionError(f"command failed: {args}\n{result.stdout}\n{result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly passed: {args}")
    return result


def page(title: str, body: str | None = None) -> str:
    body = body or title
    return f"---\ntype: note\ntitle: {title}\nlast_updated: 2026-01-01\n---\n# {title}\n\n{body}\n"


def setup(root: Path, ignored: bool = True) -> tuple[Path, Path]:
    vault = root / "vault"
    caller = root / "caller"
    (vault / "wiki").mkdir(parents=True)
    caller.mkdir()
    (vault / "wiki/base.md").write_text(page("Base"))
    (vault / "wiki/log.md").write_text("# Directory Update Log\n\n## 2020-01-01\n* Historical entry.\n")
    if ignored:
        (vault / ".gitignore").write_text("/.vault-meta/\n")
    subprocess.run(["git", "-C", str(vault), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(vault), "config", "user.name", "fixture"], check=True)
    subprocess.run(["git", "-C", str(vault), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(vault), "config", "core.excludesFile", os.devnull], check=True)
    run(ROOT / "skills/wiki/scripts/okf_mw/sync.py", vault)
    subprocess.run(["git", "-C", str(vault), "add", "wiki"] + ([".gitignore"] if (vault / ".gitignore").exists() else []), check=True)
    subprocess.run(["git", "-C", str(vault), "commit", "-qm", "baseline"], check=True)
    return vault, caller


def capture(vault: Path, caller: Path, target: Path) -> None:
    run(HELPER, "capture", "--vault", vault, "--cwd", caller, "--path", target)


def capture_owned(vault: Path, caller: Path, target: Path, owner: str, tool: str) -> None:
    run(HELPER, "capture", "--vault", vault, "--cwd", caller, "--path", target,
        "--owner", owner, "--tool", tool)


def record(vault: Path, caller: Path, target: Path, ok: bool = True) -> subprocess.CompletedProcess:
    return run(HELPER, "record", "--vault", vault, "--cwd", caller, "--path", target,
               "--validator", MIDDLEWARE / "validate.py", ok=ok)


def record_owned(vault: Path, caller: Path, target: Path, owner: str, tool: str) -> subprocess.CompletedProcess:
    return run(HELPER, "record", "--vault", vault, "--cwd", caller, "--path", target,
               "--validator", MIDDLEWARE / "validate.py", "--owner", owner, "--tool", tool)


def finalize(vault: Path, ok: bool = True, retrieve: bool = False) -> subprocess.CompletedProcess:
    args = [HELPER, "finalize", "--vault", vault, "--middleware", MIDDLEWARE]
    if retrieve:
        args += ["--retrieval-script", BM25]
    return run(*args, ok=ok)


def finalize_owner(vault: Path, owner: str, *, recover: bool = False, ok: bool = True) -> subprocess.CompletedProcess:
    args = [HELPER, "finalize", "--vault", vault, "--middleware", MIDDLEWARE]
    args += ["--recover-owner", owner] if recover else ["--owner", owner]
    return run(*args, ok=ok)


def fail_after_first_sync(vault: Path) -> None:
    args = type("FinalizeArgs", (), {"vault": str(vault), "middleware": str(MIDDLEWARE), "retrieval_script": None, "owner": None, "recover_owner": None})()
    with patch.object(wiki_lifecycle, "safe_log_update", side_effect=wiki_lifecycle.LifecycleError("injected log failure")):
        try:
            wiki_lifecycle.finalize(args)
        except wiki_lifecycle.LifecycleError:
            return
    raise AssertionError("injected log failure did not fail finalization")


with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-test-") as tmp:
    root = Path(tmp)
    vault, caller = setup(root)
    head = subprocess.check_output(["git", "-C", str(vault), "rev-parse", "HEAD"], text=True).strip()

    # Creation snapshots are captured before writes; postwrite queues but does not publish.
    created = vault / "wiki" / "brand-new.md"
    capture(vault, caller, created)
    created.write_text(page("UniqueSearchTerm Quasar"))
    assert record(vault, caller, created).stdout.strip() == "pending"
    assert "brand-new.md" not in (vault / "wiki/index.md").read_text()
    assert subprocess.check_output(["git", "-C", str(vault), "rev-parse", "HEAD"], text=True).strip() == head
    finalize(vault, retrieve=True)
    assert "[UniqueSearchTerm Quasar](brand-new.md)" in (vault / "wiki/index.md").read_text()
    log = (vault / "wiki/log.md").read_text()
    assert "**Creation**" in log and "(/brand-new.md)" in log and "Historical entry." in log
    assert log.count("(/brand-new.md)") == 1
    # Crash/retry after log publication must be idempotent, and an empty retry is a no-op.
    finalize(vault)
    assert (vault / "wiki/log.md").read_text().count("(/brand-new.md)") == 1
    assert subprocess.check_output(["git", "-C", str(vault), "rev-parse", "HEAD"], text=True).strip() == head

    # Existing content produces Update; a no-op write produces no pending/log entry.
    existing = vault / "wiki/base.md"
    capture(vault, caller, existing)
    assert record(vault, caller, existing).stdout.strip() == "clean"
    finalize(vault)
    assert "(/base.md)" not in (vault / "wiki/log.md").read_text()
    capture(vault, caller, existing)
    existing.write_text(page("Base", "edited body"))
    record(vault, caller, existing)
    finalize(vault)
    assert "**Update**" in (vault / "wiki/log.md").read_text()
    assert (vault / "wiki/log.md").read_text().count("(/base.md)") == 1

    # A failure after atomic log publication keeps the batch retryable and preserves history.
    retry_page = vault / "wiki/log-retry.md"
    capture(vault, caller, retry_page); retry_page.write_text(page("Retry Log Failure")); record(vault, caller, retry_page)
    wrapper = root / "failing-middleware"
    wrapper.mkdir()
    (wrapper / "validate.py").symlink_to(MIDDLEWARE / "validate.py")
    marker = root / "sync-count"
    (wrapper / "sync.py").write_text(
        "import pathlib, subprocess, sys\n"
        f"real = {str(MIDDLEWARE / 'sync.py')!r}\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "count = int(marker.read_text()) if marker.exists() else 0\n"
        "result = subprocess.run([sys.executable, real, *sys.argv[1:]])\n"
        "marker.write_text(str(count + 1))\n"
        "raise SystemExit(1 if count == 1 else result.returncode)\n"
    )
    class DayOne:
        @staticmethod
        def now(_tz):
            return RealDateTime(2026, 10, 3, tzinfo=_tz)

    class DayTwo:
        @staticmethod
        def now(_tz):
            return RealDateTime(2026, 10, 4, tzinfo=_tz)

    finalize_args = type("FinalizeArgs", (), {"vault": str(vault), "middleware": str(wrapper), "retrieval_script": None, "owner": None, "recover_owner": None})()
    with patch.object(wiki_lifecycle, "datetime", DayOne):
        try:
            wiki_lifecycle.finalize(finalize_args)
        except wiki_lifecycle.LifecycleError:
            pass
        else:
            raise AssertionError("injected second-sync failure did not fail finalization")
    assert "(/log-retry.md)" in (vault / "wiki/log.md").read_text()
    assert "Historical entry." in (vault / "wiki/log.md").read_text()
    pending_after_log = json.loads((vault / ".vault-meta/lifecycle/state.json").read_text())["pending"]
    assert pending_after_log and next(iter(pending_after_log.values()))["log_date"] == "2026-10-03"
    # A legitimate later batch moves the persisted retry date out of first
    # position; the retry must find/deduplicate the original date section.
    wiki_lifecycle.safe_log_update(vault.resolve() / "wiki/log.md", [("Creation", "later-batch.md")], "2026-10-05")
    with patch.object(wiki_lifecycle, "datetime", DayTwo):
        wiki_lifecycle.finalize(finalize_args)
    retry_log = (vault / "wiki/log.md").read_text()
    assert retry_log.count("(/log-retry.md)") == 1
    assert retry_log.count("## 2026-10-03") == 1 and retry_log.count("## 2026-10-04") == 1 and retry_log.count("## 2026-10-05") == 1
    assert retry_log.index("## 2026-10-05") < retry_log.index("## 2026-10-03"), retry_log

    # A crash/missing tool_result is reconciled from the captured prewrite snapshot.
    missing_result = vault / "wiki/missing-result.md"
    capture(vault, caller, missing_result); missing_result.write_text(page("Missing Tool Result"))
    finalize(vault)
    assert "[Missing Tool Result](missing-result.md)" in (vault / "wiki/index.md").read_text()
    assert "(/missing-result.md)" in (vault / "wiki/log.md").read_text()

    # If first sync publishes a creation but logging fails, deletion remains a
    # reconciliation batch and must remove stale navigation without phantom log entry.
    transient = vault / "wiki/transient.md"
    capture(vault, caller, transient); transient.write_text(page("Transient Publication")); record(vault, caller, transient)
    fail_after_first_sync(vault)
    assert "[Transient Publication](transient.md)" in (vault / "wiki/index.md").read_text()
    transient.unlink()
    assert record(vault, caller, transient).stdout.strip() == "pending"
    finalize(vault)
    assert "transient.md" not in (vault / "wiki/index.md").read_text()
    assert "(/transient.md)" not in (vault / "wiki/log.md").read_text()

    # Reverting an update after partial publication similarly restores indexes
    # without treating the net no-op as another content update.
    baseline_bytes = existing.read_bytes()
    capture(vault, caller, existing); existing.write_text(page("Base", "temporary title/body"))
    record(vault, caller, existing)
    fail_after_first_sync(vault)
    existing.write_bytes(baseline_bytes)
    assert record(vault, caller, existing).stdout.strip() == "pending"
    finalize(vault)
    assert "[Base](base.md)" in (vault / "wiki/index.md").read_text()
    assert (vault / "wiki/log.md").read_text().count("(/base.md)") == 1

    # Untouched historical invalid pages are diagnostics, not a vault-wide repair veto.
    historical_bad = vault / "wiki/historical-bad.md"
    historical_bad.write_text("# old malformed page\\n")
    untouched_repair = vault / "wiki/untouched-repair.md"
    capture(vault, caller, untouched_repair); untouched_repair.write_text(page("Repair With Historical Error")); record(vault, caller, untouched_repair)
    finalize(vault)
    assert "[Repair With Historical Error](untouched-repair.md)" in (vault / "wiki/index.md").read_text()

    # An invalid member blocks the whole changed batch and retains valid sibling work.
    first, bad = vault / "wiki/first.md", vault / "wiki/bad.md"
    capture(vault, caller, first); first.write_text(page("First Batch")); record(vault, caller, first)
    capture(vault, caller, bad); bad.write_text("# invalid\n")
    record(vault, caller, bad, ok=False)
    index_before = (vault / "wiki/index.md").read_bytes()
    finalize(vault, ok=False)
    assert (vault / "wiki/index.md").read_bytes() == index_before
    assert (vault / ".vault-meta/lifecycle/state.json").exists()
    bad.write_text(page("Bad Repaired")); record(vault, caller, bad)
    finalize(vault)
    assert "[First Batch](first.md)" in (vault / "wiki/index.md").read_text()
    assert "[Bad Repaired](bad.md)" in (vault / "wiki/index.md").read_text()
    assert (vault / "wiki/log.md").read_text().count("(/first.md)") == 1

    # Deleted invalid pages can be removed and the remaining batch finalized.
    deleted = vault / "wiki/deleted.md"
    capture(vault, caller, deleted); deleted.write_text("# invalid then removed\n"); record(vault, caller, deleted, ok=False)
    capture(vault, caller, created); created.write_text(page("UniqueSearchTerm Quasar Updated")); record(vault, caller, created)
    deleted.unlink(); record(vault, caller, deleted)
    finalize(vault)

    # Newline and shell-special characters remain ordinary path data through JSON state.
    unusual = vault / "wiki" / "space ; $()\\nname.md"
    capture(vault, caller, unusual); unusual.write_text(page("Quoted Path")); record(vault, caller, unusual)
    finalize(vault)
    assert "Quoted Path" in (vault / "wiki/index.md").read_text()

    # Unrelated pre-staged/dirty files and an external commit are never changed by lifecycle.
    (vault / "unrelated.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(vault), "add", "unrelated.txt"], check=True)
    subprocess.run(["git", "-C", str(vault), "commit", "-qm", "external actor"], check=True)
    (vault / "unrelated.txt").write_text("external dirty\n")
    subprocess.run(["git", "-C", str(vault), "add", "unrelated.txt"], check=True)
    staged_before = subprocess.check_output(["git", "-C", str(vault), "diff", "--cached", "--name-only"], text=True)
    new_after_commit = vault / "wiki/after-external.md"
    capture(vault, caller, new_after_commit); new_after_commit.write_text(page("After External Commit")); record(vault, caller, new_after_commit); finalize(vault)
    assert subprocess.check_output(["git", "-C", str(vault), "diff", "--cached", "--name-only"], text=True) == staged_before
    assert "M  unrelated.txt" in subprocess.check_output(["git", "-C", str(vault), "status", "--porcelain"], text=True)
    assert subprocess.check_output(["git", "-C", str(vault), "rev-parse", "HEAD"], text=True).strip() != head

    # Package-owned retrieval works without any vault-local scripts directory.
    result = run(RETRIEVE, "UniqueSearchTerm Quasar Updated", "--vault", vault)
    assert "brand-new.md" in result.stdout
    assert not (vault / "scripts").exists()

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-no-wiki-") as tmp:
    root = Path(tmp)
    vault = root / "selected-vault"
    caller = root / "ordinary-repo"
    vault.mkdir()
    caller.mkdir()
    ordinary = caller / "ordinary.py"
    ordinary.write_text("print('unrelated')\\n")
    capture(vault, caller, ordinary)
    assert not (vault / ".vault-meta").exists()

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-no-git-") as tmp:
    vault = Path(tmp) / "vault"
    caller = Path(tmp) / "caller"
    (vault / "wiki").mkdir(parents=True)
    caller.mkdir()
    (vault / "wiki/log.md").write_text("# Directory Update Log\\n")
    target = vault / "wiki/local-only.md"
    capture(vault, caller, target)
    target.write_text(page("No Git Wiki"))
    record(vault, caller, target)
    finalize(vault, retrieve=True)
    assert "[No Git Wiki](local-only.md)" in (vault / "wiki/index.md").read_text()
    assert not (vault / ".git").exists()
    assert "local-only.md" in run(RETRIEVE, "No Git Wiki", "--vault", vault).stdout

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-owned-capture-") as tmp:
    root = Path(tmp)
    vault, caller = setup(root)
    target = vault / "wiki/base.md"
    original_index = (vault / "wiki/index.md").read_bytes()
    original_log = (vault / "wiki/log.md").read_bytes()
    capture_owned(vault, caller, target, "writer-A", "tool-A")
    active_state = json.loads((vault / ".vault-meta/lifecycle/state.json").read_text())
    target_key = str(target.resolve())
    before_digest = active_state["pending"][target_key]["before"]
    finalize_owner(vault, "reader-B", ok=False)
    assert json.loads((vault / ".vault-meta/lifecycle/state.json").read_text()) == active_state
    target.write_text("---\\ntype: note\\ntitle: Partial\\n")
    finalize_owner(vault, "reader-B", recover=True, ok=False)
    assert json.loads((vault / ".vault-meta/lifecycle/state.json").read_text()) == active_state
    assert (vault / "wiki/index.md").read_bytes() == original_index
    assert (vault / "wiki/log.md").read_bytes() == original_log
    finalize_owner(vault, "writer-A", ok=False)
    assert (json.loads((vault / ".vault-meta/lifecycle/state.json").read_text()) == active_state), \
        "ordinary finalize must not settle an active capture"
    target.write_text(page("Base", "settled update"))
    record_owned(vault, caller, target, "writer-A", "tool-A")
    settled = json.loads((vault / ".vault-meta/lifecycle/state.json").read_text())
    assert settled["pending"][target_key]["before"] == before_digest
    finalize_owner(vault, "writer-A")
    assert "**Update**" in (vault / "wiki/log.md").read_text()
    assert "**Creation**" not in (vault / "wiki/log.md").read_text()
    assert not json.loads((vault / ".vault-meta/lifecycle/state.json").read_text())["pending"]

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-owned-crash-recovery-") as tmp:
    root = Path(tmp)
    vault, caller = setup(root)
    target = vault / "wiki/base.md"
    capture_owned(vault, caller, target, "crashed-writer", "missing-result")
    target_key = str(target.resolve())
    before_digest = json.loads((vault / ".vault-meta/lifecycle/state.json").read_text())["pending"][target_key]["before"]
    target.write_text(page("Base", "finished before lost callback"))
    finalize_owner(vault, "crashed-writer", ok=False)
    assert json.loads((vault / ".vault-meta/lifecycle/state.json").read_text())["pending"][target_key]["before"] == before_digest
    finalize_owner(vault, "crashed-writer", recover=True)
    assert "**Update**" in (vault / "wiki/log.md").read_text()
    assert not json.loads((vault / ".vault-meta/lifecycle/state.json").read_text())["pending"]

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-ignore-") as tmp:
    root = Path(tmp)
    vault, caller = setup(root, ignored=False)
    warnings = run(HELPER, "diagnose", "--vault", vault).stderr
    assert ".vault-meta/lifecycle/" in warnings and ".vault-meta/retrieval/" in warnings
    (vault / ".gitignore").write_text("/.vault-meta/lifecycle/\n!/.vault-meta/lifecycle/\n/.vault-meta/lifecycle/*\n!/.vault-meta/lifecycle/finalize.lock\n/.vault-meta/retrieval/\n")
    warnings = run(HELPER, "diagnose", "--vault", vault).stderr
    assert ".vault-meta/lifecycle/" in warnings and ".vault-meta/retrieval/" not in warnings
    assert "finalize.lock" in warnings, warnings
    linked = root / "selective-negation-worktree"
    subprocess.run(["git", "-C", str(vault), "worktree", "add", "-q", "-b", "selective-negation", str(linked)], check=True)
    linked_warnings = run(HELPER, "diagnose", "--vault", linked).stderr
    assert "finalize.lock" in linked_warnings

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-tracked-") as tmp:
    root = Path(tmp)
    vault, caller = setup(root)
    state_file = vault / ".vault-meta/lifecycle/state.json"
    cache_file = vault / ".vault-meta/retrieval/bm25.json"
    state_file.parent.mkdir(parents=True)
    cache_file.parent.mkdir(parents=True)
    state_file.write_text("tracked lifecycle state\\n")
    cache_file.write_text("tracked retrieval cache\\n")
    subprocess.run(["git", "-C", str(vault), "add", "-f", str(state_file.relative_to(vault)), str(cache_file.relative_to(vault))], check=True)
    subprocess.run(["git", "-C", str(vault), "commit", "-qm", "tracked derived state"], check=True)
    warnings = run(HELPER, "diagnose", "--vault", vault).stderr
    assert warnings.count("contains tracked Git state") == 2
    linked = root / "linked-worktree"
    subprocess.run(["git", "-C", str(vault), "worktree", "add", "-q", "-b", "linked-fixture", str(linked)], check=True)
    linked_warnings = run(HELPER, "diagnose", "--vault", linked).stderr
    assert linked_warnings.count("contains tracked Git state") == 2

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-log-swap-") as tmp:
    root = Path(tmp)
    wiki = (root / "wiki")
    outside = root / "outside"
    wiki.mkdir()
    wiki = wiki.resolve()
    outside.mkdir()
    (wiki / "log.md").write_text("# Directory Update Log\n\nHistorical wiki entry.\n")
    (outside / "log.md").write_text("KEEP OUTSIDE LOG\n")
    moved = root / "moved-wiki"
    original_create_temp = wiki_lifecycle.create_log_temp
    swapped = [False]
    observed = []

    def swap_parent(parent_fd):
        if not swapped[0]:
            wiki.rename(moved)
            wiki.symlink_to(outside, target_is_directory=True)
            swapped[0] = True
        fd, name = original_create_temp(parent_fd)
        observed.append(name)
        assert not list(outside.glob(".log-*")), "temporary must never be created in the swapped outside directory"
        assert list(moved.glob(".log-*")), "temporary should remain in the originally pinned wiki directory until cleanup"
        return fd, name

    with patch.object(wiki_lifecycle, "create_log_temp", swap_parent):
        try:
            wiki_lifecycle.safe_log_update(wiki / "log.md", [("Creation", "new.md")], "2026-10-04")
        except wiki_lifecycle.LifecycleError:
            pass
        else:
            raise AssertionError("swapped wiki parent was accepted for log publication")
    assert (outside / "log.md").read_text() == "KEEP OUTSIDE LOG\n"
    assert "Historical wiki entry." in (moved / "log.md").read_text()
    assert not list(outside.glob(".log-*")), "swap-test wrote a temporary outside the original wiki"
    assert not list(moved.glob(".log-*")), "swap-test temporary was not cleaned through the pinned directory fd"

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-log-race-") as tmp:
    wiki = (Path(tmp) / "wiki")
    wiki.mkdir()
    wiki = wiki.resolve()
    log = wiki / "log.md"
    log.write_text("# Directory Update Log\n\nOriginal history.\n")
    original_create_temp = wiki_lifecycle.create_log_temp

    def concurrent_edit(parent_fd):
        result = original_create_temp(parent_fd)
        log.write_text("# Directory Update Log\n\nConcurrent edit must survive.\n")
        return result

    with patch.object(wiki_lifecycle, "create_log_temp", concurrent_edit):
        try:
            wiki_lifecycle.safe_log_update(log, [("Creation", "new.md")], "2026-10-04")
        except wiki_lifecycle.LifecycleError:
            pass
        else:
            raise AssertionError("concurrent log replacement was overwritten")
    assert "Concurrent edit must survive." in log.read_text()
    assert not list(wiki.glob(".log-*")), "concurrent-edit temporary was not cleaned"

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-log-multiline-label-") as tmp:
    wiki = (Path(tmp) / "wiki")
    wiki.mkdir()
    wiki = wiki.resolve()
    log = wiki / "log.md"
    log.write_text("# Directory Update Log\n\nEarlier history.\n")
    label = "odd\nname[brackets]*.md"
    wiki_lifecycle.safe_log_update(log, [("Creation", label)], "2026-10-04")
    first = log.read_text()
    wiki_lifecycle.safe_log_update(log, [("Creation", label)], "2026-10-04")
    content = log.read_text()
    assert content.count("**Creation**") == 1
    assert "Earlier history." in content
    assert "odd\\nname\\[brackets\\]\\*\\.md" in content
    assert "(/odd%0Aname%5Bbrackets%5D%2A.md)" in content
    assert content == first, "retry of a newline/special label must be byte-for-byte idempotent"

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-symlink-") as tmp:
    root = Path(tmp)
    vault, caller = setup(root)
    outside = root / "outside-state.json"
    outside.write_text("sentinel\\n")
    life = vault / ".vault-meta/lifecycle"
    life.mkdir(parents=True)
    (life / "state.json").symlink_to(outside)
    target = vault / "wiki/escape-state.md"
    target.write_text(page("Symlink State"))
    result = run(HELPER, "capture", "--vault", vault, "--cwd", caller, "--path", target, ok=False)
    assert outside.read_text() == "sentinel\\n"

with tempfile.TemporaryDirectory(prefix="wiki-lifecycle-ignore-proof-") as tmp:
    for existing in (False, True):
        for number, (rules, expected) in enumerate((
            ("/.vault-meta/\n", True),
            ("/.vault-meta\n", True),
            ("/.vault-meta/lifecycle/\n/.vault-meta/retrieval/\n", True),
            ("/.vault-meta/lifecycle/\n!/.vault-meta/lifecycle/\n"
             "/.vault-meta/lifecycle/*\n!/.vault-meta/lifecycle/.state-[0-9]*\n"
             "/.vault-meta/retrieval/\n", False),
            ("/.vault-meta/lifecycle/\n!/.vault-meta/lifecycle/\n"
             "/.vault-meta/lifecycle/*\n/.vault-meta/lifecycle/*/\n"
             "!/.vault-meta/lifecycle/.state-[0-9]*\n"
             "/.vault-meta/retrieval/\n", False),
        )):
            vault = Path(tmp).resolve() / f"vault-{existing}-{number}"
            vault.mkdir()
            subprocess.run(["git", "-C", str(vault), "init", "-q"], check=True)
            (vault / ".gitignore").write_text(rules)
            if existing:
                (vault / ".vault-meta/lifecycle").mkdir(parents=True)
                (vault / ".vault-meta/retrieval").mkdir()
            assert wiki_lifecycle._git_directory_ignored(vault, ".vault-meta/lifecycle/") == expected
            diagnostics = wiki_lifecycle.exclusion_diagnostic(vault)
            assert any("lifecycle derived-state scope" in item for item in diagnostics) != expected

print("wiki lifecycle durable-state regressions PASS")
