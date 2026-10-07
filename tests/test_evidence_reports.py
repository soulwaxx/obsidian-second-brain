from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source
MODULE = load_source(
    "evidence_reports_test", str(ROOT / "scripts/evidence_reports.py")
)
CLI = ROOT / "scripts/obsidian-second-brain.py"
LEDGER = load_source(
    "evidence_report_ledger_test", str(ROOT / "scripts/evidence_ledger.py")
)


class EvidenceReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        self.config = self.root / "properties.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")

    def write_record(self, record):
        path = self.vault / LEDGER.record_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def cli_report(self, vault=None, as_of="2026-06-01"):
        env = {**__import__("os").environ, "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        args = [sys.executable, str(CLI), "evidence-report", "--as-of", as_of,
                "--vault", str(vault or self.vault), "--json"]
        return subprocess.run(args, env=env, text=True, capture_output=True, timeout=3)

    @staticmethod
    def snapshot(root):
        result = {}
        for path in root.rglob("*"):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                value = path.read_bytes()
            elif stat.S_ISLNK(info.st_mode):
                value = os.readlink(path)
            else:
                value = None
            result[path.relative_to(root).as_posix()] = (info.st_mode, info.st_mtime_ns, value)
        return result

    def test_missing_ledger_minimal_page_and_type_validity_are_independent_of_optional_metadata(self):
        page = self.vault / "wiki/minimal.md"
        page.write_text("---\ntype: Note\n---\nA minimal page.\n", encoding="utf-8")
        before = {p.relative_to(self.vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.vault.rglob("*") if p.is_file()}
        report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertEqual(report["ledger"]["state"], "missing")
        self.assertEqual(report["pages"][0]["structuralValidity"]["status"], "valid")
        self.assertEqual(report["pages"][0]["freshness"]["status"], "unknown")
        self.assertEqual(report["pages"][0]["references"], [])
        after = {p.relative_to(self.vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((self.vault / "wiki/meta/evidence").exists())

    def test_as_of_staleness_review_and_source_reference_checks(self):
        source = LEDGER.make_source("file:///notes/source.txt", b"snapshot", freshness="stale")
        self.write_record(source)
        claim = LEDGER.make_claim("claim-one", "A claim", support=[source["id"]],
                                  contradictions=["source-000000000000000000000000"])
        self.write_record(claim)
        (self.vault / "wiki/source.md").write_text("---\ntype: Note\n---\nSource page.\n", encoding="utf-8")
        page = self.vault / "wiki/report.md"
        page.write_text(
            "---\ntype: Note\nstale_after: 2026-05-31\nstatus: draft\nverified:\n  by: human:test\n  at: 2026-05-01\nclaims: [claim-one]\nsources:\n  - id: " + source["id"] + "\n  - id: absent-id\n    resource: wiki/missing.md\n  - id: linked-page\n    resource: wiki/source.md\n  - id: remote\n    resource: https://example.test/evidence\n---\nA cited claim.[^" + source["id"] + "] A dangling claim.[^missing-cite]\n", encoding="utf-8")
        report = MODULE.build_report(self.vault, "2026-06-01")
        by_path = {item["path"]: item for item in report["pages"]}
        item = by_path["wiki/report.md"]
        self.assertEqual(report["sources"][0]["freshness"], "stale")
        self.assertEqual(report["claims"][0]["evidence"][0]["state"], "stale")
        self.assertEqual(item["structuralValidity"]["status"], "valid")
        self.assertEqual(item["freshness"]["status"], "stale")
        self.assertEqual(item["review"]["verificationMetadata"], "present-unverified-by-report")
        self.assertEqual(item["claims"][0]["state"], "contradictory-evidence-recorded")
        self.assertIn("missing-source-record", item["claims"][0]["issues"])
        ref_states = [reference["state"] for reference in item["references"]]
        self.assertEqual(ref_states, ["ledger-source-stale", "missing", "resolved-in-vault", "external-unchecked"])
        self.assertEqual(item["citations"], [
            {"id": "missing-cite", "state": "missing-source-entry"},
            {"id": source["id"], "state": "source-entry-present"},
        ])
        self.assertIn("missing-citation-source-entry", item["issues"])
        self.assertIn("stale-source-record", item["issues"])
        self.assertIn("contradictory-evidence-recorded", item["issues"])

    def test_malformed_optional_source_ids_are_reported_without_crashing(self):
        page = self.vault / "wiki/malformed-sources.md"
        page.write_text("---\ntype: Note\nsources:\n  - id: [not, a, scalar]\n---\nText.[^known]\n", encoding="utf-8")
        report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertEqual(report["pages"][0]["citations"], [{"id": "known", "state": "missing-source-entry"}])
        self.assertIn("invalid-source-reference", report["pages"][0]["issues"])

    def test_invalid_okf_structure_and_bad_optional_date_do_not_become_freshness_claims(self):
        page = self.vault / "wiki/invalid.md"
        page.write_text("---\ntitle: no type\nstale_after: yesterday\n---\nBody\n", encoding="utf-8")
        report = MODULE.build_report(self.vault, "2026-06-01")
        item = report["pages"][0]
        self.assertEqual(item["structuralValidity"]["status"], "invalid")
        self.assertEqual(item["freshness"]["status"], "unknown")
        self.assertIn("invalid-stale-after", item["freshness"]["issues"])

    def test_cli_report_json_as_of_is_read_only(self):
        (self.vault / "wiki/minimal.md").write_text("---\ntype: Note\n---\nText\n", encoding="utf-8")
        env = {**__import__("os").environ, "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        before = {p.relative_to(self.vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.vault.rglob("*") if p.is_file()}
        result = subprocess.run([sys.executable, str(CLI), "evidence-report", "--as-of", "2026-06-01",
                                 "--vault", str(self.vault), "--json"], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["asOf"], "2026-06-01")
        after = {p.relative_to(self.vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.vault / ".vault-meta").exists())
        bad = subprocess.run([sys.executable, str(CLI), "evidence-report", "--as-of", "not-a-date",
                              "--vault", str(self.vault), "--json"], env=env, text=True, capture_output=True)
        self.assertNotEqual(bad.returncode, 0)

    def test_ledger_namespace_ancestors_and_entries_never_follow_symlinks(self):
        outside = self.root / "outside"
        evidence = outside / "meta/evidence"
        evidence.mkdir(parents=True)
        record = LEDGER.make_source("file:///OUTSIDE-PRIVATE-LOCATION", b"outside")
        outside_record = evidence / f"{record['id']}.json"
        outside_record.write_text(json.dumps(record), encoding="utf-8")
        outside_before = self.snapshot(outside)
        namespaces = self.vault / "wiki/meta/evidence"
        namespaces.mkdir(parents=True)
        (self.vault / "wiki/page.md").write_text("---\ntype: Note\n---\nText\n", encoding="utf-8")
        cases = ("wiki", "meta", "evidence", "entry")
        for case in cases:
            with self.subTest(case=case):
                if case == "wiki":
                    (self.vault / "wiki").rename(self.vault / "wiki-real")
                    (self.vault / "wiki").symlink_to(outside, target_is_directory=True)
                elif case == "meta":
                    (self.vault / "wiki/meta").rename(self.vault / "wiki/meta-real")
                    (self.vault / "wiki/meta").symlink_to(outside / "meta", target_is_directory=True)
                elif case == "evidence":
                    (self.vault / "wiki/meta/evidence").rename(self.vault / "wiki/meta/evidence-real")
                    (self.vault / "wiki/meta/evidence").symlink_to(evidence, target_is_directory=True)
                else:
                    (self.vault / "wiki/meta/evidence").mkdir(parents=True, exist_ok=True)
                    (self.vault / "wiki/meta/evidence" / outside_record.name).symlink_to(outside_record)
                before = self.snapshot(self.vault)
                try:
                    try:
                        report = MODULE.build_report(self.vault, "2026-06-01")
                        self.assertNotIn("OUTSIDE-PRIVATE-LOCATION", json.dumps(report))
                        if case in {"meta", "evidence"}:
                            self.assertEqual(report["ledger"]["state"], "unavailable")
                        elif case == "entry":
                            self.assertEqual(report["ledger"]["invalidCount"], 1)
                    except MODULE.ReportError:
                        self.assertEqual(case, "wiki")
                    result = self.cli_report()
                    self.assertNotIn("OUTSIDE-PRIVATE-LOCATION", result.stdout + result.stderr)
                    if case == "wiki":
                        self.assertNotEqual(result.returncode, 0)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.snapshot(self.vault), before)
                    self.assertEqual(self.snapshot(outside), outside_before)
                    self.assertFalse((self.vault / ".vault-meta").exists())
                finally:
                    if (self.vault / "wiki").is_symlink():
                        (self.vault / "wiki").unlink()
                        (self.vault / "wiki-real").rename(self.vault / "wiki")
                    if (self.vault / "wiki/meta").is_symlink():
                        (self.vault / "wiki/meta").unlink()
                        (self.vault / "wiki/meta-real").rename(self.vault / "wiki/meta")
                    if (self.vault / "wiki/meta/evidence").is_symlink():
                        (self.vault / "wiki/meta/evidence").unlink()
                        (self.vault / "wiki/meta/evidence-real").rename(self.vault / "wiki/meta/evidence")
                    entry = self.vault / "wiki/meta/evidence" / outside_record.name
                    if entry.is_symlink():
                        entry.unlink()
                    (self.vault / "wiki/meta/evidence").mkdir(parents=True, exist_ok=True)

    def test_yaml_date_and_timestamp_frontmatter_is_json_safe_in_api_and_cli(self):
        values = (
            ("unquoted-date", "2026-05-31", "2026-05-31"),
            ("quoted-date", '"2026-05-31"', "2026-05-31"),
            ("unquoted-timestamp", "2026-05-31T12:00:00Z", "2026-05-31T12:00:00+00:00"),
            ("quoted-timestamp", '"2026-05-31T12:00:00Z"', "2026-05-31T12:00:00Z"),
        )
        for name, value, expected in values:
            with self.subTest(name=name):
                page = self.vault / "wiki" / f"{name}.md"
                page.write_text(f"---\ntype: Note\nstale_after: {value}\n---\nText\n", encoding="utf-8")
                before = self.snapshot(self.vault)
                report = MODULE.build_report(self.vault, date(2026, 6, 1))
                item = next(page for page in report["pages"] if page["path"].endswith(f"{name}.md"))
                self.assertEqual(item["structuralValidity"]["status"], "valid")
                self.assertEqual(item["freshness"]["status"], "stale")
                self.assertEqual(item["freshness"]["staleAfter"], expected)
                result = self.cli_report()
                self.assertEqual(result.returncode, 0, result.stderr)
                cli_item = next(page for page in json.loads(result.stdout)["pages"]
                                if page["path"].endswith(f"{name}.md"))
                self.assertEqual(cli_item, item)
                self.assertEqual(self.snapshot(self.vault), before)

    def test_middleware_frontmatter_delimiters_preserve_optional_report_metadata(self):
        variants = (
            ("bom", "\ufeff---\n", "---\n"),
            ("spaces", "---  \n", "---\n"),
            ("tabs", "---\t\r\n", "---\r\n"),
            ("crlf-ellipsis", "---\r\n", "...  \r\n"),
        )
        for name, opening, closing in variants:
            with self.subTest(name=name):
                page = self.vault / "wiki" / f"{name}.md"
                body = f"type: Note\nstale_after: 2026-05-31\nsources:\n  - id: missing\n    resource: wiki/absent.md\n"
                page.write_text(opening + body + closing + "Text\n", encoding="utf-8")
                before = self.snapshot(self.vault)
                report = MODULE.build_report(self.vault, "2026-06-01")
                item = next(page for page in report["pages"] if page["path"].endswith(f"{name}.md"))
                self.assertEqual(item["structuralValidity"]["status"], "valid")
                self.assertEqual(item["freshness"]["status"], "stale")
                self.assertEqual(item["freshness"]["staleAfter"], "2026-05-31")
                self.assertEqual(item["references"], [{"id": "missing", "resource": "wiki/absent.md", "state": "missing"}])
                result = self.cli_report()
                self.assertEqual(result.returncode, 0, result.stderr)
                cli_item = next(page for page in json.loads(result.stdout)["pages"]
                                if page["path"].endswith(f"{name}.md"))
                self.assertEqual(cli_item, item)
                self.assertEqual(self.snapshot(self.vault), before)

    def test_legacy_sources_remain_unchecked_without_false_invalid_findings(self):
        fixtures = (
            ("scalar", "sources: https://example.test/legacy-source"),
            ("string-list", "sources:\n  - https://example.test/legacy-source"),
            ("mixed", "sources:\n  - https://example.test/legacy-source\n  - id: page-ref\n    resource: wiki/absent.md"),
        )
        for name, source_yaml in fixtures:
            with self.subTest(name=name):
                page = self.vault / "wiki" / f"legacy-{name}.md"
                page.write_text(f"---\ntype: Note\n{source_yaml}\n---\nText\n", encoding="utf-8")
                before = self.snapshot(self.vault)
                report = MODULE.build_report(self.vault, "2026-06-01")
                item = next(page for page in report["pages"] if page["path"].endswith(f"legacy-{name}.md"))
                self.assertEqual(item["structuralValidity"]["status"], "valid")
                self.assertNotIn("invalid-source-metadata", item["issues"])
                self.assertNotIn("invalid-source-reference", item["issues"])
                self.assertEqual(item["references"][0]["state"], "legacy-source-unchecked")
                if name == "mixed":
                    self.assertEqual(item["references"][1]["state"], "missing")
                result = self.cli_report()
                self.assertEqual(result.returncode, 0, result.stderr)
                cli_item = next(page for page in json.loads(result.stdout)["pages"]
                                if page["path"].endswith(f"legacy-{name}.md"))
                self.assertEqual(cli_item, item)
                self.assertEqual(self.snapshot(self.vault), before)

    def test_yaml_frontmatter_reference_and_review_values_never_break_json_reports(self):
        page = self.vault / "wiki/unsafe-yaml-types.md"
        page.write_text(
            "---\ntype: Note\nstatus: 2026-05-31\nsources:\n  - id: 2026-05-31\n    resource: {nested: value}\n  - id: plain-id\n    resource: 2026-05-31T12:00:00Z\nclaims:\n  - 2026-05-31\n  - [nested, values]\n  - 2026-05-31T12:00:00Z\n---\nText\n",
            encoding="utf-8",
        )
        sibling = self.vault / "wiki/valid-sibling.md"
        sibling.write_text("---\ntype: Note\n---\nStill reported\n", encoding="utf-8")
        before = self.snapshot(self.vault)
        report = MODULE.build_report(self.vault, "2026-06-01")
        json.dumps(report)
        item = next(page for page in report["pages"] if page["path"] == "wiki/unsafe-yaml-types.md")
        self.assertEqual(item["structuralValidity"]["status"], "invalid")
        self.assertEqual(item["review"]["status"], None)
        self.assertIn("invalid-review-status", item["issues"])
        self.assertEqual(item["references"][0]["id"], None)
        self.assertEqual(item["references"][0]["resource"], None)
        self.assertIn("invalid-source-id", item["references"][0]["issues"])
        self.assertEqual(item["references"][1]["resource"], None)
        self.assertIn("invalid-source-resource", item["references"][1]["issues"])
        self.assertEqual(item["claims"][0]["id"], None)
        self.assertEqual(item["claims"][0]["state"], "invalid-claim-reference")
        self.assertEqual(item["claims"][2]["id"], None)
        self.assertIn("wiki/valid-sibling.md", {page["path"] for page in report["pages"]})
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), report)
        self.assertEqual(self.snapshot(self.vault), before)

    def test_nonregular_ledger_entries_are_rejected_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("named pipes are unavailable")
        ledger = self.vault / "wiki/meta/evidence"
        ledger.mkdir(parents=True)
        fifo = ledger / "claim-pipe.json"
        os.mkfifo(fifo)
        before = self.snapshot(self.vault)
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["ledger"]["state"], "present")
        self.assertEqual(report["ledger"]["invalidCount"], 1)
        self.assertEqual(report["ledger"]["invalidRecords"][0]["path"], fifo.name)
        self.assertEqual(self.snapshot(self.vault), before)

    def test_regular_entry_replaced_with_fifo_before_open_is_rejected(self):
        ledger = self.vault / "wiki/meta/evidence"
        ledger.mkdir(parents=True)
        record = LEDGER.make_claim("claim-race", "A claim")
        path = self.write_record(record)
        original_open = os.open
        replaced = False
        fifo_state = None

        def replace_before_open(name, flags, mode=0o777, *, dir_fd=None):
            nonlocal replaced, fifo_state
            if name == path.name and dir_fd is not None and not replaced:
                os.unlink(name, dir_fd=dir_fd)
                os.mkfifo(name, mode=0o600, dir_fd=dir_fd)
                fifo_state = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                replaced = True
                self.assertTrue(flags & getattr(os, "O_NONBLOCK", 0))
            return original_open(name, flags, mode, dir_fd=dir_fd)

        with patch.object(MODULE.os, "open", side_effect=replace_before_open):
            report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertTrue(replaced)
        self.assertEqual(report["ledger"]["invalidCount"], 1)
        after = path.lstat()
        self.assertTrue(stat.S_ISFIFO(after.st_mode))
        self.assertEqual(after.st_mtime_ns, fifo_state.st_mtime_ns)

    def test_malformed_ledgers_are_reported_and_symlink_namespace_is_not_followed(self):
        ledger_dir = self.vault / "wiki/meta/evidence"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "claim-bad.json").write_text("{}", encoding="utf-8")
        report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertEqual(report["ledger"]["state"], "present")
        self.assertEqual(report["ledger"]["invalidCount"], 1)
        outside = self.root / "outside"
        outside.mkdir()
        (self.vault / "wiki/meta/evidence").rename(self.vault / "wiki/meta/evidence-real")
        (self.vault / "wiki/meta/evidence").symlink_to(outside, target_is_directory=True)
        report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertEqual(report["ledger"]["state"], "unavailable")
        self.assertIn("symlink", report["ledger"]["error"])


if __name__ == "__main__":
    unittest.main()
