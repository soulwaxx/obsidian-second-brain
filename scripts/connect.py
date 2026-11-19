#!/usr/bin/env python3
"""Reviewed `connect` command: wire an agent to the wiki, preview first, undo on request.

`connect` lists detected agents and writes nothing. `connect <agent>...` previews
the exact file changes with a plan hash; `--apply --confirm <planHash>` applies them,
and `--remove` previews/applies the undo the same way. Per agent it merges one MCP
entry into the agent's user-level config, upserts a marked rules block, copies the
six portable skills, and records everything (paths, hashes, package root,
interpreter, node, version) in a manifest next to the selected integration config.

The framework owns reading, merging, hashing, backups, atomic writes, rollback and
the manifest; each agent is a small declarative module in `connect_adapters/`
(see docs/agents.md). Connect writes only user-level files outside the vault, never
runs Git, an agent CLI or the network, and never touches a project repository.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from config_contract import load_config  # noqa: E402
from module_loading import load_source  # noqa: E402
import connect_adapters  # noqa: E402
from connect_adapters.spec import Adapter, ExtraEntry, Location  # noqa: E402

PACKAGE_ROOT = SCRIPTS.parent
LAUNCHER = "scripts/obsidian-second-brain.mjs"
MANIFEST_NAME = "connect-manifest.json"
BACKUP_DIR = "connect-backups"
FORCED_DIR = "forced"  # below an agent's backup directory: --force copies of customized skills, never pruned
NODE_ENV = "OSB_NODE"  # set by the JavaScript launcher to its own node executable
MANIFEST_SCHEMA = 1
RULES_START = "<!-- obsidian-second-brain:start -->"
RULES_END = "<!-- obsidian-second-brain:end -->"
TOML_START = "# obsidian-second-brain:start"
TOML_END = "# obsidian-second-brain:end"
SKILL_NAMES = ("wiki", "wiki-query", "wiki-save", "wiki-ingest", "wiki-research", "wiki-health")
MIN_PYTHON = (3, 14)
GENERIC_PROFILES = ("read", "all")


class ConnectError(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


class Refusal(Exception):
    """A file or selection that cannot be changed safely; carries the manual snippet."""

    def __init__(self, code: str, message: str, path: Path | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.snippet: str | None = None
        self.agent: str | None = None


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def entry_hash(entry: Any) -> str:
    return sha256(json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str))


def package_version(root: Path = PACKAGE_ROOT) -> str | None:
    try:
        version = json.loads((root / "package.json").read_text(encoding="utf-8")).get("version")
    except (OSError, UnicodeError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def config_file_path() -> Path:
    """The selected integration config, selected exactly as bootstrap-vault.py does."""
    return load_source("obsidian_connect_bootstrap", str(SCRIPTS / "bootstrap-vault.py")).config_path()


# -- file access -----------------------------------------------------------------------

def read_file(path: Path) -> bytes | None:
    """Regular-file bytes, None when absent; symlinks and non-files are refused."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Refusal("path-unreadable", f"cannot read {path}: {exc}", path) from exc
    if stat.S_ISLNK(info.st_mode):
        raise Refusal("symlink-target", f"{path} is a symlink; connect never writes through symlinks", path)
    if not stat.S_ISREG(info.st_mode):
        raise Refusal("not-a-file", f"{path} is not a regular file", path)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise Refusal("path-unreadable", f"cannot read {path}: {exc}", path) from exc


def read_text(planner: "Planner", path: Path) -> str | None:
    raw = planner.current(path)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refusal("not-utf8", f"{path} is not UTF-8 text", path) from exc


def literal_location(location: Location, home: Path) -> Path:
    """The adapter's location as written (absolute, not symlink-resolved)."""
    path = location(home, dict(os.environ)) if callable(location) else home / location
    path = Path(os.path.abspath(path))
    if not path.is_absolute():
        raise Refusal("relative-target", f"adapter location {location!r} is not absolute")
    return path


def resolve_path(path: Path, directory: bool = False) -> Path:
    """Follow symlinked parent directories (a directory target is followed entirely).

    Connect writes the real file, with the temporary file and rename in its real directory.
    A symlinked final file is still refused by `read_file`.
    """
    return Path(os.path.realpath(path)) if directory else Path(os.path.realpath(path.parent)) / path.name


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Temp file in the destination directory, fsync, then rename over the target."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.osb-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def ensure_parent(path: Path, created: list[Path], mode: int | None = None) -> None:
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
        if mode is not None:
            os.chmod(directory, mode)
        created.append(directory)


# -- config formats (JSON strict, TOML table-level, YAML without comments) ------------------

def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def parse_config(fmt: str, text: str | None, path: Path | None = None) -> dict[str, Any]:
    if text is None or not text.strip():
        return {}
    try:
        if fmt == "json":
            doc = json.loads(text, object_pairs_hook=_no_duplicates)
        elif fmt == "toml":
            doc = tomllib.loads(text)
        elif fmt == "yaml":
            if re.search(r"(?m)(^|\s)#", text):
                raise ValueError("YAML comments would be lost by a rewrite")
            import yaml

            class StrictLoader(yaml.SafeLoader):
                def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
                    seen: set[Any] = set()
                    for key_node, _ in node.value:
                        if key_node.tag == "tag:yaml.org,2002:merge":
                            continue  # refused later by ensure_rewrite_safe
                        key = self.construct_object(key_node, deep=True)
                        if key in seen:
                            raise ValueError(f"duplicate key {key!r}")
                        seen.add(key)
                    return super().construct_mapping(node, deep)

            doc = yaml.load(text, Loader=StrictLoader)  # noqa: S506 - a SafeLoader subclass
            if doc is None:
                doc = {}
        else:
            raise Refusal("unsupported-format", f"unsupported config format {fmt!r}", path)
    except Refusal:
        raise
    except Exception as exc:
        hint = " (JSONC comments and trailing commas cannot be merged safely)" if fmt == "json" else ""
        raise Refusal("config-unparseable", f"cannot merge into {path or 'config'}: {exc}{hint}", path) from exc
    if not isinstance(doc, dict):
        raise Refusal("config-unparseable", f"{path or 'config'} root must be an object", path)
    return doc


_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard constant {name}")


def _number(kind: type) -> Any:
    def parse(literal: str) -> Any:
        value = kind(literal)
        if (repr(value) if kind is float else str(value)) != literal:
            raise ValueError(f"number {literal} would be rewritten as {value!r}")
        return value
    return parse


def ensure_rewrite_safe(fmt: str, text: str | None, path: Path | None = None) -> None:
    """Refuse a JSON/YAML rewrite that would change the textual form of an existing value.

    Layout (indent, line breaks, flow style, quoting of keys) is normalized by a rewrite and documented;
    values must stay as written: numbers such as `1e5` or `1.50`, JSON string escapes, YAML anchors, aliases
    and merge keys, and plain scalars that resolve to a non-string and would be respelled (`yes`, `017`, `~`,
    `1_000`, an empty value). TOML is never rewritten.
    """
    if not text:
        return
    try:
        if fmt == "json":
            json.loads(text, parse_float=_number(float), parse_int=_number(int), parse_constant=_reject_constant)
            for literal in _JSON_STRING.findall(text):
                if json.dumps(json.loads(literal), ensure_ascii=False) != literal:
                    raise ValueError(f"string {literal} would be rewritten")
        elif fmt == "yaml":
            import yaml
            resolver = yaml.SafeLoader("")
            for event in yaml.parse(text, Loader=yaml.SafeLoader):
                if isinstance(event, yaml.AliasEvent):
                    raise ValueError("YAML aliases would be expanded")
                if getattr(event, "anchor", None):
                    raise ValueError("YAML anchors would be dropped")
                if isinstance(event, yaml.ScalarEvent) and event.tag is None and event.style is None:
                    tag = resolver.resolve(yaml.ScalarNode, event.value, event.implicit)
                    if tag == "tag:yaml.org,2002:merge":
                        raise ValueError("YAML merge keys would be expanded")
                    if tag != "tag:yaml.org,2002:str":
                        value = resolver.construct_object(yaml.ScalarNode(tag, event.value))
                        dumped = yaml.safe_dump(value, allow_unicode=True).removesuffix("\n...\n").removesuffix("\n")
                        if dumped != event.value:
                            raise ValueError(f"YAML value {event.value!r} would be rewritten as {dumped}")
    except ValueError as exc:
        raise Refusal("config-rewrite-unsafe", f"cannot merge into {path or 'config'}: rewriting it would change existing values ({exc}); add the entry by hand", path) from exc


def lookup(doc: dict[str, Any], key_path: tuple[str, ...], name: str, path: Path | None = None) -> Any:
    node: Any = doc
    for key in key_path:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return None
    if not isinstance(node, dict):
        raise Refusal("config-shape", f"{path or 'config'}: {'.'.join(key_path)} is not an object", path)
    return node.get(name)


def set_entry(doc: dict[str, Any], key_path: tuple[str, ...], name: str, entry: Any, path: Path | None = None) -> None:
    node = doc
    for key in key_path:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise Refusal("config-shape", f"{path or 'config'}: {key!r} is not an object", path)
    node[name] = entry


def drop_entry(doc: dict[str, Any], key_path: tuple[str, ...], name: str, levels: int) -> None:
    """Delete the entry, then up to `levels` of its containers (innermost first) while they are empty."""
    chain = [doc]
    for key in key_path:
        chain.append(chain[-1][key])
    del chain[-1][name]
    for index in range(len(key_path), len(key_path) - levels, -1):
        if chain[index]:
            break
        del chain[index - 1][key_path[index - 1]]


def missing_levels(doc: dict[str, Any], key_path: tuple[str, ...]) -> int:
    """How many trailing levels of `key_path` are absent from `doc` (the containers a new entry would create)."""
    node: Any = doc
    for index, key in enumerate(key_path):
        if not isinstance(node, dict) or key not in node:
            return len(key_path) - index
        node = node[key]
    return 0


def _indent(text: str | None) -> int | str:
    match = re.search(r"(?m)^([ \t]+)\S", text or "")
    if not match:
        return 2
    return "\t" if match.group(1).startswith("\t") else len(match.group(1))


def dump_config(fmt: str, doc: dict[str, Any], text: str | None) -> str:
    if fmt == "json":
        out = json.dumps(doc, indent=_indent(text), ensure_ascii=False)
        return out + "\n" if text is None or text.endswith("\n") else out
    import yaml
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise Refusal("unsupported-value", f"cannot write {type(value).__name__} values to TOML")


def _toml_table(parts: list[str], table: dict[str, Any]) -> list[str]:
    lines = ["[" + ".".join(_toml_key(part) for part in parts) + "]"]
    nested = []
    for key, value in table.items():
        if isinstance(value, dict):
            nested.append((key, value))
        else:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in nested:
        lines += ["", *_toml_table([*parts, key], value)]
    return lines


def toml_block(key_path: tuple[str, ...], name: str, entry: dict[str, Any]) -> str:
    return "\n".join([TOML_START, *_toml_table([*key_path, name], entry), TOML_END]) + "\n"


_TOML_MARKER = re.compile(rf"{re.escape(TOML_START)}|{re.escape(TOML_END)}")


def _toml_header(line: str) -> tuple[tuple[str, ...], bool] | None:
    """The key path and array-ness of a `[table]` / `[[array]]` header line, else None."""
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    try:
        node: Any = tomllib.loads(stripped + "\n")
    except tomllib.TOMLDecodeError:
        return None
    parts: list[str] = []
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        parts.append(key)
    return tuple(parts), stripped.startswith("[[")


def _toml_regions(text: str, table: tuple[str, ...]) -> list[tuple[int, int, bool]]:
    """Character ranges of our entry: its table header and own subtables, plus our marker comment lines.

    Another tool (Codex rewrites config.toml with toml_edit) may move, reorder or detach the markers, so the
    entry is located by its `[table]` header. A group runs to the next header that is not ours (or EOF), minus
    trailing blank and comment lines, which belong to what follows. Only exact marker lines are taken. Adjacent
    ranges are merged; the flag says whether a range holds the table itself.
    """
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    marked = [i for i, line in enumerate(lines) if _TOML_MARKER.fullmatch(line.rstrip("\r\n").rstrip(" \t"))]
    spans: list[tuple[int, int, bool]] = []  # inclusive line numbers
    first: int | None = None
    for i, line in enumerate(lines + [""]):
        header = _toml_header(line) if i < len(lines) else None
        ours = header is not None and not header[1] and header[0][:len(table)] == table
        if first is not None and (i == len(lines) or (header is not None and not ours)):
            last = i - 1
            while last > first and (not lines[last].strip() or lines[last].lstrip().startswith("#")):
                last -= 1
            spans.append((first, last, True))
            first = None
        if ours and first is None:
            first = i
    spans += [(i, i, False) for i in marked]
    spans.sort()
    merged: list[list[Any]] = []
    for lo, hi, table_flag in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
            merged[-1][2] = merged[-1][2] or table_flag
        else:
            merged.append([lo, hi, table_flag])
    return [(offsets[lo], offsets[hi + 1], flag) for lo, hi, flag in merged]


def cut_block(text: str, start: int, end: int) -> str:
    """Remove a marked block together with the one blank line that separated it, undoing an append."""
    head, tail = text[:start], text[end:]
    if head.endswith("\n\n"):
        head = head[:-1]
    if tail.startswith("\n"):
        tail = tail[1:]
    return head + tail


def toml_cut(text: str, table: tuple[str, ...], insert: str | None = None) -> str | None:
    """`text` without our entry (markers included), or with `insert` where the entry's table was. None: not found.

    Removing drops one blank line next to the cut (the one an append adds), so an untouched append is undone exactly.
    """
    regions = _toml_regions(text, table)
    if not any(flag for _, _, flag in regions):
        return None
    place = next(index for index, (_, _, flag) in enumerate(regions) if flag)
    for index in range(len(regions) - 1, -1, -1):
        start, end, _ = regions[index]
        head, tail = text[:start], text[end:]
        if insert and index == place:
            text = head + insert + tail
        elif head.endswith("\n\n"):
            text = head[:-1] + tail
        elif not head and tail.startswith("\n"):
            text = tail[1:]
        else:
            text = head + tail
    return text


def _prune(doc: dict[str, Any], key_path: tuple[str, ...]) -> dict[str, Any]:
    doc = copy.deepcopy(doc)
    chain = [doc]
    for key in key_path:
        node = chain[-1].get(key)
        if not isinstance(node, dict):
            return doc
        chain.append(node)
    for index in range(len(key_path), 0, -1):
        if chain[index]:
            break
        del chain[index - 1][key_path[index - 1]]
    return doc


def merge_config(fmt: str, text: str | None, key_path: tuple[str, ...], name: str, entry: dict[str, Any] | None,
                 owned: str | None, created: bool, path: Path | None = None, created_keys: int = 0) -> str | None:
    """Return the new file text (None = delete the file) with only our entry added, replaced or removed.

    `entry` None removes it. Our entry may be replaced or removed only when it still
    matches the hash recorded in the manifest (`owned`); any other differing entry is refused. Removal also drops the
    containers connect created (`created_keys` innermost levels, all of them for a file connect created) once empty.
    """
    doc = parse_config(fmt, text, path)
    current = lookup(doc, key_path, name, path)
    if entry is None:
        if current is None:
            return text
        if owned is None or entry_hash(current) != owned:
            raise Refusal("entry-modified", f"{path or 'config'}: entry {name!r} was changed after connect; remove it by hand", path)
        if fmt == "toml":
            new_text = toml_cut(text or "", (*key_path, name))
            expected = copy.deepcopy(doc)
            drop_entry(expected, key_path, name, False)
            try:
                remaining = None if new_text is None else tomllib.loads(new_text)
            except tomllib.TOMLDecodeError:
                remaining = None
            if remaining is None or _prune(remaining, key_path) != _prune(expected, key_path):
                raise Refusal("toml-merge-unsafe", f"{path or 'config'}: the entry is not a removable [{'.'.join((*key_path, name))}] table (dotted keys, an inline table or a table that another section interrupts), or removing it would change other settings; remove it by hand", path)
            return None if created and not new_text.strip() else new_text
        drop_entry(doc, key_path, name, len(key_path) if created else created_keys)
        if created and not doc:
            return None
        ensure_rewrite_safe(fmt, text, path)
        return dump_config(fmt, doc, text)
    if current == entry:
        return text
    if current is not None and owned is None:
        raise Refusal("entry-differs", f"{path or 'config'}: an entry named {name!r} already exists and differs; connect only manages its own entry", path)
    if current is not None and entry_hash(current) != owned:
        raise Refusal("entry-differs", f"{path or 'config'}: the entry {name!r} that connect wrote was changed afterwards (by hand or by the agent, for example a saved tool approval); connect will not overwrite it, so update or remove it by hand", path)
    if fmt == "toml":
        block = toml_block(key_path, name, entry)
        body = text or ""
        if current is None:
            new_text = body + ("" if not body or body.endswith("\n") else "\n") + ("\n" if body.strip() else "") + block
        else:
            new_text = toml_cut(body, (*key_path, name), block)
            if new_text is None:
                raise Refusal("toml-merge-unsafe", f"{path or 'config'}: the entry is not a replaceable [{'.'.join((*key_path, name))}] table; edit it by hand", path)
        expected = copy.deepcopy(doc)
        set_entry(expected, key_path, name, copy.deepcopy(entry), path)
        try:
            merged = tomllib.loads(new_text)
        except tomllib.TOMLDecodeError as exc:
            raise Refusal("toml-merge-unsafe", f"{path or 'config'}: cannot append the table safely ({exc})", path) from exc
        if merged != expected:
            raise Refusal("toml-merge-unsafe", f"{path or 'config'}: appending the table would change other settings", path)
        return new_text
    set_entry(doc, key_path, name, copy.deepcopy(entry), path)
    ensure_rewrite_safe(fmt, text, path)
    return dump_config(fmt, doc, text)


def render_snippet(fmt: str, key_path: tuple[str, ...], name: str, entry: dict[str, Any], extras: tuple[ExtraEntry, ...] = ()) -> str:
    if fmt == "toml":
        return toml_block(key_path, name, entry)
    doc: dict[str, Any] = {}
    set_entry(doc, key_path, name, entry)
    for extra in extras:
        set_entry(doc, tuple(extra.key_path), extra.name, copy.deepcopy(extra.value))
    return dump_config(fmt, doc, None)


# -- extra entries (values connect sets next to the MCP entry, for example an approval rule) ------------------

_ABSENT = object()


def _extra_state(doc: dict[str, Any], key_path: tuple[str, ...], name: str) -> tuple[Any, Any]:
    """(value at key_path/name or _ABSENT, the container when it is not an object else _ABSENT)."""
    node: Any = doc
    for key in key_path:
        if not isinstance(node, dict) or key not in node:
            return _ABSENT, _ABSENT
        node = node[key]
    return (node.get(name, _ABSENT), _ABSENT) if isinstance(node, dict) else (_ABSENT, node)


def merge_extras(fmt: str, text: str | None, extras: tuple[ExtraEntry, ...], olds: dict[tuple[tuple[str, ...], str], dict[str, Any]],
                 path: Path) -> tuple[str | None, list[dict[str, Any]]]:
    """The text with each extra set (new keys are appended at the end of their object) and the manifest records of those connect owns.

    Absent: set. Already the wanted value: kept, owned only if connect wrote it. A value connect wrote that is unchanged:
    replaced. Another value in `accept` (or a container string in `accept`): the user's own, left alone and not owned.
    Anything else is refused: for OpenCode that is an `allow` that would let `wiki_batch_apply` run unprompted.
    """
    doc = parse_config(fmt, text, path)
    before = copy.deepcopy(doc)
    records: list[dict[str, Any]] = []
    created_levels: dict[tuple[str, ...], int] = {}  # containers created by an earlier extra of this pass count for every extra in them
    for extra in extras:
        key_path = tuple(extra.key_path)
        old = olds.get((key_path, extra.name))
        owned = old["valueSha256"] if old else None
        current, scalar = _extra_state(doc, key_path, extra.name)
        if current is _ABSENT and isinstance(scalar, str) and scalar in extra.accept:
            continue
        if current is _ABSENT:
            if key_path not in created_levels:
                created_levels[key_path] = missing_levels(doc, key_path)
            set_entry(doc, key_path, extra.name, copy.deepcopy(extra.value), path)
            levels = created_levels[key_path]
        elif current == extra.value:
            if owned != entry_hash(current):
                continue
            levels = old.get("createdKeys", 0)
        elif owned is not None and entry_hash(current) == owned:
            set_entry(doc, key_path, extra.name, copy.deepcopy(extra.value), path)
            levels = old.get("createdKeys", 0)
        elif any(current == value for value in extra.accept):
            continue
        else:
            raise Refusal("extra-differs", f"{path}: {'.'.join((*key_path, extra.name))} is {json.dumps(current)}, but connect needs "
                          f"{json.dumps(extra.value)} (or a stricter value) there so the wiki write tools stay user-approved; connect will not override it, so change it by hand", path)
        records.append({"keyPath": list(key_path), "name": extra.name, "valueSha256": entry_hash(extra.value),
                        **({"createdKeys": levels} if levels else {})})
    # Extras that share a container share its created levels: whichever is removed last must take the empty containers along.
    deepest: dict[tuple[str, ...], int] = {}
    for record in records:
        key_path = tuple(record["keyPath"])
        deepest[key_path] = max(deepest.get(key_path, 0), record.get("createdKeys", 0))
    records = [{**record, **({"createdKeys": deepest[tuple(record["keyPath"])]} if deepest[tuple(record["keyPath"])] else {})} for record in records]
    if doc == before:
        return text, records
    ensure_rewrite_safe(fmt, text, path)
    return dump_config(fmt, doc, text), records


def remove_extras(fmt: str, text: str | None, olds: dict[tuple[tuple[str, ...], str], dict[str, Any]],
                  path: Path) -> tuple[str | None, list[str]]:
    """The text without the extras connect wrote (and the containers it created, once empty), and the ones left because the value changed."""
    doc = parse_config(fmt, text, path)
    kept: list[str] = []
    changed = False
    for (key_path, name), old in olds.items():
        current, _ = _extra_state(doc, key_path, name)
        if current is _ABSENT:
            continue
        if entry_hash(current) != old["valueSha256"]:
            kept.append(".".join((*key_path, name)))
            continue
        drop_entry(doc, key_path, name, old.get("createdKeys", 0))
        changed = True
    if not changed:
        return text, kept
    ensure_rewrite_safe(fmt, text, path)
    return dump_config(fmt, doc, text), kept


# -- rules block -----------------------------------------------------------------------------

def rules_body(vault: Path | None, config_file: Path) -> str:
    where = (f"Configured Obsidian wiki: {json.dumps(str(vault / 'wiki'), ensure_ascii=False)} (OKF v0.2 knowledge base). "
             if vault else "The vault is selected by the integration config; call `wiki_doctor` to see which wiki applies. ")
    return "\n".join([
        "## Obsidian wiki (obsidian-second-brain)",
        "",
        where + f"Selected integration config: {json.dumps(str(config_file), ensure_ascii=False)}.",
        "",
        "- Use the wiki MCP tools (`wiki_search`, `wiki_outline`, `wiki_read`, `wiki_doctor`) to read the wiki; load `wiki/WIKI.md` and the relevant `wiki/index.md` only when a task needs it. Search results are leads: confirm with `wiki_outline` and `wiki_read`.",
        "- Use the wiki MCP tools; never edit vault files directly with file or shell tools, and never write `index.md` or `log.md` (they are generated).",
        "- To change the wiki, call `wiki_batch_inspect`, show the user its review summary and `planHash`, wait for explicit approval, and only then call `wiki_batch_apply` with the identical operations. If an apply fails or is cancelled, call `wiki_batch_recover` with the same `planHash` before retrying.",
        "- Never put credentials or tokens in page content.",
    ])


def rules_block(body: str) -> str:
    return f"{RULES_START}\n{body}\n{RULES_END}"


def _rules_span(text: str, path: Path) -> tuple[int, int] | None:
    starts = [m.start() for m in re.finditer(re.escape(RULES_START), text)]
    ends = [m.end() for m in re.finditer(re.escape(RULES_END), text)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise Refusal("rules-markers-malformed", f"{path}: the obsidian-second-brain start/end markers are unpaired or repeated; fix them by hand", path)
    return starts[0], ends[0]


# -- skill sources -----------------------------------------------------------------------------

def skill_sources(root: Path = PACKAGE_ROOT) -> dict[str, bytes]:
    """The copy set from #64: six SKILL.md files plus wiki/references/*.md (never wiki/scripts/)."""
    files = {f"{name}/SKILL.md": (root / "skills" / name / "SKILL.md").read_bytes() for name in SKILL_NAMES}
    for reference in sorted((root / "skills/wiki/references").glob("*.md")):
        files[f"wiki/references/{reference.name}"] = reference.read_bytes()
    return files


# -- manifest -----------------------------------------------------------------------------------

def _absolute(value: Any) -> bool:
    return isinstance(value, str) and os.path.isabs(value)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _relative(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not os.path.isabs(value) and ".." not in Path(value).parts


def _created_keys_valid(item: dict[str, Any]) -> bool:
    """`createdKeys` counts containers of `keyPath` that connect created: an int (never a bool) from 0 to len(keyPath)."""
    value = item.get("createdKeys", 0)
    return type(value) is int and 0 <= value <= len(item["keyPath"])


def _extra_valid(item: Any) -> bool:
    return (isinstance(item, dict) and _strings(item.get("keyPath")) and bool(item.get("keyPath")) and isinstance(item.get("name"), str)
            and isinstance(item.get("valueSha256"), str) and _created_keys_valid(item))


def _mcp_valid(item: Any) -> bool:
    return (isinstance(item, dict) and _absolute(item.get("path")) and item.get("format") in ("json", "toml", "yaml")
            and _strings(item.get("keyPath")) and bool(item.get("keyPath")) and isinstance(item.get("name"), str)
            and isinstance(item.get("entrySha256"), str) and isinstance(item.get("createdFile"), bool)
            and isinstance(item.get("adopted", False), bool) and _created_keys_valid(item)
            and isinstance(item.get("extras", []), list) and all(_extra_valid(extra) for extra in item.get("extras", [])))


def _rules_valid(item: Any) -> bool:
    return (isinstance(item, dict) and _absolute(item.get("path")) and isinstance(item.get("blockSha256"), str)
            and isinstance(item.get("createdFile"), bool) and isinstance(item.get("header", ""), str))


def _skills_valid(item: Any) -> bool:
    return (isinstance(item, dict) and _absolute(item.get("root")) and isinstance(item.get("files"), dict)
            and all(_relative(rel) and isinstance(digest, str) for rel, digest in item["files"].items())
            and _strings(item.get("adopted", [])) and all(rel in item["files"] for rel in item.get("adopted", []))
            and isinstance(item.get("skillsElsewhere", {}), dict)
            and all(isinstance(name, str) and _absolute(directory) for name, directory in item.get("skillsElsewhere", {}).items()))


RECORD_FIELDS = {
    "version": lambda value: value is None or isinstance(value, str),
    "packageRoot": _absolute, "interpreter": _absolute, "node": _absolute,
    "profile": lambda value: isinstance(value, str),
    "mcp": lambda value: value is None or _mcp_valid(value),
    "rules": lambda value: value is None or _rules_valid(value),
    "skills": lambda value: value is None or _skills_valid(value),
}


def _dirs_valid(value: Any) -> bool:
    return _strings(value) and all(os.path.isabs(item) for item in value)


def manifest_problem(manifest: Any) -> str | None:
    """The first structural problem of a manifest, or None; every reader may then trust the record shapes."""
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != MANIFEST_SCHEMA or not isinstance(manifest.get("agents"), dict):
        return "unsupported install manifest"
    for name, record in manifest["agents"].items():
        if not isinstance(record, dict):
            return f"agent {name!r} is not an object"
        for field_name, valid in RECORD_FIELDS.items():
            if field_name not in record or not valid(record[field_name]):
                return f"agent {name!r} has a missing or malformed {field_name!r}"
        if not _dirs_valid(record.get("createdDirs", [])):  # optional: manifests from before directory cleanup lack it
            return f"agent {name!r} has a malformed 'createdDirs'"
    return None


def load_manifest(path: Path) -> tuple[dict[str, Any], str | None]:
    """The manifest and None, or an empty manifest and an error message (code `manifest-invalid`)."""
    empty = {"schemaVersion": MANIFEST_SCHEMA, "agents": {}}
    try:
        raw = read_file(path)
    except Refusal as exc:
        return empty, str(exc)
    if raw is None:
        return empty, None
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        return empty, f"cannot load {path}: {exc}"
    problem = manifest_problem(manifest)
    if problem:
        return empty, f"invalid {path}: {problem}; fix or delete the file by hand, connect changes nothing until then"
    return manifest, None


def manifest_path_for(config_file: Path) -> Path:
    return Path(os.path.realpath(config_file.parent)) / MANIFEST_NAME


# -- adapters -------------------------------------------------------------------------------------

def load_adapters(strict: bool = True) -> tuple[list[Adapter], list[str]]:
    adapters: list[Adapter] = []
    errors: list[str] = []
    for module_name in connect_adapters.REGISTRY:
        try:
            adapter = importlib.import_module(module_name).ADAPTER
            if not isinstance(adapter, Adapter) or not re.fullmatch(r"[a-z][a-z0-9-]*", adapter.name):
                raise ValueError("ADAPTER must be an Adapter with a lowercase name")
            if any(existing.name == adapter.name for existing in adapters):
                raise ValueError(f"duplicate adapter name {adapter.name!r}")
            if adapter.mcp and adapter.mcp.extra and adapter.mcp.format not in ("json", "yaml"):
                raise ValueError("extra MCP entries need a json or yaml config file")
        except Exception as exc:
            if strict:
                raise ConnectError("adapter-invalid", f"adapter {module_name}: {exc}") from exc
            errors.append(f"{module_name}: {exc}")
            continue
        adapters.append(adapter)
    return adapters, errors


def mcp_command(package_root: Path, node: str = "node", profile: str = "all") -> list[str]:
    return [node, str(package_root / LAUNCHER), "mcp", "--tools", profile]


def mcp_env(interpreter: str, config_file: Path) -> dict[str, str]:
    env = {"PYTHON": interpreter}
    if os.environ.get("OBSIDIAN_AGENT_CONFIG"):
        env["OBSIDIAN_AGENT_CONFIG"] = str(config_file)
    return env


PROBE_CODE = """import sys, importlib.util as u
try:
    import yaml
    has_yaml = 1
except Exception:
    has_yaml = 0
print(int(sys.version_info >= %r), has_yaml, int(u.find_spec('markdown_it') is not None))
""" % (MIN_PYTHON,)


def probe_flags(path: Path, timeout: int) -> tuple[bool, bool, bool]:
    """(Python version is new enough, yaml imports, markdown_it is installed) for an interpreter; read-only."""
    if path == Path(os.path.abspath(sys.executable)):
        try:
            import yaml  # noqa: F401
            has_yaml = True
        except ImportError:
            has_yaml = False
        return sys.version_info >= MIN_PYTHON, has_yaml, importlib.util.find_spec("markdown_it") is not None
    result = subprocess.run([str(path), "-B", "-c", PROBE_CODE], capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    python_ok, yaml_ok, markdown_ok = (bool(int(value)) for value in result.stdout.split())
    return python_ok, yaml_ok, markdown_ok


def probe_interpreter(selected: str | None) -> tuple[str, list[str]]:
    """Absolute interpreter path (symlinks kept, so venvs stay pinned) and advisory notes; refuses unusable ones."""
    path = Path(os.path.abspath(os.path.expanduser(selected or sys.executable)))
    if not path.is_file() or not os.access(path, os.X_OK):
        raise Refusal("interpreter-invalid", f"interpreter {path} is not an executable file")
    try:
        python_ok, yaml_ok, markdown_ok = probe_flags(path, 30)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Refusal("interpreter-invalid", f"interpreter {path} could not be probed: {exc}") from exc
    if not python_ok:
        raise Refusal("interpreter-unsuitable", f"interpreter {path} is older than Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}; select one with --python")
    if not yaml_ok:
        raise Refusal("interpreter-unsuitable", f"interpreter {path} cannot import yaml; install requirements.txt there or select another with --python")
    notes = [] if markdown_ok else [f"interpreter {path} lacks markdown-it-py; wiki_outline and wiki_read need requirements.txt installed there"]
    return str(path), notes


def resolve_node(selected: str | None) -> str:
    """Absolute node executable to pin in MCP entries: --node, else the launcher's own node, else the one on PATH.

    The launcher's node is replaced by the `node` on PATH (symlinks kept) when both are the same executable.
    """
    launcher_node = os.environ.get(NODE_ENV)
    on_path = shutil.which("node")
    chosen = selected or launcher_node or on_path
    if not selected and launcher_node and on_path and os.path.realpath(on_path) == os.path.realpath(launcher_node):
        # The launcher's node is a version-specific path (a Homebrew Cellar directory); the same executable
        # reached through PATH (`/opt/homebrew/bin/node`) survives an upgrade, so pin that unresolved path.
        chosen = on_path
    if not chosen:
        raise Refusal("node-missing", "no node executable found; select one with --node PATH")
    path = Path(os.path.abspath(os.path.expanduser(chosen)))
    if not path.is_file() or not os.access(path, os.X_OK):
        raise Refusal("node-invalid", f"node {path} is not an executable file")
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("node-invalid", f"node {path} could not be run: {exc}") from exc
    if result.returncode != 0 or not result.stdout.startswith("v"):
        raise Refusal("node-invalid", f"{path} --version did not report a Node.js version; select node with --node PATH")
    return str(path)


# -- planning -----------------------------------------------------------------------------------------

@dataclass
class FileOp:
    path: Path
    role: str
    agents: list[str]
    before: bytes | None
    after: bytes | None
    backup: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def op(self) -> str:
        if self.before == self.after:
            return "unchanged"
        return "create" if self.before is None else "delete" if self.after is None else "update"


@dataclass
class Context:
    home: Path
    config_file: Path
    config_dir: Path
    vault: Path | None
    package_root: Path
    version: str | None
    interpreter: str | None
    node: str | None
    env: dict[str, str]
    manifest: dict[str, Any]
    remove: bool
    force: bool
    sources: dict[str, bytes]
    notes: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    claims: dict[tuple[str, str], tuple[bool, bool, int]] = field(default_factory=dict)  # (path, name) -> (adopted, createdFile, createdKeys) of the MCP entries planned so far
    directories: list[dict[str, Any]] = field(default_factory=list)  # {path, op: create|remove, agents}: directories connect creates, or removes when empty
    elsewhere: dict[str, dict[str, str]] = field(default_factory=dict)  # agent -> {skill name: directory that already provides it}
    snippets: list[dict[str, str]] = field(default_factory=list)  # printed, never written: content a plan decided not to write
    adapters: dict[str, Adapter] = field(default_factory=dict)  # every registered adapter by name
    selected: frozenset[str] = frozenset()  # the agents of this plan

    def place(self, location: Location, directory: bool = False) -> Path:
        """The real path of an adapter location; a symlinked parent shows up in the plan as `requestedPath`."""
        literal = literal_location(location, self.home)
        real = resolve_path(literal, directory)
        if real != literal:
            self.aliases[str(real)] = str(literal)
        return real

    def requested(self, path: Path) -> Path:
        for real, literal in self.aliases.items():
            if path == Path(real) or path.is_relative_to(real):
                return Path(literal) / path.relative_to(real)
        return path


class Planner:
    """Collects one FileOp per path; later agents see earlier agents' pending content."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.ops: dict[Path, FileOp] = {}

    def current(self, path: Path) -> bytes | None:
        return self.ops[path].after if path in self.ops else read_file(path)

    def set(self, path: Path, role: str, agent: str, after: bytes | None, *, backup: bool = False, **detail: Any) -> None:
        vault = self.ctx.vault
        if vault is not None and Path(os.path.realpath(path)).is_relative_to(vault):
            raise Refusal("inside-vault", f"{path} is inside the vault; connect never writes into the vault", path)
        if path in self.ops:
            op = self.ops[path]
            op.after = after
            op.backup = op.backup or backup
            op.detail.update(detail)
            if agent not in op.agents:
                op.agents.append(agent)
        else:
            self.ops[path] = FileOp(path, role, [agent], read_file(path), after, backup, dict(detail))


def _owners(ctx: Context, selected: set[str], section: str) -> dict[str, set[str]]:
    """Paths recorded in the manifest by agents outside this plan (shared files must survive their removal)."""
    owners: dict[str, set[str]] = {}
    for agent, record in ctx.manifest["agents"].items():
        if agent in selected or not isinstance(record.get(section), dict):
            continue
        item = record[section]
        paths = [item["path"]] if "path" in item else [str(Path(item["root"]) / rel) for rel in item["files"]]
        for path in paths:
            owners.setdefault(path, set()).add(agent)
    return owners


def plan_mcp(adapter: Adapter, ctx: Context, planner: Planner, old: dict[str, Any] | None, others: dict[str, set[str]]) -> dict[str, Any] | None:
    target = adapter.mcp
    olds = {(tuple(item["keyPath"]), item["name"]): item for item in (old or {}).get("extras", [])}
    keep_entry = False  # removal of an adopted entry: the entry stays, the extras connect added still go
    if ctx.remove:
        if not old:
            return None
        path, fmt, key_path, name = Path(old["path"]), old["format"], tuple(old["keyPath"]), old["name"]
        entry = None
        if old.get("adopted"):
            ctx.notes.append(f"{adapter.name}: the {name!r} entry in {path} existed before connect (adopted); it stays")
            if not olds:
                return None
            keep_entry = True
    else:
        assert target is not None and ctx.interpreter is not None and ctx.node is not None
        path, fmt, key_path, name = ctx.place(target.path), target.format, tuple(target.key_path), target.name
        entry = target.entry(mcp_command(ctx.package_root, ctx.node), ctx.env)
    key = (str(path), name)
    if old is None and not ctx.remove:
        # An entry that another record or an earlier agent of this plan owns is shared: it shares that owner's
        # adoption, file creation and created containers, so the last agent removed undoes what connect did.
        owners = [(bool(item.get("adopted")), item["createdFile"], item.get("createdKeys", 0)) for agent, other in ctx.manifest["agents"].items()
                  if agent != adapter.name and (item := other["mcp"]) and (item["path"], item["name"]) == key]
        claim = ctx.claims.get(key) or ((all(a for a, _, _ in owners), any(c for _, c, _ in owners), max(k for _, _, k in owners)) if owners else None)
    else:
        claim = None
    created = old["createdFile"] if old else (claim[1] if claim else False) or read_file(path) is None
    created_keys = old.get("createdKeys", 0) if old else (claim[2] if claim else 0)
    extra_records: list[dict[str, Any]] = []
    try:
        if entry is None and str(path) in others:
            planner.ctx.notes.append(f"{adapter.name}: {path} also serves {', '.join(sorted(others[str(path)]))}; its entry stays")
            return None
        text = read_text(planner, path)
        if text is None and entry is not None:
            created = True  # absent now (new, or deleted since an earlier connect): connect creates it, so removal may delete it
        if entry is not None and fmt != "toml" and lookup(parse_config(fmt, text, path), key_path, name, path) is None:
            created_keys = missing_levels(parse_config(fmt, text, path), key_path)
        if entry is None:
            if olds:
                text, kept = remove_extras(fmt, text, olds, path)
                for kept_name in kept:
                    ctx.notes.append(f"{adapter.name}: {kept_name} in {path} was changed after connect; it stays")
            new_text = text if keep_entry else merge_config(fmt, text, key_path, name, None, old["entrySha256"], created, path, created_keys)
        else:
            new_text = entry_text = merge_config(fmt, text, key_path, name, entry, old["entrySha256"] if old else None, created, path)
            if target.extra:
                new_text, extra_records = merge_extras(fmt, new_text, target.extra, olds, path)
    except Refusal as refusal:
        if entry is not None:
            refusal.snippet = render_snippet(fmt, key_path, name, entry, target.extra)
        raise
    # Removal never backs up: the entry is removed only while it still matches the recorded hash.
    planner.set(path, "mcp", adapter.name, None if new_text is None else new_text.encode("utf-8"), backup=entry is not None,
                **({"entry": entry} if entry is not None else {}))
    if entry is None:
        return None
    # Adopted: identical and already there, and nobody else owns it (an owner's adoption is inherited).
    inherited = bool(old.get("adopted")) if old is not None else (claim[0] if claim else True)
    adopted = text is not None and entry_text == text and inherited
    ctx.claims[key] = (adopted, created, created_keys)
    return {"path": str(path), "format": fmt, "keyPath": list(key_path), "name": name,
            "entrySha256": entry_hash(entry), "createdFile": created, **({"adopted": True} if adopted else {}),
            **({"createdKeys": created_keys} if created_keys else {}), **({"extras": extra_records} if extra_records else {})}


def plan_rules(adapter: Adapter, ctx: Context, planner: Planner, old: dict[str, Any] | None, others: dict[str, set[str]]) -> dict[str, Any] | None:
    if ctx.remove:
        if not old:
            return None
        path = Path(old["path"])
        if str(path) in others:
            ctx.notes.append(f"{adapter.name}: {path} also serves {', '.join(sorted(others[str(path)]))}; the rules block stays")
            return None
    else:
        assert adapter.rules is not None
        path = ctx.place(adapter.rules.path)
    # A rules file that another record owns is shared: it inherits that owner's file creation, so the last agent removed deletes it.
    shared_created = not old and any(
        other["rules"]["createdFile"] for agent, other in ctx.manifest["agents"].items()
        if agent != adapter.name and other["rules"] and other["rules"]["path"] == str(path))
    created = old["createdFile"] if old else shared_created or read_file(path) is None
    text = read_text(planner, path)
    if text is None and not ctx.remove and adapter.rules.create_unless is not None:
        unless = ctx.place(adapter.rules.create_unless)
        if os.path.lexists(unless) or (unless in planner.ops and planner.ops[unless].after is not None):
            # The agent reads `unless` only while the rules file is absent: creating the file would hide it.
            ctx.notes.append(f"{adapter.name}: {path} was not created because {unless} exists and {adapter.title} reads it only while {path} is absent, "
                             f"so creating {path} would hide it. The MCP server's instructions already carry the wiki rules; to keep them in a rules file too, "
                             f"paste the printed block into {unless} or create {path} yourself")
            ctx.snippets.append({"agent": adapter.name, "title": f"Wiki rules block, not written (it would hide {unless})",
                                 "destination": f"{unless}, or a {path} you create yourself",
                                 "content": rules_block(rules_body(ctx.vault, ctx.config_file)) + "\n"})
            return None
    if text is None:
        created = True  # absent now (new, or deleted since an earlier connect): connect creates it, so removal may delete it
    span = _rules_span(text, path) if text is not None else None
    recorded = old["blockSha256"] if old else None
    if span is not None:
        current_block = text[span[0]:span[1]]
        if recorded is not None and sha256(current_block) != recorded and (ctx.remove or current_block != rules_block(rules_body(ctx.vault, ctx.config_file))):
            raise Refusal("rules-block-modified", f"{path}: the marked block was edited after connect; restore or delete it by hand", path)
    if ctx.remove:
        if span is None:
            return None
        remaining = cut_block(text, span[0], span[1])
        # The file goes only when it still holds nothing but the header connect wrote with the block.
        only_ours = created and remaining.strip() == old.get("header", "").strip()
        planner.set(path, "rules", adapter.name, None if only_ours else remaining.encode("utf-8"))
        return None
    body = rules_body(ctx.vault, ctx.config_file)
    block = rules_block(body)
    header = (adapter.rules.header or "") if text is None else (old or {}).get("header", "")
    if text is None:
        new_text = (header.rstrip("\n") + "\n\n" if header.strip() else "") + block + "\n"
    elif not text.strip():
        new_text = block + "\n"
    elif span is None:
        new_text = text.rstrip("\n") + "\n\n" + block + "\n"
    else:
        new_text = text[:span[0]] + block + text[span[1]:]
    planner.set(path, "rules", adapter.name, new_text.encode("utf-8"), backup=True, block=block, **({"header": header} if text is None and header else {}))
    return {"path": str(path), "blockSha256": sha256(block), "createdFile": created, "header": header}


def skill_present(planner: Planner, path: Path) -> bool:
    """A SKILL.md at `path`, on disk or pending in this plan; a symlinked one counts (the agent follows it)."""
    try:
        return planner.current(path) is not None
    except Refusal as refusal:
        return refusal.code == "symlink-target"


def plan_skills(adapter: Adapter, ctx: Context, planner: Planner, old: dict[str, Any] | None, others: dict[str, set[str]]) -> dict[str, Any] | None:
    recorded: dict[str, str] = dict(old["files"]) if old else {}
    previously_adopted = set(old.get("adopted", [])) if old else set()
    root = Path(old["root"]) if (ctx.remove and old) else (ctx.place(adapter.skills.path, directory=True) if adapter.skills else None)
    if root is None:
        return None
    known: dict[str, set[str]] = {}
    for record in ctx.manifest["agents"].values():
        item = record.get("skills")
        if item:
            for rel, digest in item["files"].items():
                known.setdefault(str(Path(item["root"]) / rel), set()).add(digest)
    customized: list[str] = []
    foreign: list[str] = []  # differing copies that connect never wrote (no manifest record)
    files: dict[str, str] = {}
    adopted: list[str] = []
    elsewhere: dict[str, str] = {}  # skill name -> a directory this agent also reads that already provides it
    if not ctx.remove and adapter.skills and adapter.skills.also_read:
        also = [directory for directory in (ctx.place(location, directory=True) for location in adapter.skills.also_read) if directory != root]
        for skill in SKILL_NAMES:
            # Our own copy counts only while this run keeps it, so a name that another directory provides is found elsewhere
            # whether or not our copy is still there; the recorded files of that name are retired below.
            directory = next((directory for directory in also if skill_present(planner, directory / skill / "SKILL.md")), None)
            if directory is not None:
                elsewhere[skill] = str(directory)
    moved = [rel for rel in recorded if rel.split("/")[0] in elsewhere]

    def retire(rel: str, digest: str) -> None:
        path = root / rel
        current = planner.current(path)
        if current is None:
            return
        if rel in previously_adopted:
            ctx.notes.append(f"{adapter.name}: {path} existed before connect (adopted); it stays")
            return
        if str(path) in others:
            ctx.notes.append(f"{adapter.name}: {path} is also installed for {', '.join(sorted(others[str(path)]))}; it stays")
            return
        if sha256(current) == digest:
            planner.set(path, "skill", adapter.name, None, root=str(root))
        elif ctx.force:
            planner.set(path, "skill", adapter.name, None, backup=True, root=str(root))
        else:
            customized.append(str(path))

    if ctx.remove:
        for rel, digest in recorded.items():
            retire(rel, digest)
        for dependent, record in ctx.manifest["agents"].items():
            item = record.get("skills")
            if dependent in ctx.selected or not item:
                continue
            used = [name for name, directory in item.get("skillsElsewhere", {}).items()
                    if directory == str(root) and (op := planner.ops.get(root / name / "SKILL.md")) is not None and op.after is None]
            if used:
                ctx.notes.append(f"{adapter.name}: {', '.join(used)} in {root} are the copies {dependent} uses instead of its own (recorded as skillsElsewhere), "
                                 f"so {dependent} has no copy of them after this removal; reconnect it (`obsidian-second-brain connect {dependent}`) to install its own")
    else:
        for rel, data in ctx.sources.items():
            if rel.split("/")[0] in elsewhere:
                continue  # provided by another directory the agent reads: no second copy
            path = root / rel
            if Path(os.path.realpath(path.parent)) != path.parent:
                raise Refusal("symlink-target", f"{path.parent} is a symlink inside the skills directory; connect never writes through it", path)
            current = planner.current(path)
            want = sha256(data)
            files[rel] = want
            if current is not None and sha256(current) == want and path not in planner.ops and (rel in previously_adopted or str(path) not in known):
                adopted.append(rel)  # an identical copy that connect did not write: it is kept on --remove
            if current is None or sha256(current) == want:
                planner.set(path, "skill", adapter.name, data, root=str(root))
            elif sha256(current) in known.get(str(path), set()):
                planner.set(path, "skill", adapter.name, data, root=str(root))
            elif ctx.force:
                planner.set(path, "skill", adapter.name, data, backup=True, root=str(root))
            else:
                (customized if str(path) in known else foreign).append(str(path))
        for rel in moved:
            retire(rel, recorded[rel])
        if moved:
            switched = sorted({rel.split("/")[0] for rel in moved if rel.endswith("/SKILL.md") and planner.current(root / rel) is None})
            if switched:
                ctx.notes.append(f"{adapter.name}: {', '.join(switched)} are provided by a directory {adapter.title} also reads, so connect's own copies in {root} "
                                 "are removed and each skill is listed once")
        for rel, digest in recorded.items():
            if rel not in ctx.sources and rel not in moved:
                path = root / rel
                current = planner.current(path)
                if rel in previously_adopted:
                    continue
                if current is not None and sha256(current) == digest:
                    planner.set(path, "skill", adapter.name, None, root=str(root))
                elif current is not None:
                    ctx.notes.append(f"{adapter.name}: {path} is no longer shipped but was customized; left in place")
    if files:
        for reader, record in ctx.manifest["agents"].items():
            item, reader_adapter = record.get("skills"), ctx.adapters.get(reader)
            if reader == adapter.name or reader in ctx.selected or not item or Path(item["root"]) == root or not (reader_adapter and reader_adapter.skills):
                continue
            if root not in (resolve_path(literal_location(location, ctx.home), True) for location in reader_adapter.skills.also_read):
                continue
            twice = [name for name in SKILL_NAMES if f"{name}/SKILL.md" in item["files"] and f"{name}/SKILL.md" in files]
            if twice:
                ctx.notes.append(f"{adapter.name}: {reader_adapter.title} also reads {root} and has its own copies of {', '.join(twice)} in {item['root']}, "
                                 f"so it lists them twice; run `obsidian-second-brain connect {reader}` after this apply to switch it to the shared copies "
                                 "(its own are then removed), or remove one set by hand")
    if customized or foreign:
        def show(paths: list[str]) -> str:
            return ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")

        parts = []
        if customized:
            parts.append(f"customized skill copies would be overwritten or removed ({show(customized)})")
        if foreign:
            parts.append(f"{show(foreign)}: exists and was not installed by connect, so it would be overwritten")
        raise Refusal("skill-customized", "; ".join(parts) + "; review them, then rerun with --force to back them up and replace them")
    if elsewhere:
        ctx.elsewhere[adapter.name] = elsewhere
    return None if ctx.remove else {"root": str(root), "files": files, **({"adopted": sorted(adopted)} if adopted else {}),
                                    **({"skillsElsewhere": elsewhere} if elsewhere else {})}


def plan_agent(adapter: Adapter, ctx: Context, planner: Planner, others: dict[str, dict[str, set[str]]]) -> dict[str, Any] | None:
    old = ctx.manifest["agents"].get(adapter.name)
    if ctx.remove and not old:
        ctx.notes.append(f"{adapter.name}: not connected by this manifest; nothing to remove")
        return None
    record: dict[str, Any] = {"version": ctx.version, "packageRoot": str(ctx.package_root), "interpreter": ctx.interpreter,
                              "node": ctx.node, "profile": "all", "mcp": None, "rules": None, "skills": None}
    for section, plan, present in (("mcp", plan_mcp, adapter.mcp), ("rules", plan_rules, adapter.rules), ("skills", plan_skills, adapter.skills)):
        if ctx.remove or present is not None:
            record[section] = plan(adapter, ctx, planner, (old or {}).get(section), others[section])
    return None if ctx.remove else record


def plan_directories(ctx: Context, planner: Planner, names: list[str], records: dict[str, dict[str, Any] | None], manifest_path: Path) -> None:
    """Directories connect creates, recorded per agent in the manifest (`createdDirs`); on removal, the recorded ones to delete when empty.

    A directory that already existed is never recorded. One that an earlier record created and that this agent also
    writes into is recorded for it too (like a shared file), so the last agent removed deletes it. A record without
    `createdDirs` (an older manifest) simply keeps the earlier behaviour: nothing is deleted.
    """
    agents = ctx.manifest["agents"]
    if ctx.remove:
        kept = {d for agent, record in agents.items() if agent not in names for d in record.get("createdDirs", [])}
        removable: dict[str, list[str]] = {}
        for name in names:
            for directory in agents.get(name, {}).get("createdDirs", []):
                if directory not in kept:
                    removable.setdefault(directory, []).append(name)
        ctx.directories = [{"path": d, "op": "remove", "agents": sorted(a)} for d, a in sorted(removable.items())]
        return
    recorded = {d for record in agents.values() for d in record.get("createdDirs", [])}
    owners: dict[str, set[str]] = {}
    for name in names:  # directories an agent's earlier record created stay recorded while they exist
        owners.update({d: owners.get(d, set()) | {name} for d in agents.get(name, {}).get("createdDirs", []) if Path(d).is_dir()})
    writes = [(op.path, op.agents, op.detail.get("root")) for op in planner.ops.values() if op.after is not None] + [(manifest_path, names, None)]
    for path, agents_of, skills_root in writes:
        for parent in path.parents:
            if skills_root and parent.is_relative_to(skills_root):
                continue  # the skills directory and its folders are removed with the skill copies themselves
            if not parent.exists() or str(parent) in recorded:
                owners.setdefault(str(parent), set()).update(agents_of)
    created = sorted(d for d in owners if not Path(d).exists())
    ctx.directories = [{"path": d, "op": "create", "agents": sorted(owners[d])} for d in created]
    for name in names:
        if record := records[name]:
            if mine := sorted(d for d, a in owners.items() if name in a):
                record["createdDirs"] = mine


def build_plan(names: list[str], *, remove: bool, force: bool, python: str | None, node: str | None, home: Path,
               config_file: Path) -> tuple[Context, Planner, list[Adapter]]:
    registered, _ = load_adapters()
    by_name = {adapter.name: adapter for adapter in registered}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise ConnectError("unknown-agent", f"unknown agent {', '.join(unknown)}; registered agents: {', '.join(by_name) or 'none'}. See docs/agents.md for generic MCP snippets.")
    manifest_path = manifest_path_for(config_file)
    manifest, manifest_error = load_manifest(manifest_path)
    if manifest_error:
        raise ConnectError("manifest-invalid", manifest_error)
    config, config_error = load_config(config_file)
    if config_error:
        raise ConnectError("invalid-config", config_error)
    vault = None
    configured = config.get("vaultPath") if config else None
    if isinstance(configured, str) and configured:
        path = Path(configured).expanduser()
        vault = Path(os.path.abspath(path if path.is_absolute() else Path.cwd() / path)).resolve()
    notes: list[str] = []
    interpreter = node_path = None
    try:
        if not remove:
            interpreter, interpreter_notes = probe_interpreter(python)
            node_path = resolve_node(node)
            notes.extend(interpreter_notes)
            if not (PACKAGE_ROOT / LAUNCHER).is_file():
                raise Refusal("package-incomplete", f"{PACKAGE_ROOT / LAUNCHER} is missing")
    except Refusal as refusal:
        raise ConnectError(refusal.code, str(refusal)) from refusal
    ctx = Context(home=home, config_file=config_file, config_dir=manifest_path.parent, vault=vault, package_root=PACKAGE_ROOT,
                  version=package_version(), interpreter=interpreter, node=node_path,
                  env=mcp_env(interpreter, config_file) if interpreter else {}, manifest=manifest,
                  remove=remove, force=force, sources={} if remove else skill_sources(), notes=notes,
                  adapters=by_name, selected=frozenset(names))
    if manifest_path.parent != config_file.parent.absolute():
        ctx.aliases[str(manifest_path)] = str(config_file.parent.absolute() / MANIFEST_NAME)
    planner = Planner(ctx)
    selected = set(names)
    others = {section: _owners(ctx, selected, section) for section in ("mcp", "rules", "skills")}
    refusals = []
    records: dict[str, dict[str, Any] | None] = {}
    # An agent that reads other agents' skills directories is planned after them, so it sees the copies they are about to install.
    for name in sorted(names, key=lambda name: (bool(by_name[name].skills and by_name[name].skills.also_read), name)):
        try:
            records[name] = plan_agent(by_name[name], ctx, planner, others)
        except Refusal as refusal:
            refusal.agent = name
            refusals.append(refusal)
    if refusals:
        raise ConnectError("connect-refused", "; ".join(f"{r.agent}: {r}" for r in refusals), refusals=[
            {"agent": r.agent, "code": r.code, "message": str(r), "path": str(r.path) if r.path else None, "snippet": r.snippet}
            for r in refusals])
    plan_directories(ctx, planner, names, records, manifest_path)
    if not remove:
        pairs = {frozenset((name, other)): other in selected for name in names for other in by_name[name].duplicates_with
                 if other in selected or other in manifest["agents"]}
        for pair, together in pairs.items():
            first, second = sorted(pair)
            ctx.notes.append(f"{first} and {second} ({'selected together' if together else 'one is already connected'}) register the same "
                             "MCP server for overlapping clients, so a client can list it twice; keep only one (see docs/agents.md)")
    agents = copy.deepcopy(manifest["agents"])
    for name, record in records.items():
        if record is None:
            agents.pop(name, None)
        else:
            agents[name] = record
    manifest_after = None if not agents else (json.dumps({"schemaVersion": MANIFEST_SCHEMA, "agents": agents}, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        planner.set(manifest_path, "manifest", "*", manifest_after)
    except Refusal as refusal:
        raise ConnectError(refusal.code, str(refusal)) from refusal
    return ctx, planner, [by_name[name] for name in names]


def core_plan(ctx: Context, planner: Planner, names: list[str]) -> dict[str, Any]:
    """The reviewed, hashed plan: exact agent selection, inputs, and each file's before/after digests.

    A path reached through a symlinked parent directory is listed as the real path, with the path as
    configured in `requestedPath`; both are part of the hash.
    """
    return {"operation": "connect", "remove": ctx.remove, "force": ctx.force, "agents": sorted(names),
            "packageRoot": str(ctx.package_root), "version": ctx.version, "interpreter": ctx.interpreter, "node": ctx.node,
            "config": str(ctx.config_file),
            "directories": ctx.directories,
            **({"skillsElsewhere": ctx.elsewhere} if ctx.elsewhere else {}),
            "files": [{"agents": sorted(op.agents), "role": op.role, "path": str(op.path), "op": op.op,
                       **({"requestedPath": str(ctx.requested(op.path))} if ctx.requested(op.path) != op.path else {}),
                       "beforeSha256": None if op.before is None else sha256(op.before),
                       "afterSha256": None if op.after is None else sha256(op.after),
                       "backup": bool(op.backup and op.before is not None and op.op in ("update", "delete"))}
                      for op in planner.ops.values()]}  # apply order: deterministic for a sorted selection


def plan_digest(plan: dict[str, Any]) -> str:
    # One plan-hash implementation for setup and connect.
    return load_source("obsidian_connect_bootstrap", str(SCRIPTS / "bootstrap-vault.py")).digest(plan)


def backup_dir(ctx: Context, plan_hash: str) -> Path:
    """`connect-backups/<plan>/` next to the manifest; each agent's files go in `<agent>/` below it."""
    return ctx.config_dir / BACKUP_DIR / plan_hash[:16]


def forced_backup(op: FileOp) -> bool:
    """A customized skill copy that --force replaces or removes: the only copy of the user's text."""
    return op.role == "skill" and op.backup and op.before is not None and op.op in ("update", "delete")


def prune_backups(ctx: Context, agents: list[str], plan_hash: str) -> None:
    """Delete the connect-time backups of removed agents; this plan's own and every `forced/` copy stay."""
    root = ctx.config_dir / BACKUP_DIR
    if not root.is_dir():
        return
    for plan_dir in root.iterdir():
        if plan_dir.name == plan_hash[:16] or not plan_dir.is_dir():
            continue
        for agent in agents:
            agent_dir = plan_dir / agent
            if not agent_dir.is_dir():
                continue
            for item in agent_dir.iterdir():
                if item.name == FORCED_DIR:
                    continue
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            try:
                agent_dir.rmdir()
            except OSError:
                pass
        try:
            plan_dir.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


# -- apply ----------------------------------------------------------------------------------------------

def apply_plan(ctx: Context, planner: Planner, plan_hash: str, agents: list[str]) -> list[str]:
    changed = [op for op in planner.ops.values() if op.op != "unchanged"]
    for op in changed:
        if read_file(op.path) != op.before:
            raise ConnectError("destination-changed", f"{op.path} changed after review; preview again")
    for item in ctx.directories:
        if item["op"] == "create" and Path(item["path"]).exists():
            raise ConnectError("destination-changed", f"{item['path']} appeared after review; preview again")
    written: list[tuple[Path, bytes | None, bytes | None, int]] = []
    created_dirs: list[Path] = []

    def put(path: Path, before: bytes | None, after: bytes | None, mode: int, private: bool = False) -> None:
        if read_file(path) != before:
            raise ConnectError("destination-changed", f"{path} changed during apply")
        if after is None:
            path.unlink()
        else:
            ensure_parent(path, created_dirs, 0o700 if private else None)
            write_atomic(path, after, mode)
        written.append((path, before, after, mode))
        if read_file(path) != after:
            raise ConnectError("verify-failed", f"{path} does not match the written content")

    try:
        backups = [op for op in changed if op.backup and op.before is not None]
        per_agent: dict[tuple[str, bool], list[FileOp]] = {}
        for op in backups:
            for agent in op.agents:
                per_agent.setdefault((agent, forced_backup(op)), []).append(op)
        for (agent, forced), ops in sorted(per_agent.items()):
            directory = backup_dir(ctx, plan_hash) / agent / (FORCED_DIR if forced else "")
            index = {}
            for number, op in enumerate(ops, 1):
                name = f"{number:03d}-{op.path.name}"
                index[name] = str(op.path)
                if read_file(directory / name) != op.before:
                    put(directory / name, None, op.before, 0o600, private=True)
            index_data = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if read_file(directory / "backup-index.json") != index_data:
                put(directory / "backup-index.json", None, index_data, 0o600, private=True)
            private_dir = directory
            while True:  # the forced/ or agent directory, the plan directory and connect-backups itself
                os.chmod(private_dir, 0o700)
                if private_dir.name == BACKUP_DIR:
                    break
                private_dir = private_dir.parent
        for op in changed:
            mode = op.path.lstat().st_mode & 0o777 if op.before is not None else (0o600 if op.role in ("mcp", "manifest") else 0o644)
            put(op.path, op.before, op.after, mode)
    except BaseException as exc:
        conflicts = []
        for path, before, after, mode in reversed(written):
            try:
                if read_file(path) != after:
                    conflicts.append(str(path))
                elif before is None:
                    path.unlink()
                else:
                    ensure_parent(path, created_dirs)
                    write_atomic(path, before, mode)
            except (OSError, Refusal):
                conflicts.append(str(path))
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        if not isinstance(exc, Exception):
            raise
        message = f"{exc}; rolled back every change" + (f", except files changed meanwhile and left as they are: {', '.join(conflicts)}" if conflicts else "")
        raise ConnectError("apply-failed", message) from exc
    for op in changed:
        if op.after is None and op.role == "skill":
            root = Path(op.detail["root"])
            for parent in op.path.parents:
                if not parent.is_relative_to(root):
                    break
                try:
                    parent.rmdir()  # empty skill folders, then the emptied skills directory itself
                except OSError:
                    break
    if ctx.remove:
        prune_backups(ctx, [agent for agent in agents if agent in ctx.manifest["agents"]], plan_hash)
        # Directories connect created, deepest first; rmdir only ever removes an empty one, so foreign content keeps it.
        for item in sorted((d for d in ctx.directories if d["op"] == "remove"), key=lambda d: len(Path(d["path"]).parts), reverse=True):
            try:
                os.rmdir(item["path"])
            except OSError:
                pass
    return [str(op.path) for op in changed]


# -- status (doctor, list) -----------------------------------------------------------------------------------

def _entry_state(record: dict[str, Any]) -> list[dict[str, str]]:
    reasons = []
    item = record.get("mcp")
    if item:
        try:
            raw = read_file(Path(item["path"]))
            doc = parse_config(item["format"], None if raw is None else raw.decode("utf-8"), Path(item["path"]))
            current = lookup(doc, tuple(item["keyPath"]), item["name"], Path(item["path"]))
        except (Refusal, UnicodeError) as exc:
            return [{"code": "mcp-config-unreadable", "detail": str(exc)}]
        if current is None:
            reasons.append({"code": "mcp-entry-missing", "detail": item["path"]})
        elif entry_hash(current) != item["entrySha256"]:
            reasons.append({"code": "mcp-entry-modified", "detail": item["path"]})
        for extra in item.get("extras", []):
            value, _ = _extra_state(doc, tuple(extra["keyPath"]), extra["name"])
            where = f"{item['path']}: {'.'.join((*extra['keyPath'], extra['name']))}"
            if value is _ABSENT:
                reasons.append({"code": "mcp-extra-missing", "detail": where})
            elif entry_hash(value) != extra["valueSha256"]:
                reasons.append({"code": "mcp-extra-modified", "detail": where})
    return reasons


def interpreter_problem(interpreter: str, probes: dict[str, str | None]) -> str | None:
    """Why the pinned interpreter cannot run the server (Python < 3.14 or no yaml), else None.

    The check runs the interpreter read-only with a 5 s timeout, once per interpreter in a doctor run
    (`probes` is that run's cache). It never compares against the interpreter that runs doctor.
    """
    if interpreter not in probes:
        try:
            python_ok, yaml_ok, _ = probe_flags(Path(interpreter), 5)
            probes[interpreter] = (None if python_ok and yaml_ok else
                                   f"{interpreter} is older than Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}" if not python_ok else
                                   f"{interpreter} cannot import yaml")
        except subprocess.TimeoutExpired:
            probes[interpreter] = f"{interpreter} did not answer within 5 s"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            probes[interpreter] = f"{interpreter} could not be run: {exc}"
    return probes[interpreter]


def evaluate(record: dict[str, Any], probes: dict[str, str | None] | None = None, adapter: Adapter | None = None,
             home: Path | None = None) -> list[dict[str, str]]:
    """Why a recorded connection no longer works; reads only.

    The entry runs the recorded package root, so that root (not the install that runs doctor, which may be
    another copy such as the Claude plugin cache) is what must exist and match the recorded version. The adapter and
    home name the directories the agent also reads skills from.
    """
    reasons: list[dict[str, str]] = []
    root = record["packageRoot"]
    if not (Path(root) / LAUNCHER).is_file():
        reasons.append({"code": "package-root-missing", "detail": f"{root} no longer contains {LAUNCHER}"})
    installed = package_version(Path(root))
    if installed is not None and record["version"] != installed:
        reasons.append({"code": "version-mismatch", "detail": f"connected with {record['version']}, installed {installed}"})
    interpreter = record["interpreter"]
    if not os.access(interpreter, os.X_OK):
        reasons.append({"code": "interpreter-missing", "detail": interpreter})
    elif (problem := interpreter_problem(interpreter, {} if probes is None else probes)):
        reasons.append({"code": "interpreter-unsuitable", "detail": problem})
    if not os.access(record["node"], os.X_OK):
        reasons.append({"code": "node-missing", "detail": record["node"]})
    reasons += _entry_state(record)
    item = record.get("rules")
    if item:
        try:
            raw = read_file(Path(item["path"]))
            text = None if raw is None else raw.decode("utf-8")
            span = _rules_span(text, Path(item["path"])) if text is not None else None
        except (Refusal, UnicodeError):
            span = None
        if span is None:
            reasons.append({"code": "rules-block-missing", "detail": item["path"]})
        elif sha256(text[span[0]:span[1]]) != item["blockSha256"]:
            reasons.append({"code": "rules-block-modified", "detail": item["path"]})
    item = record.get("skills")
    if item and item.get("files"):
        try:
            shipped = {rel: sha256(data) for rel, data in skill_sources().items()}
        except OSError:
            shipped = {}
        buckets: dict[str, list[str]] = {"skill-missing": [], "skill-modified": [], "skill-outdated": []}
        for rel, digest in item["files"].items():
            path = Path(item["root"]) / rel
            try:
                raw = read_file(path)
            except Refusal:
                buckets["skill-modified"].append(str(path))
                continue
            if raw is None:
                buckets["skill-missing"].append(str(path))
            elif sha256(raw) != digest:
                buckets["skill-modified"].append(str(path))
            elif shipped and shipped.get(rel) != digest:
                buckets["skill-outdated"].append(str(path))
        reasons += [{"code": code, "detail": ", ".join(paths[:5])} for code, paths in buckets.items() if paths]
    if item and item.get("files") and adapter is not None and adapter.skills and home is not None:
        try:
            also = [directory for directory in (resolve_path(literal_location(location, home), True) for location in adapter.skills.also_read)
                    if directory != Path(item["root"])]
        except Refusal:
            also = []
        twice = [f"{name} ({directory})" for name in SKILL_NAMES if f"{name}/SKILL.md" in item["files"] and (Path(item["root"]) / name / "SKILL.md").is_file()
                 and (directory := next((d for d in also if (d / name / "SKILL.md").is_file()), None)) is not None]
        if twice:
            reasons.append({"code": "skill-duplicated-elsewhere", "detail": f"{', '.join(twice)}: connect's own copies in {item['root']} also exist in a directory the agent reads, "
                                                                             f"so it lists them twice; run connect {adapter.name} again to switch to the shared copies"})
    if item:
        gone = [f"{name} ({directory})" for name, directory in item.get("skillsElsewhere", {}).items()
                if not (Path(directory) / name / "SKILL.md").is_file()]
        if gone:
            reasons.append({"code": "skill-elsewhere-missing", "detail": f"{', '.join(gone)}: connect skipped these skills because another directory the agent reads "
                                                                          "provided them, and that copy is gone; run connect for this agent again to install its own"})
    return reasons


def _version_key(version: str | None) -> tuple[tuple[int, ...], int, str] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?", version or "")
    return None if match is None else ((int(match[1]), int(match[2]), int(match[3])), match[4] is None, match[4] or "")


def newer_package(record: dict[str, Any]) -> dict[str, str] | None:
    """An advisory when doctor's own install is a different, newer package than the recorded one.

    Compared with the version at the recorded root (what the entry runs), else the recorded version.
    The recorded root is never replaced silently: the user reviews a reconnect from the newer install.
    """
    root = record["packageRoot"]
    if os.path.realpath(root) == os.path.realpath(PACKAGE_ROOT):
        return None
    running = package_version(Path(root)) or record["version"]
    mine = package_version()
    mine_key, running_key = _version_key(mine), _version_key(running)
    if mine_key and running_key and mine_key > running_key:
        return {"code": "newer-package-available", "detail": f"{mine} at {PACKAGE_ROOT} is newer than {running} at {root}"}
    return None


def _agent_status(adapter: Adapter | None, record: dict[str, Any] | None, home: Path, probes: dict[str, str | None]) -> dict[str, Any]:
    detected = None
    if adapter is not None:
        try:
            detected = any(literal_location(location, home).is_dir() for location in adapter.detect)
        except Refusal:
            detected = False
    reasons: list[dict[str, str]] = []
    if record is not None:
        reasons = evaluate(record, probes, adapter, home)
        state = "stale" if reasons else "connected"
        if not reasons and (advisory := newer_package(record)):
            state, reasons = "newer-package-available", [advisory]  # advisory only: nothing is broken
    elif adapter is None or adapter.mcp is None:
        state = "manual-only"
        reasons = [{"code": "no-automatic-target", "detail": "this agent has no user-level MCP config connect can merge safely"}]
    else:
        state = "absent"
        if detected:
            try:
                target = adapter.mcp
                path = resolve_path(literal_location(target.path, home))
                raw = read_file(path)
                merge_config(target.format, None if raw is None else raw.decode("utf-8"), tuple(target.key_path), target.name,
                             target.entry(mcp_command(PACKAGE_ROOT), {"PYTHON": sys.executable}), None, raw is None, path)
            except (Refusal, UnicodeError) as exc:
                state = "manual-only"
                reasons = [{"code": getattr(exc, "code", "config-unparseable"), "detail": str(exc)}]
    return {"title": adapter.title if adapter else None, "state": state, "detected": detected,
            "verification": adapter.verification if adapter else None, "adapterRegistered": adapter is not None,
            "version": (record or {}).get("version"), "interpreter": (record or {}).get("interpreter"), "node": (record or {}).get("node"), "reasons": reasons}


def status(config_file: Path, home: Path | None = None) -> dict[str, Any]:
    """Read-only connect state for doctor and the list view: connected, newer-package-available (advisory), stale, absent or manual-only."""
    home = home or Path.home()
    manifest_path = manifest_path_for(config_file)
    manifest, error = load_manifest(manifest_path)
    registered, adapter_errors = load_adapters(strict=False)
    records = {} if error else manifest["agents"]
    probes: dict[str, str | None] = {}  # one interpreter probe per interpreter in this run
    agents = {adapter.name: _agent_status(adapter, records.get(adapter.name), home, probes) for adapter in registered}
    for name, record in records.items():
        if name not in agents:
            agents[name] = _agent_status(None, record, home, probes)
    return {"manifest": {"path": str(manifest_path), "exists": manifest_path.is_file(), "error": error,
                         "code": "manifest-invalid" if error else None},
            "agents": agents, "adapterErrors": adapter_errors}


def generic_snippets(interpreter: str, config_file: Path) -> dict[str, Any]:
    from connect_adapters.spec import standard_entry
    return {profile: standard_entry(mcp_command(PACKAGE_ROOT, "node", profile), mcp_env(interpreter, config_file)) for profile in GENERIC_PROFILES}


# -- command line -----------------------------------------------------------------------------------------------

def public_plan(ctx: Context, planner: Planner, core: dict[str, Any], plan_hash: str) -> dict[str, Any]:
    files = []
    for item in core["files"]:
        op = planner.ops[Path(item["path"])]
        shown = dict(item)
        shown.update({key: value for key, value in op.detail.items() if key in ("entry", "block", "header", "root")})
        files.append(shown)
    forced = sorted({str(backup_dir(ctx, plan_hash) / agent / FORCED_DIR) for op in planner.ops.values() if forced_backup(op) for agent in op.agents})
    return {**core, "files": files, "planHash": plan_hash,
            "backupDir": str(backup_dir(ctx, plan_hash)) if any(item["backup"] for item in core["files"]) else None,
            "forcedBackupDirs": forced}


def adapter_extras(adapters: list[Adapter], ctx: Context) -> tuple[list[dict[str, str]], list[str]]:
    body = rules_body(ctx.vault, ctx.config_file)
    snippets = [{"agent": adapter.name, "title": snippet.title, "destination": snippet.destination,
                 "content": snippet.content.replace("{{rules}}", body)} for adapter in adapters for snippet in adapter.snippets]
    notes = [f"{adapter.name}: {note}" for adapter in adapters for note in adapter.notes]
    return snippets + ctx.snippets, notes + ctx.notes


def mechanism(adapter: Adapter) -> str:
    parts = [label for label, present in (("MCP entry", adapter.mcp), ("rules block", adapter.rules), ("skill copies", adapter.skills)) if present]
    return " + ".join(parts) or "snippets only"


def list_agents(config_file: Path, home: Path) -> dict[str, Any]:
    state = status(config_file, home)
    registered = {adapter.name: adapter for adapter in load_adapters(strict=False)[0]}
    agents = [{"name": name, "mechanism": mechanism(registered[name]) if name in registered else None,
               **{key: value for key, value in item.items() if key != "adapterRegistered"}} for name, item in state["agents"].items()]
    return {"operation": "connect", "mode": "list", "manifest": state["manifest"], "agents": agents,
            "adapterErrors": state["adapterErrors"], "genericSnippets": generic_snippets(sys.executable, config_file)}


def _print_list(output: dict[str, Any]) -> None:
    if not output["agents"]:
        print("No agent adapters are registered; see docs/agents.md for generic MCP snippets.")
    for item in output["agents"]:
        flags = "detected" if item["detected"] else "not detected"
        print(f"{item['name']}\t{item['title']}\t{item['state']}\t{flags}\t{item['verification'] or 'adapter missing'}")
        for reason in item["reasons"]:
            print(f"  {reason['code']}: {reason['detail']}")
    for message in output["adapterErrors"]:
        print(f"adapter error: {message}", file=sys.stderr)
    print("Other MCP clients: print the generic snippets with --json (genericSnippets) or see docs/agents.md.")
    print("Preview a connection with: obsidian-second-brain connect <agent>")


def _print_plan(output: dict[str, Any], names: list[str], args: argparse.Namespace) -> None:
    plan = output["plan"]
    print(f"Connect {'remove' if plan['remove'] else 'wiring'} {output['mode']}: {', '.join(plan['agents'])}")
    print(f"Plan hash: {output['planHash']}")
    if not plan["remove"]:
        print(f"Package root: {plan['packageRoot']}")
        print(f"Interpreter: {plan['interpreter']}")
        print(f"Node: {plan['node']}")
    skills: dict[str, list[dict[str, Any]]] = {}
    for item in plan["files"]:
        if item["op"] == "unchanged":
            continue
        if item["role"] == "skill" and item["op"] == "create":
            skills.setdefault(item["root"], []).append(item)
            continue
        print(f"{item['op'].capitalize()} {item['role']}: {item['path']}"
              + (f" (configured as {item['requestedPath']}, a symlinked path; the real file is changed)" if "requestedPath" in item else "")
              + (" (original is backed up first)" if item["backup"] else ""))
        if "entry" in item:
            print("  entry: " + json.dumps(item["entry"], ensure_ascii=False))
        if "header" in item:
            print("  header (written with the new file):\n" + "\n".join("    " + line for line in item["header"].splitlines()))
        if "block" in item:
            print("  rules block:\n" + "\n".join("    " + line for line in item["block"].splitlines()))
    for root, items in skills.items():
        print(f"Create skill copies ({len(items)} files, JSON lists each): {root}")
    for agent, found in plan.get("skillsElsewhere", {}).items():
        for directory in sorted(set(found.values())):
            names_here = ", ".join(skill for skill, where in found.items() if where == directory)
            print(f"Skip skills for {agent}: {names_here} already in {directory}, which {agent} also reads; no second copy is installed (rerun connect for {agent} after that copy goes away)")
    for item in plan["directories"]:
        print(("Create directory: " if item["op"] == "create" else "Remove directory connect created, if empty: ") + item["path"])
    if plan["backupDir"]:
        print(f"Backups: {plan['backupDir']}")
    for directory in plan["forcedBackupDirs"]:
        print(f"Customized skill copies are saved in {directory}; removal never prunes them, delete them by hand when you no longer need them")
    if all(item["op"] == "unchanged" for item in plan["files"]):
        print("No changes planned.")
    for note in output["notes"]:
        print(f"Note: {note}")
    for snippet in output["snippets"]:
        print(f"\nSnippet for {snippet['agent']}: {snippet['title']} (paste into {snippet['destination']}; connect never writes project files)")
        print(snippet["content"].rstrip("\n"))
    if output["applied"]:
        print(f"Applied connect plan {output['planHash']}")
    elif any(item["op"] != "unchanged" for item in plan["files"]):
        selected = os.environ.get("OBSIDIAN_AGENT_CONFIG")
        prefix = f"OBSIDIAN_AGENT_CONFIG={shlex.quote(str(Path(selected).expanduser().absolute()))} " if selected else ""
        argv = ["obsidian-second-brain", "connect", *names] + (["--remove"] if args.remove else []) + (["--force"] if args.force else []) \
            + (["--python", plan["interpreter"]] if args.python else []) + (["--node", plan["node"]] if args.node else []) + ["--apply", "--confirm", output["planHash"]]
        print(f"Review, then apply exactly this plan: {prefix}{shlex.join(argv)}")


def main(argv: list[str]) -> int:
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            if "--json" in argv:
                print(json.dumps({"code": "invalid-arguments", "error": message}, ensure_ascii=False))
                raise SystemExit(2)
            super().error(message)

    parser = Parser(prog="obsidian-second-brain connect", description="Preview, apply or remove the wiring of an agent to the wiki.")
    parser.add_argument("agents", nargs="*", help="agent names; none lists detected agents and writes nothing")
    parser.add_argument("--apply", action="store_true", help="apply the plan confirmed by --confirm")
    parser.add_argument("--confirm", help="exact plan hash shown by the reviewed preview")
    parser.add_argument("--remove", action="store_true", help="plan (or apply) removal of what connect wrote")
    parser.add_argument("--force", action="store_true", help="replace customized skill copies after backing them up")
    parser.add_argument("--python", help="interpreter to pin in MCP entries (default: the one running this command); must import yaml")
    parser.add_argument("--node", help="node executable to pin in MCP entries (default: the one that runs the launcher, else node on PATH); must run")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    if args.confirm is not None and not args.apply:
        parser.error("--confirm requires --apply")
    if args.apply and args.confirm is None:
        parser.error("--apply requires --confirm <planHash> from a reviewed preview")
    if not args.agents and (args.apply or args.remove or args.force or args.python or args.node):
        parser.error("--apply, --remove, --force, --python and --node require at least one agent")
    if args.remove and (args.python or args.node):
        parser.error("--python and --node do not apply to --remove")
    names = sorted(set(args.agents))
    home = Path.home()
    config_file = config_file_path()

    def fail(code: str, message: str, **extra: Any) -> int:
        if args.json:
            print(json.dumps({"code": code, "error": message, **extra}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"obsidian-second-brain: {message}", file=sys.stderr)
            for refusal in extra.get("refusals", []):
                if refusal["snippet"]:
                    print(f"\nAdd this by hand instead ({refusal['agent']}"
                          f"{', ' + refusal['path'] if refusal['path'] else ''}):\n{refusal['snippet'].rstrip()}", file=sys.stderr)
        return 1

    try:
        if not names:
            output = list_agents(config_file, home)
            if args.json:
                print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            else:
                _print_list(output)
            return 0
        ctx, planner, adapters = build_plan(names, remove=args.remove, force=args.force, python=args.python, node=args.node,
                                            home=home, config_file=config_file)
        core = core_plan(ctx, planner, names)
        plan_hash = plan_digest(core)
        if args.apply:
            if args.confirm != plan_hash:
                return fail("stale-plan", f"stale or unconfirmed plan; preview again (hash {plan_hash})")
            apply_plan(ctx, planner, plan_hash, names)
        snippets, notes = adapter_extras(adapters, ctx)
        output = {"operation": "connect", "mode": "apply" if args.apply else "preview", "planHash": plan_hash,
                  "plan": public_plan(ctx, planner, core, plan_hash), "applied": args.apply,
                  "snippets": [] if args.remove else snippets, "notes": notes}
        if args.json:
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        else:
            _print_plan(output, names, args)
        return 0
    except ConnectError as exc:
        return fail(exc.code, str(exc), **exc.extra)
    except Refusal as exc:
        return fail(exc.code, str(exc))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
