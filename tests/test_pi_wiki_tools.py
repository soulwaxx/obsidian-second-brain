import hashlib
import importlib.util
import json
import os
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from module_loading import load_source

cli = load_source("pi_wiki_cli_tests", str(SCRIPTS / "obsidian-second-brain.py"))
spec = importlib.util.spec_from_file_location("pi_wiki_tools", SCRIPTS / "pi_wiki_tools.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class PiWikiToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pi wiki ü ")
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault space ü"
        (self.vault / "wiki" / "topic").mkdir(parents=True)
        self.page = self.vault / "wiki" / "topic" / "coffee ü.md"
        self.source = "---\ntype: note\n---\n# Coffee\nEspresso evidence.\n"
        self.page.write_text(self.source, encoding="utf-8")
        self.config = self.root / "properties.json"
        self.config.write_text(json.dumps({"vaultPath": str(self.vault)}), encoding="utf-8")
        self.env = {"HOME": str(self.root), "OBSIDIAN_AGENT_CONFIG": str(self.config), "OBSIDIAN_VAULT_PATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
        self.old_env = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        cli.INDEX_MODULE.build(self.vault)

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def test_config_vault_selection_and_cli_payload_parity_without_cwd_dependency(self):
        other = self.root / "other"
        other.mkdir()
        before = self.snapshot()
        result = bridge.run("search", {"query": "espresso"})
        expected = cli.search(self.vault.resolve(), "espresso", 10)
        self.assertEqual(result, {"operation": "search", "ok": True, "payload": expected})
        self.assertEqual(result["payload"]["results"][0]["path"], "wiki/topic/coffee ü.md")
        env_vault = self.root / "env-vault"
        (env_vault / "wiki").mkdir(parents=True)
        os.environ["OBSIDIAN_VAULT_PATH"] = str(env_vault)
        self.assertEqual(bridge.run("search", {"query": "espresso"})["payload"]["vault"], str(env_vault.resolve()))
        self.assertEqual(bridge.run("search", {"query": "espresso", "vault": str(self.vault)})["payload"]["vault"], str(self.vault.resolve()))
        self.assertEqual(self.snapshot(), before, "native reads preserve bytes and mtimes of vault/config/cache")

    def test_read_outline_hash_and_coordinate_parity_and_error_envelopes(self):
        digest = hashlib.sha256(self.page.read_bytes()).hexdigest()
        outline = bridge.run("outline", {"page": "wiki/topic/coffee ü.md"})
        expected_outline = cli.READS_MODULE.outline_page(self.vault.resolve(), "wiki/topic/coffee ü.md")
        self.assertEqual(outline["payload"], expected_outline)
        read = bridge.run("read", {"page": "wiki/topic/coffee ü.md", "expectedHash": digest,
                                    "start": 0, "end": len(self.source), "maxChars": 12})
        self.assertEqual(read["payload"]["sourceHash"], digest)
        self.assertTrue(read["payload"]["continuation"])
        saved_argv = sys.argv
        sys.argv = ["pi_wiki_tools.py", "read", json.dumps({"page": "wiki/topic/coffee ü.md", "expectedHash": "0" * 64, "section": "heading:0"})]
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                bridge.main()
        finally:
            sys.argv = saved_argv
        stale = json.loads(output.getvalue())
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["code"], "source-changed")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            bridge.run("read", {"page": "wiki/topic/coffee ü.md", "expectedHash": digest})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            bridge.run("read", {"page": "wiki/topic/coffee ü.md", "expectedHash": digest,
                                 "section": "heading:0", "start": 0, "end": 2})

    def test_unknown_section_id_returns_actionable_invalid_request_without_evidence(self):
        before = self.snapshot()
        params = {"page": "wiki/topic/coffee ü.md",
                  "expectedHash": hashlib.sha256(self.page.read_bytes()).hexdigest()}
        for section in ("Coffee", "heading:999", "missing"):
            with self.subTest(section=section):
                output = io.StringIO()
                with patch.object(sys, "argv", ["pi_wiki_tools.py", "read", json.dumps({**params, "section": section})]), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(bridge.main(), 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result["operation"], "read")
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "invalid-request")
                self.assertIn("wiki_outline", result["message"])
                self.assertIn("heading:<offset>", result["message"])
                self.assertLessEqual(len(result["message"]), 1000)
                self.assertNotIn("payload", result)
                self.assertNotIn("Espresso evidence", output.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_missing_index_is_honest_navigation_fallback_without_cache_creation(self):
        index = self.vault / ".vault-meta/retrieval/bm25.json"
        index.unlink()
        snapshot_without_index = self.snapshot()
        result = bridge.run("search", {"query": "espresso"})
        self.assertEqual(result["payload"]["fallback"], "navigation")
        self.assertEqual(result["payload"]["freshness"], "unavailable")
        self.assertFalse(index.exists())
        self.assertEqual(self.snapshot(), snapshot_without_index)

    def test_missing_markdown_parser_is_structured_dependency_error(self):
        saved_argv = sys.argv
        sys.argv = ["pi_wiki_tools.py", "outline", json.dumps({"vault": str(self.vault), "page": "wiki/topic/coffee ü.md"})]
        output = io.StringIO()
        try:
            with patch.dict(sys.modules, {"markdown_it": None}), contextlib.redirect_stdout(output):
                self.assertEqual(bridge.main(), 0)
        finally:
            sys.argv = saved_argv
        result = json.loads(output.getvalue())
        self.assertEqual(result["operation"], "outline")
        self.assertEqual(result["code"], "dependency-missing")
        self.assertLessEqual(len(result["message"]), 1000)

    def test_explicit_read_and_outline_do_not_load_selected_config(self):
        digest = hashlib.sha256(self.page.read_bytes()).hexdigest()
        previous = os.environ.get("OBSIDIAN_AGENT_CONFIG")
        os.environ["OBSIDIAN_AGENT_CONFIG"] = str(self.root / "unreadable config directory")
        Path(os.environ["OBSIDIAN_AGENT_CONFIG"]).mkdir()
        try:
            with patch.object(cli, "config_path", side_effect=AssertionError("explicit read must bypass config")), \
                    patch.object(cli, "load_config", side_effect=AssertionError("explicit read must bypass config")):
                result = bridge.run("read", {"vault": str(self.vault), "page": "wiki/topic/coffee ü.md",
                                               "expectedHash": digest, "start": 0, "end": 10})
                self.assertTrue(result["ok"], result)
                outline = bridge.run("outline", {"vault": str(self.vault), "page": "wiki/topic/coffee ü.md"})
                self.assertTrue(outline["ok"], outline)
        finally:
            if previous is None:
                os.environ.pop("OBSIDIAN_AGENT_CONFIG", None)
            else:
                os.environ["OBSIDIAN_AGENT_CONFIG"] = previous

    def test_bridge_load_failure_is_structured_dependency_error(self):
        saved_argv = sys.argv
        saved_error = bridge.CLI_LOAD_ERROR
        sys.argv = ["pi_wiki_tools.py", "doctor", "{}"]
        bridge.CLI_LOAD_ERROR = "simulated missing module"
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                self.assertEqual(bridge.main(), 0)
        finally:
            sys.argv = saved_argv
            bridge.CLI_LOAD_ERROR = saved_error
        result = json.loads(output.getvalue())
        self.assertEqual(result["code"], "dependency-missing")
        self.assertLessEqual(len(result["message"]), 1000)

    def test_malformed_config_is_exposed_by_doctor_and_other_operations_fail_closed(self):
        self.config.write_text("{broken", encoding="utf-8")
        doctor = bridge.run("doctor", {})
        self.assertFalse(doctor["ok"])
        self.assertEqual(doctor["code"], "invalid-config")
        self.assertTrue(doctor["payload"]["config"]["error"])
        search = bridge.run("search", {"query": "espresso"})
        self.assertFalse(search["ok"])
        self.assertEqual(search["code"], "invalid-config")
        explicit = bridge.run("read", {"vault": str(self.vault), "page": "wiki/topic/coffee ü.md",
                                        "expectedHash": hashlib.sha256(self.page.read_bytes()).hexdigest(), "start": 0, "end": 4})
        self.assertTrue(explicit["ok"], explicit)

    def test_no_network_without_hybrid_and_hybrid_errors_keep_bm25(self):
        with patch.object(cli.RETRIEVAL_MODULE, "semantic_candidates", side_effect=AssertionError("network attempted")) as call:
            result = bridge.run("search", {"query": "espresso"})
            self.assertEqual(result["payload"]["fallback"], "bm25")
            call.assert_not_called()
        with patch.object(cli.RETRIEVAL_MODULE, "semantic_candidates", side_effect=RuntimeError("offline fixture")):
            result = bridge.run("search", {"query": "espresso", "hybrid": True})
        self.assertEqual(result["payload"]["fallback"], "bm25")
        self.assertEqual(result["payload"]["semanticError"], "offline fixture")
        self.assertTrue(result["payload"]["results"])

    def test_result_envelope_rejects_unknown_fields_and_unbounded_diagnostics(self):
        self.assertFalse(bridge.valid_envelope("search", {"operation": "search", "ok": True, "rawArgs": ["secret"]}))
        self.assertFalse(bridge.valid_envelope("search", {"operation": "search", "ok": False, "message": "x" * 1001}))
        self.assertFalse(bridge.valid_envelope("search", {"operation": "search", "ok": True, "payload": []}))
        self.assertTrue(bridge.valid_envelope("search", {"operation": "search", "ok": True, "payload": {"results": []}}))

    def test_bridge_rejects_extra_fields_and_unbounded_values(self):
        for operation, params in (("doctor", {"args": ["search"]}),
                                  ("search", {"query": "x", "limit": 101}),
                                  ("outline", {"page": "wiki/a.md", "offset": -1}),
                                  ("read", {"page": "wiki/a.md", "expectedHash": "0" * 64, "start": True, "end": 2})):
            with self.subTest(operation=operation, params=params), self.assertRaises(ValueError):
                bridge.validate(operation, params)

    def test_config_selection_without_vault_and_absolute_vault_requirement(self):
        self.config.write_text(json.dumps({}), encoding="utf-8")
        result = bridge.run("search", {"query": "espresso"})
        self.assertEqual(result["code"], "no-vault")
        with self.assertRaises(ValueError):
            bridge.validate("search", {"query": "espresso", "vault": "relative"})


if __name__ == "__main__":
    unittest.main()
