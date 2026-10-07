"""Focused hybrid retrieval, freshness, and service-failure contracts."""
import hashlib
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
RETRIEVE = ROOT / "scripts/retrieve.py"
CLI = ROOT / "scripts/obsidian-second-brain.py"
INDEXER = ROOT / "scripts/bm25-index.py"


class DelayedTagsHandler(BaseHTTPRequestHandler):
    started = threading.Event()
    release = threading.Event()
    embed_calls = 0

    def do_GET(self):
        type(self).started.set()
        type(self).release.wait()
        body = json.dumps({"models": [{"name": "fixture", "digest": "digest"}]}).encode()
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # The client correctly timed out and closed this fixture request.

    def do_POST(self):
        type(self).embed_calls += 1
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps({"embeddings": [[0.0, 1.0] for _ in payload["input"]]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class QueryEmbedding(BaseHTTPRequestHandler):
    calls = 0
    tags_calls = []
    post_paths = []
    model_digest = "digest"
    model_name = "fixture"
    requested_models = []
    failing = False
    tags_failing = False

    def do_GET(self):
        type(self).tags_calls.append(self.path)
        if type(self).tags_failing:
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps({"models": [{"name": type(self).model_name, "digest": type(self).model_digest}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).calls += 1
        type(self).post_paths.append(self.path)
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requested_models.append(payload["model"])
        if type(self).failing:
            self.send_response(503)
            self.end_headers()
            return
        vectors = [[0.0, 1.0] if "paraphrase" in value.casefold() else [1.0, 0.0]
                   for value in payload["input"]]
        body = json.dumps({"embeddings": vectors}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class HybridRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        wiki = self.vault / "wiki"
        wiki.mkdir()
        (wiki / "lexical.md").write_text("# Lexical\npainless solution text", encoding="utf-8")
        target = wiki / "semantic-target.md"
        self.target_text = "# Semantic Target\nuncomplicated cure explanation\n"
        target.write_text(self.target_text, encoding="utf-8")
        self.build_index()
        self.write_cache()
        QueryEmbedding.calls = 0
        QueryEmbedding.tags_calls = []
        QueryEmbedding.post_paths = []
        QueryEmbedding.model_digest = "digest"
        QueryEmbedding.model_name = "fixture"
        QueryEmbedding.requested_models = []
        QueryEmbedding.failing = False
        QueryEmbedding.tags_failing = False
        self.server = HTTPServer(("127.0.0.1", 0), QueryEmbedding)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def build_index(self):
        subprocess.run([sys.executable, str(INDEXER), "build", "--vault", str(self.vault)],
                       check=True, capture_output=True)

    def write_cache(self):
        page = self.vault / "wiki/semantic-target.md"
        text = page.read_text(encoding="utf-8")
        chunk = {"text": "Title: Semantic Target\\n" + text, "start": 0, "end": len(text),
                 "sourceHash": hashlib.sha256(text.encode()).hexdigest(), "vector": [0.0, 1.0],
                 "strategy": "semantic-adjacent-cosine-v1"}
        lexical_path = self.vault / "wiki/lexical.md"
        lexical_text = lexical_path.read_text(encoding="utf-8")
        lexical_chunk = {"text": "Title: Lexical\\n" + lexical_text, "start": 0, "end": len(lexical_text),
                         "sourceHash": hashlib.sha256(lexical_text.encode()).hexdigest(), "vector": [1.0, 0.0],
                         "strategy": "semantic-adjacent-cosine-v1"}
        settings = {"algorithm": "semantic-adjacent-cosine-v1", "maxChars": 1800, "targetChars": 1000}
        cache = {"version": 1, "pages": {
            "wiki/semantic-target.md": {"contentHash": hashlib.sha256(text.encode()).hexdigest(), "model": "fixture",
                "modelDigest": "digest", "settings": settings, "chunks": [chunk]},
            "wiki/lexical.md": {"contentHash": hashlib.sha256(lexical_text.encode()).hexdigest(), "model": "fixture",
                "modelDigest": "digest", "settings": settings, "chunks": [lexical_chunk]}}}
        path = self.vault / ".vault-meta/retrieval/chunks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")

    def retrieve(self, *options):
        return subprocess.run([sys.executable, str(RETRIEVE), "paraphrase solution",
                               "--vault", str(self.vault), *options], text=True, capture_output=True)

    def test_explicit_hybrid_finds_semantic_page_outside_lexical_candidates_and_groups_pages(self):
        lexical = self.retrieve("--limit", "1")
        self.assertEqual(QueryEmbedding.calls, 0, "default lexical search is service-free")
        self.assertIn("wiki/lexical.md", lexical.stdout)
        hybrid = self.retrieve("--hybrid", "--limit", "10", "--ollama-url", self.url, "--ollama-model", "fixture")
        self.assertEqual(hybrid.returncode, 0, hybrid.stderr)
        semantic_line = next(line for line in hybrid.stdout.splitlines() if "wiki/semantic-target.md" in line)
        self.assertIn("\tsemantic\t", semantic_line)
        self.assertIn("lines=1-2 chars=0-", semantic_line)
        self.assertEqual(QueryEmbedding.calls, 1)
        repeated = self.retrieve("--hybrid", "--limit", "10", "--ollama-url", self.url, "--ollama-model", "fixture")
        self.assertEqual(repeated.stdout, hybrid.stdout, "fused ranking and ties are deterministic")

    def test_stale_chunk_is_excluded_but_rebuilt_page_level_bm25_is_current(self):
        target = self.vault / "wiki/semantic-target.md"
        target.write_text("# Updated\nparaphrase solution now lexical\n", encoding="utf-8")
        self.build_index()
        result = self.retrieve("--hybrid", "--ollama-url", self.url, "--ollama-model", "fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        target_line = next(line for line in result.stdout.splitlines() if "semantic-target.md" in line)
        self.assertNotIn("\tsemantic\t", target_line, "stale vector cannot be evidence")
        self.assertIn("wiki/semantic-target.md", result.stdout, "fresh page-level BM25 includes changed content")

    def test_installed_model_digest_change_invalidates_cached_vectors_via_localhost(self):
        QueryEmbedding.model_digest = "new-digest"
        result = self.retrieve("--hybrid", "--ollama-url", self.url.replace("127.0.0.1", "localhost"),
                               "--ollama-model", "fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(QueryEmbedding.tags_calls)
        self.assertTrue(all(path == "/api/tags" for path in QueryEmbedding.tags_calls))
        self.assertNotIn("wiki/semantic-target.md", result.stdout,
                         "vectors from a previous installed model digest are not candidates")
        QueryEmbedding.model_digest = "digest"
        valid = self.retrieve("--hybrid", "--ollama-url", self.url.replace("127.0.0.1", "localhost"),
                              "--ollama-model", "fixture")
        self.assertIn("wiki/semantic-target.md", valid.stdout)
        self.assertTrue(QueryEmbedding.post_paths)
        self.assertTrue(all(path == "/api/embed" for path in QueryEmbedding.post_paths))
        self.assertTrue(all(path == "/api/tags" for path in QueryEmbedding.tags_calls))

    def test_tags_outage_with_working_embed_fails_closed_and_restored_digest_invalidates(self):
        cache_path = self.vault / ".vault-meta/retrieval/chunks.json"
        cached_before = cache_path.read_bytes()
        QueryEmbedding.model_digest = "new-digest"
        QueryEmbedding.tags_failing = True
        retrieve_module = load_source(
            "metadata_outage_retrieval", str(RETRIEVE))
        probe = retrieve_module.embeddings(["probe"], retrieve_module.local_endpoint(self.url), "fixture", 1)
        self.assertEqual(probe, [[1.0, 0.0]], "query embedding endpoint remains available")
        QueryEmbedding.calls = 0
        QueryEmbedding.post_paths = []
        result = self.retrieve("--hybrid", "--ollama-url", self.url, "--ollama-model", "fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wiki/lexical.md", result.stdout, "page BM25 remains usable during metadata outage")
        self.assertNotIn("wiki/semantic-target.md", result.stdout)
        self.assertEqual(QueryEmbedding.calls, 0,
                         "unverifiable cache identity is rejected before semantic query ranking")
        self.assertEqual(cache_path.read_bytes(), cached_before,
                         "unverified cache is retained unchanged for later verified reuse")

        QueryEmbedding.tags_failing = False
        restored = self.retrieve("--hybrid", "--ollama-url", self.url, "--ollama-model", "fixture")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("wiki/lexical.md", restored.stdout)
        self.assertNotIn("wiki/semantic-target.md", restored.stdout,
                         "restored v2 metadata invalidates the still-cached v1 vectors")
        self.assertEqual(QueryEmbedding.calls, 0)
        self.assertEqual(cache_path.read_bytes(), cached_before)

    def test_invalid_embedding_origins_are_rejected_before_any_opener_dispatch(self):
        retrieve_module = load_source(
            "retrieval_origin_contract", str(RETRIEVE))
        chunks_module = load_source(
            "chunk_origin_contract", str(ROOT / "scripts/semantic-chunks.py"))
        with patch("urllib.request.build_opener") as build_opener:
            for origin in ("http://local/api/embed", "https://localhost/api/embed",
                           "http://localhost:bad/api/embed"):
                with self.subTest(origin=origin), self.assertRaises(ValueError):
                    retrieve_module.embeddings(["query"], origin, "fixture", 1)
            for origin in ("http://local", "https://localhost", "http://localhost:bad"):
                with self.subTest(origin=origin), self.assertRaises(ValueError):
                    chunks_module._request(origin, "/api/tags", timeout=1)
            build_opener.assert_not_called()

    def test_metadata_get_uses_query_timeout_and_falls_back_to_bm25(self):
        DelayedTagsHandler.started = threading.Event()
        DelayedTagsHandler.release = threading.Event()
        DelayedTagsHandler.embed_calls = 0
        server = HTTPServer(("127.0.0.1", 0), DelayedTagsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        process = None
        try:
            process = subprocess.Popen([sys.executable, str(RETRIEVE), "paraphrase solution",
                                        "--vault", str(self.vault), "--hybrid", "--ollama-url", url,
                                        "--ollama-model", "fixture", "--timeout", "0.05"],
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertTrue(DelayedTagsHandler.started.wait(timeout=2), "metadata GET reached loopback fixture")
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("metadata GET ignored the supplied short timeout (default timeout would be much longer)")
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("wiki/lexical.md", stdout, "metadata timeout keeps page-level BM25 results")
            self.assertNotIn("wiki/semantic-target.md", stdout)
            self.assertEqual(DelayedTagsHandler.embed_calls, 0,
                             "unknown model identity must not proceed to query-vector ranking")
        finally:
            DelayedTagsHandler.release.set()
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            server.shutdown()
            thread.join()
            server.server_close()

    def test_embedding_outage_falls_back_without_hiding_current_bm25(self):
        target = self.vault / "wiki/semantic-target.md"
        target.write_text("# Updated\nparaphrase solution current BM25 text\n", encoding="utf-8")
        self.build_index()
        QueryEmbedding.failing = True
        result = self.retrieve("--hybrid", "--ollama-url", self.url, "--ollama-model", "fixture")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Semantic search unavailable", result.stderr)
        self.assertIn("wiki/semantic-target.md", result.stdout)

    def test_date_search_remains_lexical_and_never_contacts_service(self):
        (self.vault / "wiki/meeting-2026-05-12.md").write_text(
            "# Meeting 2026-05-12\nparaphrase solution", encoding="utf-8")
        self.build_index()
        result = subprocess.run([sys.executable, str(RETRIEVE), "meeting 2026-05-12", "--vault", str(self.vault),
                                 "--hybrid", "--ollama-url", self.url, "--ollama-model", "fixture"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("1. wiki/meeting-2026-05-12.md"), result.stdout)
        self.assertEqual(QueryEmbedding.calls, 0)

    def test_embedding_defaults_use_qwen_for_all_build_and_search_entrypoints(self):
        model = "qwen3-embedding:4b"
        QueryEmbedding.model_name = model
        config = str(self.vault / "absent-fixture-config.json")
        for builder in (CLI, INDEXER):
            command = [sys.executable, str(builder), "build", "--vault", str(self.vault),
                       "--semantic-chunks", "--ollama-url", self.url]
            if builder == CLI:
                command += ["--config", config, "--json"]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            cache = json.loads((self.vault / ".vault-meta/retrieval/chunks.json").read_text())
            self.assertEqual(len(cache["pages"]), 2)
            self.assertTrue(all(entry["model"] == model for entry in cache["pages"].values()))
        cache_path = self.vault / ".vault-meta/retrieval/chunks.json"
        before = cache_path.read_bytes()
        for command in (
            [sys.executable, str(RETRIEVE), "paraphrase solution"],
            [sys.executable, str(CLI), "search", "paraphrase solution", "--config", config, "--json"],
            ["node", str(ROOT / "scripts/obsidian-second-brain.mjs"), "search", "paraphrase solution",
             "--config", config, "--json"],
        ):
            result = subprocess.run(command + ["--vault", str(self.vault), "--hybrid", "--ollama-url", self.url],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            if "--json" in command:
                self.assertEqual(json.loads(result.stdout)["fallback"], "hybrid")
            else:
                self.assertIn("\t", result.stdout)
        with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
            module = load_source("qwen_default_cli", str(CLI))
            response = module.search(self.vault.resolve(), "paraphrase solution", 10,
                                     hybrid=True, ollama_url=self.url)
        self.assertEqual(response["fallback"], "hybrid")
        self.assertEqual(set(QueryEmbedding.requested_models), {model})
        self.assertEqual(cache_path.read_bytes(), before, "queries do not rebuild or migrate caches")
        self.assertTrue(all(path == "/api/embed" for path in QueryEmbedding.post_paths), "no model pull")

    def test_qwen_default_preserves_old_model_cache_and_explicit_nomic_override(self):
        old_model = "nomic-embed-text"
        QueryEmbedding.model_name = old_model + ":latest"
        cache_path = self.vault / ".vault-meta/retrieval/chunks.json"
        cache = json.loads(cache_path.read_text())
        for entry in cache["pages"].values():
            entry["model"] = old_model
        cache_path.write_text(json.dumps(cache))
        before = cache_path.read_bytes()
        config = str(self.vault / "absent-fixture-config.json")
        command = ["node", str(ROOT / "scripts/obsidian-second-brain.mjs"), "search", "paraphrase solution",
                   "--vault", str(self.vault), "--hybrid", "--ollama-url", self.url, "--config", config, "--json"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["fallback"], "bm25")
        self.assertEqual(QueryEmbedding.calls, 0, "missing Qwen identity cannot embed or use Nomic vectors")
        self.assertEqual(cache_path.read_bytes(), before)
        override = subprocess.run(command + ["--ollama-model", old_model], capture_output=True, text=True)
        self.assertEqual(override.returncode, 0, override.stderr)
        self.assertEqual(json.loads(override.stdout)["fallback"], "hybrid")
        self.assertEqual(QueryEmbedding.requested_models, [old_model])
        self.assertEqual(cache_path.read_bytes(), before)
        self.assertTrue(all(path == "/api/embed" for path in QueryEmbedding.post_paths), "no model pull")

    def test_shared_cli_build_opt_in_builds_semantic_cache(self):
        (self.vault / ".vault-meta/retrieval/chunks.json").unlink()
        ordinary = subprocess.run([sys.executable, str(CLI), "build", "--vault", str(self.vault), "--json"],
                                  text=True, capture_output=True)
        self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
        self.assertFalse((self.vault / ".vault-meta/retrieval/chunks.json").exists())
        self.assertEqual(QueryEmbedding.calls, 0, "ordinary CLI build must not contact embeddings")
        result = subprocess.run([sys.executable, str(CLI), "build", "--vault", str(self.vault),
                                 "--semantic-chunks", "--ollama-url", self.url, "--ollama-model", "fixture", "--json"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["indexed"], 2)
        self.assertEqual(response["semantic"]["indexed"], 2)
        self.assertTrue((self.vault / ".vault-meta/retrieval/chunks.json").is_file())

    def test_shared_cli_reports_reused_and_deferred_counts_without_negative_values(self):
        target = self.vault / "wiki/semantic-target.md"
        target.write_text("# Updated Target\nnew changed content", encoding="utf-8")
        QueryEmbedding.failing = True
        result = subprocess.run([sys.executable, str(CLI), "build", "--vault", str(self.vault),
                                 "--semantic-chunks", "--ollama-url", self.url,
                                 "--ollama-model", "fixture", "--json"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        semantic = json.loads(result.stdout)["semantic"]
        self.assertEqual(semantic["indexed"], 0)
        self.assertEqual(semantic["reused"], 1)
        self.assertEqual(semantic["deferred"], ["wiki/semantic-target.md"])
        self.assertGreaterEqual(semantic["indexed"], 0)
        self.assertGreaterEqual(semantic["reused"], 0)

    def test_shared_cli_hybrid_search_is_explicit_and_returns_chunk_locations(self):
        result = subprocess.run([sys.executable, str(CLI), "search", "paraphrase solution",
                                 "--vault", str(self.vault), "--hybrid", "--json",
                                 "--ollama-url", self.url, "--ollama-model", "fixture"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        target = next(item for item in response["results"] if item["path"] == "wiki/semantic-target.md")
        self.assertEqual(response["fallback"], "hybrid")
        self.assertEqual(target["match"], "semantic")
        self.assertEqual(target["lineStart"], 1)
        self.assertGreater(target["charEnd"], target["charStart"])

    def test_forged_unsafe_cache_path_never_becomes_candidate(self):
        cache_path = self.vault / ".vault-meta/retrieval/chunks.json"
        cache = json.loads(cache_path.read_text())
        cache["pages"]["wiki/../outside.md"] = cache["pages"].pop("wiki/semantic-target.md")
        cache_path.write_text(json.dumps(cache))
        result = self.retrieve("--hybrid", "--ollama-url", self.url, "--ollama-model", "fixture")
        self.assertNotIn("outside.md", result.stdout)


if __name__ == "__main__":
    unittest.main()
