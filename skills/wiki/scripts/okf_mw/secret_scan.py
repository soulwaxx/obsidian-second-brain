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
    new_findings(text, baseline) -> list[Finding]
    has_opt_out(text) -> bool
    refusal_for_content(label, text, *, page, fragment=False, baseline=None) -> str | None
    advisory_for_content(label, text) -> str | None
    refusal_for_tool_write(vault, path, tool_input) -> str | None

Only findings that are new relative to the text already on disk are refused
(same pattern type and matched value are compared by hash, not by line), so
editing or re-baselining a legacy page that already contains a match is never
blocked, while changing a value or adding a new one is.

Usage (file-tool guard; JSON tool input on stdin):
    secret_scan.py tool-input --vault <vault> --path <absolute-path>

Exit codes:
    0: allowed (not under wiki/, opted out, or no finding)
    1: refused; the reason is on stderr
    2: usage or input error
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

OPT_OUT_KEY = "secretScan"
OPT_OUT_VALUE = "allow"
MAX_REPORTED = 10


class Finding(NamedTuple):
    line: int
    kind: str
    digest: str  # hash of the matched value; identifies a finding across edits, never shown


def _token(kind: str, pattern: str, flags: int = 0, *, need_digit: bool = False):
    return kind, re.compile(pattern, flags), need_digit


# Well-known token formats. Prefix-anchored so ordinary prose and kebab-case
# slugs do not match. Placeholder-looking bodies are skipped in scan_text().
# Patterns run on whole pages inside a 10 s hook timeout that fails open, so they
# must stay linear: a body class that contains the prefix's own characters needs a
# left-anchoring lookbehind (a bare \b lets every prefix inside a run rescan the
# run to its end) and no lookahead over the body; digit requirements are checked
# after the match (need_digit). tests/test_secret_scan.py times pathological input.
_TOKEN_PATTERNS = (
    _token("private key block", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    _token("Anthropic API key", r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}"),
    _token("OpenAI-style API key",
           r"(?<![A-Za-z0-9_-])sk-(?!ant-)(?:proj-|svcacct-|admin-)?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}"),
    _token("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    _token("GitHub fine-grained token", r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    _token("Slack token", r"(?<![A-Za-z0-9-])xox[abeprs]-[A-Za-z0-9-]{10,}", need_digit=True),
    _token("AWS access key ID", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _token("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    _token("GitLab token", r"\bglpat-[0-9A-Za-z_-]{20,}"),
    _token("npm token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    _token("Stripe live key", r"\b[sr]k_live_[0-9A-Za-z]{24,}"),
    _token("JSON Web Token",
           r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def scan_text(text: str) -> list[Finding]:
    """Return findings in order of appearance. Matched values are only kept as hashes."""
    findings: list[tuple[int, int, str, str]] = []
    spans: list[tuple[int, int]] = []
    for kind, pattern, need_digit in _TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if kind != "private key block" and _placeholder(value):
                continue
            if need_digit and not any(c.isdigit() for c in value):
                continue
            if kind == "private key block":
                # Identify the whole block: every key shares the same header line.
                end = text.find("-----END", match.end())
                value = text[match.start():end if end >= 0 else len(text)]
            findings.append((match.start(), _line_of(text, match.start()), kind, _digest(value)))
            spans.append(match.span())
    for match in _URL_CREDENTIALS.finditer(text):
        password = match.group("value")
        if _placeholder(password) or password.casefold() in {"password", "passwd", "secret"}:
            continue
        findings.append((match.start(), _line_of(text, match.start()), "credentials in URL", _digest(password)))
        spans.append(match.span())
    for match in _ASSIGNMENT.finditer(text):
        value = match.group("value")
        if any(start <= match.start("value") < end for start, end in spans):
            continue
        if (_placeholder(value) or not any(c.isdigit() for c in value) or not any(c.isalpha() for c in value)
                or _DOTTED_IDENTIFIER.fullmatch(value) or text[match.end():match.end() + 1] == "("):
            continue
        findings.append((match.start(), _line_of(text, match.start()), "credential assignment", _digest(value)))
    findings.sort()
    return [Finding(line, kind, digest) for _, line, kind, digest in findings]


def new_findings(text: str, baseline: str | None) -> list[Finding]:
    """Findings in `text` whose pattern type and value are absent from `baseline`."""
    findings = scan_text(text)
    if not findings or not baseline:
        return findings
    known = {(finding.kind, finding.digest) for finding in scan_text(baseline)}
    return [finding for finding in findings if (finding.kind, finding.digest) not in known]


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


def _refusal(label: str, findings: list[Finding], *, page: bool, fragment: bool) -> str:
    where = "the added text, " if fragment else ""
    hint = " Remove the value (reference an environment variable or secret manager instead)."
    if page:
        hint += (f" A page that documents token formats may set `{OPT_OUT_KEY}: {OPT_OUT_VALUE}`"
                 " in its frontmatter.")
    return f"secret-scan: refusing write to {label}: likely secret(s) in {where}{_summary(findings)}.{hint} The matched values are not shown."


def refusal_for_content(label: str, text: str, *, page: bool, fragment: bool = False,
                        baseline: str | None = None) -> str | None:
    """Return a refusal message, or None when the text may be written.

    `label` names the destination. For a `page`, `secretScan: allow` in the
    text's own frontmatter opts out. A `fragment` is part of a larger write (an
    edit), so it cannot carry the opt-out and its line numbers are relative.
    Findings already present in `baseline` (the destination's current text) are
    not new and are never refused.
    """
    if page and not fragment and has_opt_out(text):
        return None
    findings = new_findings(text, baseline)
    return _refusal(label, findings, page=page, fragment=fragment) if findings else None


def advisory_for_content(label: str, text: str) -> str | None:
    """Warning-only message for verbatim source captures."""
    findings = scan_text(text)
    if not findings:
        return None
    return (f"secret-scan advisory: {label} contains likely secret(s) at {_summary(findings)}; "
            "the capture is verbatim and is not blocked. Matched values are not shown. "
            "Review before Obsidian Git pushes the vault.")


_OLD_KEYS = ("old_string", "oldText")
_NEW_KEYS = ("new_string", "newText")


def _first_str(item: dict, keys: tuple[str, ...]) -> str | None:
    return next((item[key] for key in keys if isinstance(item.get(key), str)), None)


def _edit_pairs(tool_input: dict) -> list[tuple[str | None, str, bool]]:
    """(old, new, replace_all) for Claude Edit/MultiEdit and Pi edit inputs, in order."""
    items = [tool_input]
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        items.extend(edit for edit in edits if isinstance(edit, dict))
    pairs = []
    for item in items:
        new = _first_str(item, _NEW_KEYS)
        if new is not None:
            pairs.append((_first_str(item, _OLD_KEYS), new, item.get("replace_all") is True))
    return pairs


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    except OSError:
        return None


def _wiki_relative(vault: Path, target: Path) -> Path | None:
    wiki = vault.resolve() / "wiki"
    resolved = target.resolve()
    # Match the guard: a case-different spelling of wiki/ is still the wiki.
    wiki_key, target_key = str(wiki).casefold() + "/", str(resolved).casefold()
    if not target_key.startswith(wiki_key):
        return None
    return Path(*resolved.parts[len(wiki.parts):])


def refusal_for_tool_write(vault: str, path: str, tool_input: dict) -> str | None:
    """Refusal for a file-tool write; None when the target is outside wiki/ or nothing new is secret.

    Edits are applied to the text currently on disk so a value-only change (a
    placeholder replaced by a real value) is judged in context. Only findings
    absent from the current file are refused.
    """
    target = Path(path)
    relative = _wiki_relative(Path(vault), target)
    if relative is None:
        return None
    label = "wiki/" + relative.as_posix()
    is_page = target.suffix.casefold() == ".md"
    current = _read_text(target)
    full = tool_input.get("content") if isinstance(tool_input.get("content"), str) else None
    # A Write carries its own frontmatter. Edits keep the page's existing one, so
    # the opt-out is read from disk only: adding it takes a separate earlier edit
    # (accepted limitation of the per-page opt-out).
    if is_page and has_opt_out(full if full is not None else current or ""):
        return None
    checks: list[tuple[str, bool]] = []  # (text to scan, is a fragment)
    if full is not None:
        checks.append((full, False))
    else:
        text, applied = current or "", False
        for old, new, replace_all in _edit_pairs(tool_input):
            if old and old in text:
                text, applied = text.replace(old, new, -1 if replace_all else 1), True
            elif not old and not text:
                text, applied = new, True
            else:
                checks.append((new, True))  # cannot be placed in the current text
        if applied:
            checks.insert(0, (text, False))
    if isinstance(tool_input.get("new_source"), str):
        checks.append((tool_input["new_source"], True))  # NotebookEdit cell text
    for text, fragment in checks:
        findings = new_findings(text, current)
        if findings:
            return _refusal(label, findings, page=is_page, fragment=fragment)
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
