import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
import sys

PACKAGE_ROOT = Path(os.environ.get("OSB_TEST_PACKAGE_ROOT", Path(__file__).parents[1])).resolve()
SCRIPT = PACKAGE_ROOT / "scripts/obsidian_compat.py"
assert SCRIPT.is_file() and SCRIPT.is_relative_to(PACKAGE_ROOT)
spec = importlib.util.spec_from_file_location("obsidian_compat", SCRIPT)
compat = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = compat
spec.loader.exec_module(compat)
assert Path(compat.__file__).resolve() == SCRIPT.resolve()
assert Path(compat.MW).resolve().is_relative_to(PACKAGE_ROOT)


def scenario_report_categories_parsing_fallback_and_root_shadow(vault):
    (vault / "wiki/sources").mkdir(parents=True)
    (vault / "sources").mkdir()
    (vault / "wiki/sources/analysis.md").write_text("# Analysis\n")
    (vault / "sources/analysis.md").write_text("# Wrong shadow\n")
    (vault / "wiki/index.md").write_text("[folder](sources/)\n")
    (vault / "wiki/log.md").write_text("[old](/sources/analysis.md)\n")
    body = vault / "wiki/topic.md"
    body.write_text('''---
type: topic
links:
  - "[meta](/sources/analysis.md#Details)"
description: "[Analysis \\[Example\\]](/sources/analysis.md)"
custom: untouched
---
[Analysis \\[Nested [label]\\]](sources/analysis.md#Details) [inline](./sources/analysis.md)
`[code](/nope)`
```md
[code](/nope)
```
[web](https://example.test/a(b))
''')
    (vault / "wiki/topic-alias.md").symlink_to(body)
    report = compat.compatibility_report(vault)
    links = report["links"]
    assert report["evidence_quality"] == "native evidence not supplied; filesystem fallback; app not queried"
    assert any(x["classification"] == ["leading-slash"] and x["kind"] == "lifecycle-history" and x["repair_state"] == "historical-not-active-repair" for x in links)
    assert any("folder-only" in x["classification"] for x in links)
    assert any("bare-relative-root-shadow" in x["classification"] for x in links)
    assert not any("code" in x["destination"] or "example.test" in x["destination"] for x in links)
    assert any(x["destination"].startswith("sources/analysis.md#") for x in links)
    assert any(x["area"] == "frontmatter" for x in links)
    assert all("topic-alias" not in x["source"] for x in links)
    assert not any("wiki/index.md" in item or "wiki/log.md" in item for item in report["limitations"])
    for link in links:
        proposal = link["proposal"]
        if proposal:
            raw = (vault / link["source"]).read_bytes().decode("utf-8")
            start, end = proposal["span"]
            assert raw[start:end] == link["destination"]


def scenario_evidence_is_bound_to_vault_source_destination(vault):
    (vault / "wiki").mkdir()
    source = vault / "wiki/a.md"
    source.write_text("[b](./b.md)\n")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    evidence = {"vault": str(vault), "links": [{"source": "wiki/a.md", "destination": "./b.md", "resolved_path": "wiki/b.md", "source_hash": source_hash}]}
    report = compat.compatibility_report(vault, native_evidence=evidence)
    assert report["links"][0]["evidence"] == "native-evidence"
    assert report["links"][0]["native_resolved_path"] == "wiki/b.md"

    evidence["links"] = [
        {"source": "wiki/a.md", "destination": "./b.md", "resolved_path": "wiki/b.md", "source_hash": source_hash},
        {"source": "wiki/a.md", "destination": "./b.md", "resolved_path": "wiki/other.md", "source_hash": source_hash},
    ]
    expect_value_error(lambda: compat.compatibility_report(vault, native_evidence=evidence), "conflicting")
    evidence["links"] = [{"source": "wiki/a.md", "destination": "./b.md", "resolved_path": "wiki/b.md", "source_hash": "0" * 64}]
    expect_value_error(lambda: compat.compatibility_report(vault, native_evidence=evidence), "stale")
    evidence["links"] = [{"source": "../outside.md", "destination": "./b.md", "resolved_path": "wiki/b.md", "source_hash": source_hash}]
    expect_value_error(lambda: compat.compatibility_report(vault, native_evidence=evidence), "not a discovered wiki page")
    evidence["links"] = [{"source": "wiki/a.md", "destination": "./b.md", "resolved_path": "../outside.md", "source_hash": source_hash}]
    expect_value_error(lambda: compat.compatibility_report(vault, native_evidence=evidence), "canonical")
    evidence["links"] = [{"source": "wiki/a.md", "destination": "./b.md", "resolved_path": "wiki\\\\outside.md", "source_hash": source_hash}]
    expect_value_error(lambda: compat.compatibility_report(vault, native_evidence=evidence), "canonical")
    expect_value_error(lambda: compat.compatibility_report(vault, native_evidence=[]), "object")
    expect_value_error(lambda: compat.compatibility_report(vault, native_evidence={"vault": str(vault), "links": [None]}), "link must bind")

    evidence["links"] = [{"source": "wiki/a.md", "destination": "./b.md", "resolved_path": None, "source_hash": source_hash}]
    unresolved = compat.compatibility_report(vault, native_evidence=evidence)
    assert unresolved["links"][0]["evidence"] == "native-unresolved"
    assert unresolved["links"][0]["native_resolved_path"] is None
    assert unresolved["links"][0]["okf_candidates"] == []
    assert unresolved["native_graph_complete"] is False
    evidence["vault"] = str(vault / "other")
    expect_value_error(lambda: compat.compatibility_report(vault, native_evidence=evidence), "does not match")


def scenario_nested_relative_proposals_and_encoded_targets(vault):
    (vault / "wiki/a/deep").mkdir(parents=True)
    (vault / "wiki/a/sibling space #.md").write_text("# sibling")
    (vault / "wiki/a/parent.md").write_text("# parent")
    (vault / "wiki/target ü.md").write_text("# target")
    source = vault / "wiki/a/deep/source.md"
    source.write_text('''---
type: note
links: "[metadata](/wiki/target%20%C3%BC.md#Heading)"
---
[sibling](/wiki/a/sibling%20space%20%23.md) [parent](/wiki/a/parent.md) [outside](/wiki/target%20%C3%BC.md)
''')
    report = compat.compatibility_report(vault)
    proposed = [x for x in report["links"] if x["proposal"]]
    replacements = [x["proposal"]["replacement"] for x in proposed]
    assert "../../target%20%C3%BC.md#Heading" in replacements
    assert "../sibling%20space%20%23.md" in replacements
    assert "../parent.md" in replacements
    assert any(x["area"] == "frontmatter" for x in proposed)
    raw = source.read_bytes().decode("utf-8")
    for link in proposed:
        start, end = link["proposal"]["span"]
        assert raw[start:end] == link["destination"]


def scenario_bom_crlf_offsets_are_raw_source_offsets(vault):
    (vault / "wiki").mkdir()
    target = vault / "wiki/target.md"
    target.write_text("# target\n")
    source = vault / "wiki/source.md"
    source.write_bytes(b"\xef\xbb\xbf---\r\ntype: note\r\nlinks: '[meta](/wiki/target.md)'\r\n---\r\n[body](/wiki/target.md)\r\n")
    report = compat.compatibility_report(vault)
    raw = source.read_bytes().decode("utf-8")
    proposals = [x for x in report["links"] if x["proposal"]]
    assert len(proposals) == 2
    for link in proposals:
        start, end = link["proposal"]["span"]
        assert raw[start:end] == link["destination"]


def scenario_alias_ambiguity_keeps_all_markdown_target_metadata(vault):
    (vault / "wiki").mkdir()
    (vault / "root.md").write_text('---\ntype: note\naliases: [Shared]\n---\n')
    (vault / "wiki/target.md").write_text('---\ntype: note\naliases: [Shared]\n---\n')
    (vault / "wiki/source.md").write_text('---\ntype: note\n---\n[go](/Shared)\n')
    report = compat.compatibility_report(vault)
    link = next(x for x in report["links"] if x["source"] == "wiki/source.md")
    assert link["okf_candidates"] == ["root.md", "wiki/target.md"]
    assert link["intended_path"] is None
    assert link["proposal"] is None


def scenario_related_and_multiline_description(vault):
    (vault / "wiki").mkdir()
    (vault / "wiki/target.md").write_text("# Target\n")
    source = vault / "wiki/source.md"
    source.write_text('''---\ntype: note\nrelated:\n  - "[list](/wiki/target.md)"\ndescription: >-\n  [folded](/wiki/target.md)\n---\n''')
    (vault / "wiki/scalar.md").write_text('---\ntype: note\nrelated: "[scalar](/wiki/target.md)"\n---\n')
    report = compat.compatibility_report(vault)
    links = [x for x in report["links"] if x["source"] == "wiki/source.md"]
    assert {x["destination"] for x in links} >= {"/wiki/target.md"}
    scalar_links = [x for x in report["links"] if x["source"] == "wiki/scalar.md"]
    assert len([x for x in links if x["proposal"]]) == 2
    assert len([x for x in scalar_links if x["proposal"]]) == 1
    for path in (source, vault / "wiki/scalar.md"):
        raw = path.read_bytes().decode("utf-8")
        for link in report["links"]:
            if link["source"] == path.relative_to(vault).as_posix() and link["proposal"]:
                start, end = link["proposal"]["span"]
                assert raw[start:end] == link["destination"]


def scenario_nested_unknown_metadata_and_inline_code_untouched(vault):
    (vault / "wiki").mkdir()
    (vault / "wiki/target.md").write_text("# Target\n")
    source = vault / "wiki/source.md"
    source.write_text('''---\ntype: note\ncustom:\n  description: "[opaque](/wiki/target.md)"\ndescription: '`[example](/wiki/target.md)` <!-- [hidden](/wiki/target.md) --> [real](/wiki/target.md)'\n---\n''')
    report = compat.compatibility_report(vault)
    links = [x for x in report["links"] if x["source"] == "wiki/source.md"]
    assert len(links) == 1
    assert links[0]["destination"] == "/wiki/target.md"
    assert links[0]["proposal"] is not None
    raw = source.read_bytes().decode("utf-8")
    start, end = links[0]["proposal"]["span"]
    assert raw[start:end] == links[0]["destination"]


def scenario_escaped_opening_bracket_is_not_a_link(vault):
    (vault / "wiki").mkdir()
    (vault / "wiki/target.md").write_text("# Target\n")
    (vault / "wiki/source.md").write_text('---\ntype: note\n---\n\\[literal](/wiki/target.md) [real \\[label\\]](/wiki/target.md)\n')
    report = compat.compatibility_report(vault)
    links = [x for x in report["links"] if x["source"] == "wiki/source.md"]
    assert len(links) == 1
    assert links[0]["proposal"] is not None


def scenario_resolver_semantics_for_markdown_embed_and_percent_wikilink(vault):
    (vault / "wiki/sub").mkdir(parents=True)
    (vault / "wiki/sub/x.md").write_text("# sub\n")
    (vault / "x.md").write_text("# root\n")
    (vault / "wiki/100%25.md").write_text("# encoded intended\n")
    (vault / "wiki/100%.md").write_text("# double decode decoy\n")
    source = vault / "wiki/sub/source.md"
    source.write_text('---\ntype: note\n---\n![relative](/x.md) [[100%2525]]\n')
    report = compat.compatibility_report(vault)
    links = [x for x in report["links"] if x["source"] == "wiki/sub/source.md"]
    embed = next(x for x in links if x["destination"] == "/x.md")
    wiki = next(x for x in links if x["destination"] == "100%2525")
    assert embed["okf_candidates"] == ["wiki/sub/x.md"]
    assert wiki["okf_candidates"] == ["wiki/100%25.md"]


def scenario_sparse_classification_uses_resolved_graph(vault):
    (vault / "wiki/sub").mkdir(parents=True)
    (vault / "wiki/target.md").write_text("# Target\n")
    (vault / "wiki/sub/INDEX.MD").write_text("# Reserved uppercase index\n")
    (vault / "wiki/source.md").write_text('---\ntype: note\nlinks: "[actual](/wiki/target.md)"\n---\n')
    report = compat.compatibility_report(vault)
    assert "wiki/source.md" not in report["sparse_or_archive_pages"]
    assert "wiki/sub/INDEX.MD" not in report["sparse_or_archive_pages"]


def expect_value_error(callback, message):
    try:
        callback()
    except ValueError as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError("expected ValueError containing " + message)


class CompatibilityTests(unittest.TestCase):
    def _run_scenario(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            scenario(Path(directory))

    def test_report_categories_parsing_fallback_and_root_shadow(self):
        self._run_scenario(scenario_report_categories_parsing_fallback_and_root_shadow)

    def test_evidence_is_bound_to_vault_source_destination(self):
        self._run_scenario(scenario_evidence_is_bound_to_vault_source_destination)

    def test_nested_relative_proposals_and_encoded_targets(self):
        self._run_scenario(scenario_nested_relative_proposals_and_encoded_targets)

    def test_bom_crlf_offsets_are_raw_source_offsets(self):
        self._run_scenario(scenario_bom_crlf_offsets_are_raw_source_offsets)

    def test_alias_ambiguity_keeps_all_metadata(self):
        self._run_scenario(scenario_alias_ambiguity_keeps_all_markdown_target_metadata)

    def test_related_and_multiline_description(self):
        self._run_scenario(scenario_related_and_multiline_description)

    def test_nested_unknown_metadata_and_inline_code(self):
        self._run_scenario(scenario_nested_unknown_metadata_and_inline_code_untouched)

    def test_escaped_opening_bracket(self):
        self._run_scenario(scenario_escaped_opening_bracket_is_not_a_link)

    def test_original_embed_and_wikilink_resolution(self):
        self._run_scenario(scenario_resolver_semantics_for_markdown_embed_and_percent_wikilink)

    def test_sparse_classification_uses_resolved_links(self):
        self._run_scenario(scenario_sparse_classification_uses_resolved_graph)

    def test_obsidian_wikilink_embeds_remain_in_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / "wiki/meta").mkdir(parents=True)
            (vault / "wiki/meta/view.base").write_text("views: []\n")
            (vault / "wiki/dashboard.md").write_text("---\ntype: note\n---\n![[wiki/meta/view.base]]\n")
            report = compat.compatibility_report(vault)
            embeds = [link for link in report["links"] if link["source"] == "wiki/dashboard.md"]
            self.assertEqual(len(embeds), 1)
            self.assertEqual(embeds[0]["destination"], "wiki/meta/view.base")
            self.assertEqual(embeds[0]["okf_candidates"], ["wiki/meta/view.base"])
            self.assertIsNone(embeds[0]["proposal"])
            self.assertNotIn("wiki/dashboard.md", report["sparse_or_archive_pages"])


if __name__ == "__main__":
    unittest.main()
