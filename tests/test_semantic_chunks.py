"""Semantic chunk boundaries and explicit embedding-cache build contracts."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source
chunks = load_source(
    "semantic_chunks", str(ROOT / "scripts/semantic-chunks.py"))


class AliasEmbeddingHandler(BaseHTTPRequestHandler):
    digest = "v1"
    reported_name = "fixture:latest"
    document_inputs = []
    query_calls = 0

    def do_GET(self):
        body = json.dumps({"models": [{"name": self.reported_name, "model": self.reported_name,
                                         "digest": type(self).digest}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        inputs = payload["input"]
        if inputs == ["query"]:
            type(self).query_calls += 1
        else:
            type(self).document_inputs.extend(inputs)
        vector = [1.0, 0.0] if type(self).digest == "v1" else [0.0, 1.0]
        body = json.dumps({"embeddings": [vector for _ in inputs]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class EmbeddingHandler(BaseHTTPRequestHandler):
    requests = []
    model_digest = "sha256:fixture-v1"
    fail_contains = None

    def do_GET(self):
        body = json.dumps({"models": [{"name": "fixture", "model": "fixture", "digest": self.model_digest}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.extend(payload["input"])
        if type(self).fail_contains and any(type(self).fail_contains in value.casefold() for value in payload["input"]):
            self.send_response(503)
            self.end_headers()
            return
        vectors = []
        for text in payload["input"]:
            # Similar text gets aligned vectors, creating a semantic topic break.
            vectors.append([1.0, 0.0] if "alpha" in text.casefold() else [0.0, 1.0])
        body = json.dumps({"embeddings": vectors}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class SemanticChunkTests(unittest.TestCase):
    def test_untagged_alias_digest_recording_reuse_invalidation_and_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            wiki = vault / "wiki"
            wiki.mkdir()
            (wiki / "page.md").write_text("# Alias note\n\nLocal model alias details.", encoding="utf-8")
            server = HTTPServer(("127.0.0.1", 0), AliasEmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            AliasEmbeddingHandler.digest = "v1"
            AliasEmbeddingHandler.reported_name = "fixture:latest"
            AliasEmbeddingHandler.document_inputs = []
            AliasEmbeddingHandler.query_calls = 0
            retrieval = load_source(
                "alias_retrieval", str(ROOT / "scripts/retrieve.py"))
            try:
                built = chunks.build(vault, url, "fixture", max_chars=180, target_chars=100)
                self.assertEqual(built["indexed"], 1)
                cache_path = vault / ".vault-meta/retrieval/chunks.json"
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                self.assertEqual(cache["pages"]["wiki/page.md"]["modelDigest"], "v1")
                first_document_inputs = len(AliasEmbeddingHandler.document_inputs)
                reused = chunks.build(vault, url, "fixture", max_chars=180, target_chars=100)
                self.assertEqual(reused["reused"], 1)
                self.assertEqual(len(AliasEmbeddingHandler.document_inputs), first_document_inputs)

                AliasEmbeddingHandler.digest = "v2"
                self.assertEqual(retrieval.semantic_candidates("query", vault.resolve(), url, "fixture", 1), [],
                                 "old vectors are excluded as soon as the installed digest changes")
                self.assertEqual(AliasEmbeddingHandler.query_calls, 0,
                                 "no query vector is requested when all cached evidence is stale")
                rebuilt = chunks.build(vault, url, "fixture", max_chars=180, target_chars=100)
                self.assertEqual(rebuilt["indexed"], 1)
                self.assertEqual(rebuilt["reused"], 0)
                self.assertGreater(len(AliasEmbeddingHandler.document_inputs), first_document_inputs)
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                entry = cache["pages"]["wiki/page.md"]
                self.assertEqual(entry["modelDigest"], "v2")
                self.assertTrue(all(chunk["vector"] == [0.0, 1.0] for chunk in entry["chunks"]))
                self.assertEqual(chunks.model_digest(url, "fixture"), "v2")
                _, current_pages = retrieval._valid_semantic_pages(vault.resolve(), "fixture", url, 1)
                self.assertEqual(set(current_pages), {"wiki/page.md"})
                candidates = retrieval.semantic_candidates("query", vault.resolve(), url, "fixture", 1)
                self.assertEqual([candidate[0] for candidate in candidates], ["wiki/page.md"])
                self.assertEqual(AliasEmbeddingHandler.query_calls, 1)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_model_digest_matches_untagged_explicit_namespaced_and_registry_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = HTTPServer(("127.0.0.1", 0), AliasEmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            cases = (
                ("fixture", "fixture:latest"),
                ("fixture:v3", "fixture:v3"),
                ("org/fixture", "org/fixture:latest"),
                ("registry.example:5000/org/fixture", "registry.example:5000/org/fixture:latest"),
                ("registry.example:5000/org/fixture:v3", "registry.example:5000/org/fixture:v3"),
            )
            try:
                for requested, installed in cases:
                    with self.subTest(requested=requested):
                        AliasEmbeddingHandler.reported_name = installed
                        self.assertEqual(chunks.model_digest(url, requested), AliasEmbeddingHandler.digest)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_semantic_split_and_offsets_keep_context_and_code_fence(self):
        source = (
            "# Notes\n\n## Alpha\nalpha first thought.\nalpha second thought.\n\n"
            "## Beta\nbeta separate idea.\n```py\nprint('beta')\n```\n")
        atoms = chunks.text_units(source)
        vectors = [[1, 0] if "alpha" in atom["text"].casefold() else [0, 1] for atom in atoms]
        semantic = chunks.semantic_chunks("wiki/notes.md", source, vectors, max_chars=90, target_chars=55)
        self.assertGreaterEqual(len(semantic), 2)
        self.assertTrue(all(chunk["text"].startswith("Title: Notes") for chunk in semantic))
        self.assertTrue(all(0 <= chunk["start"] < chunk["end"] <= len(source) for chunk in semantic))
        for chunk in semantic:
            self.assertEqual(chunk["sourceHash"], chunks.sha256(source[chunk["start"]:chunk["end"]]))
        self.assertTrue(any("```py" in c["text"] and "```" in c["text"] for c in semantic))
        self.assertEqual(semantic, chunks.semantic_chunks("wiki/notes.md", source, vectors, max_chars=90, target_chars=55))

    def test_cache_reuses_unchanged_pages_and_defers_changed_pages_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            wiki = vault / "wiki"
            wiki.mkdir()
            page = wiki / "page.md"
            page.write_text("# Alpha\n\nalpha details.\n", encoding="utf-8")
            unchanged = wiki / "unchanged.md"
            unchanged.write_text("# Alpha\n\nalpha stable reference.\n", encoding="utf-8")
            server = HTTPServer(("127.0.0.1", 0), EmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            EmbeddingHandler.requests = []
            EmbeddingHandler.model_digest = "sha256:fixture-v1"
            EmbeddingHandler.fail_contains = None
            try:
                result = chunks.build(vault, url, "fixture", max_chars=120, target_chars=80)
                cache_path = vault / ".vault-meta/retrieval/chunks.json"
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                self.assertEqual(result["indexed"], 2)
                entry = cache["pages"]["wiki/page.md"]
                self.assertEqual(entry["contentHash"], chunks.sha256(page.read_text(encoding="utf-8")))
                self.assertEqual(entry["modelDigest"], "sha256:fixture-v1")
                previous = len(EmbeddingHandler.requests)
                chunks.build(vault, url, "fixture", max_chars=120, target_chars=80)
                self.assertEqual(len(EmbeddingHandler.requests), previous)
                EmbeddingHandler.model_digest = "sha256:fixture-v2"
                refreshed = chunks.build(vault, url, "fixture", max_chars=120, target_chars=80)
                self.assertEqual(refreshed["indexed"], 2, "a changed model digest invalidates cached vectors")
                self.assertGreater(len(EmbeddingHandler.requests), previous)
                page.write_text("# Beta\n\nbeta changed page.\n", encoding="utf-8")
                server.shutdown()
                thread.join()
                server.server_close()
                deferred = chunks.build(vault, "http://127.0.0.1:1", "fixture", max_chars=120, target_chars=80)
                current = json.loads(cache_path.read_text(encoding="utf-8"))
                self.assertEqual(deferred["indexed"], 0)
                self.assertEqual(deferred["reused"], 1)
                self.assertEqual(deferred["deferred"], ["wiki/page.md"])
                self.assertNotIn("wiki/page.md", current["pages"])
                self.assertIn("wiki/unchanged.md", current["pages"], "unchanged valid semantic evidence is reusable")
            finally:
                if thread.is_alive():
                    server.shutdown()
                    thread.join()
                    server.server_close()

    def test_bm25_semantic_build_is_explicit_and_optional(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "wiki").mkdir()
            (vault / "wiki/one.md").write_text("# Alpha\n\nalpha note.\n", encoding="utf-8")
            indexer = ROOT / "scripts/bm25-index.py"
            regular = subprocess.run([sys.executable, str(indexer), "build", "--vault", str(vault)],
                                     text=True, capture_output=True, check=True)
            self.assertIn("Indexed 1 Markdown pages", regular.stdout)
            cache = vault / ".vault-meta/retrieval/chunks.json"
            self.assertFalse(cache.exists(), "ordinary BM25 builds must not call embeddings")
            server = HTTPServer(("127.0.0.1", 0), EmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                semantic = subprocess.run([
                    sys.executable, str(indexer), "build", "--vault", str(vault), "--semantic-chunks",
                    "--ollama-url", f"http://127.0.0.1:{server.server_port}", "--ollama-model", "fixture",
                ], text=True, capture_output=True, check=True)
                self.assertIn("Semantic chunks: indexed 1", semantic.stdout)
                self.assertTrue(cache.is_file())
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_partial_embedding_failure_reports_only_final_surviving_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            wiki = vault / "wiki"
            wiki.mkdir()
            (wiki / "stable.md").write_text("# Stable\n\nstable source.", encoding="utf-8")
            (wiki / "alpha.md").write_text("# Alpha\n\nalpha original.", encoding="utf-8")
            (wiki / "beta.md").write_text("# Beta\n\nbeta original.", encoding="utf-8")
            server = HTTPServer(("127.0.0.1", 0), EmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                initial = chunks.build(vault, url, "fixture", max_chars=120, target_chars=80)
                self.assertEqual(initial["indexed"], 3)
                (wiki / "alpha.md").write_text("# Alpha\n\nalpha changed.", encoding="utf-8")
                (wiki / "beta.md").write_text("# Beta\n\nbeta changed.", encoding="utf-8")
                EmbeddingHandler.fail_contains = "beta changed"
                result = chunks.build(vault, url, "fixture", max_chars=120, target_chars=80)
                self.assertEqual(result["indexed"], 1)
                self.assertEqual(result["reused"], 1)
                self.assertEqual(result["deferred"], ["wiki/beta.md"])
                cache = json.loads((vault / ".vault-meta/retrieval/chunks.json").read_text(encoding="utf-8"))
                self.assertEqual(set(cache["pages"]), {"wiki/stable.md", "wiki/alpha.md"})
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_removed_reused_page_is_excluded_from_returned_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            wiki = vault / "wiki"
            wiki.mkdir()
            removed = wiki / "removed.md"
            retained = wiki / "retained.md"
            removed.write_text("# Removed\n\nremove me.", encoding="utf-8")
            retained.write_text("# Retained\n\nkeep me.", encoding="utf-8")
            server = HTTPServer(("127.0.0.1", 0), EmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            original_page_list = chunks._page_list
            calls = 0

            def remove_between_scan_and_recheck(root):
                nonlocal calls
                calls += 1
                if calls == 2:
                    removed.unlink()
                return original_page_list(root)

            try:
                chunks.build(vault, url, "fixture", max_chars=120, target_chars=80)
                with patch.object(chunks, "_page_list", side_effect=remove_between_scan_and_recheck):
                    result = chunks.build(vault, url, "fixture", max_chars=120, target_chars=80)
                self.assertEqual(result["indexed"], 0)
                self.assertEqual(result["reused"], 1)
                self.assertEqual(result["deferred"], ["wiki/removed.md"])
                cache = json.loads((vault / ".vault-meta/retrieval/chunks.json").read_text(encoding="utf-8"))
                self.assertEqual(set(cache["pages"]), {"wiki/retained.md"})
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_mutated_reused_page_is_excluded_from_returned_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            wiki = vault / "wiki"
            wiki.mkdir()
            changed = wiki / "changed.md"
            retained = wiki / "retained.md"
            changed.write_text("# Changed\n\noriginal text.", encoding="utf-8")
            retained.write_text("# Retained\n\nretained text.", encoding="utf-8")
            server = HTTPServer(("127.0.0.1", 0), EmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            original_page_list = chunks._page_list
            calls = 0

            def mutate_between_scan_and_recheck(root):
                nonlocal calls
                calls += 1
                if calls == 2:
                    changed.write_text("# Changed\n\nconcurrently modified.", encoding="utf-8")
                return original_page_list(root)

            try:
                chunks.build(vault, url, "fixture", max_chars=120, target_chars=80)
                with patch.object(chunks, "_page_list", side_effect=mutate_between_scan_and_recheck):
                    result = chunks.build(vault, url, "fixture", max_chars=120, target_chars=80)
                self.assertEqual(result["indexed"], 0)
                self.assertEqual(result["reused"], 1)
                self.assertEqual(result["deferred"], ["wiki/changed.md"])
                cache = json.loads((vault / ".vault-meta/retrieval/chunks.json").read_text(encoding="utf-8"))
                self.assertEqual(set(cache["pages"]), {"wiki/retained.md"})
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_malformed_cache_roots_leave_optional_bm25_build_successful(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            wiki = vault / "wiki"
            wiki.mkdir()
            (wiki / "current.md").write_text("# Current\n\ncurrent BM25 remains.", encoding="utf-8")
            cache_path = vault / ".vault-meta/retrieval/chunks.json"
            indexer = ROOT / "scripts/bm25-index.py"
            for malformed in ([], None, "scalar", 42):
                with self.subTest(root=malformed):
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(malformed), encoding="utf-8")
                    result = subprocess.run([
                        sys.executable, str(indexer), "build", "--vault", str(vault), "--semantic-chunks",
                        "--ollama-url", "http://127.0.0.1:1", "--ollama-model", "fixture", "--timeout", "0.1",
                    ], text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Indexed 1 Markdown pages", result.stdout)
                    self.assertIn("Semantic chunks unavailable", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertTrue((vault / ".vault-meta/retrieval/bm25.json").is_file())

    def test_context_ignores_backtick_and_tilde_fenced_heading_comments(self):
        source = (
            "~~~python\n# Fenced title comment\n~~~\n"
            "# Actual page title\n## Real section\n"
            "```python\n# A code comment\n## Not a heading\n```\n"
            "~~~text\n### Still not a heading\n~~~\n"
            "Later prose stays in the real section.\n")
        prose_start = source.index("Later prose")
        title, active = chunks._context(source, "wiki/fallback-name.md", prose_start, len(source))
        self.assertEqual(title, "Actual page title")
        self.assertEqual(active, ["Real section"])
        rendered = chunks._render("wiki/fallback-name.md", source, prose_start, len(source))
        self.assertTrue(rendered.startswith("Title: Actual page title\nHeading: Real section\n"))
        self.assertIn(source[prose_start:], rendered)
        units = chunks.text_units(source)
        self.assertTrue(any("# A code comment" in unit["text"] and "## Not a heading" in unit["text"]
                            for unit in units))
        self.assertTrue(any("### Still not a heading" in unit["text"] for unit in units))
        self.assertEqual(source[prose_start:], "Later prose stays in the real section.\n")

    def test_heading_baseline_is_not_semantic(self):
        source = "# T\nplain paragraph one.\nplain paragraph two.\n## Heading\nfinal.\n"
        baseline = chunks.heading_chunks("wiki/t.md", source, max_chars=100)
        self.assertTrue(any("Heading" in chunk["text"] for chunk in baseline))
        self.assertTrue(all(chunk["strategy"] == "heading-baseline-v1" for chunk in baseline))

    def test_rejects_non_loopback_endpoint_and_symlink_cache_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "wiki").mkdir()
            outside = vault / "outside"
            outside.mkdir()
            (vault / ".vault-meta").mkdir()
            (vault / ".vault-meta/retrieval").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                chunks.build(vault, "http://example.org:11434", "fixture")
            with self.assertRaises(ValueError):
                chunks.publish_cache(vault, {"version": chunks.CACHE_VERSION, "pages": {}})


if __name__ == "__main__":
    unittest.main()
