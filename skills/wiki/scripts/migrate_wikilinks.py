#!/usr/bin/env python3
"""One-shot wikilink -> Markdown-link migration for an OKF v0.2 wiki vault.

Rewrites Obsidian wikilinks in concept *bodies* to bundle-root-relative
Markdown links, the canonical OKF v0.2 internal-link form:

    [[concepts/rag]]            -> [Retrieval-Augmented Generation](/concepts/rag.md)
    [[rag|the RAG page]]        -> [the RAG page](/concepts/rag.md)
    [[architecture#Ownership]]  -> [Architecture](/architecture.md#Ownership)
    [[#Local Heading]]          -> [Local Heading](#Local%20Heading)

What it deliberately does NOT touch, to stay lossless:

- Embeds (``![[...]]``) — image/PDF/page transclusions have no faithful
  Markdown-link equivalent. Left as-is and reported.
- Frontmatter (including the ``related:`` wikilink list) — not rendered
  Markdown, so it produces no OKF backlinks. Left as-is.
- Text inside fenced or inline code — never rewritten.
- Reserved files (``index.md``, ``log.md``) — generated / hook-owned.
- Links whose target does not resolve to exactly one concept file — left
  unchanged and reported so a human can fix them.

Resolution matches Obsidian's rules for this vault: a path-qualified target
(``concepts/rag``) resolves by vault-relative path; a bare target (``rag``)
resolves by unique filename stem. ``.md`` is optional; matching is
case-insensitive.

Label (when no explicit ``|alias``): the target page's frontmatter ``title``
if present, else the target's filename stem.

Usage:
    migrate_wikilinks.py <vault-root> [--apply] [--verbose]

Default is a dry run (nothing written). Pass --apply to rewrite files.
After --apply, regenerate indexes and re-lint:
    python3 okf_mw/sync.py <vault-root>
    python3 okf_mw/lint.py <vault-root> --format markdown

Exit codes:
    0 — completed (dry run, or applied with no unresolved links)
    1 — completed but some wikilinks could not be resolved (need manual fix)
    2 — usage / vault error
"""

import argparse
import os
import re
import sys
import urllib.parse

RESERVED = {"index.md", "log.md"}

# Non-embed wikilink: the leading `!` (embed) is captured so we can skip it.
_WIKILINK_RE = re.compile(r"(?P<embed>!)?\[\[(?P<body>[^\]\r\n]+?)\]\]")
_TITLE_RE = re.compile(r"^title\s*:\s*(.*)$", re.MULTILINE)


def _strip_quotes(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _split_frontmatter(text: str) -> tuple[str, int]:
    """Return (frontmatter_block, body_start_offset).

    body_start_offset is the char index where the Markdown body begins (0 if
    there is no frontmatter). The frontmatter block excludes the fences.
    """
    if not text.startswith("---"):
        return "", 0
    m = re.match(r"^---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|$)", text, re.DOTALL)
    if not m:
        return "", 0
    return m.group(1), m.end()


def _frontmatter_title(fm_block: str) -> str | None:
    m = _TITLE_RE.search(fm_block)
    if not m:
        return None
    val = _strip_quotes(m.group(1))
    return val or None


def _build_code_mask(body: str) -> list[bool]:
    """Mark every body char that lives inside fenced or inline code.

    Fenced blocks (``` / ~~~ with <=3 leading spaces) mask their whole region
    including the fence lines; inline code spans mask the backticks and their
    content. Matches overlapping any masked char are left untouched.
    """
    mask = [False] * len(body)
    fence: str | None = None  # active fence marker char run, e.g. "```"
    offset = 0
    for line in body.splitlines(keepends=True):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        fence_match = re.match(r"(`{3,}|~{3,})", stripped) if indent <= 3 else None
        if fence is None:
            if fence_match:
                fence = fence_match.group(1)[0] * 3  # normalize to marker char
                for i in range(offset, offset + len(line)):
                    mask[i] = True
                offset += len(line)
                continue
        else:
            # inside a fenced block: everything is code until a closing fence
            for i in range(offset, offset + len(line)):
                mask[i] = True
            if fence_match and fence_match.group(1)[0] * 3 == fence:
                fence = None
            offset += len(line)
            continue
        # Not in a fence: mask inline code spans on this line.
        for span in re.finditer(r"(`+)(?:.+?)(\1)", line):
            for i in range(offset + span.start(), offset + span.end()):
                mask[i] = True
        offset += len(line)
    return mask


class _Index:
    """Resolves wikilink targets to vault-relative concept paths."""

    def __init__(self, wiki_dir: str) -> None:
        self.by_relpath: dict[str, str] = {}   # 'concepts/rag' -> 'concepts/rag.md'
        self.by_stem: dict[str, list[str]] = {}  # 'rag' -> ['concepts/rag.md']
        self.titles: dict[str, str] = {}         # 'concepts/rag.md' -> title
        for root, dirnames, filenames in os.walk(wiki_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and not os.path.islink(os.path.join(root, d))]
            for fname in filenames:
                if (fname.startswith(".") or not fname.endswith(".md")
                        or os.path.islink(os.path.join(root, fname))):
                    continue
                if fname.casefold() in RESERVED:
                    continue
                rel = os.path.relpath(os.path.join(root, fname), wiki_dir)
                rel = rel.replace(os.sep, "/")
                self.by_relpath[rel[:-3].casefold()] = rel
                stem = fname[:-3]
                self.by_stem.setdefault(stem.casefold(), []).append(rel)
                try:
                    with open(os.path.join(root, fname), encoding="utf-8",
                              errors="replace") as fh:
                        fm, _ = _split_frontmatter(fh.read())
                    title = _frontmatter_title(fm)
                    if title:
                        self.titles[rel] = title
                except OSError:
                    pass

    def resolve(self, file_part: str) -> tuple[str | None, str]:
        """Return (relpath_or_None, status).

        status is 'ok', 'ambiguous', or 'not-found'. relpath is vault-relative
        (includes .md) when status == 'ok'.
        """
        fp = file_part.strip().strip("/")
        if fp.casefold().endswith(".md"):
            fp = fp[:-3]
        if not fp:
            return None, "not-found"
        if "/" in fp:
            hit = self.by_relpath.get(fp.casefold())
            return (hit, "ok") if hit else (None, "not-found")
        paths = self.by_stem.get(fp.casefold())
        if not paths:
            return None, "not-found"
        if len(paths) > 1:
            return None, "ambiguous"
        return paths[0], "ok"


def _split_alias(body: str) -> tuple[str, str | None]:
    """Split a wikilink body into (target, alias) on the first unescaped '|'."""
    escaped = False
    for i, ch in enumerate(body):
        if ch == "\\" and not escaped:
            escaped = True
            continue
        if ch == "|" and not escaped:
            return body[:i], body[i + 1:]
        escaped = False
    return body, None


def _split_fragment(target: str) -> tuple[str, str | None, str]:
    """Split 'file#frag' -> (file, frag, kind). kind is 'heading' or 'block'."""
    idx = target.find("#")
    if idx < 0:
        return target, None, "heading"
    file_part = target[:idx]
    frag = target[idx + 1:].strip()
    if frag.startswith("^"):
        return file_part, frag[1:], "block"
    return file_part, frag, "heading"


def _escape_label(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def _encode_fragment(frag: str, kind: str) -> str:
    if kind == "block":
        # Block ids are [A-Za-z0-9_-]; safe verbatim, prefixed with ^.
        return "#^" + frag
    # Heading: percent-encode so spaces/parens don't break the Markdown link.
    # lint.py unquotes + casefolds both sides, so this round-trips exactly.
    return "#" + urllib.parse.quote(frag, safe="")


def _stem(file_part: str) -> str:
    return file_part.strip().strip("/").rsplit("/", 1)[-1].removesuffix(".md")


class _Stats:
    def __init__(self) -> None:
        self.converted = 0
        self.embeds = 0
        self.unresolved: list[tuple[str, str, str]] = []  # (relpath, raw, reason)


def _convert_body(body: str, source_rel: str, index: _Index,
                  stats: _Stats) -> str:
    mask = _build_code_mask(body)

    def repl(m: re.Match) -> str:
        raw = m.group(0)
        if m.group("embed"):
            stats.embeds += 1
            return raw
        if any(mask[i] for i in range(m.start(), m.end())):
            return raw  # inside code — never touch

        target, alias = _split_alias(m.group("body"))
        file_part, frag, kind = _split_fragment(target)
        alias = alias.strip() if alias else None

        # Same-page fragment link: [[#Heading]]
        if not file_part.strip():
            if not frag:
                return raw
            label = alias or frag
            return f"[{_escape_label(label)}]({_encode_fragment(frag, kind)})"

        relpath, status = index.resolve(file_part)
        if status != "ok" or relpath is None:
            stats.unresolved.append((source_rel, raw, status))
            return raw

        label = alias or index.titles.get(relpath) or _stem(file_part)
        dest = "/" + urllib.parse.quote(relpath, safe="/")
        if frag:
            dest += _encode_fragment(frag, kind)
        stats.converted += 1
        return f"[{_escape_label(label)}]({dest})"

    return _WIKILINK_RE.sub(repl, body)


def _process_file(path: str, rel: str, index: _Index, stats: _Stats,
                  apply: bool) -> bool:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    _, body_start = _split_frontmatter(text)
    head, body = text[:body_start], text[body_start:]
    new_body = _convert_body(body, rel, index, stats)
    if new_body == body:
        return False
    if apply:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(head + new_body)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vault_root", help="Vault root (the directory containing wiki/)")
    ap.add_argument("--apply", action="store_true",
                    help="Rewrite files in place (default: dry run)")
    ap.add_argument("--verbose", action="store_true",
                    help="List every changed file")
    args = ap.parse_args()

    wiki_dir = os.path.join(os.path.abspath(args.vault_root), "wiki")
    if not os.path.isdir(wiki_dir) or os.path.islink(wiki_dir):
        print(f"error: {wiki_dir} is not a directory", file=sys.stderr)
        return 2

    index = _Index(wiki_dir)
    stats = _Stats()
    changed_files: list[str] = []

    for root, dirnames, filenames in os.walk(wiki_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")
                       and not os.path.islink(os.path.join(root, d))]
        for fname in sorted(filenames):
            if (fname.startswith(".") or not fname.endswith(".md")
                    or os.path.islink(os.path.join(root, fname))):
                continue
            if fname.casefold() in RESERVED:
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, wiki_dir).replace(os.sep, "/")
            if _process_file(path, rel, index, stats, args.apply):
                changed_files.append(rel)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] wikilink -> Markdown-link migration")
    print(f"  files changed:        {len(changed_files)}")
    print(f"  wikilinks converted:  {stats.converted}")
    print(f"  embeds left as-is:    {stats.embeds}")
    print(f"  unresolved (skipped): {len(stats.unresolved)}")

    if args.verbose and changed_files:
        print("\nChanged files:")
        for rel in changed_files:
            print(f"  wiki/{rel}")

    if stats.unresolved:
        print("\nUnresolved wikilinks (left unchanged — fix by hand):")
        for rel, raw, reason in stats.unresolved:
            print(f"  wiki/{rel}: {raw}  [{reason}]")

    if not args.apply and changed_files:
        print("\nDry run only. Re-run with --apply to write, then:")
        print(f"  python3 {os.path.dirname(__file__)}/okf_mw/sync.py {args.vault_root}")
        print(f"  python3 {os.path.dirname(__file__)}/okf_mw/lint.py "
              f"{args.vault_root} --format markdown")

    return 1 if stats.unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
