"""Conservative current/history projection contracts on public-safe fixtures."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import retrieval_projection as P
from module_loading import load_source
B = load_source("projection_bm25", str(ROOT / "scripts/bm25-index.py"))
R = load_source("projection_retrieve", str(ROOT / "scripts/retrieve.py"))
C = load_source("projection_chunks", str(ROOT / "scripts/semantic-chunks.py"))
CLI = load_source("projection_cli", str(ROOT / "scripts/obsidian-second-brain.py"))


class ProjectionTests(unittest.TestCase):
    def test_masks_only_clear_strike_preserving_original_coordinates(self):
        source = "# Café\n\n~~Old quota 10~~ Current quota 20.\nUnicode: ~~élève~~ città.\n"
        projected = P.current(source)
        self.assertEqual(len(projected), len(source))
        self.assertEqual(projected.count("\n"), source.count("\n"))
        self.assertNotIn("Old quota", projected)
        self.assertNotIn("élève", projected)
        self.assertEqual(projected.index("Current quota"), source.index("Current quota"))
        self.assertEqual(projected.index("città"), source.index("città"))
        self.assertEqual(P.current(projected), projected)

    def test_code_literal_links_quotes_and_frontmatter_untouched(self):
        sources = [
            "```md\n~~literal~~\n```\n", "~~~md\n~~literal~~\n~~~\n",
            "    ~~indented code~~\n", "> ~~quoted example~~\n", "`~~inline~~`\n",
            "``~~code with ` backtick~~``\n", "\\~~escaped~~\n",
            "[label](https://example.test/~~literal~~)\n", "[~~label~~](url)\n",
            "~~~not-strike~~~\n", "~~ spaces ~~\n", "~~unclosed\n",
            "---\ntitle: '~~literal metadata~~'\n---\n", "<code>~~literal~~</code>\n",
            "<pre>\n~~literal block~~\n</pre>\n", "<!-- example\n~~literal~~\n-->\n",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(P.current(source), source)
        self.assertEqual(P.current("`unclosed\n~~literal~~\n"), "`unclosed\n~~literal~~\n")

    def test_current_and_iso_history_align_lexical_semantic_and_excerpts(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "wiki").mkdir()
            source = "---\ntype: note\n---\n# Quota\n\n## 2025-02-14\n~~Old quota was ten~~\n\nCurrent quota is twenty.\n"
            path = vault / "wiki/quota.md"
            path.write_text(source)
            original = path.read_bytes()
            B.build(vault)
            data = B.load_index(vault / B.DEFAULT_INDEX)
            self.assertEqual(B.query(data, "old ten"), [])
            self.assertTrue(B.query(data, "old ten 2025-02-14"))
            result = CLI.search(vault, "quota", 10)
            self.assertTrue(result["results"])
            self.assertNotIn("Old quota", result["results"][0]["excerpt"])
            self.assertIn("Current quota is twenty", result["results"][0]["excerpt"])
            history = CLI.search(vault, "old ten 2025-02-14", 10)
            self.assertIn("Old quota", history["results"][0]["excerpt"])
            sent = []
            def embed(inputs, *args):
                sent.extend(inputs)
                return [[1., 0.] for _ in inputs]
            with patch.object(C, "model_digest", return_value="verified"), patch.object(C, "embeddings", side_effect=embed):
                C.build(vault, "http://127.0.0.1:1", "fixture")
            self.assertTrue(sent)
            self.assertTrue(all("Old quota" not in value for value in sent))
            cache = json.loads((vault / C.DEFAULT_CACHE).read_text())
            for chunk in cache["pages"]["wiki/quota.md"]["chunks"]:
                self.assertEqual(chunk["sourceHash"], hashlib.sha256(source[chunk["start"]:chunk["end"]].encode()).hexdigest())
            r = CLI.RETRIEVAL_MODULE
            cm = load_source("projection_service", str(ROOT / "scripts/semantic-chunks.py"))
            with patch.object(r, "load_source", return_value=cm), patch.object(cm, "model_digest", return_value="verified"), patch.object(cm, "embeddings", return_value=[[1., 0.]]):
                hybrid = CLI.search(vault, "quota", 10, True, ollama_model="fixture")
            self.assertEqual(hybrid["fallback"], "hybrid")
            self.assertTrue(all("Old quota" not in result["excerpt"] for result in hybrid["results"]))
            self.assertIn("Current quota is twenty", hybrid["results"][0]["excerpt"])
            self.assertEqual(path.read_bytes(), original)

    def test_structured_search_excerpt_matches_qualifiers_not_page_title_or_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "wiki").mkdir()
            source = ("# Cashflow Manager\n"
                      "## 2026-04-25 — auth and forecast\n"
                      "Cashflow Manager authentication setup.\n"
                      "## 2026-04-25 — billing migration\n"
                      "Billing migration evidence.\n")
            (vault / "wiki/cashflow.md").write_text(source)
            B.build(vault)

            result = CLI.search(vault, "cashflow manager 2026-04-25 billing migration", 10)

            self.assertEqual(result["results"][0]["match"], "canonical-date-section")
            self.assertIn("Billing migration evidence.", result["results"][0]["excerpt"])
            self.assertNotIn("authentication setup", result["results"][0]["excerpt"])
            self.assertEqual((result["results"][0]["lineStart"], result["results"][0]["lineEnd"]), (5, 5))

    def test_dated_page_title_does_not_override_relevant_child_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "wiki").mkdir()
            source = ("# Cashflow Manager 2026-04-25\n"
                      "## 2026-04-25 — auth and forecast\n"
                      "Authentication migration setup.\n\n\n"
                      "## 2026-04-25 — billing migration\n"
                      "Billing migration evidence.\n")
            (vault / "wiki/cashflow.md").write_text(source)
            B.build(vault)

            result = CLI.search(vault, "cashflow manager 2026-04-25 billing migration", 10)

            self.assertIn("Billing migration evidence.", result["results"][0]["excerpt"])
            self.assertNotIn("Authentication migration setup.", result["results"][0]["excerpt"])
            self.assertEqual((result["results"][0]["lineStart"], result["results"][0]["lineEnd"]), (7, 7))

    def test_historical_excerpt_uses_relevant_dated_section_and_ends_at_boundary(self):
        source = ("# Cashflow Manager\n"
                  "## 2026-04-25 — auth and forecast\nAuth forecast discussion.\n"
                  "## 2026-04-25 — billing migration\nBilling migration evidence.\n"
                  "## 2026-04-26 — unrelated\nLater evidence.\n")
        excerpt, start, end = P.excerpt(source, ["cashflow", "manager", "2026-04-25", "billing", "migration"],
                                        historical=True, iso_date="2026-04-25")
        self.assertIn("Billing migration evidence", excerpt)
        self.assertNotIn("Auth forecast", excerpt)
        self.assertNotIn("Later evidence", excerpt)
        self.assertEqual((start, end), (5, 5))

    def test_legacy_rerank_projects_superseded_spans_before_embedding(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "wiki").mkdir()
            page = vault / "wiki/archive.md"
            page.write_text("# Archive\n~~Quota was ten~~\nCurrent quota is twenty.\n")
            sent = []
            with patch.object(R, "embeddings", side_effect=lambda inputs, *args: (sent.extend(inputs) or [[1.0, 0.0] for _ in inputs])):
                R.rerank("quota", [("wiki/archive.md", 1.0)], vault, "http://127.0.0.1:11434", "fixture", 1)
            self.assertIn("Current quota is twenty", sent[1])
            self.assertNotIn("Quota was ten", sent[1])

    def test_discovery_resolution_error_preserves_cache_and_freshness_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "wiki").mkdir()
            page = vault / "wiki/blocked.md"
            page.write_text("distinctive evidence")
            B.build(vault)
            index = vault / B.DEFAULT_INDEX
            before = index.read_bytes()
            original = Path.resolve

            def failing_resolve(path, *args, **kwargs):
                if path == page:
                    raise PermissionError("fixture cannot resolve discovered page")
                return original(path, *args, **kwargs)

            with patch.object(Path, "resolve", failing_resolve):
                with self.assertRaisesRegex(PermissionError, "fixture cannot resolve discovered page"):
                    B.build(vault)
                freshness = CLI.freshness(index, ["wiki/blocked.md"], vault)
            self.assertEqual(index.read_bytes(), before)
            self.assertNotEqual(freshness, "current-by-hash")

    def test_traversal_error_preserves_previous_manifest_and_freshness_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "wiki/restricted").mkdir(parents=True)
            (vault / "wiki/open.md").write_text("open note")
            (vault / "wiki/restricted/hidden.md").write_text("hidden note")
            B.build(vault)
            index = vault / B.DEFAULT_INDEX
            before = index.read_bytes()
            original = B.os.scandir
            def failing_scandir(path):
                if Path(path) == vault / "wiki/restricted":
                    raise PermissionError("fixture unreadable subtree")
                return original(path)
            with patch.object(B.os, "scandir", side_effect=failing_scandir):
                with self.assertRaisesRegex(PermissionError, "fixture unreadable subtree"):
                    B.build(vault)
                with self.assertRaisesRegex(PermissionError, "fixture unreadable subtree"):
                    CLI.INDEX_MODULE.source_manifest(vault)
            self.assertEqual(index.read_bytes(), before)
            self.assertIn("wiki/restricted/hidden.md", B.load_index(index)["sourceHashes"])

    def test_crlf_source_hashes_and_character_coordinates_remain_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "wiki").mkdir()
            source = "# Café\r\n\r\n~~Quota was ten~~\r\n\r\nQuota is twenty.\r\n"
            (vault / "wiki/note.md").write_bytes(source.encode())
            with patch.object(C, "model_digest", return_value="verified"), patch.object(C, "embeddings", side_effect=lambda inputs, *a: [[1., 0.] for _ in inputs]):
                C.build(vault, "http://127.0.0.1:1", "fixture")
            cache = json.loads((vault / C.DEFAULT_CACHE).read_text())
            entry = cache["pages"]["wiki/note.md"]
            self.assertEqual(entry["contentHash"], hashlib.sha256(source.encode()).hexdigest())
            for chunk in entry["chunks"]:
                self.assertEqual(chunk["sourceHash"], hashlib.sha256(source[chunk["start"]:chunk["end"]].encode()).hexdigest())
            self.assertEqual((vault / "wiki/note.md").read_bytes(), source.encode())

    def test_unmarked_old_content_is_not_magically_resolved(self):
        source = "# Values\nOld quota is ten.\nCurrent quota is twenty.\n"
        self.assertEqual(P.current(source), source)

    def test_legacy_vectors_not_reinterpreted_and_legacy_lexical_diagnosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "wiki").mkdir()
            source = "# Note\n~~old statement~~\ncurrent statement\n"
            (vault / "wiki/note.md").write_text(source)
            B.build(vault)
            index = vault / B.DEFAULT_INDEX
            data = json.loads(index.read_text())
            data.pop("projectionScheme")
            index.write_text(json.dumps(data))
            self.assertEqual(CLI.search(vault, "statement", 10)["freshness"], "stale-projection")
            with patch.object(C, "model_digest", return_value="verified"), patch.object(C, "embeddings", side_effect=lambda inputs, *a: [[1., 0.] for _ in inputs]):
                # A single text unit is sufficient for this fixture.
                C.build(vault, "http://127.0.0.1:1", "fixture")
            path = vault / C.DEFAULT_CACHE
            cache = json.loads(path.read_text())
            cache["pages"]["wiki/note.md"]["inputScheme"] = "raw-unit-utf8-pooled-v1"
            path.write_text(json.dumps(cache))
            r = CLI.RETRIEVAL_MODULE
            cm = load_source("projection_identity", str(ROOT / "scripts/semantic-chunks.py"))
            with patch.object(r, "load_source", return_value=cm), patch.object(cm, "model_digest", return_value="verified"):
                self.assertEqual(r._valid_semantic_pages(vault, "fixture", "http://127.0.0.1:1", 1)[1], {})


if __name__ == "__main__":
    unittest.main()
