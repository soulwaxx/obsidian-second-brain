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
import hashlib
import json
import posixpath
import re
import urllib.parse
import sys
from pathlib import Path, PurePosixPath
from typing import Any

MW = Path(__file__).resolve().parents[1] / "skills/wiki/scripts"
sys.path.insert(0, str(MW))
from okf_mw import lint  # noqa: E402


def _frontmatter_links(text: str) -> tuple[list[tuple[str, int, int, str, str]], bool]:
    """Read only supported top-level link and authored description values."""
    fm = lint._parse_frontmatter(text)
    if not fm.present:
        return [], False
    lines = text[:fm.end_offset].splitlines(keepends=True)
    starts = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    result = []
    recognized = {"link", "links", "related", "description", "descriptions", "navigation_description"}
    top_key = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
    for i, line in enumerate(lines):
        raw = line.rstrip("\r\n")
        match = top_key.match(raw)
        if not match or match.group(1).casefold() not in recognized:
            continue
        key = match.group(1).casefold()
        value = match.group(2) or ""
        value_start = starts[i] + match.start(2) if match.group(2) is not None else starts[i] + len(raw)
        if key in {"link", "links", "related"}:
            if value.strip():
                segment, segment_start, base_line = value, value_start, i + 1
                result.extend(_metadata_markdown_links(segment, segment_start, base_line))
            else:
                for j in range(i + 1, len(lines)):
                    child = lines[j].rstrip("\r\n")
                    if top_key.match(child):
                        break
                    list_item = re.match(r"^[ \t]*-[ \t]+(.*)$", child)
                    if list_item and list_item.group(1):
                        start = starts[j] + list_item.start(1)
                        result.extend(_metadata_markdown_links(list_item.group(1), start, j + 1))
        elif value.strip() in {">", ">-", ">+", "|", "|-", "|+"}:
            end = fm.end_offset
            for j in range(i + 1, len(lines)):
                if top_key.match(lines[j].rstrip("\r\n")):
                    end = starts[j]
                    break
            start = starts[i] + len(line)
            result.extend(_metadata_markdown_links(text[start:end], start, i + 2))
        elif value.strip():
            result.extend(_metadata_markdown_links(value, value_start, i + 1))
    return result, True


def _metadata_markdown_links(value: str, offset: int, line: int) -> list[tuple[str, int, int, str, str]]:
    masked = lint._mask_markdown_code(value)
    return _markdown_links(masked, line, offset, "frontmatter")


def _markdown_links(text: str, line: int, offset: int, area: str) -> list[tuple[str, int, int, str, str]]:
    """Parse inline Markdown links with balanced labels/destinations and escapes."""
    found = []
    i = 0
    while i < len(text):
        if text[i] != "[":
            i += 1
            continue
        slash_count = 0
        before = i - 1
        while before >= 0 and text[before] == "\\":
            slash_count += 1
            before -= 1
        if slash_count % 2:
            i += 1
            continue
        embedded = i > 0 and text[i - 1] == "!"
        depth, j = 1, i + 1
        while j < len(text) and depth:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == "[":
                depth += 1
            elif text[j] == "]":
                depth -= 1
            j += 1
        if depth or j >= len(text) or text[j] != "(":
            i += 1
            continue
        k, parens = j + 1, 1
        while k < len(text) and parens:
            if text[k] == "\\":
                k += 2
                continue
            if text[k] == "(":
                parens += 1
            elif text[k] == ")":
                parens -= 1
            k += 1
        if parens:
            i += 1
            continue
        dest_start = j + 1
        while dest_start < k - 1 and text[dest_start].isspace():
            dest_start += 1
        dest_end = dest_start
        if dest_start < k - 1 and text[dest_start] == "<":
            dest_start += 1
            dest_end = text.find(">", dest_start, k - 1)
            if dest_end < 0:
                i = k
                continue
        else:
            nested = 0
            while dest_end < k - 1:
                char = text[dest_end]
                if char == "\\":
                    dest_end += 2
                    continue
                if char == "(":
                    nested += 1
                elif char == ")":
                    if not nested:
                        break
                    nested -= 1
                elif char.isspace() and not nested:
                    break
                dest_end += 1
        destination = text[dest_start:dest_end]
        if destination and not re.match(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//)", destination):
            found.append((destination, line + text.count("\n", 0, dest_start), offset + dest_start, area, "markdown-embed" if embedded else "markdown-link"))
        i = k
    return found


def _source_offset_map(path: Path) -> list[int]:
    """Map lint's UTF-8-sig/universal-newline offsets to original UTF-8 text offsets."""
    raw = path.read_bytes().decode("utf-8")
    bom = raw.startswith("\ufeff")
    body = raw[1:] if bom else raw
    offsets = [1 if bom else 0]
    index = 0
    while index < len(body):
        index += 2 if body.startswith("\r\n", index) else 1
        offsets.append((1 if bom else 0) + index)
    return offsets


def _markdown_resolver_link(page: lint._Page, destination: str, line: int, syntax: str) -> lint._Link:
    raw_file, raw_fragment, fragment_kind = lint._split_fragment(destination)
    file_part = urllib.parse.unquote(raw_file)
    fragment = urllib.parse.unquote(raw_fragment) if raw_fragment is not None else None
    target = file_part + (f"#{fragment}" if fragment is not None else "")
    return lint._Link(page.path, line, target, file_part, fragment, fragment_kind, syntax, True)


def _authored_links(page: lint._Page) -> tuple[list[dict[str, Any]], list[str]]:
    links, limitations = [], []
    fm_links, has_fm = _frontmatter_links(page.text)
    body_links = _markdown_links(page.masked, 1, 0, "body")
    parsed = lint._parse_links(page)
    embeds = [link for link in parsed if link.syntax == "markdown-embed"]
    for dest, line, start, area, syntax in fm_links + body_links:
        resolver_link = _markdown_resolver_link(page, dest, line, syntax)
        if area == "body" and syntax == "markdown-embed":
            resolver_link = next((link for link in embeds if link.line == line and link.target == resolver_link.target), resolver_link)
        links.append({"destination": dest, "line": line, "offset": start, "area": area, "syntax": syntax, "resolver_link": resolver_link})
    for item in parsed:
        if item.syntax.startswith("wiki") or item.syntax == "embed":
            links.append({"destination": item.target, "line": item.line, "area": "body", "syntax": item.syntax, "resolver_link": item, "span_supported": False})
    if not has_fm:
        limitations.append("frontmatter unavailable or malformed")
    return links, limitations


def compatibility_report(vault: str | Path, *, native_evidence: Any = None) -> dict[str, Any]:
    root = Path(vault).expanduser().resolve(strict=True)
    if not root.is_dir(): raise ValueError("vault must be a directory")
    wiki = root / "wiki"
    all_paths = lint._walk_files(root)
    pages = []
    target_pages = {}
    limitations: list[str] = ["reference-style Markdown links and HTML anchor links are not resolved; inspect these manually"]
    for path in all_paths:
        if path.suffix.casefold() != ".md": continue
        rel = path.relative_to(root).as_posix()
        try: page = lint._page_metadata(path, rel, "wiki")
        except (OSError, UnicodeError):
            limitations.append(f"unreadable Markdown: {rel}"); continue
        target_pages[rel] = page
        if not page.is_wiki_page: continue
        pages.append(page)
    targets = [lint._Target(path.relative_to(root).as_posix(), path, target_pages.get(path.relative_to(root).as_posix())) for path in all_paths]
    # Keep all Markdown metadata for target resolution; report sources remain wiki-only.
    page_map = {p.path: p for p in pages}
    known_link_keys = {(page.path, link["destination"]) for page in pages for link in _authored_links(page)[0]}
    resolver = lint._Resolver(targets, "wiki")
    if not wiki.is_dir(): limitations.append("wiki directory missing")
    evidence_map: dict[tuple[str, str], str | None] = {}
    evidence_status = "native evidence not supplied; filesystem fallback; app not queried"
    if native_evidence is not None:
        if not isinstance(native_evidence, dict):
            raise ValueError("native evidence must be an object")
        if not isinstance(native_evidence.get("vault"), str) or Path(native_evidence["vault"]).expanduser().resolve() != root:
            raise ValueError("native evidence vault does not match requested vault")
        if not isinstance(native_evidence.get("links"), list):
            raise ValueError("native evidence must contain a links array")
        for item in native_evidence["links"]:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("source", "destination", "source_hash")) or not (item.get("resolved_path") is None or isinstance(item.get("resolved_path"), str)) or "resolved_path" not in item:
                raise ValueError("native evidence link must bind source, destination, source_hash, and resolved_path (string or null)")
            source = item["source"]
            if source not in page_map:
                raise ValueError(f"native evidence source is not a discovered wiki page: {source}")
            source_path = page_map[source].absolute
            if (source, item["destination"]) not in known_link_keys:
                raise ValueError(f"native evidence destination is not a discovered link in {source}")
            if hashlib.sha256(source_path.read_bytes()).hexdigest() != item["source_hash"]:
                raise ValueError(f"stale native evidence for {source}")
            resolved_path = item["resolved_path"]
            if resolved_path is not None:
                pure = PurePosixPath(resolved_path)
                if pure.is_absolute() or not resolved_path or ".." in pure.parts or chr(92) in resolved_path or pure.as_posix() != resolved_path:
                    raise ValueError("native resolved_path must be canonical vault-relative path or null")
            key = source, item["destination"]
            if key in evidence_map and evidence_map[key] != resolved_path:
                raise ValueError("conflicting native evidence for source/destination")
            evidence_map[key] = resolved_path
        evidence_status = "reviewed native evidence supplied; unmatched links remain filesystem fallback"
    results = []
    for page in pages:
        kind = "generated-navigation" if Path(page.path).name.casefold() == "index.md" else "lifecycle-history" if Path(page.path).name.casefold() == "log.md" else "authored"
        links, warns = _authored_links(page)
        if kind == "authored":
            limitations.extend(f"{page.path}: {w}" for w in warns)
        source_offsets = _source_offset_map(page.absolute)
        for link in links:
            dest = link["destination"]
            source_link = link["resolver_link"]
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
            intended = candidates[0].path if len(candidates) == 1 else None
            proposal = None
            if flags and intended and link.get("span_supported") is not False:
                rel = posixpath.relpath(intended, posixpath.dirname(page.path))
                if not rel.startswith("../"):
                    rel = "./" + rel
                replacement = urllib.parse.quote(rel, safe="/.-_") + ("#" + dest.split("#",1)[1] if "#" in dest else "")
                proposal = {"replacement": replacement, "span": [source_offsets[link["offset"]], source_offsets[link["offset"] + len(dest)] ]}
            results.append({"source": page.path, "kind": kind, "destination": dest, "line": link["line"], "area": link.get("area", "body"), "classification": flags, "okf_candidates": [c.path for c in candidates], "intended_path": intended, "native_resolved_path": native, "evidence": ("native-unresolved" if (page.path, dest) in evidence_map and evidence_map[(page.path, dest)] is None else status), "proposal": proposal, "repair_state": "historical-not-active-repair" if kind == "lifecycle-history" else "active"})
    resolved_sources = {entry["source"] for entry in results if entry["intended_path"] is not None or entry["native_resolved_path"] is not None}
    sparse = [p.path for p in pages if Path(p.path).name.casefold() not in {"index.md", "log.md"} and p.path not in resolved_sources]
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
