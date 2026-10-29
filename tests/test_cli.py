import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/obsidian-second-brain.py"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        (self.vault / "wiki" / "coffee.md").write_text(
            "# Coffee Notes\n\nEspresso beans and careful roasting.\n", encoding="utf-8"
        )
        self.config = self.root / "properties.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault), "unrelated": {"keep": True}}), encoding="utf-8")
        env = os.environ.copy()
        env["HOME"] = str(self.root / "empty-home")
        env["OBSIDIAN_AGENT_CONFIG"] = str(self.config)
        env.pop("OBSIDIAN_VAULT_PATH", None)
        self.env = env

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(CLI), *args], cwd=cwd or self.root,
            env=self.env, text=True, capture_output=True,
        )

    def test_doctor_json_is_read_only_and_reports_ready_state(self):
        before = sorted(str(path.relative_to(self.vault)) for path in self.vault.rglob("*"))
        result = self.run_cli("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["vault"], str(self.vault.resolve()))
        self.assertTrue(report["config"]["valid"])
        self.assertFalse(report["retrieval"]["indexExists"])
        self.assertFalse(report["lifecycle"]["pending"])
        self.assertEqual(before, sorted(str(path.relative_to(self.vault)) for path in self.vault.rglob("*")))
        self.assertFalse((self.vault / ".vault-meta").exists())

    def test_search_from_arbitrary_cwd_requires_existing_index_no_build(self):
        indexer = ROOT / "scripts/bm25-index.py"
        built = subprocess.run([sys.executable, str(indexer), "build", "--vault", str(self.vault)], text=True, capture_output=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        index = self.vault / ".vault-meta/retrieval/bm25.json"
        old_index = index.read_bytes()
        other = self.root / "elsewhere"
        other.mkdir()
        result = self.run_cli("search", "espresso", "--json", cwd=other)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["fallback"], "bm25", response)
        self.assertEqual(response["results"][0]["path"], "wiki/coffee.md")
        legacy = {key: value for key, value in response["results"][0].items()
                  if key not in {"sourceHash", "readRef"}}
        self.assertEqual({key: value for key, value in legacy.items() if key != "score"},
                         {"path": "wiki/coffee.md", "match": "bm25", "lineStart": 3,
                          "lineEnd": 3, "excerpt": "Espresso beans and careful roasting."})
        self.assertAlmostEqual(legacy["score"], 0.28768207245178085)
        self.assertGreaterEqual(response["results"][0]["lineStart"], 1)
        self.assertIn("Espresso", response["results"][0]["excerpt"])
        self.assertEqual(response["freshness"], "current-by-hash")
        reference = response["results"][0]["readRef"]
        self.assertEqual(reference["page"], "wiki/coffee.md")
        self.assertEqual(reference["expectedHash"], response["results"][0]["sourceHash"])
        focused = self.run_cli("read", "--json", "--vault", str(self.vault), "--page", reference["page"],
                               "--expected-hash", reference["expectedHash"], "--start", str(reference["start"]),
                               "--end", str(reference["end"]), cwd=other)
        self.assertEqual(focused.returncode, 0, focused.stderr)
        self.assertIn("Espresso", json.loads(focused.stdout)["text"])
        (self.vault / "wiki" / "new.md").write_text("new material\n", encoding="utf-8")
        stale = self.run_cli("search", "espresso", "--json", cwd=other)
        self.assertEqual(json.loads(stale.stdout)["freshness"], "stale")
        self.assertEqual(index.read_bytes(), old_index)

    def test_search_read_refs_preserve_current_and_dated_raw_views(self):
        dated = self.vault / "wiki/meeting-2026-05-12.md"
        dated.write_text("# Meeting 2026-05-12\ncurrent phrase and ~~historic phrase~~\n", encoding="utf-8")
        built = subprocess.run([sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault)],
                               text=True, capture_output=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        for query, expected_view, term in (("current phrase", "current", "current phrase"),
                                           ("meeting 2026-05-12", "raw", "historic phrase")):
            result = self.run_cli("search", query, "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            hit = next(item for item in json.loads(result.stdout)["results"] if item["path"] == "wiki/meeting-2026-05-12.md")
            reference = hit["readRef"]
            self.assertEqual(reference["view"], expected_view)
            focused = self.run_cli("read", "--json", "--vault", str(self.vault), "--page", reference["page"],
                                   "--expected-hash", reference["expectedHash"], "--start", str(reference["start"]),
                                   "--end", str(reference["end"]), "--view", reference["view"])
            self.assertEqual(focused.returncode, 0, focused.stderr)
            self.assertIn(term, json.loads(focused.stdout)["text"])

    def test_doctor_reports_malformed_lifecycle_roots_as_structured_unavailable(self):
        state_path = self.vault / ".vault-meta/lifecycle/state.json"
        state_path.parent.mkdir(parents=True)
        for invalid in ([], None, 7, "broken"):
            with self.subTest(state=invalid):
                payload = json.dumps(invalid).encode()
                state_path.write_bytes(payload)
                result = self.run_cli("doctor", "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["lifecycle"]["state"], "unavailable")
                self.assertIn("JSON object", report["lifecycle"]["error"])
                self.assertIn("cacheExclusions", report)
                self.assertIn("retrieval", report)
                self.assertEqual(state_path.read_bytes(), payload)

    def test_doctor_json_reports_malformed_config_shapes_without_crashing(self):
        for invalid in (
            {"vaultPath": str(self.vault), "features": []},
            {"vaultPath": str(self.vault), "features": "invalid"},
            {"vaultPath": str(self.vault), "features": {"retrievalRefresh": "false"}},
            {"vaultPath": []},
            [],
        ):
            with self.subTest(config=invalid):
                self.config.write_text(json.dumps(invalid), encoding="utf-8")
                result = self.run_cli("doctor", "--json")
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report["config"]["valid"])
                self.assertTrue(report["config"]["error"])
                self.assertIn("retrieval", report)
                if isinstance(invalid, dict) and isinstance(invalid.get("vaultPath"), str) and not isinstance(invalid.get("features"), dict):
                    self.assertIsNone(report["retrieval"]["automaticRefreshEnabled"])

    def test_doctor_reports_automatic_refresh_defaults_for_valid_feature_settings(self):
        for features, expected in (
            ("omitted", True),
            (None, True),
            ({}, True),
            ({"retrievalRefresh": True}, True),
            ({"retrievalRefresh": False}, False),
        ):
            with self.subTest(features=features):
                config = {"vaultPath": str(self.vault)}
                if features != "omitted":
                    config["features"] = features
                self.config.write_text(json.dumps(config), encoding="utf-8")
                result = self.run_cli("doctor", "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertTrue(report["config"]["valid"], report)
                self.assertIs(expected, report["retrieval"]["automaticRefreshEnabled"])

    def test_blank_vault_override_falls_back_to_config_like_hook(self):
        self.env["OBSIDIAN_VAULT_PATH"] = ""
        doctor = self.run_cli("doctor", "--json")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertEqual(json.loads(doctor.stdout)["vault"], str(self.vault.resolve()))
        search = self.run_cli("search", "espresso", "--json")
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertEqual(json.loads(search.stdout)["vault"], str(self.vault.resolve()))
        hook = subprocess.run(
            ["bash", str(ROOT / "hooks/obsidian-session.sh"), "start"], cwd=self.root,
            env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertIn(f"Configured Obsidian wiki: {self.vault.resolve()}/wiki", hook.stdout)

    def test_doctor_reports_pending_lifecycle_without_mutating_state(self):
        state_path = self.vault / ".vault-meta/lifecycle/state.json"
        state_path.parent.mkdir(parents=True)
        state = {"version": 1, "vault": str(self.vault.resolve()), "pending": {"wiki/unfinished.md": {"changed": True}}}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        before = state_path.read_bytes()
        result = self.run_cli("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["lifecycle"]["pending"])
        self.assertEqual(report["lifecycle"]["count"], 1)
        self.assertEqual(state_path.read_bytes(), before)

    def test_doctor_and_search_freshness_match_case_insensitive_indexer_discovery(self):
        upper_page = self.vault / "wiki" / "Valid.MD"
        upper_page.write_text("# Valid page\n\nEspresso appears here.\n", encoding="utf-8")
        built = subprocess.run(
            [sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault)],
            text=True, capture_output=True,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        doctor = self.run_cli("doctor", "--json")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        doctor_data = json.loads(doctor.stdout)
        self.assertTrue(doctor_data["retrieval"]["ready"], doctor_data)
        self.assertEqual(doctor_data["retrieval"]["freshness"], "current-by-hash")
        search = self.run_cli("search", "espresso", "--json")
        self.assertEqual(search.returncode, 0, search.stderr)
        search_data = json.loads(search.stdout)
        self.assertEqual(search_data["freshness"], "current-by-hash")
        self.assertEqual(search_data["results"][0]["path"], "wiki/Valid.MD")

    def test_freshness_matches_nonempty_tokenized_bm25_documents_read_only(self):
        empty_page = self.vault / "wiki" / "empty.MD"
        tokenless_page = self.vault / "wiki" / "tokenless.md"
        empty_page.write_text("", encoding="utf-8")
        tokenless_page.write_text("--- !!! ... ---\n", encoding="utf-8")
        index = self.vault / ".vault-meta/retrieval/bm25.json"

        def build_index(expected_count):
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault)],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Indexed {expected_count} Markdown pages", result.stdout)

        def freshness_reports():
            doctor = self.run_cli("doctor", "--json")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            search = self.run_cli("search", "espresso", "--json")
            self.assertEqual(search.returncode, 0, search.stderr)
            return json.loads(doctor.stdout), json.loads(search.stdout)

        def snapshot():
            return {
                path.relative_to(self.vault).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.vault.rglob("*") if path.is_file()
            }

        build_index(1)
        initial_index_time = index.stat().st_mtime_ns
        # Empty/tokenless pages newer than the index are not BM25 documents and
        # therefore do not make the index stale or contribute freshness mtimes.
        for page in (empty_page, tokenless_page):
            os.utime(page, ns=(initial_index_time + 3_000_000_000, initial_index_time + 3_000_000_000))
        before = snapshot()
        doctor, search = freshness_reports()
        self.assertTrue(doctor["retrieval"]["ready"], doctor)
        self.assertEqual(doctor["retrieval"]["freshness"], "current-by-hash")
        self.assertEqual(search["freshness"], "current-by-hash")
        self.assertEqual(snapshot(), before, "doctor/search must not write while ignoring tokenless pages")

        # Empty-to-nonempty changes alter the indexer's document set and remain stale.
        empty_page.write_text("newly tokenized material\n", encoding="utf-8")
        os.utime(empty_page, ns=(initial_index_time + 4_000_000_000, initial_index_time + 4_000_000_000))
        before = snapshot()
        doctor, search = freshness_reports()
        self.assertEqual(doctor["retrieval"]["freshness"], "stale")
        self.assertEqual(search["freshness"], "stale")
        self.assertEqual(snapshot(), before)

        # Rebuild with the now-nonempty page, then prove meaningful content changes stale.
        now = time.time_ns()
        os.utime(empty_page, ns=(now, now))
        os.utime(tokenless_page, ns=(now, now))
        build_index(2)
        built_index_time = index.stat().st_mtime_ns
        coffee = self.vault / "wiki" / "coffee.md"
        coffee.write_text("# Updated topic\n\nSubstantive different content.\n", encoding="utf-8")
        os.utime(coffee, ns=(built_index_time + 4_000_000_000, built_index_time + 4_000_000_000))
        before = snapshot()
        doctor, search = freshness_reports()
        self.assertEqual(doctor["retrieval"]["freshness"], "stale")
        self.assertEqual(search["freshness"], "stale")
        self.assertEqual(snapshot(), before)

        # A rebuilt index followed by removal of an indexed page is also stale.
        now = time.time_ns()
        os.utime(coffee, ns=(now, now))
        os.utime(empty_page, ns=(now, now))
        build_index(2)
        coffee.unlink()
        before = snapshot()
        doctor, search = freshness_reports()
        self.assertEqual(doctor["retrieval"]["freshness"], "stale")
        self.assertEqual(search["freshness"], "stale")
        self.assertEqual(snapshot(), before)

    def test_doctor_reports_negation_exposed_git_state_and_is_read_only(self):
        subprocess.run(["git", "init", "-q", str(self.vault)], check=True)
        subprocess.run(["git", "-C", str(self.vault), "config", "core.excludesFile", os.devnull], check=True)
        ignore = self.vault / ".gitignore"
        ignore.write_text(
            ".vault-meta/retrieval/\n.vault-meta/lifecycle/*\n!.vault-meta/lifecycle/state.json\n",
            encoding="utf-8",
        )
        before = ignore.read_bytes()
        result = self.run_cli("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["cacheExclusions"]["effective"], report["cacheExclusions"])
        self.assertIn("not effectively ignored", report["cacheExclusions"]["detail"])
        exposed = subprocess.run(
            ["git", "-C", str(self.vault), "check-ignore", "--no-index", "-q", "--", ".vault-meta/lifecycle/state.json"],
            check=False,
        )
        self.assertEqual(exposed.returncode, 1)
        self.assertEqual(ignore.read_bytes(), before)
        self.assertFalse((self.vault / ".vault-meta").exists())

    def test_search_without_index_reports_fallback_without_creating_cache(self):
        result = self.run_cli("search", "espresso", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["fallback"], "navigation")
        self.assertEqual(response["results"], [])
        self.assertFalse((self.vault / ".vault-meta").exists())

    def test_json_failures_preserve_exit_codes_and_legacy_text_without_writes(self):
        bundle = self.root / "invalid-bundle.json"
        bundle.write_text("{}")
        cases = [
            (("search",), 2, "requires query terms"),
            (("doctor", "--as-of", "2026-01-01"), 2, "--as-of requires"),
            (("batch-inspect",), 2, "requires --bundle"),
            (("batch-apply",), 2, "requires --bundle"),
            (("batch-recover",), 2, "requires --batch-id"),
            (("batch-rollback",), 2, "requires --batch-id"),
            (("capture-inspect",), 2, "requires at least one --source"),
            (("capture-apply",), 2, "requires at least one --source"),
            (("batch-inspect", "--bundle", str(bundle)), 1, "version-1 object"),
            (("batch-apply", "--bundle", str(bundle)), 1, "version-1 object"),
            (("batch-recover", "--batch-id", "invalid"), 1, "invalid batch ID"),
            (("batch-rollback", "--batch-id", "invalid"), 1, "invalid batch ID"),
            (("capture-inspect", "--source", "relative.txt"), 1, "absolute paths"),
            (("capture-apply", "--source", "relative.txt"), 1, "absolute paths"),
        ]
        before = {path.relative_to(self.vault): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.vault.rglob("*") if path.is_file()}
        for arguments, status, message in cases:
            for json_mode in (False, True):
                with self.subTest(arguments=arguments, json=json_mode):
                    result = self.run_cli(*arguments, *(["--json"] if json_mode else []))
                    self.assertEqual(result.returncode, status, result.stderr)
                    if json_mode:
                        self.assertEqual(result.stderr, "")
                        error = json.loads(result.stdout)
                        self.assertEqual(error["code"], "invalid-arguments" if status == 2 else "command-error")
                        self.assertIn(message, error["error"])
                    else:
                        self.assertEqual(result.stdout, "")
                        self.assertIn(message, result.stderr)
        self.assertEqual({path.relative_to(self.vault): (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in self.vault.rglob("*") if path.is_file()}, before)
        self.assertFalse((self.vault / ".vault-meta").exists())

    def test_write_commands_report_invalid_config_as_json_or_legacy_text(self):
        self.config.write_text("{invalid")
        for command in ("batch-inspect", "batch-apply", "batch-recover", "batch-rollback",
                        "capture-inspect", "capture-apply"):
            with self.subTest(command=command):
                result = self.run_cli("--json", command)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "")
                error = json.loads(result.stdout)
                self.assertEqual(error["code"], "command-error")
                self.assertIn("invalid selected integration config", error["error"])
                legacy = self.run_cli(command)
                self.assertEqual(legacy.returncode, 1)
                self.assertEqual(legacy.stdout, "")
                self.assertIn("invalid selected integration config", legacy.stderr)
        self.assertFalse((self.vault / ".vault-meta").exists())

    def test_explicit_vault_takes_precedence_and_bad_config_is_diagnosed(self):
        self.config.write_text("{bad", encoding="utf-8")
        alternate = self.root / "alternate"
        (alternate / "wiki").mkdir(parents=True)
        report = self.run_cli("doctor", "--json", "--vault", str(alternate))
        self.assertEqual(report.returncode, 1, report.stderr)
        data = json.loads(report.stdout)
        self.assertEqual(data["vault"], str(alternate.resolve()))
        self.assertFalse(data["config"]["valid"])


if __name__ == "__main__":
    unittest.main()
