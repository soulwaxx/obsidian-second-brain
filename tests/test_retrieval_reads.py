import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/obsidian-second-brain.py"
sys.path.insert(0, str(ROOT / "scripts"))
import retrieval_reads as reads


class RetrievalReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        self.page = self.vault / "wiki/example.md"

    def tearDown(self):
        self.temp.cleanup()

    def hash_page(self):
        return hashlib.sha256(self.page.read_bytes()).hexdigest()

    def cli(self, *args, timeout=10):
        return subprocess.run([sys.executable, str(CLI), *args], text=True, capture_output=True,
                              timeout=timeout, env={**os.environ, "OBSIDIAN_AGENT_CONFIG": str(Path(self.temp.name) / "invalid.json")})

    def test_cli_default_budget_is_12000_and_smaller_override_continues_exactly(self):
        text = "A" * 13020 + "qualification at end\n"
        self.page.write_text(text)
        digest = self.hash_page()
        outline = self.cli("outline", "--json", "--vault", str(self.vault), "--page", "wiki/example.md")
        self.assertEqual(outline.returncode, 0, outline.stderr)
        entry_id = json.loads(outline.stdout)["entries"][0]["id"]
        default = self.cli("read", "--json", "--vault", str(self.vault), "--page", "wiki/example.md",
                           "--expected-hash", digest, "--section", entry_id)
        self.assertEqual(default.returncode, 0, default.stderr)
        response = json.loads(default.stdout)
        self.assertEqual(response["text"], text[:12000])
        self.assertEqual(response["returned"], {"start": 0, "end": 12000})
        self.assertTrue(response["truncated"])
        self.assertFalse(response["selectionComplete"])
        self.assertEqual(response["continuation"]["cursor"], 12000)
        smaller = self.cli("read", "--json", "--vault", str(self.vault), "--page", "wiki/example.md",
                           "--expected-hash", digest, "--section", entry_id, "--max-chars", "2000")
        self.assertEqual(smaller.returncode, 0, smaller.stderr)
        small_response = json.loads(smaller.stdout)
        self.assertEqual(small_response["text"], text[:2000])
        self.assertEqual(small_response["continuation"]["cursor"], 2000)
        self.assertEqual(reads.MAX_CHARS_DEFAULT, 12000)

    def test_outline_recognizes_frontmatter_preamble_unicode_duplicate_setext_and_fences(self):
        self.page.write_bytes(("---\r\ntype: Note\r\ncustom: keep\r\n---\r\nIntro 🌱\r\n\r\n## Same\r\nbody\r\n\r\n### Child 😀\r\ninner\r\n\n## Same\r\nagain\r\n\nSetext heading\r\n===============\r\n\n```md\r\n# hidden\r\n```\r\n").encode())
        result = reads.outline_page(self.vault, "wiki/example.md")
        entries = result["entries"]
        self.assertEqual(result["sourceChars"], len(self.page.read_bytes().decode()))
        self.assertEqual(entries[0]["id"], "metadata")
        self.assertIn("preamble", [entry["id"] for entry in entries])
        headings = [item for item in entries if item["kind"] == "heading"]
        self.assertEqual([item["title"] for item in headings], ["Same", "Child 😀", "Same", "Setext heading"])
        self.assertEqual(len({item["id"] for item in headings}), 4)
        self.assertLess(headings[0]["end"], headings[2]["start"] + 1)
        self.assertNotIn("hidden", json.dumps(entries))
        selected = reads.read_page(self.vault, "wiki/example.md", result["sourceHash"], section=headings[0]["id"])
        self.assertIn("### Child 😀", selected["text"])
        self.assertNotIn("again", selected["text"])

    def test_selector_hash_budget_cursor_and_outline_pagination_validation(self):
        self.page.write_text("# Header\nbody\n")
        digest = self.hash_page()
        invalid_reads = [
            {"expected_hash": ""}, {"expected_hash": "A" * 64, "start": 0, "end": 1},
            {"expected_hash": digest}, {"expected_hash": digest, "section": "heading:0", "start": 0},
            {"expected_hash": digest, "start": 0}, {"expected_hash": digest, "section": "missing"},
            {"expected_hash": digest, "start": -1, "end": 2}, {"expected_hash": digest, "start": 2, "end": 1},
            {"expected_hash": digest, "start": 0, "end": 99},
            {"expected_hash": digest, "start": 0, "end": 2, "max_chars": 0},
            {"expected_hash": digest, "start": 0, "end": 2, "max_chars": 12001},
            {"expected_hash": digest, "start": 0, "end": 2, "cursor": -1},
            {"expected_hash": digest, "start": 0, "end": 2, "cursor": 3},
            {"expected_hash": digest, "start": 0, "end": 2, "cursor": True},
        ]
        for original in invalid_reads:
            with self.subTest(kwargs=original), self.assertRaises(reads.ReadError):
                kwargs = dict(original)
                expected = kwargs.pop("expected_hash")
                reads.read_page(self.vault, "wiki/example.md", expected, **kwargs)
        for kwargs in ({"offset": 1}, {"limit": 0}, {"limit": 101}, {"offset": -1},
                       {"offset": 1, "expected_hash": "invalid"}):
            with self.subTest(outline=kwargs), self.assertRaises(reads.ReadError):
                reads.outline_page(self.vault, "wiki/example.md", **kwargs)
        with self.assertRaises(reads.SourceChanged):
            reads.outline_page(self.vault, "wiki/example.md", offset=1, expected_hash="0" * 64)

    def test_unknown_section_id_guides_outline_lookup_without_resolving_titles(self):
        self.page.write_text("# Problem and goal\nbody\n# Problem and goal\nagain\n")
        outline = reads.outline_page(self.vault, "wiki/example.md")
        for section in ("Problem and goal", "heading:999", "missing"):
            with self.subTest(section=section), self.assertRaises(reads.ReadError) as caught:
                reads.read_page(self.vault, "wiki/example.md", outline["sourceHash"], section=section)
            message = str(caught.exception)
            self.assertIn("unknown section ID", message)
            self.assertIn("wiki_outline", message)
            self.assertIn("heading:<offset>", message)
            self.assertIn("metadata", message)
            self.assertIn("preamble", message)
            self.assertLessEqual(len(message), 1000)
        for entry in outline["entries"]:
            result = reads.read_page(self.vault, "wiki/example.md", outline["sourceHash"], section=entry["id"])
            self.assertEqual(result["selected"], {"start": entry["start"], "end": entry["end"]})

    def test_unicode_line_separator_characters_do_not_shift_commonmark_offsets(self):
        text = "preamble\u2028not a physical line\n# H\u2028# still heading text\nbody\u0085not a line\n## Child\nend\n"
        self.page.write_text(text)
        result = reads.outline_page(self.vault, "wiki/example.md")
        headings = [entry for entry in result["entries"] if entry["kind"] == "heading"]
        self.assertEqual([item["start"] for item in headings], [text.index("# H"), text.index("## Child")])
        self.assertIn("# still heading text", headings[0]["title"])
        self.assertEqual(headings[1]["lineStart"], 4)

    def test_long_heading_title_truncation_is_explicit(self):
        self.page.write_text("# " + "x" * 200 + "\nbody\n")
        heading = next(item for item in reads.outline_page(self.vault, "wiki/example.md")["entries"] if item["kind"] == "heading")
        self.assertTrue(heading["titleTruncated"])
        self.assertTrue(heading["title"].endswith("…"))

    def test_metadata_and_preamble_are_actual_hash_bound_read_spans(self):
        text = "---\ntype: Note\ncustom: keep\n---\npreamble 😀\n# Heading\nbody\n"
        self.page.write_text(text)
        outline = reads.outline_page(self.vault, "wiki/example.md")
        for item in outline["entries"][:2]:
            result = reads.read_page(self.vault, "wiki/example.md", outline["sourceHash"], section=item["id"])
            self.assertEqual(result["text"], text[item["start"]:item["end"]])
        self.assertEqual(outline["entries"][0]["start"], 0)
        self.assertIn("---", reads.read_page(self.vault, "wiki/example.md", outline["sourceHash"], section="metadata")["text"])

    def test_no_heading_and_empty_pages_are_selectable_without_body_outline_dump(self):
        for text, expected in (("", ["preamble"]), ("preamble\nwith text\n", ["preamble"])):
            self.page.write_text(text)
            result = reads.outline_page(self.vault, "wiki/example.md")
            self.assertEqual([entry["id"] for entry in result["entries"]], expected)
            self.assertNotIn("text", json.dumps(result))

    def test_exact_ranges_truncate_and_continue_only_within_original_selection(self):
        text = "---\ntype: Note\n---\n😀 A  \r\nB\nqualified ending\n"
        self.page.write_bytes(text.encode())
        digest = self.hash_page()
        start, end = text.index("😀"), len(text)
        first = reads.read_page(self.vault, "wiki/example.md", digest, start=start, end=end, max_chars=5)
        self.assertEqual(first["text"], text[start:start + 5])
        self.assertTrue(first["truncated"])
        continuation = first["continuation"]
        self.assertEqual(continuation["cursor"], start + 5)
        next_page = reads.read_page(self.vault, "wiki/example.md", digest, start=start, end=end,
                                    max_chars=12000, cursor=continuation["cursor"])
        self.assertEqual(next_page["text"], text[start + 5:end])
        self.assertFalse(next_page["selectionComplete"])
        self.assertTrue(next_page["truncated"])
        self.assertEqual(next_page["lineStart"], text.count("\n", 0, start + 5) + 1)
        self.assertEqual(next_page["lineEnd"], text.count("\n", 0, end - 1) + 1)
        complete = reads.read_page(self.vault, "wiki/example.md", digest, start=start, end=end, max_chars=12000)
        self.assertTrue(complete["selectionComplete"])
        self.assertFalse(complete["truncated"])

    def test_empty_spans_are_complete_with_same_start_and_end_line(self):
        self.page.write_text("first\r\nsecond")
        result = reads.read_page(self.vault, "wiki/example.md", self.hash_page(), start=7, end=7)
        self.assertEqual(result["lineStart"], 2)
        self.assertEqual(result["lineEnd"], 2)
        self.assertTrue(result["selectionComplete"])
        self.assertFalse(result["truncated"])

    def test_descriptor_detects_in_place_mutation_during_read(self):
        self.page.write_text("original payload")
        original_read = os.read
        changed = False

        def read_then_mutate(fd, size):
            nonlocal changed
            data = original_read(fd, size)
            if data and not changed:
                changed = True
                self.page.write_text("concurrent replacement")
            return data

        with patch("retrieval_reads.os.read", side_effect=read_then_mutate):
            with self.assertRaises(reads.SourceChanged):
                reads.read_snapshot(self.vault, "wiki/example.md")

    def test_descriptor_detects_replacement_and_symlink_swap_during_read(self):
        self.page.write_text("original payload")
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("outside evidence must not leak")
        original_read = os.read
        swapped = False

        def replace_with_symlink(fd, size):
            nonlocal swapped
            data = original_read(fd, size)
            if data and not swapped:
                swapped = True
                self.page.unlink()
                self.page.symlink_to(outside)
            return data

        with patch("retrieval_reads.os.read", side_effect=replace_with_symlink):
            with self.assertRaises(reads.SourceChanged):
                reads.read_snapshot(self.vault, "wiki/example.md")

    def test_root_symlink_swap_before_open_is_refused(self):
        self.page.write_text("inside vault")
        outside = Path(self.temp.name) / "outside-vault"
        (outside / "wiki").mkdir(parents=True)
        (outside / "wiki/example.md").write_text("outside evidence")
        root = Path(os.path.realpath(self.vault))
        detached = root.with_name(root.name + "-detached")
        original_open = os.open
        swapped = False

        def swap_root(path, flags, *args, **kwargs):
            nonlocal swapped
            parent_fd = kwargs.get("dir_fd")
            is_root_entry = (path == root.name and parent_fd is not None
                             and (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
                             == (root.parent.stat().st_dev, root.parent.stat().st_ino))
            if is_root_entry and not swapped:
                swapped = True
                root.rename(detached)
                root.symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        try:
            with patch("retrieval_reads.os.open", side_effect=swap_root):
                with self.assertRaises(reads.ReadError):
                    reads.read_snapshot(self.vault, "wiki/example.md")
            self.assertTrue(swapped)
        finally:
            if root.is_symlink():
                root.unlink()
            if detached.exists():
                detached.rename(root)

    def test_root_and_canonical_ancestor_replacement_during_read_refuse_all_evidence(self):
        self.page.write_text("detached original evidence")
        original_hash = self.hash_page()
        original_read = os.read

        def replace_root(fd, size):
            data = original_read(fd, size)
            if data and self.vault.exists():
                detached = self.vault.with_name(self.vault.name + "-old")
                self.vault.rename(detached)
                (self.vault / "wiki").mkdir(parents=True)
                (self.vault / "wiki/example.md").write_text("replacement evidence")
            return data

        with patch("retrieval_reads.os.read", side_effect=replace_root):
            with self.assertRaises(reads.SourceChanged):
                reads.read_page(self.vault, "wiki/example.md", original_hash, start=0, end=len("detached original evidence"))
        self.assertEqual((self.vault / "wiki/example.md").read_text(), "replacement evidence")

        old_root = self.vault.with_name(self.vault.name + "-outline-old")
        (self.vault / "wiki/example.md").write_text("outline source")
        original_read = os.read
        swapped = False

        def replace_root_for_outline(fd, size):
            nonlocal swapped
            data = original_read(fd, size)
            if data and not swapped:
                swapped = True
                self.vault.rename(old_root)
                (self.vault / "wiki").mkdir(parents=True)
                (self.vault / "wiki/example.md").write_text("replacement outline")
            return data

        with patch("retrieval_reads.os.read", side_effect=replace_root_for_outline):
            with self.assertRaises(reads.SourceChanged):
                reads.outline_page(self.vault, "wiki/example.md")

        parent = Path(self.temp.name) / "parent"
        ancestor_vault = parent / "vault"
        (ancestor_vault / "wiki").mkdir(parents=True)
        source = ancestor_vault / "wiki/page.md"
        source.write_text("ancestor detached evidence")
        outside_parent = Path(self.temp.name) / "outside-parent"
        (outside_parent / "vault/wiki").mkdir(parents=True)
        (outside_parent / "vault/wiki/page.md").write_text("ancestor replacement evidence")
        detached_parent = parent.with_name(parent.name + "-detached")
        original_read = os.read
        swapped = False

        def replace_ancestor(fd, size):
            nonlocal swapped
            data = original_read(fd, size)
            if data and not swapped:
                swapped = True
                parent.rename(detached_parent)
                parent.symlink_to(outside_parent, target_is_directory=True)
            return data

        try:
            with patch("retrieval_reads.os.read", side_effect=replace_ancestor):
                with self.assertRaises(reads.SourceChanged):
                    reads.read_snapshot(ancestor_vault, "wiki/page.md")
        finally:
            if parent.is_symlink():
                parent.unlink()
            if detached_parent.exists():
                detached_parent.rename(parent)

    def test_terminal_alias_added_during_read_is_source_change(self):
        self.page.write_text("original contents")
        wiki = self.vault / "wiki"
        wiki_stat = wiki.stat()
        original_read, original_listdir = os.read, os.listdir
        changed = False

        def mark_changed(fd, size):
            nonlocal changed
            data = original_read(fd, size)
            if data and not changed:
                changed = True
            return data

        def add_alias(fd):
            entries = original_listdir(fd)
            info = os.fstat(fd)
            if changed and (info.st_dev, info.st_ino) == (wiki_stat.st_dev, wiki_stat.st_ino):
                return [*entries, "EXAMPLE.md"]
            return entries

        with patch("retrieval_reads.os.read", side_effect=mark_changed), \
                patch("retrieval_reads.os.listdir", side_effect=add_alias):
            with self.assertRaises(reads.SourceChanged):
                reads.read_snapshot(self.vault, "wiki/example.md")

        if os.name == "posix":
            alias = wiki / "EXAMPLE.md"
            original = self.page.read_bytes()
            alias.write_text("alias")
            entries = os.listdir(wiki)
            if "example.md" not in entries or "EXAMPLE.md" not in entries:
                self.page.write_bytes(original)
                self.skipTest("filesystem cannot represent case-distinct sibling entries")
            try:
                with self.assertRaises(reads.ReadError):
                    reads.read_snapshot(self.vault, "wiki/example.md")
            finally:
                alias.unlink()

    def test_current_projection_preserves_coordinates_and_hash_mismatch_has_no_text(self):
        text = "---\ntype: Note\n---\nold ~~claim~~\n"
        self.page.write_text(text)
        digest = self.hash_page()
        raw = reads.read_page(self.vault, "wiki/example.md", digest, start=0, end=len(text))
        current = reads.read_page(self.vault, "wiki/example.md", digest, start=0, end=len(text), view="current")
        self.assertEqual(raw["text"], text)
        self.assertEqual(len(current["text"]), len(text))
        self.assertEqual(current["projection"], "current-strike-mask-v1")
        self.page.write_text(text + "changed")
        with self.assertRaises(reads.SourceChanged):
            reads.read_page(self.vault, "wiki/example.md", digest, start=0, end=len(text))

    def test_path_rejections_include_reserved_alias_hidden_symlink_and_hardlink(self):
        self.page.write_text("page")
        for bad in ("/wiki/example.md", "wiki/../example.md", "wiki//example.md", "wiki/.hidden.md",
                    "wiki/index.md", "wiki/LOG.MD", "wiki/_plan.md", "wiki/file.txt"):
            with self.subTest(page=bad), self.assertRaises(reads.ReadError):
                reads.read_snapshot(self.vault, bad)
        (self.vault / "wiki/alias.md").symlink_to(self.page)
        with self.assertRaises(reads.ReadError):
            reads.read_snapshot(self.vault, "wiki/alias.md")
        os.link(self.page, self.vault / "wiki/hard.md")
        with self.assertRaises(reads.ReadError):
            reads.read_snapshot(self.vault, "wiki/example.md")

    def test_parent_symlink_and_fifo_are_rejected_without_blocking(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "page.md").write_text("must not read")
        (self.vault / "wiki/linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(reads.ReadError):
            reads.read_snapshot(self.vault, "wiki/linked/page.md")
        if hasattr(os, "mkfifo"):
            fifo = self.vault / "wiki/fifo.md"
            os.mkfifo(fifo)
            timed = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import retrieval_reads as r; r.read_snapshot(sys.argv[2], 'wiki/fifo.md')",
                                    str(ROOT / "scripts"), str(self.vault)], capture_output=True, timeout=2)
            self.assertNotEqual(timed.returncode, 0)

    def test_casefold_and_unicode_collisions_and_single_entry_aliases_are_rejected(self):
        self.page.write_text("one")
        with patch("retrieval_reads.os.listdir", return_value=["example.md", "EXAMPLE.md"]):
            with self.assertRaisesRegex(reads.ReadError, "ambiguous"):
                reads.read_snapshot(self.vault, "wiki/example.md")
        composed = self.vault / "wiki/café.md"
        composed.write_text("one")
        with patch("retrieval_reads.os.listdir", return_value=["café.md", "cafe\u0301.md"]):
            with self.assertRaisesRegex(reads.ReadError, "ambiguous"):
                reads.read_snapshot(self.vault, "wiki/café.md")
        with patch("retrieval_reads.os.listdir", return_value=["Example.md"]):
            with self.assertRaisesRegex(reads.ReadError, "ambiguous"):
                reads.read_snapshot(self.vault, "wiki/example.md")
        with patch("retrieval_reads.os.listdir", return_value=["café.md"]):
            with self.assertRaisesRegex(reads.ReadError, "ambiguous"):
                reads.read_snapshot(self.vault, "wiki/cafe\u0301.md")

    def test_cli_bypasses_config_and_read_outline_require_explicit_vault(self):
        self.page.write_text("# Header\ncontent\n")
        config_file = Path(self.temp.name) / "invalid-config.json"
        config_file.write_text("not json")
        env = {**os.environ, "OBSIDIAN_AGENT_CONFIG": str(config_file)}

        def snapshot():
            return sorted((str(path.relative_to(self.vault)), path.read_bytes(), path.stat().st_mtime_ns)
                          for path in self.vault.rglob("*") if path.is_file())

        before_read = snapshot(), config_file.read_bytes(), config_file.stat().st_mtime_ns
        outline = self.cli("outline", "--json", "--vault", str(self.vault), "--page", "wiki/example.md")
        self.assertEqual(outline.returncode, 0, outline.stderr)
        digest = json.loads(outline.stdout)["sourceHash"]
        read = self.cli("read", "--json", "--vault", str(self.vault), "--page", "wiki/example.md",
                        "--expected-hash", digest, "--section", "heading:0")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertIn("# Header", json.loads(read.stdout)["text"])
        self.assertEqual((snapshot(), config_file.read_bytes(), config_file.stat().st_mtime_ns), before_read)
        self.assertFalse((self.vault / ".vault-meta").exists())
        self.page.write_text("# Changed private evidence\n")
        mismatch = self.cli("read", "--json", "--vault", str(self.vault), "--page", "wiki/example.md",
                            "--expected-hash", digest, "--section", "heading:0")
        self.assertNotEqual(mismatch.returncode, 0)
        mismatch_json = json.loads(mismatch.stdout)
        self.assertEqual(mismatch_json["code"], "source-changed")
        self.assertNotIn("Changed private evidence", mismatch.stdout)
        required = self.cli("outline", "--json", "--page", "wiki/example.md")
        self.assertNotEqual(required.returncode, 0)
        malformed = self.cli("read", "--json", "--vault", str(self.vault), "--page", "wiki/example.md",
                             "--expected-hash", digest, "--start", "not-an-offset", "--end", "4")
        self.assertNotEqual(malformed.returncode, 0)
        self.assertEqual(json.loads(malformed.stdout)["code"], "invalid-read")
        self.assertEqual(malformed.stderr, "")

    def test_cli_json_parse_errors_do_not_depend_on_command_position(self):
        for command, option in (("read", "--start"), ("outline", "--limit")):
            for prefix in ([], ["--vault", str(self.vault)], [f"--vault={self.vault}"],
                           ["--config", "read"], ["--page", "outline"]):
                with self.subTest(command=command, prefix=prefix):
                    result = self.cli(*prefix, command, "--json", option, "not-an-integer")
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stderr, "")
                    error = json.loads(result.stdout)
                    self.assertEqual(error["code"], "invalid-read")
                    self.assertIn(option, error["error"])
            with self.subTest(command=command, invalid_option_before_command=True):
                result = self.cli("--json", option, "not-an-integer", command)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, "")
                self.assertEqual(json.loads(result.stdout)["code"], "invalid-read")
        for query in ("read", "outline"):
            with self.subTest(search_query=query):
                result = self.cli("search", query, "--json", "--limit", "not-an-integer")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, "")
                error = json.loads(result.stdout)
                self.assertEqual(error["code"], "invalid-arguments")
                self.assertIn("--limit", error["error"])
        for invocation, option in ((("search", "query", "--json", "--limit", "bad"), "--limit"),
                                   (("--json", "--limit", "bad", "search", "read"), "--limit"),
                                   (("doctor", "--json", "--timeout", "bad"), "--timeout")):
            with self.subTest(search_parse=invocation):
                result = self.cli(*invocation)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, "")
                error = json.loads(result.stdout)
                self.assertEqual(error["code"], "invalid-arguments")
                self.assertIn(option, error["error"])
        human_error = self.cli("--vault", str(self.vault), "read", "--start", "not-an-integer")
        self.assertEqual(human_error.returncode, 2)
        self.assertEqual(human_error.stdout, "")
        self.assertIn("--start", human_error.stderr)

    def test_outline_pagination_requires_hash_and_detects_drift(self):
        self.page.write_text("# one\n# two\n# three\n")
        first = reads.outline_page(self.vault, "wiki/example.md", limit=1)
        continuation = first["continuation"]
        self.assertIsNotNone(continuation)
        with self.assertRaises(reads.SourceChanged):
            reads.outline_page(self.vault, "wiki/example.md", expected_hash="0" * 64,
                               limit=1, offset=continuation["offset"])


if __name__ == "__main__":
    unittest.main()
