"""Conservative, offset-preserving retrieval view; never rewrites authored text.

Only unambiguous, single-line ~~strikethrough~~ outside literal contexts is
masked. Whitespace/newlines and all original character coordinates survive.
Ambiguous Markdown is left intact for human temporal judgment, not guessed.
"""
import re

SCHEME = "current-strike-mask-v1"
ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}(?:-\d{2})?(?!\d)")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _escaped(text, offset):
    backslashes = 0
    while offset > 0 and text[offset - 1] == "\\":
        offset -= 1
        backslashes += 1
    return backslashes % 2 == 1


def _pair(text, offset):
    return (text[offset:offset + 2] == "~~" and text[offset - 1:offset] != "~"
            and text[offset + 2:offset + 3] != "~")


def current(source):
    """Mask clear superseded spans with spaces, retaining source offsets."""
    output = []
    fence = None
    inline = None
    html_end = None
    frontmatter = source.startswith("---\n") or source.startswith("---\r\n")
    for number, line in enumerate(source.splitlines(keepends=True)):
        if frontmatter:
            output.append(line)
            if number and line.strip() in {"---", "..."}:
                frontmatter = False
            continue
        if fence is None and inline is None:
            if html_end is None:
                opening = re.search(r"<(pre|code|script|style)(?:\s[^>]*|)>", line, re.IGNORECASE)
                if opening:
                    html_end = "</" + opening.group(1).casefold() + ">"
                elif "<!--" in line:
                    html_end = "-->"
            if html_end is not None:
                output.append(line)
                if html_end in line.casefold():
                    html_end = None
                continue
        marker = FENCE.match(line)
        if marker and inline is None:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence) and not line[marker.end():].strip():
                fence = None
            output.append(line)
            continue
        if fence is not None:
            output.append(line)
            continue
        # Quoted/indented examples and link/HTML lines remain literal. This
        # deliberately under-projects complicated Markdown rather than damaging
        # destinations, labels, escapes or examples with a global substitution.
        literal = (line.startswith(("    ", "\t")) or line.lstrip().startswith(">")
                   or any(ch in line for ch in "[]<>"))
        if literal:
            output.append(line)
            continue
        chars = list(line)
        cursor = 0
        while cursor < len(line):
            if line[cursor] == "`" and not _escaped(line, cursor):
                end = cursor + 1
                while end < len(line) and line[end] == "`":
                    end += 1
                size = end - cursor
                if inline is None:
                    inline = size
                elif inline == size:
                    inline = None
                cursor = end
                continue
            if inline is not None or literal or not _pair(line, cursor):
                cursor += 1
                continue
            closing = cursor + 2
            while closing < len(line) and not (_pair(line, closing) and not _escaped(line, closing)):
                closing += 1
            if closing >= len(line):
                break
            body = line[cursor + 2:closing]
            if (not _escaped(line, cursor) and body and not body[0].isspace() and not body[-1].isspace()
                    and "`" not in body and "\\" not in body and "\n" not in body and "\r" not in body):
                chars[cursor:closing + 2] = [" " if not ch.isspace() else ch for ch in line[cursor:closing + 2]]
            cursor = closing + 2
        output.append("".join(chars))
    return "".join(output)


def view(source, query):
    return source if ISO_DATE.search(query) else current(source)


def read_source(path):
    """Read original decoded character coordinates, including CRLF pairs."""
    with path.open(encoding="utf-8", errors="replace", newline="") as stream:
        return stream.read()


def render_span(source, start, end, max_chars=360):
    """Render the exposed prefix and map its end to original source characters."""
    matches = list(re.finditer(r"\S+", source[start:end]))
    text = " ".join(match.group() for match in matches)
    prefix = text if len(text) <= max_chars else text[:max_chars - 1].rstrip()
    shown = prefix if len(text) <= max_chars else prefix + "…"
    consumed, visible_end = 0, start
    for index, match in enumerate(matches):
        if index:
            consumed += 1  # Collapsed whitespace separator.
        if consumed >= len(prefix):
            break
        take = min(len(match.group()), len(prefix) - consumed)
        visible_end = start + match.start() + take
        consumed += take
    return shown, visible_end


def excerpt(source, terms, *, historical=False, iso_date=None, max_chars=360):
    """Expose matching body evidence, not a frontmatter/title-only page head."""
    text = source if historical else current(source)
    lines = text.splitlines() or [""]
    folded = [term.casefold() for term in terms if term]
    body_start = 0
    if lines and lines[0].strip() == "---":
        body_start = next((i + 1 for i, line in enumerate(lines[1:], 1) if line.strip() in {"---", "..."}), len(lines))
    body = [i for i in range(body_start, len(lines)) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    selected = next((i for i in body if any(term in lines[i].casefold() for term in folded)),
                    body[0] if body else min(body_start, len(lines) - 1))
    section_end = len(lines)
    if historical and iso_date:
        headings = [(i, len(match.group(1)), match.group(2))
                    for i, line in enumerate(lines)
                    if (match := re.match(r"^(#{1,6})\s+(.+)$", line))]
        date_sections = []
        title = next((match.group(1) for line in lines
                      if (match := re.match(r"^#\s+(.+?)\s*#*\s*$", line))), "")
        title_terms = {term.casefold() for term in re.findall(r"[\w]+", title)}
        date_terms = {term.casefold() for term in re.findall(r"[\w]+", iso_date)}
        query_terms = set(folded) - title_terms - date_terms
        for index, (line_no, level, title) in enumerate(headings):
            if iso_date not in title:
                continue
            end_line = next((following for following, next_level, _ in headings[index + 1:]
                             if next_level <= level), len(lines))
            section_text = " ".join(lines[line_no:end_line]).casefold()
            score = sum(term in section_text for term in query_terms)
            date_sections.append((score, level, line_no, end_line))
        if date_sections:
            _, _, heading_line, section_end = max(date_sections, key=lambda item: (item[0], item[1], -item[2]))
            section_body = [i for i in range(heading_line, section_end)
                            if i >= body_start and lines[i].strip()
                            and not lines[i].lstrip().startswith("#")]
            selected = next((i for i in section_body
                             if any(term in lines[i].casefold() for term in query_terms)),
                            section_body[0] if section_body else heading_line)
    original_lines = text.splitlines(keepends=True)
    start = sum(map(len, original_lines[:selected]))
    end = sum(map(len, original_lines[:min(selected + 3, section_end)]))
    value, _ = render_span(text, start, end, max_chars)
    return value, selected + 1, min(len(lines), selected + 3, section_end)
