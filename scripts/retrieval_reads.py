"""Hash-bound, read-only wiki source reads and compact outlines."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import unicodedata

MAX_CHARS_DEFAULT = 12000
MAX_CHARS_LIMIT = 12000


class ReadError(ValueError):
    pass


class SourceChanged(ReadError):
    pass


def _open_dir(parent: int, name: str) -> int:
    try:
        return os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    except OSError as exc:
        raise ReadError("unsafe or missing wiki path component") from exc


def _check_siblings(fd: int, name: str) -> None:
    wanted = unicodedata.normalize("NFC", name).casefold()
    try:
        entries = os.listdir(fd)
    except OSError as exc:
        raise ReadError("unable to inspect wiki path") from exc
    for entry in entries:
        if entry != name and unicodedata.normalize("NFC", entry).casefold() == wanted:
            raise ReadError("ambiguous case or Unicode-normalization path alias")


def _canonical_page(page: str) -> tuple[str, ...]:
    if not isinstance(page, str) or not page or "\\" in page:
        raise ReadError("--page must be one canonical vault-relative wiki Markdown path")
    parts = page.split("/")
    if (parts[0] != "wiki" or any(not part or part in {".", ".."} for part in parts)
            or page.startswith("/") or not page.casefold().endswith(".md") or any(part.startswith(".") for part in parts)):
        raise ReadError("--page must be one canonical, visible wiki Markdown path")
    if any(part.casefold() in {"index.md", "log.md", "_plan.md"} for part in parts):
        raise ReadError("reserved wiki pages cannot be read")
    return tuple(parts)


def _open_root(vault: Path, *, changed: bool = False) -> list[int]:
    """Open every canonical absolute root component without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        descriptors.append(os.open("/", flags))
        current = descriptors[0]
        for part in vault.parts[1:]:
            descriptors.append(os.open(part, flags, dir_fd=current))
            current = descriptors[-1]
        return descriptors
    except OSError as exc:
        for fd in reversed(descriptors):
            os.close(fd)
        if changed:
            raise SourceChanged("selected vault root changed while being read") from exc
        raise ReadError("unsafe or missing selected vault root") from exc


def _verify_root(vault: Path, opened: list[int]) -> None:
    verified = _open_root(vault, changed=True)
    try:
        if len(verified) != len(opened) or any(
            (os.fstat(left).st_dev, os.fstat(left).st_ino) != (os.fstat(right).st_dev, os.fstat(right).st_ino)
            for left, right in zip(verified, opened)
        ):
            raise SourceChanged("selected vault root changed while being read")
    finally:
        for fd in reversed(verified):
            os.close(fd)


def read_snapshot(vault: Path, page: str) -> tuple[bytes, str]:
    parts = _canonical_page(page)
    vault = Path(os.path.realpath(vault))
    root_chain = _open_root(vault)
    root_fd = root_chain[-1]
    current = root_fd
    opened: list[int] = []
    try:
        for name in parts[:-1]:
            _check_siblings(current, name)
            child = _open_dir(current, name)
            opened.append(child)
            current = child
        name = parts[-1]
        _check_siblings(current, name)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(name, flags, dir_fd=current)
        except OSError as exc:
            raise ReadError("unsafe or missing wiki page") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ReadError("wiki page must be a regular file without hardlink aliases")
            chunks = []
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                chunks.append(block)
            after = os.fstat(fd)
            try:
                path_stat = os.stat(name, dir_fd=current, follow_symlinks=False)
            except OSError as exc:
                raise SourceChanged("source changed while being read") from exc
            identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity(before) != identity(after) or identity(after) != identity(path_stat):
                raise SourceChanged("source changed while being read")
            # Ensure every directory descriptor still denotes the directory at
            # its canonical name beneath the selected vault.
            verify = root_fd
            verify_fds = []
            try:
                for part, original in zip(parts[:-1], opened):
                    _check_siblings(verify, part)
                    child = _open_dir(verify, part)
                    verify_fds.append(child)
                    if identity(os.fstat(child)) != identity(os.fstat(original)):
                        raise SourceChanged("wiki path changed while being read")
                    verify = child
                try:
                    _check_siblings(verify, name)
                except ReadError as exc:
                    raise SourceChanged("wiki path aliases changed while being read") from exc
                try:
                    terminal_stat = os.stat(name, dir_fd=verify, follow_symlinks=False)
                except OSError as exc:
                    raise SourceChanged("source changed while being read") from exc
                if identity(terminal_stat) != identity(after):
                    raise SourceChanged("source changed while being read")
                _verify_root(vault, root_chain)
            finally:
                for verify_fd in reversed(verify_fds):
                    os.close(verify_fd)
            data = b"".join(chunks)
            return data, hashlib.sha256(data).hexdigest()
        finally:
            os.close(fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)
        for fd in reversed(root_chain):
            os.close(fd)


def _line(text: str, offset: int) -> int:
    return sum(match.end() <= offset for match in re.finditer(r"\r\n|\r|\n", text)) + 1


def _lines(text: str) -> list[tuple[int, int]]:
    """Source offsets for CommonMark physical lines (CRLF, CR, or LF only)."""
    boundaries = [match.end() for match in re.finditer(r"\r\n|\r|\n", text)]
    result = []
    start = 0
    for end in boundaries:
        result.append((start, end))
        start = end
    if start < len(text) or not result:
        result.append((start, len(text)))
    return result


def _projection(text: str) -> str:
    try:
        from retrieval_projection import current
    except ImportError:
        from .retrieval_projection import current
    return current(text)


def read_page(vault: Path, page: str, expected_hash: str, *, section: str | None = None,
              start: int | None = None, end: int | None = None, view: str = "raw",
              max_chars: int = MAX_CHARS_DEFAULT, cursor: int | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash or ""):
        raise ReadError("--expected-hash must be 64 lowercase hexadecimal characters")
    if section is not None:
        if start is not None or end is not None:
            raise ReadError("--section cannot be combined with --start or --end")
    elif start is None or end is None:
        raise ReadError("select exactly one --section or complete --start/--end range")
    if view not in {"raw", "current"}:
        raise ReadError("--view must be raw or current")
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_CHARS_LIMIT:
        raise ReadError(f"--max-chars must be between 1 and {MAX_CHARS_LIMIT}")
    raw, digest = read_snapshot(vault, page)
    if digest != expected_hash:
        raise SourceChanged("source hash changed; request a fresh outline/search reference")
    source = raw.decode("utf-8", errors="replace")
    if section is not None:
        entries = outline_entries(source)
        selected = next((item for item in entries if item["id"] == section), None)
        if selected is None:
            raise ReadError("unknown section ID")
        selected_start, selected_end = selected["start"], selected["end"]
        selector = {"section": section}
    else:
        if type(start) is not int or type(end) is not int or start < 0 or end < start or end > len(source):
            raise ReadError("range must be within decoded source character coordinates")
        selected_start, selected_end = start, end
        selector = {"start": start, "end": end}
    display = source if view == "raw" else _projection(source)
    if len(display) != len(source):
        raise ReadError("current projection changed source coordinates")
    if cursor is None:
        cursor = selected_start
    if type(cursor) is not int or not selected_start <= cursor <= selected_end:
        raise ReadError("--cursor must be within the original selected range")
    returned_end = min(selected_end, cursor + max_chars)
    line_start = _line(source, cursor)
    line_end = _line(source, returned_end - 1) if returned_end > cursor else line_start
    response = {"sourceHash": digest, "page": page, "path": page, "view": view,
                "projection": "raw" if view == "raw" else "current-strike-mask-v1", "selected": {"start": selected_start, "end": selected_end},
                "returned": {"start": cursor, "end": returned_end}, "lineStart": line_start,
                "lineEnd": line_end, "text": display[cursor:returned_end],
                "truncated": cursor > selected_start or returned_end < selected_end,
                "selectionComplete": cursor == selected_start and returned_end == selected_end,
                "warning": "An incomplete selection may omit qualifications; hash identity does not establish factual currency.",
                "selector": selector}
    if returned_end < selected_end:
        response["continuation"] = {"page": page, "expectedHash": digest, "view": view,
                                    **selector, "cursor": returned_end}
    return response


def outline_entries(source: str) -> list[dict]:
    """Return selectable source-coordinate ranges for frontmatter, preamble and headings."""
    from markdown_it import MarkdownIt

    front_end = 0
    source_lines = _lines(source)
    line_text = [source[start:end] for start, end in source_lines]
    if line_text and line_text[0].rstrip("\r\n") == "---":
        closing = next((idx for idx in range(1, len(line_text)) if line_text[idx].rstrip("\r\n") in {"---", "..."}), None)
        if closing is not None:
            front_end = source_lines[closing][1]
    body = source[front_end:]
    line_offsets = [0]
    for start, end in _lines(body):
        line_offsets.append(end)
    tokens = MarkdownIt("commonmark").parse(body)
    headings = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or not token.map:
            continue
        inline = tokens[index + 1]
        line = token.map[0]
        start = front_end + line_offsets[line]
        title = inline.content if inline.type == "inline" else ""
        title_truncated = len(title) > 160
        headings.append({"id": f"heading:{start}", "kind": "heading", "level": int(token.tag[1]),
                         "title": title[:160] + ("…" if title_truncated else ""), "titleTruncated": title_truncated,
                         "start": start, "lineStart": _line(source, start)})
    entries = []
    if front_end:
        entries.append({"id": "metadata", "kind": "metadata", "title": "Frontmatter", "start": 0, "end": front_end,
                        "lineStart": 1})
    if headings:
        first = headings[0]["start"]
        if front_end < first:
            entries.append({"id": "preamble", "kind": "preamble", "title": "Preamble", "start": front_end, "end": first,
                            "lineStart": _line(source, front_end)})
    elif front_end < len(source) or not headings:
        entries.append({"id": "preamble", "kind": "preamble", "title": "Page content", "start": front_end,
                        "end": len(source), "lineStart": _line(source, front_end)})
    for index, heading in enumerate(headings):
        end = len(source)
        for next_heading in headings[index + 1:]:
            if next_heading["level"] <= heading["level"]:
                end = next_heading["start"]
                break
        entries.append({**heading, "end": end})
    return entries


def outline_page(vault: Path, page: str, *, expected_hash: str | None = None, limit: int = 10,
                 offset: int = 0) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ReadError("outline --limit must be 1–100 and --offset must be nonnegative")
    if offset > 0 and expected_hash is None:
        raise ReadError("outline continuation requires --expected-hash")
    raw, digest = read_snapshot(vault, page)
    if expected_hash is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ReadError("--expected-hash must be 64 lowercase hexadecimal characters")
        if digest != expected_hash:
            raise SourceChanged("source hash changed; restart outline pagination")
    source = raw.decode("utf-8", errors="replace")
    entries = outline_entries(source)
    selected = entries[offset:offset + limit]
    next_offset = offset + len(selected)
    return {"sourceHash": digest, "page": page, "path": page, "sourceChars": len(source),
            "entries": selected, "offset": offset, "limit": limit,
            "nextOffset": next_offset if next_offset < len(entries) else None,
            "continuation": ({"page": page, "expectedHash": digest, "offset": next_offset, "limit": limit}
                             if next_offset < len(entries) else None)}
