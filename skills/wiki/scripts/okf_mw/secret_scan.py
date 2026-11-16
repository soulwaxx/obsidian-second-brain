#!/usr/bin/env python3
"""
Write-path secret check for Obsidian wiki vaults.

Refuses (never redacts) text that is about to be written under <vault>/wiki/
when it matches a conservative set of well-known credential formats. This is a
separate entry point from validate.py: validation also runs for read-only
reports and re-validation of pages nobody is writing, whereas this check must
only run on text an agent is adding.

It is pattern matching, not a guarantee: unknown formats are missed, and it is
not a substitute for keeping secrets out of prompts and notes. Matched values
are never included in findings or messages.

Reusable API (used by the file-tool guard and by scripts/wiki_batch.py, so every
reviewed write path shares one implementation):
    scan_text(text) -> list[Finding]
    has_opt_out(text) -> bool
    refusal_for_content(label, text, *, page, fragment=False) -> str | None
    advisory_for_content(label, text) -> str | None
    refusal_for_tool_write(vault, path, tool_input) -> str | None

Usage (file-tool guard; JSON tool input on stdin):
    secret_scan.py tool-input --vault <vault> --path <absolute-path>

Exit codes:
    0: allowed (not under wiki/, opted out, or no finding)
    1: refused; the reason is on stderr
    2: usage or input error
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

OPT_OUT_KEY = "secretScan"
OPT_OUT_VALUE = "allow"
MAX_REPORTED = 10
# Keys in file-tool inputs that carry text being added (Claude and Pi).
_ADDED_KEYS = ("content", "new_string", "newText", "new_source")
_EDIT_LIST_KEYS = ("edits",)


class Finding(NamedTuple):
    line: int
    kind: str


def _token(kind: str, pattern: str, flags: int = 0):
    return kind, re.compile(pattern, flags)


# Well-known token formats. Prefix-anchored so ordinary prose and kebab-case
# slugs do not match. Placeholder-looking bodies are skipped in scan_text().
_TOKEN_PATTERNS = (
    _token("private key block", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    _token("Anthropic API key", r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}"),
    _token("OpenAI-style API key",
           r"(?<![A-Za-z0-9_-])sk-(?!ant-)(?:proj-|svcacct-|admin-)?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}"),
    _token("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    _token("GitHub fine-grained token", r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    _token("Slack token", r"\bxox[abeprs]-(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{10,}"),
    _token("AWS access key ID", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _token("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    _token("GitLab token", r"\bglpat-[0-9A-Za-z_-]{20,}"),
    _token("npm token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    _token("Stripe live key", r"\b[sr]k_live_[0-9A-Za-z]{24,}"),
    _token("JSON Web Token",
           r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?:api[_-]?key|secret(?:[_-]?key)?|passw(?:or)?d|token)
    ["']?\s*[:=]\s*(?!=)["']?
    (?P<value>[A-Za-z0-9_\-+/.@:%!#^*~=]{8,})
    """
)
_URL_CREDENTIALS = re.compile(
    r"\b[a-z][a-z0-9+.-]{1,15}://[^\s/:@<>\"'\[\]]+:(?P<value>[^\s/@<>\"'\[\]]{6,})@[^\s/<>\"']+",
    re.IGNORECASE,
)
_PLACEHOLDER_WORDS = (
    "your", "example", "placeholder", "changeme", "change-me", "redacted",
    "dummy", "sample", "xxxx", "****", "...", "<", "{{", "${",
)
_DOTTED_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _placeholder(value: str) -> bool:
    lowered = value.casefold()
    return (any(word in lowered for word in _PLACEHOLDER_WORDS)
            or value.startswith(("$", "%", "{"))
            or len(set(value)) < 5)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_text(text: str) -> list[Finding]:
    """Return (line, kind) findings in order of appearance, without the matched values."""
    findings: list[tuple[int, int, str]] = []
    spans: list[tuple[int, int]] = []
    for kind, pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            if kind != "private key block" and _placeholder(match.group(0)):
                continue
            findings.append((match.start(), _line_of(text, match.start()), kind))
            spans.append(match.span())
    for match in _URL_CREDENTIALS.finditer(text):
        password = match.group("value")
        if _placeholder(password) or password.casefold() in {"password", "passwd", "secret"}:
            continue
        findings.append((match.start(), _line_of(text, match.start()), "credentials in URL"))
        spans.append(match.span())
    for match in _ASSIGNMENT.finditer(text):
        value = match.group("value")
        if any(start <= match.start("value") < end for start, end in spans):
            continue
        if (_placeholder(value) or not any(c.isdigit() for c in value) or not any(c.isalpha() for c in value)
                or _DOTTED_IDENTIFIER.fullmatch(value) or text[match.end():match.end() + 1] == "("):
            continue
        findings.append((match.start(), _line_of(text, match.start()), "credential assignment"))
    findings.sort()
    return [Finding(line, kind) for _, line, kind in findings]


def has_opt_out(text: str) -> bool:
    """True when the leading YAML frontmatter contains `secretScan: allow`."""
    if not text.startswith("---"):
        return False
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return False
    for line in lines[1:]:
        if line.rstrip() in {"---", "..."}:
            return False
        if re.fullmatch(rf"{OPT_OUT_KEY}:\s*[\"']?{OPT_OUT_VALUE}[\"']?\s*(?:#.*)?", line.rstrip()):
            return True
    return False


def _summary(findings: list[Finding]) -> str:
    seen: list[str] = []
    for finding in findings:
        item = f"line {finding.line} ({finding.kind})"
        if item not in seen:
            seen.append(item)
    shown = "; ".join(seen[:MAX_REPORTED])
    return shown + (f"; and {len(seen) - MAX_REPORTED} more" if len(seen) > MAX_REPORTED else "")


def refusal_for_content(label: str, text: str, *, page: bool, fragment: bool = False) -> str | None:
    """Return a refusal message, or None when the text may be written.

    `label` names the destination. For a `page`, `secretScan: allow` in the
    text's own frontmatter opts out. A `fragment` is part of a larger write (an
    edit), so it cannot carry the opt-out and its line numbers are relative.
    """
    if page and not fragment and has_opt_out(text):
        return None
    findings = scan_text(text)
    if not findings:
        return None
    where = "the added text, " if fragment else ""
    hint = " Remove the value (reference an environment variable or secret manager instead)."
    if page:
        hint += (f" A page that documents token formats may set `{OPT_OUT_KEY}: {OPT_OUT_VALUE}`"
                 " in its frontmatter.")
    return f"secret-scan: refusing write to {label}: likely secret(s) in {where}{_summary(findings)}.{hint} The matched values are not shown."


def advisory_for_content(label: str, text: str) -> str | None:
    """Warning-only message for verbatim source captures."""
    findings = scan_text(text)
    if not findings:
        return None
    return (f"secret-scan advisory: {label} contains likely secret(s) at {_summary(findings)}; "
            "the capture is verbatim and is not blocked. Matched values are not shown. "
            "Review before Obsidian Git pushes the vault.")


def added_text(tool_input: dict) -> list[str]:
    """Collect text a Claude or Pi file tool would add (Write, Edit, MultiEdit, NotebookEdit)."""
    pieces = [tool_input[key] for key in _ADDED_KEYS if isinstance(tool_input.get(key), str)]
    for key in _EDIT_LIST_KEYS:
        edits = tool_input.get(key)
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict):
                    pieces.extend(edit[k] for k in _ADDED_KEYS if isinstance(edit.get(k), str))
    return pieces


def _wiki_relative(vault: Path, target: Path) -> Path | None:
    wiki = vault.resolve() / "wiki"
    resolved = target.resolve()
    # Match the guard: a case-different spelling of wiki/ is still the wiki.
    wiki_key, target_key = str(wiki).casefold() + "/", str(resolved).casefold()
    if not target_key.startswith(wiki_key):
        return None
    return Path(*resolved.parts[len(wiki.parts):])


def refusal_for_tool_write(vault: str, path: str, tool_input: dict) -> str | None:
    """Refusal for a file-tool write; None when the target is outside wiki/ or the text is clean."""
    target = Path(path)
    relative = _wiki_relative(Path(vault), target)
    if relative is None:
        return None
    label = "wiki/" + relative.as_posix()
    pieces = added_text(tool_input)
    if not pieces:
        return None
    is_page = target.suffix.casefold() == ".md"
    full_write = isinstance(tool_input.get("content"), str)
    opted_out = False
    if is_page:
        if full_write:
            opted_out = has_opt_out(tool_input["content"])
        else:
            # Edits keep the page's existing frontmatter, which carries any opt-out.
            try:
                opted_out = target.is_file() and has_opt_out(target.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                opted_out = False
    if opted_out:
        return None
    for piece in pieces:
        message = refusal_for_content(label, piece, page=is_page, fragment=not full_write)
        if message:
            return message
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest="command", required=True)
    tool = sub.add_parser("tool-input")
    tool.add_argument("--vault", required=True)
    tool.add_argument("--path", required=True)
    args = parser.parse_args()
    try:
        tool_input = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"secret-scan: tool input is not valid JSON: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    if not isinstance(tool_input, dict):
        print("secret-scan: tool input must be a JSON object", file=sys.stderr)
        return 2
    message = refusal_for_tool_write(args.vault, args.path, tool_input)
    if message:
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
