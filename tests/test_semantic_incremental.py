"""Exact-input reuse and publication tests with synthetic disposable sources."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source
C = load_source("incremental_chunks", str(ROOT / "scripts/semantic-chunks.py"))


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / "wiki").mkdir()
        self.page = self.vault / "wiki/long.md"
        self.source = "# Long\n\n" + "\n\n".join(f"Paragraph {i}: alpha information." for i in range(30)) + "\n"
        self.page.write_text(self.source)
        self.inputs = []
        self.digest = "verified-v1"
        self.addCleanup(patch.stopall)
        patch.object(C, "model_digest", side_effect=lambda *a, **kw: self.digest).start()
        patch.object(C, "embeddings", side_effect=self.embed).start()

    def embed(self, inputs, *args):
        self.inputs.extend(inputs)
        return [[1., 0.] if "alpha" in s else [0., 1.] for s in inputs]

    def build(self):
        return C.build(self.vault, "http://127.0.0.1:1", "fixture", max_chars=180, target_chars=100)

    def cache(self):
        return json.loads((self.vault / C.DEFAULT_CACHE).read_text())

    def test_cold_repeat_and_small_edit_count_actual_inputs(self):
        cold = self.build()
        self.assertEqual(cold["embeddedInputs"], len(C.text_units(self.source, 180)))
        self.assertEqual(cold["embeddedChars"], sum(map(len, self.inputs)))
        self.inputs.clear()
        repeated = self.build()
        self.assertEqual(repeated["embeddedInputs"], 0)
        self.assertEqual(self.inputs, [])
        self.page.write_text(self.source.replace("Paragraph 15: alpha", "Paragraph 15: beta"))
        edited = self.build()
        self.assertEqual(edited["embeddedInputs"], 1)
        self.assertGreater(edited["reusedInputs"], 20)
        self.assertIn("beta", self.inputs[0])
        for chunk in self.cache()["pages"]["wiki/long.md"]["chunks"]:
            self.assertEqual(chunk["sourceHash"], C.sha256(self.page.read_text()[chunk["start"]:chunk["end"]]))

    def test_duplicates_moves_and_heading_changes_recompute_mappings(self):
        (self.vault / "wiki/duplicate.md").write_text(self.source)
        result = self.build()
        self.assertEqual(result["embeddedInputs"], len(set(u["text"] for u in C.text_units(self.source, 180))))
        self.inputs.clear()
        self.page.write_text("\n\n" + self.source)
        self.build()
        self.assertFalse(self.inputs)
        self.assertEqual(self.cache()["pages"]["wiki/long.md"]["chunks"][0]["start"], 2)
        self.page.write_text(self.page.read_text().replace("# Long", "# Renamed"))
        self.inputs.clear()
        self.assertEqual(self.build()["embeddedInputs"], 1, "raw-unit representation embeds changed headings only")
        self.assertTrue(all(c["text"].startswith("Title: Renamed") for c in self.cache()["pages"]["wiki/long.md"]["chunks"]))

    def test_identity_scheme_corruption_and_legacy_transition(self):
        self.build()
        self.inputs.clear()
        self.digest = "verified-v2"
        self.assertGreater(self.build()["embeddedInputs"], 20)
        self.inputs.clear()
        with patch.object(C, "INPUT_SCHEME", "raw-unit-test-v2"):
            self.assertGreater(self.build()["embeddedInputs"], 20)
        cache = self.cache()
        cache.pop("inputVectors")
        cache.pop("inputScheme")
        for page in cache["pages"].values():
            page.pop("inputScheme", None)
            page.pop("inputHashes", None)
        (self.vault / C.DEFAULT_CACHE).write_text(json.dumps(cache))
        self.assertGreater(self.build()["embeddedInputs"], 20, "pooled legacy vectors cannot recover units")
        cache = self.cache()
        cache["inputVectors"]["dimension"] += 1
        (self.vault / C.DEFAULT_CACHE).write_text(json.dumps(cache))
        self.assertGreater(self.build()["embeddedInputs"], 20, "malformed vector dimensions cannot be reused")

    def test_unknown_digest_no_inference_and_removed_inputs_pruned(self):
        self.build()
        self.digest = None
        self.inputs.clear()
        self.page.write_text(self.source + "\nNew fact\n")
        result = self.build()
        self.assertEqual(self.inputs, [])
        self.assertEqual(result["deferred"], ["wiki/long.md"])
        self.assertFalse(self.cache()["pages"])
        self.digest = "verified-v1"
        self.build()
        self.page.unlink()
        self.build()
        self.assertFalse(self.cache()["inputVectors"]["vectors"])

    def test_racing_source_model_or_cache_never_overwrites_stale_mappings(self):
        self.build()
        self.page.write_text(self.source + "\nChanged fact\n")
        def racing(inputs, *args):
            vectors = self.embed(inputs, *args)
            self.page.write_text(self.page.read_text() + "racing edit\n")
            return vectors
        with patch.object(C, "embeddings", side_effect=racing):
            result = self.build()
        self.assertEqual(result["deferred"], ["wiki/long.md"])
        self.assertFalse(self.cache()["pages"])
        self.build()
        self.page.write_text(self.source + "\nNew input\n")
        original = self.cache()
        def replaced(inputs, *args):
            path = self.vault / C.DEFAULT_CACHE
            payload = {**original, "concurrent": True}
            tmp = path.with_suffix(".other")
            tmp.write_text(json.dumps(payload))
            tmp.replace(path)
            return self.embed(inputs, *args)
        with patch.object(C, "embeddings", side_effect=replaced):
            with self.assertRaisesRegex(ValueError, "concurrent"):
                self.build()
        self.assertTrue(self.cache()["concurrent"])
        before = (self.vault / C.DEFAULT_CACHE).read_bytes()
        with patch.object(C, "model_digest", side_effect=["verified-v1", "changed-model"]):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                self.build()
        self.assertEqual((self.vault / C.DEFAULT_CACHE).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
