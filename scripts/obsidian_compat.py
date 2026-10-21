#!/usr/bin/env python3
"""Read-only Obsidian compatibility diagnostics (independent of OKF lint).

Public API: ``compatibility_report(vault, *, native_evidence=None) -> dict``.
The versioned, JSON-serializable report contains ``links`` for the complete
parseable wiki graph, ``limitations``, and evidence-quality metadata. Optional
native evidence is reviewed JSON of shape ``{"vault": "/absolute/path", "links":
[{"source": "wiki/a.md", "destination": "./b.md", "resolved_path": "wiki/b.md", "source_hash": "<sha256-of-current-source>"}]}``.
Every observation must bind to the resolved vault, exact source/destination, and SHA-256 of the current source text (``source_hash``). A null ``resolved_path`` explicitly records native-unresolved. Stale or conflicting evidence is rejected. This tool never invokes Obsidian, accesses the network, or writes to the vault.
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import urllib.parse
import sys
from pathlib import Path
from typing import Any

MW = Path(__file__).resolve().parents[1] / "skills/wiki/scripts"
sys.path.insert(0, str(MW))
from okf_mw import lint  # noqa: E402


def _frontmatter_links(text: str) -> tuple[list[tuple[str, int, int, str]], bool]:
    """Read link strings in YAML-ish frontmatter while leaving unrelated metadata alone."""
    fm = lint._parse_frontmatter(text)
    if not fm.present:
        return [], False
    result = []
    lines = text[:fm.end_offset].splitlines(keepends=True)
    offset = 0
    link_key = False
    for line_no, line in enumerate(lines, 1):
        raw = line.rstrip("\r\n")
        match = re.match(r"^\s*(links?|description|descriptions|navigation_description):\s*(.*)$", raw, re.I)
        if match:
            key, value = match.groups()
            link_key = key.casefold() in {"link", "links"}
            if value:
                start = offset + raw.index(value)
                result.extend(_markdown_links(value, line_no, start, "frontmatter"))
            elif link_key:
                # Block sequence continues until next top-level YAML key.
                pass
        elif link_key and re.match(r"^\s+-\s+", raw):
            value_start = raw.index("-") + 1
            value = raw[value_start:].strip().strip("\"'")
            start = offset + raw.index(value) if value else offset
            result.extend(_markdown_links(value, line_no, start, "frontmatter"))
        elif raw and not raw[0].isspace():
            link_key = False
        offset += len(line)
    return result, True


def _markdown_links(text: str, line: int, offset: int, area: str) -> list[tuple[str, int, int, str]]:
    """Parse inline Markdown links with balanced/escaped labels and destinations."""
    masked = text
    found = []
    i = 0
    while i < len(masked):
        if masked[i] != "[" or (i and masked[i - 1] == "!"):
            i += 1; continue
        depth, j = 1, i + 1
        while j < len(masked) and depth:
            if masked[j] == "\\": j += 2; continue
            if masked[j] == "[": depth += 1
            elif masked[j] == "]": depth -= 1
            j += 1
        if depth or j >= len(masked) or masked[j] != "(":
            i += 1; continue
        k, parens = j + 1, 1
        while k < len(masked) and parens:
            if masked[k] == "\\": k += 2; continue
            if masked[k] == "(": parens += 1
            elif masked[k] == ")": parens -= 1
            k += 1
        if parens:
            i += 1; continue
        dest_start = j + 1
        while dest_start < k - 1 and masked[dest_start].isspace(): dest_start += 1
        dest_end = dest_start
        if dest_start < k - 1 and masked[dest_start] == "<":
            dest_start += 1
            dest_end = masked.find(">", dest_start, k - 1)
            if dest_end < 0: i = k; continue
        else:
            nested = 0
            while dest_end < k - 1:
                c = masked[dest_end]
                if c == "\\": dest_end += 2; continue
                if c == "(": nested += 1
                elif c == ")":
                    if not nested: break
                    nested -= 1
                elif c.isspace() and not nested: break
                dest_end += 1
        destination = masked[dest_start:dest_end]
        if destination and not re.match(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//)", destination):
            found.append((destination, line, offset + dest_start, area))
        i = k
    return found


def _authored_links(page: lint._Page) -> tuple[list[dict[str, Any]], list[str]]:
    links, limitations = [], []
    fm_links, has_fm = _frontmatter_links(page.text)
    # lint parser handles wikilinks/embeds, but markdown nested labels are covered here.
    masked = page.masked
    body_links = _markdown_links(masked, 1, 0, "body")
    # Assign actual source line and don't reparse generated reserved content.
    for dest, line, start, area in fm_links + body_links:
        if area == "body":
            line += masked.count("\n", 0, start)
            start = start
        links.append({"destination": dest, "line": line, "offset": start, "area": area, "syntax": "markdown"})
    # Wikilinks are graph evidence, but not safely rewritable Markdown destinations.
    parsed = lint._parse_links(page)
    for item in parsed:
        if item.syntax.startswith("wiki") or item.syntax.endswith("embed"):
            links.append({"destination": item.target, "line": item.line, "area": "body", "syntax": item.syntax, "span_supported": False})
    if not has_fm:
        limitations.append("frontmatter unavailable or malformed")
    return links, limitations


def compatibility_report(vault: str | Path, *, native_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(vault).expanduser().resolve(strict=True)
    if not root.is_dir(): raise ValueError("vault must be a directory")
    wiki = root / "wiki"
    all_paths = lint._walk_files(root)
    pages = []
    limitations: list[str] = ["reference-style Markdown links and HTML anchor links are not resolved; inspect these manually"]
    for path in all_paths:
        if path.suffix.casefold() != ".md": continue
        rel = path.relative_to(root).as_posix()
        try: page = lint._page_metadata(path, rel, "wiki")
        except (OSError, UnicodeError):
            limitations.append(f"unreadable Markdown: {rel}"); continue
        if not page.is_wiki_page: continue
        pages.append(page)
    targets = [lint._Target(path.relative_to(root).as_posix(), path, None) for path in all_paths]
    # add page metadata to index for aliases, exactly as OKF resolver does
    page_map = {p.path: p for p in pages}
    targets = [lint._Target(t.path, t.absolute, page_map.get(t.path)) for t in targets]
    resolver = lint._Resolver(targets, "wiki")
    if not wiki.is_dir(): limitations.append("wiki directory missing")
    evidence_map: dict[tuple[str, str], str] = {}
    evidence_status = "app-unavailable; filesystem fallback only"
    if native_evidence is not None:
        if Path(str(native_evidence.get("vault", ""))).expanduser().resolve() != root:
            raise ValueError("native evidence vault does not match requested vault")
        if not isinstance(native_evidence, dict) or not isinstance(native_evidence.get("links"), list):
            raise ValueError("native evidence must contain a links array")
        for item in native_evidence["links"]:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("source", "destination", "source_hash")) or not (item.get("resolved_path") is None or isinstance(item.get("resolved_path"), str)) or "resolved_path" not in item:
                raise ValueError("native evidence link must bind source, destination, source_hash, and resolved_path (string or null)")
            source_path = root / item["source"]
            if not source_path.is_file() or __import__("hashlib").sha256(source_path.read_bytes()).hexdigest() != item["source_hash"]:
                raise ValueError(f"stale native evidence for {item['source']}")
            key = item["source"], item["destination"]
            if key in evidence_map and evidence_map[key] != item["resolved_path"]:
                raise ValueError("conflicting native evidence for source/destination")
            evidence_map[key] = item["resolved_path"]
        evidence_status = "reviewed native evidence supplied; unmatched links remain filesystem fallback"
    results = []
    for page in pages:
        kind = "generated-navigation" if Path(page.path).name.casefold() == "index.md" else "lifecycle-history" if Path(page.path).name.casefold() == "log.md" else "authored"
        links, warns = _authored_links(page); limitations.extend(f"{page.path}: {w}" for w in warns)
        for link in links:
            dest = link["destination"]
            raw_file, raw_fragment, fragment_kind = lint._split_fragment(dest)
            file_part = urllib.parse.unquote(raw_file)
            fragment = urllib.parse.unquote(raw_fragment) if raw_fragment is not None else None
            decoded = file_part + (f"#{fragment}" if fragment is not None else "")
            source_link = lint._Link(page.path, link["line"], decoded, file_part, fragment, fragment_kind, link["syntax"], link["syntax"] == "markdown")
            candidates = resolver.resolve(source_link)
            native = evidence_map.get((page.path, dest))
            flags = []
            if dest.startswith("/"): flags.append("leading-slash")
            if dest.split("#", 1)[0].endswith("/"): flags.append("folder-only")
            if dest and not dest.startswith(("./", "../", "/")) and "/" in dest and not re.match(r"^[A-Za-z][\w+.-]*:", dest):
                src_relative = Path(page.path).parent / urllib.parse.unquote(dest.split("#", 1)[0])
                root_dupe = root / urllib.parse.unquote(dest.split("#", 1)[0])
                if root_dupe.exists() and root_dupe.is_file() and (root / src_relative).exists() and root_dupe.resolve() != (root / src_relative).resolve(): flags.append("bare-relative-root-shadow")
            status = "native-evidence" if native is not None else "filesystem-fallback"
            resolved = native if native is not None else (candidates[0].path if len(candidates) == 1 else None)
            intended = candidates[0].path if len(candidates) == 1 else None
            proposal = None
            if flags and intended and link.get("span_supported") is not False:
                rel = posixpath.relpath(intended, posixpath.dirname(page.path))
                if not rel.startswith("../"):
                    rel = "./" + rel
                replacement = urllib.parse.quote(rel, safe="/.-_") + ("#" + dest.split("#",1)[1] if "#" in dest else "")
                proposal = {"replacement": replacement, "span": [link["offset"], link["offset"] + len(dest)]}
            results.append({"source": page.path, "kind": kind, "destination": dest, "line": link["line"], "area": link.get("area", "body"), "classification": flags, "okf_candidates": [c.path for c in candidates], "intended_path": intended, "native_resolved_path": native, "evidence": ("native-unresolved" if (page.path, dest) in evidence_map and native is None else status), "proposal": proposal, "repair_state": "historical-not-active-repair" if kind == "lifecycle-history" else "active"})
    sparse = [p.path for p in pages if p.path != "wiki/index.md" and p.path != "wiki/log.md" and not lint._parse_links(p)]
    return {"version": 1, "vault": str(root), "report_api": "obsidian_compat.compatibility_report(vault, native_evidence=None)", "discovery": {"markdown_pages": len(pages), "links": len(results), "symlinks_followed": False, "scope": "parser-discovered wiki Markdown links; this is not a completeness-certified native graph; hidden/pruned paths, unsupported syntax, and non-Markdown are excluded"},
        "native_graph_complete": False, "evidence_quality": evidence_status, "links": results, "sparse_or_archive_pages": sparse, "limitations": sorted(set(limitations))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--json", action="store_true", help="write JSON report to stdout")
    parser.add_argument("--native-evidence", help="reviewed JSON evidence file")
    args = parser.parse_args()
    evidence = json.loads(Path(args.native_evidence).read_text()) if args.native_evidence else None
    report = compatibility_report(args.vault, native_evidence=evidence)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__": raise SystemExit(main())
