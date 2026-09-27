#!/usr/bin/env python3
"""
OKF v0.2 frontmatter validation for wiki markdown files.

Validates that a .md file conforms to the Open Knowledge Format (OKF)
frontmatter specification, version 0.2. Only `type` is required. Legacy v0.1
shapes (last_updated, updated, source_url, scalar/string-list sources) are
accepted so an existing vault keeps validating under dual-read.

Usage:
    validate.py <path-to-.md-file>

Exit codes:
    0: valid OKF frontmatter
    1: invalid (errors printed to stderr as JSON array)
    2: not a markdown file
"""

import json
import os
import re
import sys
from pathlib import Path

from okf_paths import RESERVED_WIKI_FILES, in_set

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def _is_root_level_index(file_path: Path) -> bool:
    """Determine if an index.md is at vault root level.

    The canonical root index is <vault>/wiki/index.md — the one sync.py writes
    'okf_version' into (sync.py: is_root = dirpath == ''). Its parent is the
    wiki/ directory itself, which contains neither marker below, so matching on
    the parent's name is what keeps this in agreement with the generator.
    Vault-root markers are kept as a fallback for a <vault>/index.md layout.
    """
    parent = file_path.resolve().parent
    if parent.name.casefold() == "wiki":
        return True
    if (parent / "wiki").is_dir():
        return True
    if (parent / ".obsidian").is_dir():
        return True
    return False


def _has_frontmatter_delimiters(content: str) -> bool:
    """Check if content starts with YAML frontmatter delimiters."""
    stripped = content.lstrip("\ufeff")  # strip BOM if present
    return re.match(r"\A---[ \t]*\r?\n", stripped) is not None


def _parse_frontmatter_yaml(content: str) -> tuple[dict | None, str | None]:
    """Parse frontmatter YAML. Returns (parsed_dict, error_message)."""
    stripped = content.lstrip("\ufeff")
    opening = re.match(r"\A---[ \t]*\r?\n", stripped)
    if opening is None:
        return None, "no YAML frontmatter delimiters found"

    rest = stripped[opening.end():]
    closing = re.search(r"(?m)^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", rest)
    if closing is None:
        return None, "unclosed YAML frontmatter (no closing '---' found)"

    yaml_str = rest[:closing.start()]

    if yaml is None:
        return None, "yaml library not available (install PyYAML)"

    try:
        data = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        line_info = ""
        # problem_mark and problem are dynamic attributes on YAMLError
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            line_info = f" at line {mark.line + 1}"
        else:
            prob = getattr(e, "problem", None)
            if prob is not None:
                line_info = f": {prob}"
        return None, f"YAML parse error{line_info}"

    return data, None


def _validate_frontmatter(
    data: object, filename: str, file_path: Path
) -> list[dict[str, str]]:
    """Validate parsed frontmatter against OKF v0.2 rules.

    Returns a list of error dicts with 'field' and 'message' keys.
    """
    errors: list[dict[str, str]] = []

    # Must be a YAML mapping
    if not isinstance(data, dict):
        errors.append(
            {
                "field": "frontmatter",
                "message": "frontmatter must be a YAML mapping (dict), "
                f"got {type(data).__name__}",
            }
        )
        return errors

    # Special handling for index.md
    if in_set(filename, {"index.md"}):
        is_root = _is_root_level_index(file_path)
        if is_root:
            # Root-level index.md may only have okf_version
            allowed_keys = {"okf_version"}
            actual_keys = set(data.keys())
            extra_keys = actual_keys - allowed_keys
            if extra_keys:
                errors.append(
                    {
                        "field": "frontmatter",
                        "message": f"root-level index.md may only contain "
                        f"'okf_version', got: {', '.join(sorted(extra_keys))}",
                    }
                )
            if "okf_version" in data and data["okf_version"] not in ("0.1", "0.2"):
                errors.append(
                    {
                        "field": "okf_version",
                        "message": "okf_version must be '0.2' (or legacy '0.1') "
                        "if present",
                    }
                )
        else:
            # Subdirectory index.md must have NO frontmatter
            if data:
                errors.append(
                    {
                        "field": "frontmatter",
                        "message": "subdirectory index.md must have no frontmatter",
                    }
                )
        return errors

    # Remaining reserved file (log.md) — written by the lifecycle hook, so it
    # must NOT carry concept frontmatter.
    if in_set(filename, RESERVED_WIKI_FILES):
        allowed_keys = {"okf_version"}
        actual_keys = set(data.keys())
        extra_keys = actual_keys - allowed_keys
        if extra_keys:
            errors.append(
                {
                    "field": "frontmatter",
                    "message": f"{filename} must not have concept frontmatter "
                    f"(allowed: 'okf_version'), got: "
                    f"{', '.join(sorted(extra_keys))}",
                }
            )
        if "okf_version" in data and data["okf_version"] not in ("0.1", "0.2"):
            errors.append(
                {
                    "field": "okf_version",
                    "message": "okf_version must be '0.2' (or legacy '0.1') "
                    "if present",
                }
            )
        return errors

    # All other .md files: type is REQUIRED
    if "type" not in data:
        errors.append(
            {"field": "type", "message": "required field 'type' is missing"}
        )
    elif not isinstance(data["type"], str) or not data["type"].strip():
        errors.append(
            {
                "field": "type",
                "message": "'type' must be a non-empty string",
            }
        )

    # Staleness signal. OKF v0.2 makes `type` the only required field and
    # records last content change as `generated.at`; this vault keeps
    # `last_updated` as a recommended producer extension and still authors it,
    # but validation no longer *requires* it — an imported conformant OKF page
    # that lacks it must still validate (dual-read). Legacy `updated` is
    # accepted, not nagged.
    _validate_staleness(errors, data)

    # OKF v0.2 optional families — validated leniently. Per OKF §11, consumers
    # MUST NOT reject documents for unrecognized fields, so these only flag a
    # clearly wrong shape, never absence.
    _validate_optional_field(errors, data, "title", str)
    _validate_optional_field(errors, data, "description", str)
    _validate_optional_field(errors, data, "source_url", str)  # legacy v0.1
    _validate_optional_field(errors, data, "resource", str)
    _validate_optional_field(errors, data, "status", str)
    _validate_actor_stamp(errors, data, "generated")
    _validate_verified(errors, data)
    _validate_sources(errors, data)
    _validate_date_field(errors, data, "stale_after")
    _validate_tags(errors, data)
    _validate_timestamp(errors, data)

    return errors


def _validate_staleness(errors: list[dict[str, str]], data: dict) -> None:
    """Format-check the staleness fields when present. None is required."""
    if "last_updated" in data:
        _validate_date_like(errors, data, "last_updated")
    if "updated" in data:  # legacy v0.1 spelling, accepted under dual-read
        _validate_date_like(errors, data, "updated")


def _validate_date_field(
    errors: list[dict[str, str]], data: dict, field: str
) -> None:
    """Format-check an optional date-like field when present."""
    if field in data:
        _validate_date_like(errors, data, field)


def _validate_actor_stamp(
    errors: list[dict[str, str]], data: dict, field: str
) -> None:
    """Validate an OKF actor stamp: a mapping with optional `by`/`at`.

    Shape is `{ by: <actor>, at: <ISO 8601> }` (OKF §5.2/§7). Both keys are
    optional; only obviously-wrong types are flagged.
    """
    if field not in data:
        return
    val = data[field]
    if not isinstance(val, dict):
        errors.append(
            {
                "field": field,
                "message": f"'{field}' must be a mapping like "
                f"{{ by, at }}, got {type(val).__name__}",
            }
        )
        return
    if "by" in val and not isinstance(val["by"], str):
        errors.append(
            {"field": f"{field}.by", "message": f"'{field}.by' must be a string"}
        )
    if "at" in val:
        _validate_date_like(errors, val, "at")


def _validate_verified(errors: list[dict[str, str]], data: dict) -> None:
    """Validate `verified`: a single actor stamp or a list of them (OKF §5.3)."""
    if "verified" not in data:
        return
    val = data["verified"]
    if isinstance(val, list):
        for i, entry in enumerate(val):
            _validate_actor_stamp(errors, {f"verified[{i}]": entry}, f"verified[{i}]")
        return
    _validate_actor_stamp(errors, data, "verified")


def _validate_sources(errors: list[dict[str, str]], data: dict) -> None:
    """Validate the `sources` field.

    OKF v0.2 shape is a list of mappings, each with at least `id` or
    `resource` (§5.1). Legacy v0.1 scalar or string-list values are accepted
    unchanged under dual-read.
    """
    if "sources" not in data:
        return
    val = data["sources"]
    if isinstance(val, str):  # legacy scalar
        return
    if not isinstance(val, list):
        errors.append(
            {
                "field": "sources",
                "message": "'sources' must be a list (of mappings, or legacy "
                f"strings), got {type(val).__name__}",
            }
        )
        return
    for i, entry in enumerate(val):
        if isinstance(entry, str):  # legacy string-list
            continue
        if not isinstance(entry, dict):
            errors.append(
                {
                    "field": f"sources[{i}]",
                    "message": "each source must be a mapping (or legacy "
                    f"string), got {type(entry).__name__}",
                }
            )
            continue
        if "id" not in entry and "resource" not in entry:
            errors.append(
                {
                    "field": f"sources[{i}]",
                    "message": "each source mapping needs at least 'id' or "
                    "'resource'",
                }
            )


def _validate_date_like(
    errors: list[dict[str, str]], data: dict, field: str
) -> None:
    """Accept an ISO 8601 string or a YAML-parsed date/datetime."""
    import datetime

    val = data[field]
    if isinstance(val, (datetime.datetime, datetime.date)):
        return
    if not isinstance(val, str) or not _ISO8601_RE.match(val):
        errors.append(
            {
                "field": field,
                "message": f"'{field}' must be an ISO 8601 date "
                f"(YYYY-MM-DD), got: {val!r}",
            }
        )


def _validate_optional_field(
    errors: list[dict[str, str]],
    data: dict,
    field: str,
    expected_type: type,
) -> None:
    """Validate an optional field if present in data."""
    if field not in data:
        return
    val = data[field]
    if not isinstance(val, expected_type):
        errors.append(
            {
                "field": field,
                "message": f"'{field}' must be a {expected_type.__name__}, "
                f"got {type(val).__name__}",
            }
        )


def _validate_tags(errors: list[dict[str, str]], data: dict) -> None:
    """Validate the 'tags' field if present."""
    if "tags" not in data:
        return
    tags = data["tags"]
    if not isinstance(tags, list):
        errors.append(
            {
                "field": "tags",
                "message": f"'tags' must be a list, got {type(tags).__name__}",
            }
        )
        return
    for i, tag in enumerate(tags):
        if not isinstance(tag, str):
            errors.append(
                {
                    "field": f"tags[{i}]",
                    "message": f"each tag must be a string, "
                    f"got {type(tag).__name__}",
                }
            )


_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(T\d{2}:\d{2}:\d{2}"
    r"(\.\d+)?"
    r"(Z|[+-]\d{2}:\d{2})?)?$"
)


def _validate_timestamp(errors: list[dict[str, str]], data: dict) -> None:
    """Validate the 'timestamp' field if present."""
    if "timestamp" not in data:
        return
    ts = data["timestamp"]
    # YAML automatically parses ISO 8601 strings into datetime objects;
    # accept these as valid (they're valid ISO 8601 by construction)
    import datetime
    if isinstance(ts, (datetime.datetime, datetime.date)):
        return
    if not isinstance(ts, str):
        errors.append(
            {
                "field": "timestamp",
                "message": f"'timestamp' must be a string or ISO 8601 datetime, "
                f"got {type(ts).__name__}",
            }
        )
        return
    if not _ISO8601_RE.match(ts):
        errors.append(
            {
                "field": "timestamp",
                "message": f"'timestamp' is not a valid ISO 8601 string: "
                f"'{ts[:80]}{'...' if len(ts) > 80 else ''}'",
            }
        )


def _detect_frontmatter_no_yaml(content: str) -> list[dict[str, str]]:
    """Basic frontmatter detection when yaml library is unavailable.

    This is a degraded mode — we can only detect the presence of delimiters
    but cannot parse or validate YAML structure.
    """
    errors: list[dict[str, str]] = []
    stripped = content.lstrip("\ufeff")
    if not stripped.startswith("---"):
        errors.append(
            {
                "field": "frontmatter",
                "message": "no frontmatter delimiters found",
            }
        )
    else:
        errors.append(
            {
                "field": "yaml",
                "message": "yaml library not available — "
                "cannot validate frontmatter content (install PyYAML)",
            }
        )
    return errors


def validate_file(file_path: str) -> dict:
    """Validate a markdown file against OKF v0.2 rules.

    Returns a dict with 'valid' (bool) and 'errors' (list).
    """
    path = Path(file_path)

    # Check extension
    if path.suffix.lower() != ".md":
        return {
            "valid": False,
            "errors": [
                {
                    "field": "file",
                    "message": f"not a markdown file (suffix: "
                    f"'{path.suffix}')",
                }
            ],
        }

    filename = path.name

    # Read file content
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            "valid": False,
            "errors": [
                {"field": "file", "message": f"file not found: {file_path}"}
            ],
        }
    except UnicodeDecodeError as e:
        return {
            "valid": False,
            "errors": [
                {
                    "field": "file",
                    "message": f"cannot read file as UTF-8: {e}",
                }
            ],
        }

    content_stripped = content.lstrip("\ufeff")

    # Reserved files (index.md, log.md) may or may not need frontmatter
    if in_set(filename, RESERVED_WIKI_FILES):
        if not _has_frontmatter_delimiters(content):
            return {"valid": True, "errors": []}

        # Has frontmatter delimiters — parse and validate
        if yaml is None:
            errors = _detect_frontmatter_no_yaml(content)
            return {"valid": False, "errors": errors}

        data, parse_err = _parse_frontmatter_yaml(content)
        if parse_err:
            return {
                "valid": False,
                "errors": [{"field": "yaml", "message": parse_err}],
            }

        errors = _validate_frontmatter(data, filename, path)
        return {"valid": len(errors) == 0, "errors": errors}

    # All other .md files MUST have frontmatter
    if not _has_frontmatter_delimiters(content):
        return {
            "valid": False,
            "errors": [
                {
                    "field": "frontmatter",
                    "message": "file must have YAML frontmatter delimited by '---'",
                }
            ],
        }

    # Parse frontmatter
    if yaml is None:
        errors = _detect_frontmatter_no_yaml(content)
        return {"valid": False, "errors": errors}

    data, parse_err = _parse_frontmatter_yaml(content)
    if parse_err:
        return {
            "valid": False,
            "errors": [{"field": "yaml", "message": parse_err}],
        }

    errors = _validate_frontmatter(data, filename, path)
    return {"valid": len(errors) == 0, "errors": errors}


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path-to-.md-file>", file=sys.stderr)
        return 2

    file_path = sys.argv[1]

    if not file_path.lower().endswith(".md"):
        result = {
            "valid": False,
            "errors": [
                {
                    "field": "file",
                    "message": f"not a markdown file (.md extension required)",
                }
            ],
        }
        print(json.dumps(result), file=sys.stderr)
        return 2

    result = validate_file(file_path)
    print(json.dumps(result), file=sys.stderr)

    if result["valid"]:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
