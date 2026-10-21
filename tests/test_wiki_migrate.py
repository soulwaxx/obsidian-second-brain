from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get("OSB_TEST_PACKAGE_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source

MODULE = load_source("wiki_migrate_test", str(ROOT / "scripts/wiki_migrate.py"))


class WikiMigrateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / "wiki/a").mkdir(parents=True)
        self.config = Path(self.temp.name) / "properties.json"
        self.assertTrue(Path(MODULE.__file__).resolve().is_relative_to(ROOT))
        self.assertTrue(Path(MODULE.COMPAT.__file__).resolve().is_relative_to(ROOT))
        self.assertTrue(Path(MODULE.BATCH.__file__).resolve().is_relative_to(ROOT))

    def snapshot(self):
        result = {}
        for path in [self.vault, *self.vault.rglob("*")]:
            info = path.lstat()
            content = path.read_bytes() if stat.S_ISREG(info.st_mode) else path.readlink().as_posix() if stat.S_ISLNK(info.st_mode) else None
            result[path.relative_to(self.vault).as_posix() if path != self.vault else "."] = (info.st_mode, info.st_mtime_ns, content)
        return result

    def test_prepare_preserves_authored_bytes_and_inspects_version_one_bundle(self):
        (self.vault / "wiki/a/target space.md").write_text("---\ntype: note\n---\n", encoding="utf-8")
        original = ("---\ntype: topic\nlinks: \"[front](/wiki/a/target%20space.md#Title)\"\n"
                   "description: \"[desc](/wiki/a/target%20space.md#Title)\"\n"
                   "custom: \"leave [unknown](/wiki/a/target%20space.md)\"\n---\n"
                   "[escaped \\[label\\]](/wiki/a/target%20space.md#Title) "
                   "[nested [label]](/wiki/a/target%20space.md#Title)\n"
                   "ID: 2024-01-02\n`[code](/wiki/a/target%20space.md)`\n"
                   "```md\n[x](/wiki/a/target%20space.md)\n```\n"
                   "[web](https://example.test/a)\n")
        source = self.vault / "wiki/a/source.md"
        source.write_bytes(original.encode())
        before = self.snapshot()
        preview = MODULE.prepare(self.vault, self.config)
        self.assertEqual(self.snapshot(), before)
        bundle = preview["bundle"]
        self.assertEqual(bundle["version"], 1)
        self.assertEqual(len(bundle["operations"]), 1)
        operation = bundle["operations"][0]
        self.assertEqual(operation["path"], "wiki/a/source.md")
        self.assertEqual(operation["expectedHash"], hashlib.sha256(original.encode()).hexdigest())
        content = operation["content"]
        self.assertEqual(content.count("./target%20space.md#Title"), 4)
        self.assertIn('custom: "leave [unknown](/wiki/a/target%20space.md)"', content)
        self.assertIn("ID: 2024-01-02", content)
        self.assertIn("`[code](/wiki/a/target%20space.md)`", content)
        self.assertIn("[x](/wiki/a/target%20space.md)", content)
        self.assertIn("[web](https://example.test/a)", content)
        self.assertIsNotNone(preview["plan"])
        self.assertEqual(preview["planHash"], MODULE.BATCH.inspect(self.vault, bundle, self.config)["planHash"])

    def test_multiple_pages_subset_reserved_and_historical_findings(self):
        (self.vault / "wiki/target.md").write_text("---\ntype: note\n---\n")
        (self.vault / "wiki/one.md").write_text("---\ntype: note\n---\n[x](/wiki/target.md)\n")
        (self.vault / "wiki/two.md").write_text("---\ntype: note\n---\n[x](/wiki/target.md)\n")
        (self.vault / "wiki/index.md").write_text("[x](/wiki/target.md)\n")
        (self.vault / "wiki/deep").mkdir()
        (self.vault / "wiki/deep/LOG.md").write_text("[old](/wiki/target.md)\n")
        (self.vault / "wiki/deep/_PLAN.md").write_text("---\ntype: note\n---\n[x](/wiki/target.md)\n")
        preview = MODULE.prepare(self.vault, self.config, ["wiki/one.md"])
        self.assertEqual([op["path"] for op in preview["bundle"]["operations"]], ["wiki/one.md"])
        kinds = {item["kind"] for item in preview["historical_or_generated_findings"]}
        self.assertTrue({"generated-navigation", "lifecycle-history"}.issubset(kinds))
        self.assertTrue(all("_PLAN.md" not in op["path"] and "index.md" not in op["path"] and "log.md" not in op["path"] for op in preview["bundle"]["operations"]))

    def test_noop_does_not_call_batch_inspect(self):
        (self.vault / "wiki/page.md").write_text("---\ntype: note\n---\nno links\n")
        original = MODULE.BATCH.inspect
        MODULE.BATCH.inspect = lambda *args, **kwargs: self.fail("empty bundles must not be inspected")
        try:
            preview = MODULE.prepare(self.vault, self.config)
        finally:
            MODULE.BATCH.inspect = original
        self.assertTrue(preview["no_op"])
        self.assertEqual(preview["bundle"], {"version": 1, "operations": []})
        self.assertIsNone(preview["plan"])
        self.assertIsNone(preview["planHash"])

    def test_stale_bytes_and_batch_preimage_conflict_are_refused(self):
        (self.vault / "wiki/target.md").write_text("---\ntype: note\n---\n")
        source = self.vault / "wiki/page.md"
        source.write_text("---\ntype: note\n---\n[x](/wiki/target.md)\n")
        original_report = MODULE.COMPAT.compatibility_report
        def stale_report(vault):
            result = original_report(vault)
            source.write_text("---\ntype: note\n---\n[x](/wiki/other.md)\n")
            return result
        MODULE.COMPAT.compatibility_report = stale_report
        try:
            with self.assertRaises(MODULE.MigrationError):
                MODULE.prepare(self.vault, self.config)
        finally:
            MODULE.COMPAT.compatibility_report = original_report
        source.write_text("---\ntype: note\n---\n[x](/wiki/target.md)\n")
        preview = MODULE.prepare(self.vault, self.config)
        source.write_text("---\ntype: note\n---\nuser edit\n")
        with self.assertRaises(MODULE.BATCH.BatchError):
            MODULE.BATCH.apply(self.vault, preview["bundle"], self.config, preview["planHash"])
        self.assertIn("user edit", source.read_text())

    def test_existing_batch_apply_and_retry_remain_publication_authority(self):
        (self.vault / "wiki/target.md").write_text("---\ntype: note\n---\n")
        source = self.vault / "wiki/page.md"
        source.write_text("---\ntype: note\n---\n[x](/wiki/target.md)\n")
        preview = MODULE.prepare(self.vault, self.config)
        applied = MODULE.BATCH.apply(self.vault, preview["bundle"], self.config, preview["planHash"])
        self.assertEqual(applied["phase"], "complete")
        self.assertIn("./target.md", source.read_text())
        retry = MODULE.BATCH.apply(self.vault, preview["bundle"], self.config, preview["planHash"])
        self.assertTrue(retry["noop"])

    def test_bom_and_crlf_proposals_are_exactly_spanned(self):
        (self.vault / "wiki/target.md").write_text("---\ntype: note\n---\n")
        source = self.vault / "wiki/page.md"
        original = b"\xef\xbb\xbf---\r\ntype: note\r\n---\r\n[x](/wiki/target.md)\r\n"
        source.write_bytes(original)
        preview = MODULE.prepare(self.vault, self.config)
        operation = preview["bundle"]["operations"][0]
        self.assertEqual(operation["expectedHash"], hashlib.sha256(original).hexdigest())
        self.assertTrue(operation["content"].startswith("\ufeff---\r\ntype: note\r\n---\r\n"))
        self.assertTrue(operation["content"].endswith("[x](./target.md)\r\n"))


if __name__ == "__main__":
    unittest.main()
