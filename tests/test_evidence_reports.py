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

    def test_optional_asset_observation_and_per_pair_baseline_history_are_reported_read_only(self):
        observations = []
        assets = [LEDGER.make_asset("asset-one", "root-one", "folder/one.txt"),
                  LEDGER.make_asset("asset-two", "root-one", "folder/two.txt")]
        for index, asset in enumerate(assets, start=1):
            asset["producerExtension"] = {"preserve": index}
            source = LEDGER.make_source(
                f"local:{asset['rootId']}/{asset['locator']}", f"source bytes {index}".encode()
            )
            observation = LEDGER.make_observation(asset, source, f"2026-06-0{index}T12:00:00Z")
            self.write_record(asset)
            self.write_record(source)
            self.write_record(observation)
            observations.append((asset, source, observation))
        first_revision_source = LEDGER.make_source(
            f"local:{assets[0]['rootId']}/{assets[0]['locator']}", b"revised source bytes"
        )
        first_revision = LEDGER.make_observation(assets[0], first_revision_source, "2026-06-03T12:00:00Z")
        self.write_record(first_revision_source)
        self.write_record(first_revision)

        page_one = "wiki/page-one.md"
        page_two = "wiki/page-two.md"
        for page in (page_one, page_two):
            (self.vault / page).write_bytes(b"---\ntype: Note\n---\nBody\r\n")

        def baseline(page, asset, observation, source, batch, time, outcome="updated"):
            return LEDGER.make_baseline(page, asset["id"], [{
                "observationId": observation["id"], "sourceId": source["id"],
                "contentHash": observation["contentHash"], "pageHash": "sha256:" + "a" * 64,
                "pageBatchId": batch, "reconciledAt": time, "outcome": outcome,
            }])

        first, second = observations
        left = baseline(page_one, first[0], first[2], first[1], "batch-one", "2026-06-03T12:00:00Z")
        left["history"].append({**left["history"][0], "observationId": first_revision["id"],
                                "sourceId": first_revision_source["id"], "contentHash": first_revision["contentHash"],
                                "pageBatchId": "batch-two", "reconciledAt": "2026-06-04T12:00:00Z",
                                "outcome": "no-change"})
        left["futureExtension"] = "keep"
        right = baseline(page_two, second[0], second[2], second[1], "batch-three", "2026-06-05T12:00:00Z")
        self.write_record(left)
        self.write_record(right)
        before = self.snapshot(self.vault)

        report = MODULE.build_report(self.vault, "2026-06-06")
        self.assertEqual(report["ledgerSummary"]["assets"], 2)
        self.assertEqual(report["ledgerSummary"]["observations"], 3)
        self.assertEqual(report["ledgerSummary"]["baselines"], 2)
        self.assertEqual(report["assets"][0]["producerExtension"], {"preserve": 1})
        self.assertEqual(report["observations"][0]["reportState"],
                         "recorded-observation-not-reconciliation-or-review")
        self.assertEqual(report["baselines"][0]["reportState"],
                         "declared-reconciliation-not-factual-verification")
        page_reports = {page["path"]: page for page in report["pages"]}
        self.assertEqual(len(page_reports[page_one]["baselines"]), 1)
        history = page_reports[page_one]["baselines"][0]
        self.assertEqual(len(history["history"]), 2)
        self.assertEqual(history["latest"]["outcome"], "no-change")
        self.assertEqual(history["state"], "declared-reconciliation-not-factual-verification")
        self.assertEqual(page_reports[page_two]["baselines"][0]["assetId"], second[0]["id"])
        self.assertIsNone(page_reports[page_one]["review"]["status"])
        self.assertEqual(self.snapshot(self.vault), before)

    def test_malformed_known_optional_record_is_listed_as_invalid_without_mutation(self):
        malformed = {"schema": LEDGER.SCHEMA, "kind": "observation", "id": "observation-" + "a" * 24,
                     "assetId": "asset-bad", "rootId": "root", "locator": "file.bin",
                     "sourceId": "source-" + "b" * 24, "contentHash": "not-a-hash",
                     "observedAt": "2026-06-01T12:00:00Z", "review": {"status": "unreviewed"},
                     "unknownExtension": "kept on disk"}
        invalid_path = self.vault / f"wiki/meta/evidence/{malformed['id']}.json"
        invalid_path.parent.mkdir(parents=True, exist_ok=True)
        invalid_path.write_text(json.dumps(malformed), encoding="utf-8")
        (self.vault / "wiki/valid.md").write_text("---\ntype: Note\n---\nOK\n", encoding="utf-8")
        before = self.snapshot(self.vault)
        report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertEqual(report["ledger"]["invalidCount"], 1)
        self.assertIn("unsafe-or-invalid-ledger-record", report["ledger"]["invalidRecords"][0]["issues"])
        self.assertEqual(report["ledgerSummary"]["observations"], 0)
        self.assertEqual(report["pages"][0]["structuralValidity"]["status"], "valid")
        self.assertEqual(self.snapshot(self.vault), before)

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

    def test_missing_markdown_parser_has_actionable_error_and_leaves_doctor_available(self):
        modules = self.root / "without-parser"
        modules.mkdir()
        (modules / "markdown_it.py").write_text(
            "raise ModuleNotFoundError('No module named markdown-it-py', name='markdown_it')\n",
            encoding="utf-8",
        )
        env = {**os.environ, "OBSIDIAN_AGENT_CONFIG": str(self.config), "PYTHONPATH": str(modules)}
        report = subprocess.run(
            [sys.executable, str(CLI), "evidence-report", "--vault", str(self.vault), "--json"],
            env=env, text=True, capture_output=True, timeout=3,
        )
        self.assertEqual(report.returncode, 1)
        self.assertEqual(report.stderr, "")
        report_error = json.loads(report.stdout)
        self.assertEqual(report_error["code"], "dependency-missing")
        self.assertIn("evidence-report requires markdown-it-py", report_error["error"])
        self.assertIn("pip install 'markdown-it-py>=4.2.0,<5'", report_error["error"])
        doctor = subprocess.run(
            [sys.executable, str(CLI), "doctor", "--vault", str(self.vault), "--json"],
            env=env, text=True, capture_output=True, timeout=3,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertEqual(json.loads(doctor.stdout)["vault"], str(self.vault.resolve()))
        search = subprocess.run(
            [sys.executable, str(CLI), "search", "local query", "--vault", str(self.vault), "--json"],
            env=env, text=True, capture_output=True, timeout=3,
        )
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertEqual(json.loads(search.stdout)["results"], [])

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
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(bad.stderr, "")
        bad_error = json.loads(bad.stdout)
        self.assertEqual(bad_error["code"], "command-error")
        self.assertIn("date", bad_error["error"])
        legacy = subprocess.run([sys.executable, str(CLI), "evidence-report", "--as-of", "not-a-date",
                                "--vault", str(self.vault)], env=env, text=True, capture_output=True)
        self.assertEqual(legacy.returncode, 1)
        self.assertEqual(legacy.stdout, "")
        self.assertIn("obsidian-second-brain: ", legacy.stderr)

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

    def test_singleton_and_raw_resource_locators_are_inspected_read_only(self):
        (self.vault / ".raw").mkdir()
        (self.vault / ".raw/source.txt").write_text("raw bytes", encoding="utf-8")
        (self.vault / "wiki/existing.txt").write_text("local bytes", encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("outside bytes", encoding="utf-8")
        (self.vault / "wiki/linked.txt").symlink_to(outside)
        page = self.vault / "wiki/sources.md"
        page.write_text("""---
type: source
resource: wiki/missing.txt
---
Source.
""", encoding="utf-8")
        bundle = self.vault / "wiki/bundle.md"
        bundle.write_text("""---
type: source
sources:
  - id: raw
    resource: .raw/source.txt
  - id: absent
    resource: .raw/absent.txt
  - id: external
    resource: https://example.test/source
  - id: linked
    resource: wiki/linked.txt
---
Bundle.
""", encoding="utf-8")
        before = self.snapshot(self.vault)
        report = MODULE.build_report(self.vault, "2026-06-01")
        pages = {item["path"]: item for item in report["pages"]}
        self.assertEqual(pages["wiki/sources.md"]["references"], [
            {"id": None, "resource": "wiki/missing.txt", "state": "missing"}
        ])
        self.assertEqual([ref["state"] for ref in pages["wiki/bundle.md"]["references"]], [
            "resolved-in-vault", "missing", "external-unchecked", "unsafe-or-unresolved"
        ])
        self.assertEqual(self.snapshot(self.vault), before)

    def test_ledger_identity_does_not_hide_missing_or_conflicting_resource(self):
        source = LEDGER.make_source("https://example.test/canonical", b"ledger snapshot", freshness="current")
        self.write_record(source)
        page = self.vault / "wiki/ledger-link.md"
        page.write_text(
            f"---\ntype: Note\nsources:\n  - id: {source['id']}\n"
            f"    resource: wiki/missing.md\n  - id: {source['id']}\n"
            f"    resource: https://example.test/other\n  - id: {source['id']}\n"
            "    resource: https://example.test/canonical\n---\nText\n", encoding="utf-8"
        )
        before = self.snapshot(self.vault)
        report = MODULE.build_report(self.vault, "2026-06-01")
        refs = report["pages"][0]["references"]
        self.assertEqual([ref["state"] for ref in refs], ["ledger-source-current"] * 3)
        self.assertEqual(refs[0]["resourceState"], "missing")
        self.assertIn("referenced-resource-missing", refs[0]["issues"])
        self.assertIn("ledger-resource-mismatch", refs[0]["issues"])
        self.assertIn("ledger-resource-mismatch", refs[1]["issues"])
        self.assertEqual(refs[2]["resourceState"], "external-unchecked")
        self.assertNotIn("issues", refs[2])
        self.assertIn("referenced-resource-missing", report["pages"][0]["issues"])
        self.assertIn("ledger-resource-mismatch", report["pages"][0]["issues"])
        self.assertEqual(self.snapshot(self.vault), before)

    def test_citations_ignore_frontmatter_and_code_but_count_prose_and_definitions(self):
        page = self.vault / "wiki/footnotes.md"
        page.write_text(
            "---\ntype: Note\nexample: '[^frontmatter]'\n---\n"
            "Inline `[^inline]` and ``code `[^double-inline]` `` examples.\n"
            "\\[^escaped] and \\\\[^double-escaped]. A real claim.[^real]\n"
            "```markdown\nExample [^fenced]\n```\n~~~text\n[^tilde]\n~~~\n"
            "    [^indented]\n\n[^defined]: Footnote definition text.\n", encoding="utf-8"
        )
        report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertEqual(report["pages"][0]["citations"], [
            {"id": "defined", "state": "missing-source-entry"},
            {"id": "double-escaped", "state": "missing-source-entry"},
            {"id": "real", "state": "missing-source-entry"},
        ])
        self.assertIn("missing-citation-source-entry", report["pages"][0]["issues"])

    def test_unclosed_list_fence_does_not_hide_citation_after_list_ends(self):
        page = self.vault / "wiki/unclosed-list-fence.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "- ~~~markdown\n"
            "  [^literal]\n"
            "\n"
            "Real claim.[^real]\n",
            encoding="utf-8",
        )
        expected = [{"id": "real", "state": "missing-source-entry"}]
        before = self.snapshot(self.vault)
        report = MODULE.build_report(self.vault, "2026-06-01")
        item = next(page for page in report["pages"] if page["path"] == "wiki/unclosed-list-fence.md")
        self.assertEqual(item["citations"], expected)
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_item = next(page for page in json.loads(result.stdout)["pages"]
                        if page["path"] == "wiki/unclosed-list-fence.md")
        self.assertEqual(cli_item["citations"], expected)
        self.assertEqual(self.snapshot(self.vault), before)

    def test_emphasis_like_footnote_identifiers_survive_parser_tokenization(self):
        page = self.vault / "wiki/emphasis-like-citation.md"
        page.write_text(
            "---\ntype: Note\n---\nReal claim.[^source-_draft_]\n",
            encoding="utf-8",
        )
        controls = self.vault / "wiki/citation-controls.md"
        controls.write_text(
            "---\ntype: Note\nsources:\n  - id: defined-_draft_\n"
            "    resource: https://example.test/source\n---\n"
            "A claim.[^missing] and [^defined-_draft_].\n"
            "Escaped \\[^escaped-_draft_] and inline `[^code-_draft_]`.\n",
            encoding="utf-8",
        )
        before = self.snapshot(self.vault)
        expected = [{"id": "source-_draft_", "state": "missing-source-entry"}]
        control_expected = [
            {"id": "defined-_draft_", "state": "source-entry-present"},
            {"id": "missing", "state": "missing-source-entry"},
        ]
        report = MODULE.build_report(self.vault, "2026-06-01")
        items = {item["path"]: item for item in report["pages"]}
        self.assertEqual(items["wiki/emphasis-like-citation.md"]["citations"], expected)
        self.assertIn("missing-citation-source-entry", items["wiki/emphasis-like-citation.md"]["issues"])
        self.assertEqual(items["wiki/citation-controls.md"]["citations"], control_expected)
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_items = {item["path"]: item for item in json.loads(result.stdout)["pages"]}
        self.assertEqual(cli_items["wiki/emphasis-like-citation.md"]["citations"], expected)
        self.assertIn("missing-citation-source-entry", cli_items["wiki/emphasis-like-citation.md"]["issues"])
        self.assertEqual(cli_items["wiki/citation-controls.md"]["citations"], control_expected)
        self.assertEqual(self.snapshot(self.vault), before)

    def test_container_footnote_definitions_preserve_original_citation_labels(self):
        pages = (
            ("blockquote-definition", "> [^note]: https://example.test/source\n"),
            ("list-definition", "- [^note]: https://example.test/source\n"),
        )
        expected = [{"id": "note", "state": "source-entry-present"}]
        for name, definition in pages:
            with self.subTest(name=name):
                page = self.vault / "wiki" / f"{name}.md"
                page.write_text(
                    "---\ntype: Note\nsources:\n  - id: note\n"
                    "    resource: https://example.test/source\n---\n"
                    "Claim.[^note]\n\n" + definition,
                    encoding="utf-8",
                )
                before = self.snapshot(self.vault)
                report = MODULE.build_report(self.vault, "2026-06-01")
                item = next(page for page in report["pages"] if page["path"] == f"wiki/{name}.md")
                self.assertEqual(item["citations"], expected)
                self.assertNotIn("missing-citation-source-entry", item["issues"])
                result = self.cli_report()
                self.assertEqual(result.returncode, 0, result.stderr)
                cli_item = next(page for page in json.loads(result.stdout)["pages"]
                                if page["path"] == f"wiki/{name}.md")
                self.assertEqual(cli_item["citations"], expected)
                self.assertNotIn("missing-citation-source-entry", cli_item["issues"])
                self.assertEqual(self.snapshot(self.vault), before)

    def test_unclosed_list_fence_does_not_hide_later_citation(self):
        page = self.vault / "wiki/unclosed-list-fence-no-blank.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "- ~~~markdown\n"
            "  [^literal]\n"
            "\nReal claim.[^real]\n",
            encoding="utf-8",
        )
        expected = [{"id": "real", "state": "missing-source-entry"}]
        before = self.snapshot(self.vault)
        report = MODULE.build_report(self.vault, "2026-06-01")
        item = next(page for page in report["pages"] if page["path"] == "wiki/unclosed-list-fence-no-blank.md")
        self.assertEqual(item["citations"], expected)
        self.assertIn("missing-citation-source-entry", item["issues"])
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_item = next(page for page in json.loads(result.stdout)["pages"]
                        if page["path"] == "wiki/unclosed-list-fence-no-blank.md")
        self.assertEqual(cli_item["citations"], expected)
        self.assertIn("missing-citation-source-entry", cli_item["issues"])
        self.assertEqual(self.snapshot(self.vault), before)

    def test_list_item_fence_example_does_not_hide_later_citation(self):
        page = self.vault / "wiki/list-fence.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "- ~~~markdown\n"
            "  [^literal]\n"
            "  ~~~\n\n"
            "Real claim.[^real]\n",
            encoding="utf-8",
        )
        report = MODULE.build_report(self.vault, "2026-06-01")
        expected = [{"id": "real", "state": "missing-source-entry"}]
        self.assertEqual(report["pages"][0]["citations"], expected)
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pages"][0]["citations"], expected)

    def test_escaped_backticks_do_not_mask_citations_but_even_backslashes_do(self):
        page = self.vault / "wiki/escaped-backticks.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "Escaped \\` marker. Claim.[^real] Another \\` marker.\n"
            "Even \\\\`[^literal]\\\\` delimiters.\n",
            encoding="utf-8",
        )
        report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertEqual(report["pages"][0]["citations"], [
            {"id": "real", "state": "missing-source-entry"}
        ])

    def test_inline_code_closing_backslash_does_not_expose_literal_footnote(self):
        page = self.vault / "wiki/closing-backslash.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "Example `[^literal]\\`. Real claim.[^real]\n",
            encoding="utf-8",
        )
        expected = [{"id": "real", "state": "missing-source-entry"}]
        report = MODULE.build_report(self.vault, "2026-06-01")
        item = next(page for page in report["pages"] if page["path"] == "wiki/closing-backslash.md")
        self.assertEqual(item["citations"], expected)
        self.assertNotIn("literal", {citation["id"] for citation in item["citations"]})
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_item = next(page for page in json.loads(result.stdout)["pages"]
                        if page["path"] == "wiki/closing-backslash.md")
        self.assertEqual(cli_item["citations"], expected)

    def test_unmatched_inline_backticks_do_not_cross_paragraph_boundaries(self):
        page = self.vault / "wiki/separate-paragraphs.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "An unmatched ` marker.\n\nReal claim.[^real]\n\nAnother ` marker.\n",
            encoding="utf-8",
        )
        expected = [{"id": "real", "state": "missing-source-entry"}]
        report = MODULE.build_report(self.vault, "2026-06-01")
        item = next(page for page in report["pages"] if page["path"] == "wiki/separate-paragraphs.md")
        self.assertEqual(item["citations"], expected)
        self.assertIn("missing-citation-source-entry", item["issues"])
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_item = next(page for page in json.loads(result.stdout)["pages"]
                        if page["path"] == "wiki/separate-paragraphs.md")
        self.assertEqual(cli_item["citations"], expected)
        self.assertIn("missing-citation-source-entry", cli_item["issues"])

    def test_indented_paragraph_continuation_is_prose_not_code(self):
        page = self.vault / "wiki/indented-paragraph.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "A factual paragraph\n"
            "    continues here.[^real]\n",
            encoding="utf-8",
        )
        expected = [{"id": "real", "state": "missing-source-entry"}]
        report = MODULE.build_report(self.vault, "2026-06-01")
        item = next(page for page in report["pages"] if page["path"] == "wiki/indented-paragraph.md")
        self.assertEqual(item["citations"], expected)
        self.assertIn("missing-citation-source-entry", item["issues"])
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_item = next(page for page in json.loads(result.stdout)["pages"]
                        if page["path"] == "wiki/indented-paragraph.md")
        self.assertEqual(cli_item["citations"], expected)
        self.assertIn("missing-citation-source-entry", cli_item["issues"])

    def test_blockquote_fence_ends_when_its_container_ends(self):
        page = self.vault / "wiki/blockquote-fence-boundary.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "> ~~~markdown\n"
            "> [^literal]\n"
            "\n"
            "Real claim.[^real]\n",
            encoding="utf-8",
        )
        expected = [{"id": "real", "state": "missing-source-entry"}]
        report = MODULE.build_report(self.vault, "2026-06-01")
        item = next(page for page in report["pages"] if page["path"] == "wiki/blockquote-fence-boundary.md")
        self.assertEqual(item["citations"], expected)
        self.assertIn("missing-citation-source-entry", item["issues"])
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_item = next(page for page in json.loads(result.stdout)["pages"]
                        if page["path"] == "wiki/blockquote-fence-boundary.md")
        self.assertEqual(cli_item["citations"], expected)
        self.assertIn("missing-citation-source-entry", cli_item["issues"])

    def test_citation_code_masking_handles_multiline_spans_and_blockquote_fences(self):
        page = self.vault / "wiki/code-examples.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "Example `literal\n[^multiline]` text.\n"
            "> ~~~markdown\n> [^blockquote]\n> ~~~\n",
            encoding="utf-8",
        )
        before = self.snapshot(self.vault)
        report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertEqual(report["pages"][0]["citations"], [])
        self.assertNotIn("missing-citation-source-entry", report["pages"][0]["issues"])
        result = self.cli_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pages"][0]["citations"], [])
        self.assertEqual(self.snapshot(self.vault), before)

    def test_list_item_continuation_is_prose_but_top_level_indented_code_is_not(self):
        page = self.vault / "wiki/list-continuation.md"
        page.write_text(
            "---\ntype: Note\n---\n"
            "1. Item\n\n"
            "    Continued factual claim.[^real]\n"
            "\nOutside the list.\n\n    [^code-example]\n",
            encoding="utf-8",
        )
        report = MODULE.build_report(self.vault, "2026-06-01")
        self.assertEqual(report["pages"][0]["citations"], [
            {"id": "real", "state": "missing-source-entry"}
        ])
        self.assertIn("missing-citation-source-entry", report["pages"][0]["issues"])

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
