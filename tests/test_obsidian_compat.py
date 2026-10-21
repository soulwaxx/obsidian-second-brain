import importlib.util
import json
import hashlib
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/obsidian_compat.py"
spec = importlib.util.spec_from_file_location("obsidian_compat", SCRIPT)
compat = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = compat
spec.loader.exec_module(compat)


def test_report_categories_parsing_fallback_and_root_shadow(tmp_path):
    vault = tmp_path
    (vault / "wiki/sources").mkdir(parents=True)
    (vault / "sources").mkdir()
    (vault / "wiki/sources/analysis.md").write_text("# Analysis\n")
    (vault / "sources/analysis.md").write_text("# Wrong shadow\n")
    (vault / "wiki/index.md").write_text("[folder](sources/)\n")
    (vault / "wiki/log.md").write_text("[old](/sources/analysis.md)\n")
    body = (vault / "wiki/topic.md")
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
    assert report["evidence_quality"].startswith("app-unavailable")
    assert any(x["classification"] == ["leading-slash"] and x["kind"] == "lifecycle-history" and x["repair_state"] == "historical-not-active-repair" for x in links)
    assert any("folder-only" in x["classification"] for x in links)
    assert any("bare-relative-root-shadow" in x["classification"] for x in links)
    assert not any("code" in x["destination"] or "example.test" in x["destination"] for x in links)
    assert any(x["destination"].startswith("sources/analysis.md#") for x in links)
    assert any(x["area"] == "frontmatter" for x in links if "area" in x)
    assert all("topic-alias" not in x["source"] for x in links)


def test_evidence_is_bound_to_vault_source_destination(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki/a.md").write_text("[b](./b.md)\n")
    evidence = {"vault": str(tmp_path), "links": [{"source": "wiki/a.md", "destination": "./b.md", "resolved_path": "wiki/b.md", "source_hash": hashlib.sha256((tmp_path / "wiki/a.md").read_bytes()).hexdigest()}]}
    report = compat.compatibility_report(tmp_path, native_evidence=evidence)
    assert report["links"][0]["evidence"] == "native-evidence"
    assert report["links"][0]["native_resolved_path"] == "wiki/b.md"
    evidence["vault"] = str(tmp_path / "other")
    try:
        compat.compatibility_report(tmp_path, native_evidence=evidence)
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("unbound evidence accepted")
    evidence["vault"] = str(tmp_path)
    evidence["links"] = [{"source": "wiki/a.md", "destination": "./b.md", "resolved_path": None, "source_hash": hashlib.sha256((tmp_path / "wiki/a.md").read_bytes()).hexdigest()}]
    unresolved = compat.compatibility_report(tmp_path, native_evidence=evidence)
    assert unresolved["links"][0]["evidence"] == "native-unresolved"
    assert unresolved["links"][0]["okf_candidates"] == []
    assert unresolved["native_graph_complete"] is False


def test_nested_relative_proposals_and_encoded_targets(tmp_path):
    (tmp_path / "wiki/a/deep").mkdir(parents=True)
    (tmp_path / "wiki/a/sibling space #.md").write_text("# sibling")
    (tmp_path / "wiki/a/parent.md").write_text("# parent")
    (tmp_path / "wiki/target ü.md").write_text("# target")
    source = tmp_path / "wiki/a/deep/source.md"
    source.write_text('''---\ntype: note\nlinks: "[metadata](/wiki/target%20%C3%BC.md#Heading)"\n---\n[sibling](/wiki/a/sibling%20space%20%23.md) [parent](/wiki/a/parent.md) [outside](/wiki/target%20%C3%BC.md)\n''')
    report = compat.compatibility_report(tmp_path)
    proposed = [x["proposal"]["replacement"] for x in report["links"] if x["proposal"]]
    assert "../../target%20%C3%BC.md#Heading" in proposed
    assert "../sibling%20space%20%23.md" in proposed
    assert "../parent.md" in proposed
    assert any(x["area"] == "frontmatter" and x["proposal"] for x in report["links"])
