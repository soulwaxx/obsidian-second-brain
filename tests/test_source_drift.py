from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source

DRIFT = load_source("source_drift_test", str(ROOT / "scripts/source_drift.py"))
LEDGER = load_source("source_drift_ledger_test", str(ROOT / "scripts/evidence_ledger.py"))
CLI = ROOT / "scripts/obsidian-second-brain.py"


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class SourceDriftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir()))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        self.source = self.base / "source"
        self.source.mkdir()
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")

    def save(self, record):
        path = self.vault / LEDGER.record_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return record

    def setup_asset(self, content=b"v1", locator="notes/a.txt", page="wiki/claim.md", claims=None):
        target = self.source / locator
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        asset = LEDGER.make_asset("asset-one", "root-notes", locator)
        source = LEDGER.make_source(DRIFT.BASELINES._portable_source_locator("root-notes", locator), content)
        observation = LEDGER.make_observation(asset, source, "2026-01-01T00:00:00Z")
        self.save(asset); self.save(source); self.save(observation)
        page_file = self.vault / page
        page_file.parent.mkdir(parents=True, exist_ok=True)
        page_file.write_text("---\ntype: Note\nclaims: " + json.dumps(claims or []) + "\nsources: []\n---\nClaim text.\n", encoding="utf-8")
        baseline = LEDGER.make_baseline(page, asset["id"], [{
            "observationId": observation["id"], "sourceId": source["id"], "contentHash": source["contentHash"],
            "pageHash": digest(page_file.read_bytes()), "pageBatchId": "batch-one",
            "reconciledAt": "2026-01-01T00:00:00Z", "outcome": "updated",
        }])
        self.save(baseline)
        return asset, source, observation, baseline, page_file

    def run_check(self):
        return DRIFT.check(self.vault, "root-notes", self.source)

    def snapshot(self):
        state = {}
        for root in (self.vault, self.source):
            for path in [root, *root.rglob("*")]:
                info = path.lstat()
                state[str(path)] = (info.st_mode, info.st_mtime_ns,
                                    path.read_bytes() if path.is_file() and not path.is_symlink() else os.readlink(path) if path.is_symlink() else None)
        state[str(self.config)] = (self.config.read_bytes(), self.config.stat().st_mtime_ns)
        return state

    def test_detects_dirty_uncommitted_source_drift_and_preserves_read_only_inputs(self):
        asset, _source, _observation, _baseline, _page = self.setup_asset()
        source_path = self.source / "notes/a.txt"
        source_path.write_bytes(b"v2")
        before_check = self.snapshot()
        dirty = self.run_check()
        self.assertEqual(self.snapshot(), before_check)
        self.assertEqual(dirty["assets"][0]["state"], "changed")
        self.assertEqual(dirty["pages"][0]["state"], "affected")
        self.assertTrue(dirty["complete"])
        self.assertEqual(dirty["snapshotValidity"], "stable")
        self.assertEqual(dirty["assets"][0]["id"], asset["id"])

    def test_page_baseline_is_latest_appended_history_and_page_hash_is_separate(self):
        asset, source, observation, baseline, page = self.setup_asset()
        path = self.source / "notes/a.txt"
        path.write_bytes(b"v2")
        source2 = self.save(LEDGER.make_source("local:root-notes/notes/a.txt", b"v2"))
        observation2 = self.save(LEDGER.make_observation(asset, source2, "2026-01-02T00:00:00Z"))
        baseline["history"].append({"observationId": observation2["id"], "sourceId": source2["id"],
            "contentHash": source2["contentHash"], "pageHash": digest(page.read_bytes()),
            "pageBatchId": "batch-two", "reconciledAt": "2026-01-02T00:00:00Z", "outcome": "no-change"})
        # A no-change history is valid only for equal snapshot metadata; latest v2 is now current.
        self.save(baseline)
        second_page = self.vault / "wiki/old-baseline.md"
        second_page.write_text("---\ntype: Note\n---\nOld baseline page.\n", encoding="utf-8")
        old_baseline = LEDGER.make_baseline("wiki/old-baseline.md", asset["id"], [{
            "observationId": observation["id"], "sourceId": source["id"], "contentHash": source["contentHash"],
            "pageHash": digest(second_page.read_bytes()), "pageBatchId": "batch-old",
            "reconciledAt": "2026-01-01T00:00:00Z", "outcome": "updated"}])
        self.save(old_baseline)
        result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "changed")
        pages = {item["path"]: item for item in result["pages"]}
        self.assertEqual(pages["wiki/claim.md"]["state"], "current")
        self.assertEqual(pages["wiki/old-baseline.md"]["state"], "affected")
        page.write_text(page.read_text() + "edited\n", encoding="utf-8")
        result = self.run_check()
        self.assertEqual(result["pages"][0]["state"], "page-changed")

    def test_other_root_and_external_provenance_sort_without_being_read(self):
        self.setup_asset()
        asset = LEDGER.make_asset("asset-other", "root-other", "private/item.txt")
        source = LEDGER.make_source("local:root-other/private/item.txt", b"outside")
        observation = LEDGER.make_observation(asset, source, "2026-01-03T00:00:00Z")
        external = LEDGER.make_source("https://example.test/evidence", b"external")
        self.save(asset); self.save(source); self.save(observation); self.save(external)
        result = self.run_check()
        self.assertTrue(result["complete"])
        states = {item["id"]: item["state"] for item in result["assets"]}
        self.assertEqual(states["asset-other"], "unchecked")
        self.assertEqual(states[source["id"]], "unchecked")

    def test_missing_baseline_asset_is_structured_and_keeps_safe_peer_results(self):
        self.setup_asset()
        peer_asset = LEDGER.make_asset("asset-peer", "root-notes", "notes/peer.txt")
        (self.source / "notes/peer.txt").write_bytes(b"peer")
        peer_source = LEDGER.make_source(DRIFT.BASELINES._portable_source_locator("root-notes", "notes/peer.txt"), b"peer")
        peer_observation = LEDGER.make_observation(peer_asset, peer_source, "2026-01-03T00:00:00Z")
        self.save(peer_asset); self.save(peer_source); self.save(peer_observation)
        (self.vault / "wiki/meta/evidence/asset-one.json").unlink()
        result = self.run_check()
        peer = next(item for item in result["assets"] if item["id"] == peer_asset["id"])
        self.assertEqual(peer["state"], "unchanged")
        page = next(item for item in result["pages"] if item["path"] == "wiki/claim.md")
        self.assertEqual(page["baselines"][0]["state"], "unsafe-unresolvable")
        self.assertFalse(result["complete"])

    def test_direct_source_and_claim_refs_report_drift_without_baseline(self):
        asset, source, _observation, _baseline, _page = self.setup_asset()
        claim = self.save(LEDGER.make_claim("claim-direct", "Synthetic claim text", support=[source["id"]]))
        direct = self.vault / "wiki/direct.md"
        direct.write_text("---\ntype: Note\nclaims: [claim-direct]\nsources:\n  - id: " + source["id"] + "\n---\nText\n", encoding="utf-8")
        (self.source / asset["locator"]).write_bytes(b"v2")
        result = self.run_check()
        page = next(row for row in result["pages"] if row["path"] == "wiki/direct.md")
        self.assertEqual(page["state"], "affected")
        self.assertIn("direct-source-changed", page["reviewReasons"])
        self.assertEqual(page["sourceReferences"][0]["state"], "changed")
        claim_row = next(row for row in result["claims"] if row["id"] == claim["id"])
        self.assertTrue(claim_row["reviewRequired"])
        self.assertEqual(claim_row["evidence"][0]["state"], "changed")
        self.assertEqual(page["claims"], [claim["id"]])

    def test_invalid_source_reference_resources_are_incomplete(self):
        _asset, source, *_ = self.setup_asset()
        malformed = self.vault / "wiki/bad-resource.md"
        malformed.write_text("---\ntype: Note\nsources:\n  - id: " + source["id"] + "\n    resource: []\n---\nText\n", encoding="utf-8")
        mismatch = self.vault / "wiki/mismatched-resource.md"
        mismatch.write_text("---\ntype: Note\nsources:\n  - id: " + source["id"] + "\n    resource: https://example.test/wrong\n---\nText\n", encoding="utf-8")
        result = self.run_check()
        states = {page["path"]: page for page in result["pages"]}
        self.assertEqual(states["wiki/bad-resource.md"]["sourceReferences"][0]["state"], "invalid-reference")
        self.assertEqual(states["wiki/mismatched-resource.md"]["sourceReferences"][0]["state"], "invalid-reference")
        self.assertTrue(any(issue["reason"] == "invalid-page-source-resource" for issue in result["issues"]))
        self.assertTrue(any(issue["reason"] == "page-source-resource-mismatch" for issue in result["issues"]))
        self.assertFalse(result["complete"])

    def test_missing_claim_source_is_incomplete_without_a_baseline(self):
        self.setup_asset()
        missing = "source-" + "f" * 24
        claim = self.save(LEDGER.make_claim("claim-missing", "Synthetic claim", support=[missing]))
        page = self.vault / "wiki/missing-source.md"
        page.write_text("---\ntype: Note\nclaims: [claim-missing]\n---\nText\n", encoding="utf-8")
        before = self.snapshot()
        result = self.run_check()
        self.assertEqual(self.snapshot(), before)
        row = next(item for item in result["claims"] if item["id"] == claim["id"])
        self.assertEqual(row["evidence"][0]["state"], "missing-source-record")
        self.assertFalse(result["complete"])
        self.assertTrue(any(issue["reason"] == "missing-claim-source" for issue in result["issues"]))

    def test_explicit_source_and_claim_refs_are_discovered_without_baselines(self):
        asset, source, _observation, _baseline, _page = self.setup_asset()
        claim = self.save(LEDGER.make_claim("claim-explicit", "Synthetic claim", support=[source["id"]]))
        page = self.vault / "wiki/unbaselined.md"
        page.write_text("---\ntype: Note\nclaims: [claim-explicit]\nsources:\n  - id: " + source["id"] + "\n---\nText\n", encoding="utf-8")
        result = self.run_check()
        page_result = next(item for item in result["pages"] if item["path"] == "wiki/unbaselined.md")
        self.assertEqual(page_result["claims"], [claim["id"]])
        self.assertEqual(page_result["sourceReferences"][0]["id"], source["id"])
        self.assertNotIn("secret", json.dumps(result))

    def test_broken_observation_and_baseline_edges_do_not_hide_unrelated_assets(self):
        self.setup_asset()
        bad_asset = LEDGER.make_asset("asset-broken", "root-notes", "notes/broken.txt")
        (self.source / "notes/broken.txt").write_bytes(b"broken")
        good_source = LEDGER.make_source(DRIFT.BASELINES._portable_source_locator("root-notes", "notes/broken.txt"), b"broken")
        bad_observation = LEDGER.make_observation(bad_asset, good_source, "2026-01-02T00:00:00Z")
        bad_observation["locator"] = "notes/other.txt"
        bad_observation["id"] = LEDGER._identity_id("observation-", LEDGER._observation_material(bad_observation))
        self.save(bad_asset); self.save(good_source); self.save(bad_observation)
        # This entry is shape-valid but points at no observation; the unrelated asset remains inspectable.
        page = self.vault / "wiki/broken.md"
        page.write_text("---\ntype: Note\n---\nBroken baseline fixture.\n", encoding="utf-8")
        broken_baseline = LEDGER.make_baseline("wiki/broken.md", bad_asset["id"], [{
            "observationId": "observation-" + "0" * 24, "sourceId": good_source["id"],
            "contentHash": good_source["contentHash"], "pageHash": digest(page.read_bytes()),
            "pageBatchId": "batch-broken", "reconciledAt": "2026-01-02T00:00:00Z", "outcome": "updated"}])
        self.save(broken_baseline)
        result = self.run_check()
        rows = {item["id"]: item for item in result["assets"]}
        self.assertEqual(rows["asset-one"]["state"], "unchanged")
        self.assertEqual(rows["asset-broken"]["state"], "unsafe-unresolvable")
        page_row = next(item for item in result["pages"] if item["path"] == "wiki/broken.md")
        self.assertEqual(page_row["baselines"][0]["state"], "unsafe-unresolvable")

    def test_baseline_history_hash_must_match_observation_and_source(self):
        asset, source, _observation, baseline, _page = self.setup_asset()
        baseline["history"][-1]["contentHash"] = digest(b"different recorded hash")
        path = self.vault / LEDGER.record_path(baseline)
        path.write_text(json.dumps(baseline), encoding="utf-8")
        result = self.run_check()
        page = next(item for item in result["pages"] if item["path"] == "wiki/claim.md")
        self.assertEqual(page["baselines"][0]["state"], "unsafe-unresolvable")
        self.assertEqual(next(item for item in result["assets"] if item["id"] == asset["id"])["state"], "unchanged")
        self.assertFalse(result["complete"])

    def test_baseline_asset_does_not_fabricate_claim_links(self):
        asset, source, _observation, _baseline, _page = self.setup_asset()
        self.save(LEDGER.make_claim("claim-not-linked", "Unlinked claim", support=[source["id"]]))
        (self.source / "notes/a.txt").write_bytes(b"v2")
        result = self.run_check()
        page = next(item for item in result["pages"] if item["path"] == "wiki/claim.md")
        claim = next(item for item in result["claims"] if item["id"] == "claim-not-linked")
        self.assertEqual(page["claims"], [])
        self.assertEqual(claim["pages"], [])
        self.assertTrue(claim["reviewRequired"])
        self.assertNotIn(claim["id"], page["claims"])

    def test_other_root_baseline_is_unchecked_not_broken(self):
        self.setup_asset()
        asset = LEDGER.make_asset("asset-other", "root-other", "folder/other.txt")
        source = LEDGER.make_source("local:root-other/folder/other.txt", b"other")
        observation = LEDGER.make_observation(asset, source, "2026-01-03T00:00:00Z")
        self.save(asset); self.save(source); self.save(observation)
        page = self.vault / "wiki/other.md"
        page.write_text("---\ntype: Note\n---\nOther\n", encoding="utf-8")
        baseline = LEDGER.make_baseline("wiki/other.md", asset["id"], [{"observationId": observation["id"],
            "sourceId": source["id"], "contentHash": source["contentHash"], "pageHash": digest(page.read_bytes()),
            "pageBatchId": "batch-other", "reconciledAt": "2026-01-03T00:00:00Z", "outcome": "updated"}])
        self.save(baseline)
        result = self.run_check()
        page_result = next(item for item in result["pages"] if item["path"] == "wiki/other.md")
        self.assertEqual(page_result["baselines"][0]["state"], "unchecked")
        self.assertEqual(page_result["baselines"][0]["reason"], "unselected-root")

    def test_many_sources_one_claim_and_one_source_many_claims_preserve_relations(self):
        asset, source, _observation, _baseline, _page = self.setup_asset()
        second_path = self.source / "notes/b.txt"
        second_path.write_bytes(b"second")
        second_asset = LEDGER.make_asset("asset-two", "root-notes", "notes/b.txt")
        second_source = LEDGER.make_source("local:root-notes/notes/b.txt", b"second")
        second_observation = LEDGER.make_observation(second_asset, second_source, "2026-01-02T00:00:00Z")
        self.save(second_asset); self.save(second_source); self.save(second_observation)
        claim = LEDGER.make_claim("claim-many", "Synthetic statement", support=[source["id"], second_source["id"]])
        claim_two = LEDGER.make_claim("claim-one", "Another synthetic statement", contradictions=[source["id"]])
        self.save(claim); self.save(claim_two)
        second_path.write_bytes(b"second v2")
        page = self.vault / "wiki/links.md"
        page.write_text("---\ntype: Note\nclaims: [claim-many, claim-one]\nsources:\n  - id: " + source["id"] + "\n  - id: " + second_source["id"] + "\n---\nText\n", encoding="utf-8")
        result = self.run_check()
        page_result = next(row for row in result["pages"] if row["path"] == "wiki/links.md")
        self.assertEqual(page_result["claims"], ["claim-many", "claim-one"])
        rows = {row["id"]: row for row in result["claims"]}
        self.assertEqual({e["sourceId"] for e in rows["claim-many"]["evidence"]}, {source["id"], second_source["id"]})
        self.assertIn("contradictions", [e["relation"] for e in rows["claim-one"]["evidence"]])
        self.assertTrue(rows["claim-many"]["reviewRequired"])
        changed = next(e for e in rows["claim-many"]["evidence"] if e["sourceId"] == second_source["id"])
        self.assertEqual((changed["relation"], changed["state"], changed["reason"]),
                         ("support", "changed", "recorded-source-snapshot-differs"))
        self.assertFalse(rows["claim-one"]["reviewRequired"])

    def test_unobserved_source_only_records_do_not_fail_selected_asset_check(self):
        asset, *_ = self.setup_asset()
        root_only = self.save(LEDGER.make_source("local:root-notes/legacy.txt", b"legacy"))
        other_root = self.save(LEDGER.make_source("local:root-other/other.txt", b"other"))
        external = self.save(LEDGER.make_source("https://example.test/reference", b"external"))
        result = self.run_check()
        self.assertTrue(result["complete"])
        asset_row = next(row for row in result["assets"] if row["id"] == asset["id"])
        self.assertEqual(asset_row["state"], "unchanged")
        rows = {row["id"]: row for row in result["assets"]}
        for source in (root_only, other_root, external):
            self.assertEqual(rows[source["id"]]["state"], "unchecked")
        self.assertFalse((self.source / "legacy.txt").exists())
        self.assertFalse((self.source / "other.txt").exists())

    def test_external_legacy_refs_and_unrecorded_files_remain_unchecked(self):
        self.setup_asset()
        (self.source / "notes/not-approved.txt").write_bytes(b"must not discover")
        page = self.vault / "wiki/external.md"
        page.write_text("---\ntype: Note\nresource: https://example.test/item\nsources: [notes/legacy.txt]\n---\nText\n", encoding="utf-8")
        result = self.run_check()
        page_row = next(item for item in result["pages"] if item["path"] == "wiki/external.md")
        self.assertEqual([ref["state"] for ref in page_row["sourceReferences"]],
                         ["external-or-legacy-unchecked", "external-or-legacy-unchecked"])
        self.assertTrue(result["complete"])
        self.assertNotIn("not-approved.txt", json.dumps(result))

    def test_invalid_source_identity_propagates_to_explicit_page_and_claim_edges(self):
        self.setup_asset()
        source = LEDGER.make_source("local:root-notes/not-inspected.txt", b"SYNTHETIC_SOURCE_CONTENT")
        forged_id = "source-" + "f" * 24
        source["id"] = forged_id
        self.save(source)
        support = self.save(LEDGER.make_claim("claim-forged-support", "SYNTHETIC_CLAIM_STATEMENT support",
                                              support=[forged_id]))
        contradiction = self.save(LEDGER.make_claim("claim-forged-contradiction", "SYNTHETIC_CLAIM_STATEMENT contradiction",
                                                    contradictions=[forged_id]))
        page = self.vault / "wiki/forged-source.md"
        page.write_text("---\ntype: Note\nclaims: [claim-forged-support, claim-forged-contradiction]\nsources:\n  - id: " + forged_id + "\n---\nSynthetic page\n",
                        encoding="utf-8")
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)
        result = subprocess.run([sys.executable, str(CLI), "source-check", "--root-id", "root-notes",
                                 "--source-root", str(self.source), "--json"], cwd=self.base,
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        page_row = next(row for row in report["pages"] if row["path"] == "wiki/forged-source.md")
        self.assertEqual(page_row["state"], "unsafe-unresolvable")
        self.assertEqual(page_row["sourceReferences"][0]["state"], "unsafe-unresolvable")
        self.assertEqual(page_row["sourceReferences"][0]["reason"], "invalid-source-identity")
        self.assertIn("invalid-source-identity", page_row["reviewReasons"])
        claims = {row["id"]: row for row in report["claims"]}
        self.assertEqual(claims[support["id"]]["evidence"][0]["reason"], "invalid-source-identity")
        self.assertEqual(claims[contradiction["id"]]["evidence"][0]["reason"], "invalid-source-identity")
        self.assertEqual(claims[support["id"]]["pages"], ["wiki/forged-source.md"])
        self.assertEqual(claims[contradiction["id"]]["pages"], ["wiki/forged-source.md"])
        self.assertEqual(next(row for row in report["pages"] if row["path"] == "wiki/claim.md")["state"], "current")
        self.assertEqual(next(row for row in report["assets"] if row["id"] == "asset-one")["state"], "unchanged")
        self.assertNotIn("SYNTHETIC_SOURCE_CONTENT", result.stdout + result.stderr)
        self.assertNotIn("SYNTHETIC_CLAIM_STATEMENT", result.stdout + result.stderr)

    def test_ledger_valid_claim_identifiers_are_accepted_in_page_references(self):
        _asset, source, *_ = self.setup_asset()
        identities = ["claim-", "claim-_test", "claim-.dot", "claim--dash", "claim-" + "a" * 122]
        for identity in identities:
            self.save(LEDGER.make_claim(identity, "Synthetic schema-valid claim",
                                        support=[source["id"]] if identity == "claim-_test" else []))
        page = self.vault / "wiki/schema-valid-claims.md"
        page.write_text("---\ntype: Note\nclaims: " + json.dumps(identities) + "\n---\nSynthetic page\n",
                        encoding="utf-8")
        invalid = self.vault / "wiki/invalid-claim-reference.md"
        invalid.write_text("---\ntype: Note\nclaims: [not-a-claim]\n---\nSynthetic page\n", encoding="utf-8")
        (self.source / "notes/a.txt").write_bytes(b"v2")
        result = self.run_check()
        pages = {row["path"]: row for row in result["pages"]}
        self.assertEqual(pages["wiki/schema-valid-claims.md"]["claims"], sorted(identities))
        self.assertEqual(pages["wiki/schema-valid-claims.md"]["state"], "affected")
        linked_claim = next(row for row in result["claims"] if row["id"] == "claim-_test")
        self.assertEqual(linked_claim["pages"], ["wiki/schema-valid-claims.md"])
        self.assertTrue(linked_claim["reviewRequired"])
        self.assertEqual(linked_claim["evidence"][0]["state"], "changed")
        self.assertTrue(any(issue.get("path") == "wiki/invalid-claim-reference.md"
                            and issue["reason"] == "invalid-page-claim-reference" for issue in result["issues"]))
        self.assertFalse(result["complete"])

    def test_portable_source_locator_uses_encoded_path_segments(self):
        _asset, source, _observation, _baseline, _page = self.setup_asset(locator="notes/a b#1.txt")
        self.assertEqual(source["locator"], "local:root-notes/notes/a%20b%231.txt")
        result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unchanged")

    def test_moved_locator_with_equal_bytes_is_not_content_drift(self):
        asset, _source, _observation, _baseline, _page = self.setup_asset(content=b"same")
        old = self.source / "notes/a.txt"
        new = self.source / "notes/renamed.txt"
        new.write_bytes(old.read_bytes())
        asset = LEDGER.make_asset(asset["id"], "root-notes", "notes/renamed.txt")
        self.save(asset)
        result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unchanged")
        self.assertEqual(result["assets"][0]["locator"], "notes/renamed.txt")

    def test_selected_asset_without_observation_is_explicitly_unchecked(self):
        self.setup_asset()
        self.save(LEDGER.make_asset("asset-unobserved", "root-notes", "notes/unobserved.txt"))
        result = self.run_check()
        row = next(item for item in result["assets"] if item["id"] == "asset-unobserved")
        self.assertEqual((row["state"], row["reason"]), ("unchecked", "no-recorded-observation"))
        self.assertFalse(result["complete"])

    def test_missing_source_and_unknown_root_are_not_redirected(self):
        self.setup_asset()
        (self.source / "notes/a.txt").unlink()
        before_check = self.snapshot()
        result = self.run_check()
        self.assertEqual(self.snapshot(), before_check)
        self.assertEqual(result["assets"][0]["state"], "missing")
        self.assertEqual(result["complete"], True)
        unselected = LEDGER.make_asset("asset-other", "root-other", "private/file.txt")
        self.save(unselected)
        result = self.run_check()
        other = next(item for item in result["assets"] if item["id"] == "asset-other")
        self.assertEqual((other["state"], other["reason"]), ("unchecked", "unselected-root"))
        self.assertTrue(result["complete"])

    def test_root_unavailable_permission_fifo_and_case_collision_states(self):
        self.setup_asset()
        missing_root = self.base / "unavailable"
        result = DRIFT.check(self.vault, "root-notes", missing_root)
        self.assertFalse(result["complete"])
        self.assertIn(result["assets"][0]["state"], {"operational-error", "unsafe-unresolvable"})
        with mock.patch.object(DRIFT.BASELINES, "_inspect_selected", side_effect=PermissionError("denied")):
            denied = self.run_check()
        self.assertEqual(denied["assets"][0]["state"], "operational-error")
        self.assertEqual(denied["assets"][0]["reason"], "permission-denied")
        fifo_root = self.base / "fifo-root"
        fifo_root.mkdir()
        try:
            os.mkfifo(fifo_root / "pipe")
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("FIFO unavailable")
        fifo_asset = LEDGER.make_asset("asset-fifo", "root-notes", "pipe")
        fifo_source = LEDGER.make_source("local:root-notes/pipe", b"fixture")
        self.save(fifo_asset); self.save(fifo_source)
        self.save(LEDGER.make_observation(fifo_asset, fifo_source, "2026-01-05T00:00:00Z"))
        fifo_result = DRIFT.check(self.vault, "root-notes", fifo_root)
        fifo_row = next(item for item in fifo_result["assets"] if item["id"] == "asset-fifo")
        self.assertEqual(fifo_row["state"], "unsafe-unresolvable")
        collision_root = self.base / "collision-root"
        collision_root.mkdir()
        (collision_root / "A").write_bytes(b"a")
        try:
            (collision_root / "a").write_bytes(b"b")
        except OSError:
            self.skipTest("case-distinct collision cannot be represented")
        if len(os.listdir(collision_root)) != 2:
            self.skipTest("filesystem normalizes case-distinct siblings")
        self.save(LEDGER.make_asset("asset-collision", "root-notes", "A"))
        collision_source = LEDGER.make_source("local:root-notes/A", b"a")
        collision_asset = LEDGER.make_asset("asset-collision", "root-notes", "A")
        self.save(collision_source)
        self.save(LEDGER.make_observation(collision_asset, collision_source, "2026-01-04T00:00:00Z"))
        collision = DRIFT.check(self.vault, "root-notes", collision_root)
        row = next(item for item in collision["assets"] if item["id"] == "asset-collision")
        self.assertEqual(row["state"], "unsafe-unresolvable")

    def test_symlink_and_traversal_are_safe_and_incomplete(self):
        self.setup_asset()
        original = self.source / "notes/a.txt"
        original.unlink()
        original.symlink_to(self.base / "outside")
        result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unsafe-unresolvable")
        self.assertFalse(result["complete"])
        root_alias = self.base / "root-alias"
        root_alias.symlink_to(self.source, target_is_directory=True)
        root_result = DRIFT.check(self.vault, "root-notes", root_alias)
        self.assertEqual(root_result["assets"][0]["state"], "unsafe-unresolvable")
        parent_alias = self.base / "parent-alias"
        parent_alias.symlink_to(self.base, target_is_directory=True)
        parent_result = DRIFT.check(self.vault, "root-notes", parent_alias / "source")
        self.assertEqual(parent_result["assets"][0]["state"], "unsafe-unresolvable")

    def test_source_hardlink_aliases_are_refused_by_drift_check(self):
        self.setup_asset()
        original = self.source / "notes/a.txt"
        try:
            os.link(original, self.source / "notes/alias.txt")
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("hardlinks unavailable")
        result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unsafe-unresolvable")
        self.assertEqual(result["assets"][0]["reason"], "unsafe-path")

    def test_non_nfc_selected_locator_is_refused(self):
        decomposed = "notes/e\u0301.txt"
        self.setup_asset(locator=decomposed)
        result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unsafe-unresolvable")
        self.assertEqual(result["assets"][0]["reason"], "unsafe-path")

    def test_source_check_cli_arguments_selection_and_config_failure(self):
        self.setup_asset()
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)
        def invoke(*args):
            return subprocess.run([sys.executable, str(CLI), "source-check", *args], cwd=self.base,
                                  env=env, text=True, capture_output=True)
        missing = invoke("--json")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("source-check requires --root-id", json.loads(missing.stdout)["error"])
        result = invoke("--root-id", "root-notes", "--source-root", str(self.source), "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema"], "okf-source-drift-v1")
        alternate = self.base / "alternate-vault"
        (alternate / "wiki").mkdir(parents=True)
        override = invoke("--root-id", "root-notes", "--source-root", str(self.source), "--vault", str(alternate), "--json")
        self.assertEqual(override.returncode, 0, override.stdout + override.stderr)
        self.assertEqual(json.loads(override.stdout)["coverage"]["selectedAssets"], 0)
        human = invoke("--root-id", "root-notes", "--source-root", str(self.source))
        self.assertEqual(human.returncode, 0)
        self.assertIn("Source check", human.stdout)
        self.assertIn("Coverage:", human.stdout)
        bad_record = self.vault / "wiki/meta/evidence/bad.json"
        bad_record.parent.mkdir(parents=True, exist_ok=True)
        bad_record.write_text("{}", encoding="utf-8")
        incomplete_human = invoke("--root-id", "root-notes", "--source-root", str(self.source))
        self.assertEqual(incomplete_human.returncode, 1)
        self.assertIn("Check issue:", incomplete_human.stdout)
        unsupported = invoke("--root-id", "root-notes", "--source-root", str(self.source), "--source", str(self.source / "notes/a.txt"), "--json")
        self.assertEqual(unsupported.returncode, 1)
        self.assertEqual(json.loads(unsupported.stdout)["code"], "source-check-error")
        self.config.write_text("[]", encoding="utf-8")
        invalid = invoke("--root-id", "root-notes", "--source-root", str(self.source), "--json")
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(json.loads(invalid.stdout)["code"], "invalid-config")

    def test_source_check_validates_frontmatter_without_leaking_field_values(self):
        self.setup_asset()
        fixtures = {
            "missing-type": "title: Missing type",
            "empty-type": "type: ''",
            "list-type": "type: []",
            "integer-type": "type: 17",
            "bad-optional": "type: Note\ntimestamp: DO_NOT_LEAK_VALIDATOR_VALUE",
            "type-only": "type: Note",
            "unknown-field": "type: Note\nunknown_extension: accepted",
            "legacy-source": "type: Note\nsources:\n  - id: fpa-handbook\n    resource: https://example.test/handbook",
        }
        for name, metadata in fixtures.items():
            page = self.vault / "wiki" / (name + ".md")
            page.write_text("---\n" + metadata + "\n---\nSynthetic body\n", encoding="utf-8")
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)
        result = subprocess.run([sys.executable, str(CLI), "source-check", "--root-id", "root-notes",
                                 "--source-root", str(self.source), "--json"], cwd=self.base,
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        invalid_paths = {"wiki/missing-type.md", "wiki/empty-type.md", "wiki/list-type.md",
                         "wiki/integer-type.md", "wiki/bad-optional.md"}
        self.assertEqual({issue["path"] for issue in report["issues"]
                          if issue["reason"] == "invalid-page-frontmatter"}, invalid_paths)
        page_rows = {page["path"]: page for page in report["pages"]}
        for path in invalid_paths:
            self.assertEqual(page_rows[path]["state"], "unsafe-unresolvable")
        self.assertEqual(page_rows["wiki/type-only.md"]["state"], "current")
        self.assertEqual(page_rows["wiki/unknown-field.md"]["state"], "current")
        self.assertEqual(page_rows["wiki/legacy-source.md"]["sourceReferences"][0]["state"],
                         "external-or-legacy-unchecked")
        self.assertNotIn("DO_NOT_LEAK_VALIDATOR_VALUE", result.stdout + result.stderr)

    def test_singleton_resource_does_not_hide_malformed_sources(self):
        self.setup_asset()
        for filename, sources in (("boolean", "true"), ("integer", "123"), ("mapping", "{}")):
            page = self.vault / "wiki" / (filename + "-sources.md")
            page.write_text("---\ntype: Note\nresource: https://example.test/item\nsources: " + sources + "\n---\nText\n",
                            encoding="utf-8")
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)
        result = subprocess.run([sys.executable, str(CLI), "source-check", "--root-id", "root-notes",
                                 "--source-root", str(self.source), "--json"], cwd=self.base,
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        invalid_pages = {issue["path"] for issue in report["issues"] if issue["reason"] == "invalid-page-sources"}
        self.assertEqual(invalid_pages, {"wiki/boolean-sources.md", "wiki/integer-sources.md", "wiki/mapping-sources.md"})
        for page in report["pages"]:
            if page["path"] in invalid_pages:
                self.assertTrue(any(ref["state"] == "external-or-legacy-unchecked" for ref in page["sourceReferences"]))

    def test_source_check_rejects_hardlinked_ledger_record(self):
        _asset, source, *_ = self.setup_asset(content=b"synthetic-private-source-content")
        self.save(LEDGER.make_claim("claim-private", "synthetic-private-claim-statement", support=[source["id"]]))
        record = self.vault / "wiki/meta/evidence/asset-one.json"
        outside = self.base / "outside-asset-record.json"
        outside.write_bytes(record.read_bytes())
        record.unlink()
        os.link(outside, record)
        self.assertEqual(record.stat().st_nlink, 2)
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)
        result = subprocess.run([sys.executable, str(CLI), "source-check", "--root-id", "root-notes",
                                 "--source-root", str(self.source), "--json"], cwd=self.base,
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        self.assertTrue(any(issue["state"] == "unsafe-unresolvable" for issue in report["issues"]))
        self.assertNotIn("synthetic-private-source-content", result.stdout + result.stderr)
        self.assertNotIn("synthetic-private-claim-statement", result.stdout + result.stderr)

    def test_final_ledger_hardlink_replacement_invalidates_snapshot(self):
        self.setup_asset()
        batch = load_source("source_drift_batch_hardlink_race_test", str(ROOT / "scripts/wiki_batch.py"))
        original_ledger = batch._ledger_files
        record = self.vault / "wiki/meta/evidence/asset-one.json"
        outside = self.base / "late-hardlinked-record.json"
        original_bytes = record.read_bytes()
        calls = 0

        def replace_after_initial_load(vault, *, reject_hardlinks=False):
            nonlocal calls
            result = original_ledger(vault, reject_hardlinks=reject_hardlinks)
            calls += 1
            if calls == 1:
                outside.write_bytes(original_bytes)
                record.unlink()
                os.link(outside, record)
            return result

        real_load = load_source
        try:
            with mock.patch.object(DRIFT, "load_source", side_effect=lambda name, path:
                                  batch if Path(path).name == "wiki_batch.py" else real_load(name, path)):
                with mock.patch.object(batch, "_ledger_files", side_effect=replace_after_initial_load):
                    result = self.run_check()
        finally:
            if record.exists():
                record.unlink()
            record.write_bytes(original_bytes)
        asset = next(row for row in result["assets"] if row["id"] == "asset-one")
        self.assertTrue(any(issue.get("reason") == "ledger-changed-during-check" for issue in result["issues"]))
        self.assertEqual(asset["state"], "unstable")
        self.assertEqual(asset["observations"][0]["state"], "unstable")
        self.assertFalse(result["complete"])

    def test_source_page_and_ledger_changes_during_final_validation_are_unstable(self):
        _asset, source, *_ = self.setup_asset(claims=["claim-source-race"])
        peer_path = self.source / "notes/peer.txt"
        peer_path.write_bytes(b"peer")
        peer_asset = LEDGER.make_asset("asset-peer", "root-notes", "notes/peer.txt")
        peer_source = LEDGER.make_source("local:root-notes/notes/peer.txt", b"peer")
        self.save(peer_asset); self.save(peer_source)
        self.save(LEDGER.make_observation(peer_asset, peer_source, "2026-01-02T00:00:00Z"))
        self.save(LEDGER.make_claim("claim-source-race", "Synthetic source-race fixture", support=[source["id"]]))
        original_open = DRIFT.BASELINES._inspect_selected
        calls = 0
        def mutating_open(*args, **kwargs):
            nonlocal calls
            value = original_open(*args, **kwargs)
            calls += 1
            if calls == 1:
                path = self.source / "notes/a.txt"
                replacement = path.with_name("replacement")
                replacement.write_bytes(path.read_bytes())
                os.replace(replacement, path)
            return value
        with mock.patch.object(DRIFT.BASELINES, "_inspect_selected", side_effect=mutating_open):
            result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unstable")
        self.assertEqual(result["pages"][0]["state"], "unstable")
        self.assertEqual(result["pages"][0]["baselines"][0]["state"], "unstable")
        self.assertEqual(result["snapshotValidity"], "unstable")
        self.assertEqual(result["claims"][0]["evidence"][0]["state"], "unstable")
        unstable = next(row for row in result["assets"] if row["id"] == "asset-one")
        self.assertEqual(unstable["observations"][0]["state"], "unstable")
        peer = next(row for row in result["assets"] if row["id"] == "asset-peer")
        self.assertEqual(peer["observations"][0]["state"], "unchanged")

        _asset, page_source, *_ = self.setup_asset(claims=["claim-page-race"])
        self.save(LEDGER.make_claim("claim-page-race", "Synthetic page-link fixture", support=[page_source["id"]]))
        original_read = DRIFT._read_page
        calls = 0
        def mutating_read(*args, **kwargs):
            nonlocal calls
            value = original_read(*args, **kwargs)
            calls += 1
            if calls == 1:
                path = self.vault / "wiki/claim.md"
                replacement = path.with_name("replacement.md")
                replacement.write_bytes(path.read_bytes())
                os.replace(replacement, path)
            return value
        with mock.patch.object(DRIFT, "_read_page", side_effect=mutating_read):
            result = self.run_check()
        self.assertEqual(result["pages"][0]["state"], "unstable")
        self.assertEqual(result["pages"][0]["baselines"][0]["pageState"], "unstable")
        self.assertEqual(result["claims"][0]["pageLinkState"], "unstable")
        self.assertEqual(result["claims"][0]["state"], "unstable")

        _asset, source, *_ = self.setup_asset()
        self.save(LEDGER.make_claim("claim-race", "Synthetic race fixture", support=[source["id"]]))
        batch = load_source("source_drift_batch_race_test", str(ROOT / "scripts/wiki_batch.py"))
        original_ledger = batch._ledger_files
        calls = 0
        def mutating_ledger(*args, **kwargs):
            nonlocal calls
            value = original_ledger(*args, **kwargs)
            calls += 1
            if calls == 1:
                (self.vault / "wiki/meta/evidence/late.json").write_text("{}", encoding="utf-8")
            return value
        with mock.patch.object(DRIFT, "load_source", side_effect=lambda name, path: batch if Path(path).name == "wiki_batch.py" else load_source(name, path)):
            with mock.patch.object(batch, "_ledger_files", side_effect=mutating_ledger):
                result = self.run_check()
        self.assertIn("ledger-changed-during-check", {item.get("reason") for item in result["issues"]})
        self.assertEqual(result["pages"][0]["state"], "unstable")
        self.assertEqual(result["assets"][0]["state"], "unstable")
        self.assertEqual(result["assets"][0]["observations"][0]["state"], "unstable")
        self.assertEqual(result["claims"][0]["state"], "unstable")

    def test_final_source_race_propagates_to_claim_only_pages_without_inferred_links(self):
        _asset, source, *_ = self.setup_asset()
        self.save(LEDGER.make_claim("claim-only", "Synthetic claim-only fixture", support=[source["id"]]))
        page = self.vault / "wiki/claim-only.md"
        page.write_text("---\ntype: Note\nclaims: [claim-only]\n---\n", encoding="utf-8")
        original = DRIFT.BASELINES._inspect_selected
        calls = 0

        def mutate_after_initial_hash(*args, **kwargs):
            nonlocal calls
            value = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                (self.source / "notes/a.txt").write_bytes(b"v2")
            return value

        with mock.patch.object(DRIFT.BASELINES, "_inspect_selected", side_effect=mutate_after_initial_hash):
            result = self.run_check()
        pages = {entry["path"]: entry for entry in result["pages"]}
        claim = result["claims"][0]
        self.assertEqual(claim["state"], "unstable")
        self.assertEqual(pages["wiki/claim-only.md"]["state"], "unstable")
        self.assertIn("declared-claim-evidence-needs-review", pages["wiki/claim-only.md"]["reviewReasons"])
        self.assertEqual(claim["pages"], ["wiki/claim-only.md"])
        self.assertEqual(pages["wiki/claim.md"]["claims"], [])
        self.assertFalse(result["complete"])

    def test_source_removed_after_directory_enumeration_is_unstable_in_cli_report(self):
        _asset, source, *_ = self.setup_asset(content=b"list-stat-race", claims=["claim-list-stat"])
        self.save(LEDGER.make_claim("claim-list-stat", "Synthetic list-stat claim", support=[source["id"]]))
        target = self.source / "notes/a.txt"
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)
        bootstrap = f"""
import runpy, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import module_loading
real_load = module_loading.load_source
def hooked_load(name, path):
    module = real_load(name, path)
    if name == "obsidian_cli_source_drift":
        baseline = module.BASELINES
        real_stat = baseline.os.stat
        fired = False
        def stat_spy(path, *args, **kwargs):
            nonlocal fired
            if path == "a.txt" and kwargs.get("dir_fd") is not None and not fired:
                fired = True
                Path({str(target)!r}).unlink()
            return real_stat(path, *args, **kwargs)
        baseline.os.stat = stat_spy
    return module
module_loading.load_source = hooked_load
sys.argv = [{str(CLI)!r}, "source-check", "--root-id", "root-notes", "--source-root", {str(self.source)!r}, "--json"]
runpy.run_path({str(CLI)!r}, run_name="__main__")
"""
        result = subprocess.run([sys.executable, "-c", bootstrap], cwd=self.base,
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        self.assertEqual(report["snapshotValidity"], "unstable")
        asset = next(row for row in report["assets"] if row["id"] == "asset-one")
        self.assertEqual(asset["state"], "unstable")
        self.assertNotIn(str(self.source), result.stdout + result.stderr)

    def test_source_removed_between_stat_and_open_is_unstable_in_cli_report(self):
        _asset, source, *_ = self.setup_asset(content=b"open-race-payload", claims=["claim-open-race"])
        self.save(LEDGER.make_claim("claim-open-race", "Synthetic open-race claim", support=[source["id"]]))
        target = self.source / "notes/a.txt"
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)
        bootstrap = f"""
import runpy, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import module_loading
real_load = module_loading.load_source
def hooked_load(name, path):
    module = real_load(name, path)
    if name == "obsidian_cli_source_drift":
        baseline = module.BASELINES
        real_open = baseline.os.open
        fired = False
        def open_spy(path, *args, **kwargs):
            nonlocal fired
            if path == "a.txt" and kwargs.get("dir_fd") is not None and not fired:
                fired = True
                Path({str(target)!r}).unlink()
            return real_open(path, *args, **kwargs)
        baseline.os.open = open_spy
    return module
module_loading.load_source = hooked_load
sys.argv = [{str(CLI)!r}, "source-check", "--root-id", "root-notes", "--source-root", {str(self.source)!r}, "--json"]
runpy.run_path({str(CLI)!r}, run_name="__main__")
"""
        result = subprocess.run([sys.executable, "-c", bootstrap], cwd=self.base,
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        self.assertEqual(report["snapshotValidity"], "unstable")
        asset = next(row for row in report["assets"] if row["id"] == "asset-one")
        self.assertEqual(asset["state"], "unstable")
        page = next(row for row in report["pages"] if row["path"] == "wiki/claim.md")
        self.assertEqual(page["state"], "unstable")
        self.assertEqual(page["baselines"][0]["state"], "unstable")
        claim = next(row for row in report["claims"] if row["id"] == "claim-open-race")
        self.assertEqual(claim["evidence"][0]["state"], "unstable")
        self.assertNotIn(str(self.source), result.stdout + result.stderr)

    def test_source_removed_after_hash_starts_is_unstable_in_cli_report(self):
        _asset, source, *_ = self.setup_asset(content=b"race-source-payload", claims=["claim-source-race"])
        self.save(LEDGER.make_claim("claim-source-race", "Synthetic race claim", support=[source["id"]]))
        target = self.source / "notes/a.txt"
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)
        bootstrap = f"""
import runpy, sys, types
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import module_loading
real_load = module_loading.load_source
def hooked_load(name, path):
    module = real_load(name, path)
    if name == "obsidian_cli_source_drift":
        baseline = module.BASELINES
        real_sha256 = baseline.hashlib.sha256
        fired = False
        class HashSpy:
            def __init__(self, inner):
                self.inner = inner
            def update(self, data):
                nonlocal fired
                self.inner.update(data)
                if data == b"race-source-payload" and not fired:
                    fired = True
                    Path({str(target)!r}).unlink()
            def hexdigest(self):
                return self.inner.hexdigest()
        baseline.hashlib = types.SimpleNamespace(sha256=lambda *args, **kwargs: HashSpy(real_sha256(*args, **kwargs)))
    return module
module_loading.load_source = hooked_load
sys.argv = [{str(CLI)!r}, "source-check", "--root-id", "root-notes", "--source-root", {str(self.source)!r}, "--json"]
runpy.run_path({str(CLI)!r}, run_name="__main__")
"""
        result = subprocess.run([sys.executable, "-c", bootstrap], cwd=self.base,
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        self.assertEqual(report["snapshotValidity"], "unstable")
        asset = next(row for row in report["assets"] if row["id"] == "asset-one")
        self.assertEqual(asset["state"], "unstable")
        page = next(row for row in report["pages"] if row["path"] == "wiki/claim.md")
        self.assertEqual(page["state"], "unstable")
        self.assertEqual(page["baselines"][0]["state"], "unstable")
        claim = next(row for row in report["claims"] if row["id"] == "claim-source-race")
        self.assertEqual(claim["evidence"][0]["state"], "unstable")
        self.assertNotIn(str(self.source), result.stdout + result.stderr)

    def test_invalid_claim_references_make_pages_unsafe_but_preserve_valid_edges(self):
        _asset, source, *_ = self.setup_asset()
        self.save(LEDGER.make_claim("claim-safe", "Synthetic safe claim", support=[source["id"]]))
        references = {
            "scalar": "17",
            "array": "[17]",
            "syntax": "[not-a-claim]",
            "mixed": "[claim-safe, 17]",
        }
        for name, claims in references.items():
            (self.vault / "wiki" / f"invalid-{name}.md").write_text(
                f"---\ntype: Note\nclaims: {claims}\n---\nSynthetic body\n", encoding="utf-8")
        before = self.snapshot()
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)
        result = subprocess.run([sys.executable, str(CLI), "source-check", "--root-id", "root-notes",
                                 "--source-root", str(self.source), "--json"], cwd=self.base,
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)
        report = json.loads(result.stdout)
        pages = {page["path"]: page for page in report["pages"]}
        for name in references:
            page = pages[f"wiki/invalid-{name}.md"]
            self.assertEqual(page["state"], "unsafe-unresolvable")
        mixed = pages["wiki/invalid-mixed.md"]
        self.assertEqual(mixed["claims"], ["claim-safe"])
        claim = next(row for row in report["claims"] if row["id"] == "claim-safe")
        self.assertIn("wiki/invalid-mixed.md", claim["pages"])
        self.assertEqual(pages["wiki/claim.md"]["state"], "current")

    def test_invalid_claim_does_not_downgrade_unstable_page(self):
        self.setup_asset()
        page = self.vault / "wiki/claim.md"
        page.write_text("---\ntype: Note\nclaims: 17\n---\nOriginal\n", encoding="utf-8")
        original_read = DRIFT._read_page
        changed = False

        def mutate_after_read(*args, **kwargs):
            nonlocal changed
            result = original_read(*args, **kwargs)
            if not changed:
                changed = True
                page.write_text("---\ntype: Note\nclaims: 17\n---\nChanged\n", encoding="utf-8")
            return result

        with mock.patch.object(DRIFT, "_read_page", side_effect=mutate_after_read):
            result = self.run_check()
        page_row = next(row for row in result["pages"] if row["path"] == "wiki/claim.md")
        self.assertEqual(page_row["state"], "unstable")
        self.assertTrue(any(issue.get("reason") == "invalid-page-claims" for issue in result["issues"]))

    def test_source_policy_legacy_ids_are_unchecked_with_or_without_resource(self):
        self.setup_asset()
        page = self.vault / "wiki/source-policy.md"
        page.write_text("---\ntype: Note\nsources:\n  - id: source-policy\n  - id: source-policy\n    resource: https://example.test/policy\n  - id: citation with spaces\n---\nSynthetic body\n",
                        encoding="utf-8")
        result = self.run_check()
        report_page = next(row for row in result["pages"] if row["path"] == "wiki/source-policy.md")
        self.assertEqual([reference["state"] for reference in report_page["sourceReferences"]],
                         ["external-or-legacy-unchecked"] * 3)
        self.assertFalse(any(reference.get("id") == "citation with spaces" for reference in report_page["sourceReferences"]))
        self.assertTrue(result["complete"])

    def test_missing_claim_does_not_downgrade_an_unstable_page(self):
        self.setup_asset(claims=["claim-missing"])
        original = DRIFT._read_page
        calls = 0

        def mutate_after_initial_page_read(*args, **kwargs):
            nonlocal calls
            value = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                (self.vault / "wiki/claim.md").write_bytes(value[0] + b"changed\n")
            return value

        with mock.patch.object(DRIFT, "_read_page", side_effect=mutate_after_initial_page_read):
            result = self.run_check()
        self.assertEqual(result["pages"][0]["state"], "unstable")
        self.assertIn("missing-page-claim-record", result["pages"][0]["reviewReasons"])
        self.assertFalse(result["complete"])

    def test_missing_source_that_appears_during_check_is_unstable_through_dependents(self):
        self.setup_asset()
        target = self.source / "notes/a.txt"
        target.unlink()
        original = DRIFT.BASELINES._inspect_selected
        calls = 0
        def create_after_missing(*args, **kwargs):
            nonlocal calls
            calls += 1
            try:
                return original(*args, **kwargs)
            except ValueError:
                if calls == 1:
                    target.write_bytes(b"v1")
                raise
        with mock.patch.object(DRIFT.BASELINES, "_inspect_selected", side_effect=create_after_missing):
            result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unstable")
        self.assertEqual(result["pages"][0]["baselines"][0]["state"], "unstable")
        self.assertEqual(result["pages"][0]["state"], "unstable")

    def test_source_parent_replacement_with_same_file_and_bytes_is_unstable(self):
        self.setup_asset()
        original = DRIFT.BASELINES._inspect_selected
        calls = 0
        def replace_parent(*args, **kwargs):
            nonlocal calls
            value = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                parent = self.source / "notes"
                moved = self.source / "notes-moved"
                parent.rename(moved)
                parent.mkdir()
                (moved / "a.txt").rename(parent / "a.txt")
                moved.rmdir()
            return value
        with mock.patch.object(DRIFT.BASELINES, "_inspect_selected", side_effect=replace_parent):
            result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unstable")
        self.assertEqual(result["assets"][0]["observations"][0]["state"], "unstable")
        self.assertEqual(result["pages"][0]["baselines"][0]["state"], "unstable")

    def test_metadata_only_source_and_page_mutations_are_unstable(self):
        self.setup_asset()
        original_inspect = DRIFT.BASELINES._inspect_selected
        calls = 0
        def chmod_after_inspect(*args, **kwargs):
            nonlocal calls
            value = original_inspect(*args, **kwargs)
            calls += 1
            if calls == 1:
                os.chmod(self.source / "notes/a.txt", 0o600)
            return value
        with mock.patch.object(DRIFT.BASELINES, "_inspect_selected", side_effect=chmod_after_inspect):
            result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unstable")

        self.setup_asset()
        original_page_read = DRIFT._read_page
        calls = 0
        def touch_after_read(*args, **kwargs):
            nonlocal calls
            value = original_page_read(*args, **kwargs)
            calls += 1
            if calls == 1:
                path = self.vault / "wiki/claim.md"
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
            return value
        with mock.patch.object(DRIFT, "_read_page", side_effect=touch_after_read):
            result = self.run_check()
        self.assertEqual(result["pages"][0]["state"], "unstable")

    def test_page_set_change_during_check_is_unstable(self):
        _asset, source, *_ = self.setup_asset(claims=["claim-page-set-race"])
        self.save(LEDGER.make_claim("claim-page-set-race", "Synthetic page-set fixture", support=[source["id"]]))
        original = DRIFT._discover_pages
        calls = 0
        def add_page(*args, **kwargs):
            nonlocal calls
            value = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                (self.vault / "wiki/late.md").write_text("---\\ntype: Note\\n---\\nLate\\n", encoding="utf-8")
            return value
        with mock.patch.object(DRIFT, "_discover_pages", side_effect=add_page):
            result = self.run_check()
        self.assertTrue(any(issue["reason"] == "page-set-changed-during-check" for issue in result["issues"]))
        self.assertEqual(result["pages"][0]["state"], "unstable")
        self.assertEqual(result["pages"][0]["baselines"][0]["state"], "unstable")
        self.assertEqual(result["claims"][0]["evidence"][0]["state"], "unstable")
        self.assertEqual(result["claims"][0]["pageLinkState"], "unstable")

    def test_dirty_git_source_uses_working_bytes_without_runtime_git(self):
        self.setup_asset()
        subprocess.run(["git", "init", "--quiet", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.email", "fixture@example.test"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.name", "Synthetic Fixture"], check=True)
        selected = self.source / "notes/a.txt"
        subprocess.run(["git", "-C", str(self.source), "add", "notes/a.txt"], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-m", "synthetic base"], check=True, capture_output=True)
        selected.write_bytes(b"dirty but uncommitted")
        with mock.patch.object(DRIFT.BASELINES.subprocess, "Popen", side_effect=AssertionError("runtime invoked Git")):
            result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "changed")

    def test_absolute_source_root_rejects_unicode_sibling_aliases(self):
        self.setup_asset()
        parent = self.base / "unicode-parent"
        parent.mkdir()
        name = "é-ancestor"
        root = parent / name / "source-root"
        (root / "notes").mkdir(parents=True)
        (root / "notes/a.txt").write_bytes(b"v1")
        alias = unicodedata.normalize("NFD", name)
        self.assertNotEqual(alias, name)
        original_listdir = DRIFT.BASELINES.os.listdir

        def listdir_with_alias(path):
            names = original_listdir(path)
            if isinstance(path, (str, os.PathLike)) and Path(path) == parent:
                return [*names, alias]
            return names

        with mock.patch.object(DRIFT.BASELINES.os, "listdir", side_effect=listdir_with_alias):
            result = DRIFT.check(self.vault, "root-notes", root)
        self.assertFalse(result["complete"])
        self.assertTrue(any(issue.get("reason") == "selected-root-unavailable" for issue in result["issues"]))
        asset = next(row for row in result["assets"] if row["id"] == "asset-one")
        self.assertEqual(asset["state"], "unsafe-unresolvable")
        self.assertNotIn(str(root), json.dumps(result))

    def test_root_removed_after_validation_is_unstable_before_selected_open(self):
        self.setup_asset()
        moved = self.source.with_name("source-moved-before-open")
        original_snapshot = DRIFT.BASELINES._mapping_snapshot
        calls = 0

        def remove_after_root_validation(path, **kwargs):
            nonlocal calls
            result = original_snapshot(path, **kwargs)
            calls += 1
            if calls == 1:
                self.source.rename(moved)
            return result

        try:
            with mock.patch.object(DRIFT.BASELINES, "_mapping_snapshot", side_effect=remove_after_root_validation):
                result = self.run_check()
        finally:
            if moved.exists() and not self.source.exists():
                moved.rename(self.source)
        self.assertFalse(result["complete"])
        self.assertEqual(result["snapshotValidity"], "unstable")
        self.assertEqual(result["assets"][0]["state"], "unstable")

    def test_final_absolute_source_mapping_rejects_new_unicode_alias(self):
        self.setup_asset()
        parent = self.base / "unicode-parent"
        parent.mkdir()
        name = "é-ancestor"
        root = parent / name / "source-root"
        (root / "notes").mkdir(parents=True)
        (root / "notes/a.txt").write_bytes(b"v1")
        alias = unicodedata.normalize("NFD", name)
        active = False
        original_listdir = DRIFT.BASELINES.os.listdir
        original_inspect = DRIFT.BASELINES._inspect_selected

        def listdir_with_alias(path):
            names = original_listdir(path)
            if active and isinstance(path, (str, os.PathLike)) and Path(path) == parent:
                return [*names, alias]
            return names

        def activate_after_initial_inspection(*args, **kwargs):
            nonlocal active
            result = original_inspect(*args, **kwargs)
            active = True
            return result

        with mock.patch.object(DRIFT.BASELINES.os, "listdir", side_effect=listdir_with_alias):
            with mock.patch.object(DRIFT.BASELINES, "_inspect_selected", side_effect=activate_after_initial_inspection):
                result = DRIFT.check(self.vault, "root-notes", root)
        self.assertFalse(result["complete"])
        self.assertEqual(result["snapshotValidity"], "unstable")
        asset = next(row for row in result["assets"] if row["id"] == "asset-one")
        self.assertEqual(asset["state"], "unstable")
        self.assertNotIn(str(root), json.dumps(result))

    def test_source_root_remap_during_check_is_unstable(self):
        self.setup_asset()
        original = DRIFT.BASELINES._inspect_selected
        calls = 0
        moved = self.source.with_name("source-moved")
        outside = self.base / "outside-root"
        outside.mkdir()
        (outside / "notes").mkdir()
        (outside / "notes/a.txt").write_bytes(b"outside")
        def remap(*args, **kwargs):
            nonlocal calls
            value = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                self.source.rename(moved)
                self.source.symlink_to(outside, target_is_directory=True)
            return value
        with mock.patch.object(DRIFT.BASELINES, "_inspect_selected", side_effect=remap):
            result = self.run_check()
        self.assertEqual(result["assets"][0]["state"], "unstable")
        self.assertEqual(result["assets"][0]["observations"][0]["state"], "unstable")
        self.source.unlink()
        moved.rename(self.source)

    def test_legacy_mapping_source_ids_are_unchecked_and_malformed_types_invalid(self):
        self.setup_asset()
        legacy = self.vault / "wiki/legacy.md"
        legacy.write_text("---\ntype: Note\nsources:\n  - id: fpa-handbook\n    resource: https://example.test/handbook\n  - id: legacy-id-only\n---\nText\n", encoding="utf-8")
        result = self.run_check()
        pages = {page["path"]: page for page in result["pages"]}
        self.assertEqual([ref["state"] for ref in pages["wiki/legacy.md"]["sourceReferences"]],
                         ["external-or-legacy-unchecked", "external-or-legacy-unchecked"])
        self.assertTrue(result["complete"])
        malformed = self.vault / "wiki/malformed.md"
        malformed.write_text("---\ntype: Note\nsources:\n  - id: []\n    resource: https://example.test/handbook\n---\nText\n", encoding="utf-8")
        result = self.run_check()
        self.assertFalse(result["complete"])
        self.assertTrue(any(issue["reason"] == "invalid-page-source-id" for issue in result["issues"]))

    def test_missing_asset_observation_invalidates_explicit_claim_and_page_edges(self):
        asset, source, _observation, baseline, _page = self.setup_asset()
        claim = self.save(LEDGER.make_claim("claim-missing-asset", "Synthetic claim", support=[source["id"]]))
        contradiction = self.save(LEDGER.make_claim("claim-contradiction-missing-asset", "Synthetic contradiction",
                                                    contradictions=[source["id"]]))
        page = self.vault / "wiki/claim-only-missing-asset.md"
        page.write_text("---\ntype: Note\nclaims: [claim-missing-asset]\nsources:\n  - id: " + source["id"] + "\n---\nText\n", encoding="utf-8")
        (self.vault / "wiki/meta/evidence" / (asset["id"] + ".json")).unlink()
        (self.vault / "wiki/meta/evidence" / (baseline["id"] + ".json")).unlink()
        result = self.run_check()
        claim_row = next(row for row in result["claims"] if row["id"] == claim["id"])
        page_row = next(row for row in result["pages"] if row["path"] == "wiki/claim-only-missing-asset.md")
        contradiction_row = next(row for row in result["claims"] if row["id"] == contradiction["id"])
        edges = claim_row["evidence"] + contradiction_row["evidence"]
        self.assertEqual({(edge["relation"], edge["state"], edge["reason"]) for edge in edges},
                         {("support", "unsafe-unresolvable", "broken-observation-dependency"),
                          ("contradictions", "unsafe-unresolvable", "broken-observation-dependency")})
        self.assertTrue(claim_row["reviewRequired"])
        self.assertIn(page_row["state"], {"affected", "unsafe-unresolvable"})
        self.assertTrue(any("unsafe" in reason or "needs-review" in reason for reason in page_row["reviewReasons"]))
        self.assertEqual(page_row["sourceReferences"][0]["state"], "unsafe-unresolvable")
        self.assertFalse(result["complete"])

    def test_unsafe_ledger_errors_are_structured_for_json_cli(self):
        self.setup_asset(content=b"synthetic-secret-payload")
        evidence = self.vault / "wiki/meta/evidence"
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        env.pop("OBSIDIAN_VAULT_PATH", None)

        def assert_structured(reason):
            result = subprocess.run([sys.executable, str(CLI), "source-check", "--root-id", "root-notes",
                                     "--source-root", str(self.source), "--json"], cwd=self.base,
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["complete"])
            self.assertTrue(any(issue["reason"] == reason and issue["state"] == "unsafe-unresolvable"
                                for issue in payload["issues"]))
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("synthetic-secret-payload", result.stdout + result.stderr)

        record_link = evidence / "source-link.json"
        record_link.symlink_to(self.base / "secret.json")
        try:
            with self.subTest(boundary="symlink-record"):
                assert_structured("ledger-unsafe-path")
        finally:
            record_link.unlink()

        namespace = self.vault / "wiki/meta/evidence"
        moved = self.vault / "wiki/meta/evidence-safe"
        namespace.rename(moved)
        try:
            namespace.symlink_to(self.base / "outside-evidence", target_is_directory=True)
            with self.subTest(boundary="symlink-namespace"):
                assert_structured("ledger-unsafe-path")
        finally:
            if namespace.is_symlink():
                namespace.unlink()
            moved.rename(namespace)

    def test_ledger_permission_failure_remains_operational_error(self):
        self.setup_asset()
        batch = load_source("source_drift_batch_permission_test", str(ROOT / "scripts/wiki_batch.py"))

        def denied(_vault, *, reject_hardlinks=False):
            self.assertTrue(reject_hardlinks)
            try:
                raise PermissionError(errno.EACCES, "denied")
            except PermissionError as exc:
                raise batch.BatchError("cannot read ledger safely") from exc

        real_load = load_source
        with mock.patch.object(DRIFT, "load_source", side_effect=lambda name, path:
                              batch if Path(path).name == "wiki_batch.py" else real_load(name, path)):
            with mock.patch.object(batch, "_ledger_files", side_effect=denied):
                _records, _raw, issues = DRIFT._load(self.vault)
        self.assertEqual(issues, [{"state": "operational-error", "reason": "ledger-permission-denied"}])

    def test_invalid_records_are_reported_without_exposing_record_content(self):
        self.setup_asset()
        path = self.vault / "wiki/meta/evidence/not-a-real-record.json"
        path.write_text(json.dumps({"statement": "secret claim"}), encoding="utf-8")
        unsafe = LEDGER.make_asset("asset-traversal", "root-notes", "safe/file.txt")
        unsafe["locator"] = "../outside.txt"
        unsafe_path = self.vault / "wiki/meta/evidence" / (unsafe["id"] + ".json")
        unsafe_path.write_text(json.dumps(unsafe), encoding="utf-8")
        before_check = self.snapshot()
        result = self.run_check()
        self.assertEqual(self.snapshot(), before_check)
        self.assertFalse(result["complete"])
        self.assertNotIn("secret claim", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
