#!/usr/bin/env python3
"""Prepare a reviewed Markdown-link migration bundle without writing to a vault.

The returned bundle is only a preview. Publication remains owned by
``wiki_batch.inspect/apply/recover``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any

from module_loading import load_source

COMPAT = load_source("wiki_migrate_compat", str(Path(__file__).with_name("obsidian_compat.py")))
BATCH = load_source("wiki_migrate_batch", str(Path(__file__).with_name("wiki_batch.py")))
RESERVED = {"index.md", "log.md", "_plan.md"}


class MigrationError(ValueError):
    """A compatibility proposal cannot safely be prepared."""


def _eligible_source(source: Any) -> str | None:
    if not isinstance(source, str):
        return None
    path = PurePosixPath(source)
    if (path.is_absolute() or "\\" in source or path.as_posix() != source
            or len(path.parts) < 2 or path.parts[0] != "wiki"
            or path.suffix.casefold() != ".md"
            or any(part.casefold() in RESERVED for part in path.parts)):
        return None
    return source


def prepare(vault: str | Path, config: str | Path, pages: list[str] | None = None) -> dict[str, Any]:
    """Return compatibility findings, a v1 bundle, and its reviewed batch plan.

    No vault or config files are written. ``pages`` optionally restricts the
    authored-page subset by canonical vault-relative paths.
    """
    root = Path(vault).expanduser().resolve(strict=True)
    config_path = Path(config).expanduser()
    selected = set(pages) if pages is not None else None
    if selected is not None:
        for page in selected:
            if _eligible_source(page) != page:
                raise MigrationError(f"invalid authored wiki page selection: {page}")

    report = COMPAT.compatibility_report(root)
    historical = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in report.get("links", []):
        kind = finding.get("kind")
        if kind in {"generated-navigation", "lifecycle-history"} or finding.get("repair_state") == "historical-not-active-repair":
            historical.append(finding)
            continue
        source = _eligible_source(finding.get("source"))
        if source is None or finding.get("kind") != "authored" or not finding.get("proposal"):
            continue
        if selected is not None and source not in selected:
            continue
        grouped.setdefault(source, []).append(finding)

    operations = []
    for source, findings in grouped.items():
        path = root / source
        try:
            original = path.read_bytes()
            text = original.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise MigrationError(f"cannot read authored Markdown as UTF-8: {source}: {exc}") from exc
        replacements = []
        for finding in findings:
            proposal = finding["proposal"]
            span = proposal.get("span") if isinstance(proposal, dict) else None
            replacement = proposal.get("replacement") if isinstance(proposal, dict) else None
            if (not isinstance(span, list) or len(span) != 2
                    or any(type(index) is not int for index in span)
                    or not isinstance(replacement, str)):
                raise MigrationError(f"unsupported proposal span for {source}")
            start, end = span
            destination = finding.get("destination")
            if (not isinstance(destination, str) or start < 0 or end < start or end > len(text)
                    or text[start:end] != destination):
                raise MigrationError(f"stale or out-of-bounds proposal span for {source}")
            replacements.append((start, end, replacement))
        replacements.sort(key=lambda item: (item[0], item[1]))
        for previous, current in zip(replacements, replacements[1:]):
            if current[0] < previous[1]:
                raise MigrationError(f"overlapping proposal spans for {source}")
        updated = text
        for start, end, replacement in reversed(replacements):
            updated = updated[:start] + replacement + updated[end:]
        if updated != text:
            operations.append({"path": source, "expectedHash": hashlib.sha256(original).hexdigest(), "content": updated})

    bundle = {"version": 1, "operations": operations}
    result: dict[str, Any] = {
        "version": 1,
        "vault": str(root),
        "compatibility": report,
        "historical_or_generated_findings": historical,
        "bundle": bundle,
        "plan": None,
        "planHash": None,
        "no_op": not operations,
    }
    if operations:
        inspected = BATCH.inspect(root, bundle, config_path, "wiki-page")
        result["plan"] = inspected["plan"]
        result["planHash"] = inspected["planHash"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--json", action="store_true", help="write JSON preview to stdout")
    parser.add_argument("--page", action="append", dest="pages", help="restrict to this vault-relative authored page (repeatable)")
    args = parser.parse_args()
    try:
        preview = prepare(args.vault, args.config, args.pages)
    except (MigrationError, BATCH.BatchError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
