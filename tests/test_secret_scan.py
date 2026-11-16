#!/usr/bin/env python3
"""Write-path secret check: patterns, opt-out, file-tool input, and batch refusal."""
from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/obsidian-second-brain.py"
MIDDLEWARE = ROOT / "skills/wiki/scripts/okf_mw"
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source

SCAN = load_source("secret_scan_test", str(MIDDLEWARE / "secret_scan.py"))
BATCH = load_source("secret_scan_test_batch", str(ROOT / "scripts/wiki_batch.py"))
CAPTURE = load_source("secret_scan_test_capture", str(ROOT / "scripts/local_capture.py"))

# Synthetic values assembled at runtime so no literal credential-shaped string
# exists in the repository (GitHub push protection scans committed text).
RANDOM = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jK5mP7rS9"
SECRETS = {
    "private key block": "-----BEGIN " + "RSA PRIVATE KEY-----\n" + RANDOM + "\n-----END " + "RSA PRIVATE KEY-----",
    "OpenAI-style API key": "sk" + "-proj-" + RANDOM,
    "Anthropic API key": "sk" + "-ant-" + "api03-" + RANDOM,
    "GitHub token": "gh" + "p_" + RANDOM[:36],
    "GitHub fine-grained token": "github" + "_pat_" + RANDOM + "_Zq",
    "Slack token": "xox" + "b-" + "1234567890-" + RANDOM[:24],
    "AWS access key ID": "AK" + "IA" + "QWERTY12345ABCDE",
    "Google API key": "AI" + "za" + RANDOM[:35],
    "GitLab token": "gl" + "pat-" + RANDOM[:24],
    "npm token": "npm" + "_" + RANDOM[:36],
    "Stripe live key": "sk" + "_live_" + RANDOM[:26],
    "JSON Web Token": "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0." + "SflKxwRJSMeKKF2QT4fwpMeJf36P",
    "credentials in URL": "post" + "gres://admin:" + "Sup3rS3cretPw" + "@db.internal.test/app",
    "credential assignment": "export SERVICE_" + "API_KEY=" + "abc123def456ghi789",
}
PLACEHOLDERS = [
    "api_key=<your-key>", "api_key: your-api-key-here1", "password: ${DB_PASSWORD}",
    'token = "{{ secrets.TOKEN }}"', "token: Optional[str]", "password = getpass.getpass2()",
    "password = config.settings2.password", "PASSWORD=changeme123", "secret=redacted-value-9",
    "postgres://user:password@localhost/db", "https://user:<token>@host.test/path",
    "ghp_" + "x" * 36, "AKIA" + "IOSFODNN7EXAMPLE", "see my-sk-assessment-of-the-long-running-thing-2024-final",
    "tokens: 12345678", "Run `export GITHUB_TOKEN=$TOKEN` first.",
]
PAGE = "---\ntype: note\n---\n\n# Page\n\n"


def run_cli(vault: Path, path: Path, tool_input: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MIDDLEWARE / "secret_scan.py"), "tool-input", "--vault", str(vault), "--path", str(path)],
        input=json.dumps(tool_input), text=True, capture_output=True, check=False)


class PatternTests(unittest.TestCase):
    def test_each_pattern_family_is_detected_with_its_line_and_no_value(self):
        for kind, secret in SECRETS.items():
            with self.subTest(kind=kind):
                findings = SCAN.scan_text("first line\nsecond " + secret + "\nthird")
                self.assertIn(kind, {finding.kind for finding in findings})
                self.assertEqual(findings[0].line, 2)
                message = SCAN.refusal_for_content("wiki/a.md", "x\n" + secret, page=True)
                self.assertIn("line 2", message)
                self.assertIn(kind, message)
                for part in (secret, RANDOM[:20], "Sup3rS3cretPw", "abc123def456"):
                    self.assertNotIn(part, message)

    def test_placeholders_and_prose_pass(self):
        for text in PLACEHOLDERS:
            with self.subTest(text=text):
                self.assertEqual(SCAN.scan_text(text), [])

    def test_opt_out_is_frontmatter_only(self):
        secret = SECRETS["GitHub token"]
        allowed = "---\ntype: note\nsecretScan: allow\n---\n" + secret
        self.assertIsNone(SCAN.refusal_for_content("wiki/a.md", allowed, page=True))
        self.assertIsNone(SCAN.refusal_for_content("wiki/a.md", allowed.replace("allow", '"allow"'), page=True))
        body_only = "---\ntype: note\n---\nsecretScan: allow\n" + secret
        self.assertIsNotNone(SCAN.refusal_for_content("wiki/a.md", body_only, page=True))
        self.assertIsNotNone(SCAN.refusal_for_content("wiki/a.md", allowed.replace("allow", "deny"), page=True))
        # Ledger JSON has no opt-out.
        self.assertIsNotNone(SCAN.refusal_for_content("wiki/meta/evidence/a.json", allowed, page=False))

    def test_pathological_inputs_scan_in_linear_time(self):
        # The Claude PreToolUse hook has a 10 s timeout and fails open, so a slow scan would be a bypass.
        # Each prefix repeated inside its own body alphabet used to rescan the run once per repetition.
        units = ["xox" + "b-", "ey" + "J-", "ey" + "Jaaaaaaaaaaa.", "sk" + "-", "sk" + "-ant-", "gh" + "p_",
                 "github" + "_pat_", "AK" + "IA", "AI" + "za", "gl" + "pat-", "npm" + "_", "sk" + "_live_",
                 "api_key=", "token: ", "a://b:", "a://x:", "-----BEGIN " + "PRIVATE KEY-----"]
        slowest = 0.0
        for unit in units:
            for suffix in ("", "a", "-", " "):
                text = (unit * (100_000 // len(unit) + 1))[:100_000] + suffix
                started = time.perf_counter()
                SCAN.scan_text(text)
                elapsed = time.perf_counter() - started
                slowest = max(slowest, elapsed)
                self.assertLess(elapsed, 0.5, f"{unit!r}+{suffix!r} took {elapsed:.2f}s")
        self.assertLess(slowest, 0.5)

    def test_slack_token_still_needs_a_digit(self):
        self.assertEqual(SCAN.scan_text("xox" + "b-" + "abcdefghijklmnopqrstuvwx"), [])
        self.assertIn("Slack token", {f.kind for f in SCAN.scan_text("see xox" + "b-" + "abcdefghijk1")})

    def test_new_findings_compare_by_value_not_line(self):
        github, other = SECRETS["GitHub token"], "gh" + "p_" + RANDOM[1:37]
        self.assertEqual(SCAN.new_findings("moved\n\n" + github, "x " + github + "\n"), [])
        self.assertEqual([f.kind for f in SCAN.new_findings(github + "\n" + other, github)], ["GitHub token"])
        key = SECRETS["private key block"]
        self.assertEqual(SCAN.new_findings(key, key), [])
        second = key.replace(RANDOM, RANDOM[::-1])
        self.assertEqual([f.kind for f in SCAN.new_findings(key + "\n" + second, key)], ["private key block"])

    def test_validate_py_is_unchanged_for_existing_pages_with_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            page = Path(temp) / "page.md"
            page.write_text(PAGE + SECRETS["GitHub token"] + "\n", encoding="utf-8")
            result = subprocess.run([sys.executable, str(MIDDLEWARE / "validate.py"), str(page)],
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("secret", (result.stdout + result.stderr).lower())


class ToolInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / "wiki/topic").mkdir(parents=True)
        self.secret = SECRETS["GitHub token"]

    def refused(self, path: Path, tool_input: dict) -> str:
        result = run_cli(self.vault, path, tool_input)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(self.secret, result.stderr)
        self.assertNotIn(RANDOM[:20], result.stderr)
        return result.stderr

    def allowed(self, path: Path, tool_input: dict) -> None:
        result = run_cli(self.vault, path, tool_input)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_file_tool_shape_is_scanned_for_wiki_destinations(self):
        page = self.vault / "wiki/topic/page.md"
        edit_text = "token note\n" + self.secret
        inputs = {
            "claude Write": {"file_path": str(page), "content": PAGE + self.secret},
            "claude Edit": {"file_path": str(page), "old_string": "a", "new_string": edit_text},
            "claude MultiEdit": {"file_path": str(page), "edits": [
                {"old_string": "a", "new_string": "fine"}, {"old_string": "b", "new_string": edit_text}]},
            "claude NotebookEdit": {"notebook_path": str(page), "new_source": edit_text},
            "pi write": {"path": str(page), "content": PAGE + self.secret},
            "pi edit": {"path": str(page), "edits": [{"oldText": "a", "newText": edit_text}]},
            "pi legacy edit": {"path": str(page), "oldText": "a", "newText": edit_text},
        }
        for name, tool_input in inputs.items():
            with self.subTest(tool=name):
                message = self.refused(page, tool_input)
                self.assertIn("wiki/topic/page.md", message)
                self.assertIn("GitHub token", message)
        self.assertIn("added text", self.refused(page, inputs["claude Edit"]))

    def test_ledger_json_is_scanned_and_has_no_opt_out(self):
        record = self.vault / "wiki/meta/evidence/asset.json"
        content = json.dumps({"note": self.secret})
        self.refused(record, {"file_path": str(record), "content": content})
        self.refused(record, {"file_path": str(record), "content": "secretScan: allow\n" + content})

    def test_non_wiki_and_clean_writes_pass(self):
        self.allowed(self.vault / "outside.md", {"content": self.secret})
        self.allowed(self.vault / "wiki/topic/page.md", {"content": PAGE + "api_key=<your-key>\n"})
        self.allowed(self.vault / "wiki/topic/page.md", {"file_path": "x"})

    def test_opt_out_comes_from_write_content_or_the_existing_page(self):
        page = self.vault / "wiki/topic/formats.md"
        opted = "---\ntype: note\nsecretScan: allow\n---\n" + self.secret
        self.allowed(page, {"content": opted})
        # An edit cannot add the opt-out and a secret together; the page must already carry it.
        self.refused(page, {"new_string": opted})
        page.write_text("---\ntype: note\nsecretScan: allow\n---\nbody\n", encoding="utf-8")
        self.allowed(page, {"old_string": "body", "new_string": self.secret})
        self.refused(self.vault / "wiki/topic/other.md", {"old_string": "body", "new_string": self.secret})

    def test_value_only_edit_of_a_placeholder_is_refused(self):
        page = self.vault / "wiki/topic/page.md"
        page.write_text(PAGE + "api_key = <your-key>\n", encoding="utf-8")
        real = "abc123def456ghi789"
        for name, tool_input in {
            "claude Edit": {"old_string": "<your-key>", "new_string": real},
            "claude MultiEdit": {"edits": [{"old_string": "<your-key>", "new_string": real}]},
            "pi edit": {"edits": [{"oldText": "<your-key>", "newText": real}]},
            "pi legacy edit": {"oldText": "<your-key>", "newText": real},
        }.items():
            with self.subTest(tool=name):
                message = self.refused(page, tool_input)
                self.assertIn("credential assignment", message)
                self.assertIn(f"line {PAGE.count(chr(10)) + 1}", message)
                self.assertNotIn(real, message)
        # A split value assembled across edits is judged on the resulting text.
        self.refused(page, {"edits": [{"old_string": "<your-key>", "new_string": "abc123"},
                                      {"old_string": "abc123", "new_string": real}]})
        self.allowed(page, {"old_string": "<your-key>", "new_string": "<other-key>"})
        self.assertEqual(page.read_text(encoding="utf-8"), PAGE + "api_key = <your-key>\n")

    def test_existing_matches_are_not_new(self):
        page = self.vault / "wiki/topic/legacy.md"
        legacy = PAGE + "old key " + self.secret + "\n"
        page.write_text(legacy, encoding="utf-8")
        self.allowed(page, {"old_string": "# Page", "new_string": "# Renamed page"})
        self.allowed(page, {"content": "---\ntype: note\n---\n\nmoved down\n\n\nold key " + self.secret + "\n"})
        other = "gh" + "p_" + RANDOM[1:37]
        self.refused(page, {"old_string": "# Page", "new_string": "# Page " + other})
        self.refused(page, {"content": legacy + other})

    def test_case_different_wiki_spelling_is_scanned(self):
        # On case-sensitive filesystems the spelling does not exist yet; on case-insensitive ones it aliases wiki/.
        alias = self.vault / "WIKI/topic/page.md"
        self.refused(alias, {"content": PAGE + self.secret})
        self.refused(self.vault / "Wiki/new.md", {"old_string": "a", "new_string": self.secret})

    def test_unreadable_input_fails_closed_without_echo(self):
        result = subprocess.run(
            [sys.executable, str(MIDDLEWARE / "secret_scan.py"), "tool-input", "--vault", str(self.vault),
             "--path", str(self.vault / "wiki/a.md")],
            input="not json " + self.secret, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(self.secret, result.stderr)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.vault = root / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        self.config = root / "properties.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        self.secret = SECRETS["OpenAI-style API key"]

    @staticmethod
    def bundle(*operations):
        return {"version": 1, "operations": list(operations)}

    @staticmethod
    def op(path, content):
        return {"path": path, "expectedHash": None, "content": content}

    def snapshot(self):
        return sorted(p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*"))

    def test_inspect_and_apply_refuse_secret_drafts_with_no_partial_write(self):
        clean = self.op("wiki/clean.md", PAGE + "ok\n")
        dirty = self.op("wiki/dirty.md", PAGE + "key: " + self.secret + "\n")
        before = self.snapshot()
        with self.assertRaises(BATCH.BatchError) as raised:
            BATCH.inspect(self.vault, self.bundle(clean, dirty), self.config)
        message = str(raised.exception)
        self.assertIn("wiki/dirty.md", message)
        self.assertIn("OpenAI-style API key", message)
        self.assertNotIn(self.secret, message)
        # Apply recomputes the plan, so even a hash from a clean plan cannot publish this bundle.
        clean_plan = BATCH.inspect(self.vault, self.bundle(clean), self.config)
        with self.assertRaises(BATCH.BatchError):
            BATCH.apply(self.vault, self.bundle(clean, dirty), self.config, clean_plan["planHash"])
        self.assertEqual(self.snapshot(), before)

    def test_ledger_drafts_are_refused_before_schema_validation(self):
        content = json.dumps({"note": self.secret})
        with self.assertRaisesRegex(BATCH.BatchError, "secret-scan"):
            BATCH.inspect(self.vault, self.bundle(self.op("wiki/meta/evidence/asset-one.json", content)),
                          self.config, "wiki-ledger")

    def test_page_opt_out_is_honored_by_batch_apply(self):
        page = "---\ntype: note\nsecretScan: allow\n---\n" + self.secret + "\n"
        bundle = self.bundle(self.op("wiki/formats.md", page))
        plan = BATCH.inspect(self.vault, bundle, self.config)
        BATCH.apply(self.vault, bundle, self.config, plan["planHash"])
        self.assertEqual((self.vault / "wiki/formats.md").read_text(encoding="utf-8"), page)

    def test_source_captures_warn_without_refusing_or_changing_the_plan_hash(self):
        verbatim = "pasted log\nAuthorization: " + SECRETS["JSON Web Token"] + "\n"
        bundle = self.bundle(self.op(".raw/agent-captures/log.txt", verbatim))
        plan = BATCH.inspect(self.vault, bundle, self.config, "source-capture")
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("line 2", plan["warnings"][0])
        self.assertIn("JSON Web Token", plan["warnings"][0])
        self.assertNotIn(SECRETS["JSON Web Token"], plan["warnings"][0])
        clean = BATCH.inspect(self.vault, self.bundle(self.op(".raw/agent-captures/log.txt", "plain\n")),
                              self.config, "source-capture")
        self.assertNotIn("warnings", clean)
        BATCH.apply(self.vault, bundle, self.config, plan["planHash"], "source-capture")
        self.assertEqual((self.vault / ".raw/agent-captures/log.txt").read_text(encoding="utf-8"), verbatim)

    def publish(self, path, content):
        bundle = self.bundle(self.op(path, content))
        plan = BATCH.inspect(self.vault, bundle, self.config)
        BATCH.apply(self.vault, bundle, self.config, plan["planHash"])

    def test_legacy_pages_with_existing_matches_can_be_republished(self):
        # A page that predates the check (written outside the guarded path) must stay migratable.
        legacy = PAGE + "old key: " + self.secret + "\n"
        (self.vault / "wiki/legacy.md").write_text(legacy, encoding="utf-8")
        current = BATCH.sha(legacy.encode("utf-8"))
        edited = PAGE + "# Retitled\n\nold key: " + self.secret + "\n"
        bundle = self.bundle({"path": "wiki/legacy.md", "expectedHash": current, "content": edited})
        plan = BATCH.inspect(self.vault, bundle, self.config)
        BATCH.apply(self.vault, bundle, self.config, plan["planHash"])
        self.assertEqual((self.vault / "wiki/legacy.md").read_text(encoding="utf-8"), edited)

    def test_changing_or_adding_a_value_on_a_legacy_page_is_still_refused(self):
        legacy = PAGE + "old key: " + self.secret + "\n"
        (self.vault / "wiki/legacy.md").write_text(legacy, encoding="utf-8")
        current = BATCH.sha(legacy.encode("utf-8"))
        added = "sk" + "-proj-" + RANDOM[::-1]
        for content in (legacy + "new key: " + added + "\n", PAGE + "key: " + added + "\n"):
            with self.assertRaisesRegex(BATCH.BatchError, "secret-scan"):
                BATCH.inspect(self.vault, self.bundle({"path": "wiki/legacy.md", "expectedHash": current,
                                                       "content": content}), self.config)

    def test_cli_batch_inspect_and_apply_refuse_secrets(self):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.vault.parent), "OBSIDIAN_AGENT_CONFIG": str(self.config)}
        clean = self.bundle(self.op("wiki/clean.md", PAGE + "ok\n"))
        dirty = self.bundle(self.op("wiki/dirty.md", PAGE + "key: " + self.secret + "\n"))
        paths = {}
        for name, bundle in (("clean", clean), ("dirty", dirty)):
            paths[name] = self.vault.parent / f"{name}.json"
            paths[name].write_text(json.dumps(bundle), encoding="utf-8")

        def cli(command, name, *extra):
            return subprocess.run([sys.executable, str(CLI), command, "--bundle", str(paths[name]), "--vault", str(self.vault),
                                   "--config", str(self.config), "--json", *extra], env=env, text=True, capture_output=True)

        clean_plan = cli("batch-inspect", "clean")
        self.assertEqual(clean_plan.returncode, 0, clean_plan.stderr)
        before = self.snapshot()
        for result in (cli("batch-inspect", "dirty"),
                       cli("batch-apply", "dirty", "--plan-hash", json.loads(clean_plan.stdout)["planHash"])):
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            error = json.loads(result.stdout)["error"]
            self.assertIn("secret-scan", error)
            self.assertIn("wiki/dirty.md", error)
            self.assertIn("OpenAI-style API key", error)
            self.assertNotIn(self.secret, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_capture_inspect_surfaces_the_advisory(self):
        source = self.config.parent / "notes.txt"
        source.write_text("deploy notes\n" + SECRETS["GitHub token"] + "\n", encoding="utf-8")
        result = CAPTURE.inspect(self.vault, [source], self.config)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertNotIn(SECRETS["GitHub token"], json.dumps(result))


if __name__ == "__main__":
    unittest.main()
