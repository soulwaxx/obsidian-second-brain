from __future__ import annotations

from pathlib import Path
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source

BASELINES = load_source("source_baselines_test", str(ROOT / "scripts/source_baselines.py"))
LEDGER = load_source("source_baselines_ledger_test", str(ROOT / "scripts/evidence_ledger.py"))


class SourceBaselineHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "source-root").resolve()
        self.root.mkdir()

    def observe(self, asset="asset-one", locator="docs/a.bin", when="2025-01-02T03:04:05+00:00", **kwargs):
        return BASELINES.observe_source("root-notes", asset, self.root, locator, when, **kwargs)

    def test_exact_binary_bytes_and_portable_records(self):
        path = self.root / "docs/a.bin"
        path.parent.mkdir()
        path.write_bytes(b"\x00\xff\x80")
        result = self.observe()
        self.assertEqual(result["observation"]["contentHash"], "sha256:" + __import__("hashlib").sha256(path.read_bytes()).hexdigest())
        self.assertNotIn(str(self.root), str(result))
        self.assertEqual(result["source"]["locator"], "local:root-notes/docs/a.bin")
        self.assertEqual(result["asset"]["id"], "asset-one")
        self.assertEqual(result["observation"]["review"], {"status": "unreviewed"})

    def test_distinct_content_produces_distinct_snapshots_for_same_asset(self):
        path = self.root / "a.txt"
        path.write_bytes(b"one")
        first = self.observe(locator="a.txt", when="2025-01-02T03:04:05+00:00")
        path.write_bytes(b"two")
        second = self.observe(locator="a.txt", when="2025-01-03T03:04:05+00:00")
        self.assertEqual(first["asset"]["id"], second["asset"]["id"])
        self.assertNotEqual(first["source"]["id"], second["source"]["id"])
        self.assertNotEqual(first["observation"]["id"], second["observation"]["id"])

    def test_same_bytes_under_distinct_assets_remain_separate(self):
        (self.root / "a").write_bytes(b"same bytes")
        first = self.observe(asset="asset-one", locator="a")
        second = self.observe(asset="asset-two", locator="a")
        self.assertNotEqual(first["asset"]["id"], second["asset"]["id"])
        self.assertEqual(first["source"]["id"], second["source"]["id"])
        self.assertNotEqual(first["observation"]["id"], second["observation"]["id"])

    def test_explicit_locator_move_keeps_caller_asset_identity(self):
        (self.root / "old").write_bytes(b"same bytes")
        (self.root / "new").write_bytes(b"same bytes")
        old = self.observe(locator="old")
        moved = self.observe(locator="new")
        self.assertEqual(old["asset"]["id"], moved["asset"]["id"])
        self.assertNotEqual(old["source"]["id"], moved["source"]["id"])
        self.assertEqual(old["observation"]["locator"], "old")
        self.assertEqual(moved["observation"]["locator"], "new")

    def test_unsafe_locators_and_symlinked_files_are_refused(self):
        for locator in ("../outside", "/absolute", "a\\b", "a/./b", ".", "./a", "a//b", "a/\x00b"):
            with self.subTest(locator=locator), self.assertRaises(BASELINES.SourceBaselineError):
                self.observe(locator=locator)
        (self.root / "real").write_bytes(b"x")
        try:
            (self.root / "alias").symlink_to(self.root / "real")
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(BASELINES.SourceBaselineError):
            self.observe(locator="alias")

    def test_fifo_is_refused_without_blocking(self):
        try:
            os.mkfifo(self.root / "pipe")
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("FIFO unavailable")
        with self.assertRaises(BASELINES.SourceBaselineError):
            self.observe(locator="pipe")

    def test_same_file_mutation_during_read_is_refused(self):
        path = self.root / "a"
        path.write_bytes(b"original")
        original_sha256 = BASELINES.hashlib.sha256
        changed = False

        class MutatingHash:
            def __init__(self):
                self.inner = original_sha256()

            def update(self, value):
                nonlocal changed
                self.inner.update(value)
                if not changed:
                    changed = True
                    path.write_bytes(b"changed during read")

            def hexdigest(self):
                return self.inner.hexdigest()

        with mock.patch.object(BASELINES.hashlib, "sha256", side_effect=MutatingHash):
            with self.assertRaises(BASELINES.SourceBaselineError):
                self.observe(locator="a", git=False)

    def test_root_and_parent_remapping_during_read_are_refused(self):
        original_fdopen = BASELINES.os.fdopen
        for remap_parent in (False, True):
            with self.subTest(remap_parent=remap_parent):
                container = Path(self.temp.name) / ("parent-case" if remap_parent else "root-case")
                container.mkdir()
                selected = container / "root" if remap_parent else self.root
                selected.mkdir(exist_ok=True)
                (selected / "a").write_bytes(b"inside")
                outside = Path(self.temp.name) / f"outside-{remap_parent}"
                outside.mkdir()
                (outside / "a").write_bytes(b"outside bytes")
                moved = selected.with_name(selected.name + "-moved")
                calls = 0

                def remapping_fdopen(fd, *args, **kwargs):
                    nonlocal calls
                    stream = original_fdopen(fd, *args, **kwargs)
                    if not calls:
                        calls += 1
                        selected.rename(moved)
                        selected.symlink_to(outside, target_is_directory=True)
                    return stream

                with mock.patch.object(BASELINES.os, "fdopen", side_effect=remapping_fdopen):
                    with self.assertRaises(BASELINES.SourceBaselineError):
                        BASELINES.observe_source("root-notes", "asset-one", selected, "a", "2025-01-02T03:04:05+00:00", git=False)
                if not remap_parent:
                    selected.unlink()
                    moved.rename(selected)

    def test_parent_symlink_and_root_case_collision_are_refused(self):
        parent = Path(self.temp.name) / "actual-parent"
        root = parent / "root"
        root.mkdir(parents=True)
        (root / "a").write_bytes(b"x")
        alias = Path(self.temp.name) / "parent-alias"
        try:
            alias.symlink_to(parent, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(BASELINES.SourceBaselineError):
            BASELINES.observe_source("root-notes", "asset-one", alias / "root", "a", "2025-01-02T03:04:05+00:00", git=False)
        sibling = parent / "ROOT"
        try:
            sibling.mkdir()
        except OSError:
            self.skipTest("case-distinct sibling cannot be created on this filesystem")
        with self.assertRaises(BASELINES.SourceBaselineError):
            BASELINES.observe_source("root-notes", "asset-one", root, "a", "2025-01-02T03:04:05+00:00", git=False)

    def test_case_collision_and_symlinked_root_are_refused(self):
        (self.root / "Doc").mkdir()
        (self.root / "Doc/a").write_bytes(b"x")
        with self.assertRaises(BASELINES.SourceBaselineError):
            self.observe(locator="doc/a")
        link = Path(self.temp.name) / "root-link"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(BASELINES.SourceBaselineError):
            BASELINES.observe_source("root-notes", "asset-one", link, "Doc/a", "2025-01-02T03:04:05+00:00")

    def test_missing_git_is_explicitly_unknown_and_never_embeds_root_path(self):
        (self.root / "a").write_bytes(b"x")
        result = self.observe(locator="a")
        context = result["observation"].get("gitContext", {})
        self.assertEqual(context["selectedBytes"], "unknown")
        self.assertNotIn(str(self.root), str(context))

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def _committed_repo(self):
        (self.root / "selected.txt").write_bytes(b"committed bytes")
        result = subprocess.run(["git", "init", "--quiet", "-b", "main", str(self.root)], capture_output=True)
        if result.returncode:
            self.skipTest("Git unavailable")
        self._git("config", "user.email", "fixture@example.test")
        self._git("config", "user.name", "Disposable Fixture")
        self._git("add", "selected.txt")
        self._git("commit", "-m", "fixture base")
        return self._git("rev-parse", "HEAD")

    def test_git_selected_bytes_clean_dirty_and_unrelated_dirty(self):
        base = self._committed_repo()
        clean = self.observe(locator="selected.txt")["observation"]["gitContext"]
        self.assertEqual(clean["head"], base)
        self.assertEqual(clean["selectedBytes"], "equal")
        (self.root / "selected.txt").write_bytes(b"dirty selected bytes")
        dirty = self.observe(locator="selected.txt")["observation"]["gitContext"]
        self.assertEqual(dirty["selectedBytes"], "different")
        (self.root / "selected.txt").write_bytes(b"committed bytes")
        (self.root / "unrelated.txt").write_bytes(b"unrelated dirty")
        unrelated = self.observe(locator="selected.txt")["observation"]["gitContext"]
        self.assertEqual(unrelated["selectedBytes"], "equal")

    def test_git_uses_captured_head_if_head_advances_mid_collection(self):
        captured = self._committed_repo()
        (self.root / "selected.txt").write_bytes(b"committed bytes")
        original_run = BASELINES._git_run
        advanced = False

        def advance_after_head(root, args, **kwargs):
            nonlocal advanced
            result = original_run(root, args, **kwargs)
            if args[:3] == ["rev-parse", "--verify", "HEAD^{commit}"] and not advanced:
                advanced = True
                (self.root / "selected.txt").write_bytes(b"later committed bytes")
                self._git("add", "selected.txt")
                self._git("commit", "-m", "fixture later")
            return result

        with mock.patch.object(BASELINES, "_git_run", side_effect=advance_after_head):
            context = self.observe(locator="selected.txt")["observation"]["gitContext"]
        self.assertEqual(context["head"], captured)
        self.assertEqual(context["selectedBytes"], "equal")

    def test_history_availability_handles_ancestor_rewrite_and_missing(self):
        first = self._committed_repo()
        self._git("commit", "--allow-empty", "-m", "fixture descendant")
        self.assertEqual(BASELINES.history_availability(self.root, first)["status"], "available-ancestor")
        self._git("checkout", "--quiet", "--orphan", "rewritten")
        self._git("branch", "-D", "main")
        self._git("rm", "-rf", ".")
        (self.root / "replacement.txt").write_bytes(b"replacement")
        self._git("add", "replacement.txt")
        self._git("commit", "-m", "fixture rewrite")
        self.assertEqual(BASELINES.history_availability(self.root, first)["status"], "retained-not-ancestor")
        self._git("reflog", "expire", "--expire=now", "--all")
        self._git("gc", "--prune=now")
        self.assertEqual(BASELINES.history_availability(self.root, first)["status"], "unavailable")
        self.assertEqual(BASELINES.history_availability(self.root, None)["status"], "no-commit-recorded")

    def test_unborn_repository_is_reported_as_unknown(self):
        (self.root / "a").write_bytes(b"x")
        initialized = subprocess.run(["git", "init", "--quiet", str(self.root)], capture_output=True)
        if initialized.returncode:
            self.skipTest("Git unavailable")
        result = self.observe(locator="a")
        context = result["observation"].get("gitContext", {})
        self.assertEqual(context["selectedBytes"], "unknown")
        self.assertNotIn(str(self.root), str(context))


if __name__ == "__main__":
    unittest.main()
