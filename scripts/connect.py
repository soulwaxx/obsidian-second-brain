#!/usr/bin/env python3
"""Reviewed `connect` command: wire an agent to the wiki, preview first, undo on request.

`connect` lists detected agents and writes nothing. `connect <agent>...` previews
the exact file changes with a plan hash; `--apply --confirm <planHash>` applies them,
and `--remove` previews/applies the undo the same way. Per agent it merges one MCP
entry into the agent's user-level config, upserts a marked rules block, copies the
six portable skills, and records everything (paths, hashes, package root,
interpreter, version) in a manifest next to the selected integration config.

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
from connect_adapters.spec import Adapter, Location  # noqa: E402

PACKAGE_ROOT = SCRIPTS.parent
LAUNCHER = "scripts/obsidian-second-brain.mjs"
MANIFEST_NAME = "connect-manifest.json"
BACKUP_DIR = "connect-backups"
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


def locate(location: Location, home: Path) -> Path:
    path = location(home) if callable(location) else home / location
    path = Path(os.path.abspath(path))
    if not path.is_absolute():
        raise Refusal("relative-target", f"adapter location {location!r} is not absolute")
    return path


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


def ensure_parent(path: Path, created: list[Path]) -> None:
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
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
            doc = yaml.safe_load(text)
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


def drop_entry(doc: dict[str, Any], key_path: tuple[str, ...], name: str, prune: bool) -> None:
    chain = [doc]
    for key in key_path:
        chain.append(chain[-1][key])
    del chain[-1][name]
    if prune:
        for index in range(len(key_path), 0, -1):
            if chain[index]:
                break
            del chain[index - 1][key_path[index - 1]]


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


def _toml_span(text: str, path: Path | None) -> tuple[int, int]:
    starts = [m.start() for m in re.finditer(rf"(?m)^{re.escape(TOML_START)}[ \t]*$", text)]
    ends = [m.end() for m in re.finditer(rf"(?m)^{re.escape(TOML_END)}[ \t]*\n?", text)]
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise Refusal("toml-marker-missing", f"{path or 'config'}: the entry has no single marked {TOML_START} block; remove or edit it by hand", path)
    return starts[0], ends[0]


def cut_block(text: str, start: int, end: int) -> str:
    """Remove a marked block together with the one blank line that separated it, undoing an append."""
    head, tail = text[:start], text[end:]
    if head.endswith("\n\n"):
        head = head[:-1]
    if tail.startswith("\n"):
        tail = tail[1:]
    return head + tail


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
                 owned: str | None, created: bool, path: Path | None = None) -> str | None:
    """Return the new file text (None = delete the file) with only our entry added, replaced or removed.

    `entry` None removes it. Our entry may be replaced or removed only when it still
    matches the hash recorded in the manifest (`owned`); any other differing entry is refused.
    """
    doc = parse_config(fmt, text, path)
    current = lookup(doc, key_path, name, path)
    if entry is None:
        if current is None:
            return text
        if owned is None or entry_hash(current) != owned:
            raise Refusal("entry-modified", f"{path or 'config'}: entry {name!r} was changed after connect; remove it by hand", path)
        if fmt == "toml":
            start, end = _toml_span(text or "", path)
            new_text = cut_block(text or "", start, end)
            expected = copy.deepcopy(doc)
            drop_entry(expected, key_path, name, False)
            try:
                remaining = tomllib.loads(new_text)
            except tomllib.TOMLDecodeError:
                remaining = None
            if remaining is None or _prune(remaining, key_path) != _prune(expected, key_path):
                raise Refusal("toml-merge-unsafe", f"{path or 'config'}: removal would change other settings; remove the entry by hand", path)
            return None if created and not new_text.strip() else new_text
        drop_entry(doc, key_path, name, created)
        return None if created and not doc else dump_config(fmt, doc, text)
    if current == entry:
        return text
    if current is not None and (owned is None or entry_hash(current) != owned):
        raise Refusal("entry-differs", f"{path or 'config'}: an entry named {name!r} already exists and differs; connect only manages its own entry", path)
    if fmt == "toml":
        block = toml_block(key_path, name, entry)
        body = text or ""
        if current is None:
            new_text = body + ("" if not body or body.endswith("\n") else "\n") + ("\n" if body.strip() else "") + block
        else:
            start, end = _toml_span(body, path)
            new_text = body[:start] + block + body[end:]
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
    return dump_config(fmt, doc, text)


def render_snippet(fmt: str, key_path: tuple[str, ...], name: str, entry: dict[str, Any]) -> str:
    if fmt == "toml":
        return toml_block(key_path, name, entry)
    doc: dict[str, Any] = {}
    set_entry(doc, key_path, name, entry)
    return dump_config(fmt, doc, None)


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

def load_manifest(path: Path) -> tuple[dict[str, Any], str | None]:
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
    if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != MANIFEST_SCHEMA
            or not isinstance(manifest.get("agents"), dict)):
        return empty, f"invalid {path}: unsupported install manifest"
    return manifest, None


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
        except Exception as exc:
            if strict:
                raise ConnectError("adapter-invalid", f"adapter {module_name}: {exc}") from exc
            errors.append(f"{module_name}: {exc}")
            continue
        adapters.append(adapter)
    return adapters, errors


def mcp_command(package_root: Path, profile: str = "all") -> list[str]:
    return ["node", str(package_root / LAUNCHER), "mcp", "--tools", profile]


def mcp_env(interpreter: str, config_file: Path) -> dict[str, str]:
    env = {"PYTHON": interpreter}
    if os.environ.get("OBSIDIAN_AGENT_CONFIG"):
        env["OBSIDIAN_AGENT_CONFIG"] = str(config_file)
    return env


def probe_interpreter(selected: str | None) -> tuple[str, list[str]]:
    """Absolute interpreter path (symlinks kept, so venvs stay pinned) and advisory notes; refuses unusable ones."""
    path = Path(os.path.abspath(os.path.expanduser(selected or sys.executable)))
    if not path.is_file() or not os.access(path, os.X_OK):
        raise Refusal("interpreter-invalid", f"interpreter {path} is not an executable file")
    code = ("import sys, importlib.util as u; print(int(sys.version_info >= %r), int(u.find_spec('yaml') is not None), "
            "int(u.find_spec('markdown_it') is not None))" % (MIN_PYTHON,))
    if path == Path(os.path.abspath(sys.executable)):
        python_ok = int(sys.version_info >= MIN_PYTHON)
        yaml_ok = int(importlib.util.find_spec("yaml") is not None)
        markdown_ok = int(importlib.util.find_spec("markdown_it") is not None)
    else:
        try:
            result = subprocess.run([str(path), "-c", code], capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            python_ok, yaml_ok, markdown_ok = (int(value) for value in result.stdout.split())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise Refusal("interpreter-invalid", f"interpreter {path} could not be probed: {exc}") from exc
    if not python_ok:
        raise Refusal("interpreter-unsuitable", f"interpreter {path} is older than Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}; select one with --python")
    if not yaml_ok:
        raise Refusal("interpreter-unsuitable", f"interpreter {path} cannot import yaml; install requirements.txt there or select another with --python")
    notes = [] if markdown_ok else [f"interpreter {path} lacks markdown-it-py; wiki_outline and wiki_read need requirements.txt installed there"]
    return str(path), notes


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
    vault: Path | None
    package_root: Path
    version: str | None
    interpreter: str | None
    env: dict[str, str]
    manifest: dict[str, Any]
    remove: bool
    force: bool
    sources: dict[str, bytes]
    notes: list[str] = field(default_factory=list)


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
        paths = [item.get("path")] if "path" in item else [str(Path(item["root"]) / rel) for rel in item.get("files", {})]
        for path in paths:
            owners.setdefault(path, set()).add(agent)
    return owners


def plan_mcp(adapter: Adapter, ctx: Context, planner: Planner, old: dict[str, Any] | None, others: dict[str, set[str]]) -> dict[str, Any] | None:
    target = adapter.mcp
    if ctx.remove:
        if not old:
            return None
        path, fmt, key_path, name = Path(old["path"]), old["format"], tuple(old["keyPath"]), old["name"]
        entry = None
    else:
        assert target is not None and ctx.interpreter is not None
        path, fmt, key_path, name = locate(target.path, ctx.home), target.format, tuple(target.key_path), target.name
        entry = target.entry(mcp_command(ctx.package_root), ctx.env)
    created = old["createdFile"] if old else read_file(path) is None
    try:
        if entry is None and str(path) in others:
            planner.ctx.notes.append(f"{adapter.name}: {path} also serves {', '.join(sorted(others[str(path)]))}; its entry stays")
            return None
        text = read_text(planner, path)
        new_text = merge_config(fmt, text, key_path, name, entry, old["entrySha256"] if old else None, created, path)
    except Refusal as refusal:
        if entry is not None:
            refusal.snippet = render_snippet(fmt, key_path, name, entry)
        raise
    planner.set(path, "mcp", adapter.name, None if new_text is None else new_text.encode("utf-8"), backup=True,
                **({"entry": entry} if entry is not None else {}))
    if entry is None:
        return None
    return {"path": str(path), "format": fmt, "keyPath": list(key_path), "name": name,
            "entrySha256": entry_hash(entry), "createdFile": created}


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
        path = locate(adapter.rules.path, ctx.home)
    created = old["createdFile"] if old else read_file(path) is None
    text = read_text(planner, path)
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
        planner.set(path, "rules", adapter.name, None if created and not remaining.strip() else remaining.encode("utf-8"), backup=True)
        return None
    body = rules_body(ctx.vault, ctx.config_file)
    block = rules_block(body)
    if text is None or not text.strip():
        new_text = block + "\n"
    elif span is None:
        new_text = text.rstrip("\n") + "\n\n" + block + "\n"
    else:
        new_text = text[:span[0]] + block + text[span[1]:]
    planner.set(path, "rules", adapter.name, new_text.encode("utf-8"), backup=True, block=block)
    return {"path": str(path), "blockSha256": sha256(block), "createdFile": created}


def plan_skills(adapter: Adapter, ctx: Context, planner: Planner, old: dict[str, Any] | None, others: dict[str, set[str]]) -> dict[str, Any] | None:
    recorded: dict[str, str] = dict(old["files"]) if old else {}
    root = Path(old["root"]) if (ctx.remove and old) else (locate(adapter.skills.path, ctx.home) if adapter.skills else None)
    if root is None:
        return None
    known: dict[str, set[str]] = {}
    for record in ctx.manifest["agents"].values():
        item = record.get("skills")
        if isinstance(item, dict):
            for rel, digest in item.get("files", {}).items():
                known.setdefault(str(Path(item["root"]) / rel), set()).add(digest)
    customized: list[str] = []
    files: dict[str, str] = {}

    def retire(rel: str, digest: str) -> None:
        path = root / rel
        current = planner.current(path)
        if current is None:
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
    else:
        for rel, data in ctx.sources.items():
            path = root / rel
            current = planner.current(path)
            want = sha256(data)
            files[rel] = want
            if current is None or sha256(current) == want:
                planner.set(path, "skill", adapter.name, data, root=str(root))
            elif sha256(current) in known.get(str(path), set()):
                planner.set(path, "skill", adapter.name, data, root=str(root))
            elif ctx.force:
                planner.set(path, "skill", adapter.name, data, backup=True, root=str(root))
            else:
                customized.append(str(path))
        for rel, digest in recorded.items():
            if rel not in ctx.sources:
                path = root / rel
                current = planner.current(path)
                if current is not None and sha256(current) == digest:
                    planner.set(path, "skill", adapter.name, None, root=str(root))
                elif current is not None:
                    ctx.notes.append(f"{adapter.name}: {path} is no longer shipped but was customized; left in place")
    if customized:
        shown = ", ".join(customized[:5]) + (f" and {len(customized) - 5} more" if len(customized) > 5 else "")
        raise Refusal("skill-customized", f"customized skill copies would be overwritten ({shown}); review them, then rerun with --force to back them up and replace them")
    return None if ctx.remove else {"root": str(root), "files": files}


def plan_agent(adapter: Adapter, ctx: Context, planner: Planner, others: dict[str, dict[str, set[str]]]) -> dict[str, Any] | None:
    old = ctx.manifest["agents"].get(adapter.name)
    if ctx.remove and not old:
        ctx.notes.append(f"{adapter.name}: not connected by this manifest; nothing to remove")
        return None
    record: dict[str, Any] = {"version": ctx.version, "packageRoot": str(ctx.package_root), "interpreter": ctx.interpreter,
                              "profile": "all", "mcp": None, "rules": None, "skills": None}
    for section, plan, present in (("mcp", plan_mcp, adapter.mcp), ("rules", plan_rules, adapter.rules), ("skills", plan_skills, adapter.skills)):
        if ctx.remove or present is not None:
            record[section] = plan(adapter, ctx, planner, (old or {}).get(section), others[section])
    return None if ctx.remove else record


def build_plan(names: list[str], *, remove: bool, force: bool, python: str | None, home: Path, config_file: Path) -> tuple[Context, Planner, list[Adapter]]:
    registered, _ = load_adapters()
    by_name = {adapter.name: adapter for adapter in registered}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise ConnectError("unknown-agent", f"unknown agent {', '.join(unknown)}; registered agents: {', '.join(by_name) or 'none'}. See docs/agents.md for generic MCP snippets.")
    manifest_path = config_file.parent / MANIFEST_NAME
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
    interpreter = None
    try:
        if not remove:
            interpreter, interpreter_notes = probe_interpreter(python)
            notes.extend(interpreter_notes)
            if not (PACKAGE_ROOT / LAUNCHER).is_file():
                raise Refusal("package-incomplete", f"{PACKAGE_ROOT / LAUNCHER} is missing")
    except Refusal as refusal:
        raise ConnectError(refusal.code, str(refusal)) from refusal
    ctx = Context(home=home, config_file=config_file, vault=vault, package_root=PACKAGE_ROOT, version=package_version(),
                  interpreter=interpreter, env=mcp_env(interpreter, config_file) if interpreter else {}, manifest=manifest,
                  remove=remove, force=force, sources={} if remove else skill_sources(), notes=notes)
    planner = Planner(ctx)
    selected = set(names)
    others = {section: _owners(ctx, selected, section) for section in ("mcp", "rules", "skills")}
    refusals = []
    records: dict[str, dict[str, Any] | None] = {}
    for name in names:
        try:
            records[name] = plan_agent(by_name[name], ctx, planner, others)
        except Refusal as refusal:
            refusal.agent = name
            refusals.append(refusal)
    if refusals:
        raise ConnectError("connect-refused", "; ".join(f"{r.agent}: {r}" for r in refusals), refusals=[
            {"agent": r.agent, "code": r.code, "message": str(r), "path": str(r.path) if r.path else None, "snippet": r.snippet}
            for r in refusals])
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
    """The reviewed, hashed plan: exact agent selection, inputs, and each file's before/after digests."""
    return {"operation": "connect", "remove": ctx.remove, "force": ctx.force, "agents": sorted(names),
            "packageRoot": str(ctx.package_root), "version": ctx.version, "interpreter": ctx.interpreter,
            "config": str(ctx.config_file),
            "files": [{"agents": sorted(op.agents), "role": op.role, "path": str(op.path), "op": op.op,
                       "beforeSha256": None if op.before is None else sha256(op.before),
                       "afterSha256": None if op.after is None else sha256(op.after),
                       "backup": bool(op.backup and op.before is not None and op.op in ("update", "delete"))}
                      for op in planner.ops.values()]}  # apply order: deterministic for a sorted selection


def plan_digest(plan: dict[str, Any]) -> str:
    # One plan-hash implementation for setup and connect.
    return load_source("obsidian_connect_bootstrap", str(SCRIPTS / "bootstrap-vault.py")).digest(plan)


def backup_dir(ctx: Context, plan_hash: str) -> Path:
    return ctx.config_file.parent / BACKUP_DIR / plan_hash[:16]


# -- apply ----------------------------------------------------------------------------------------------

def apply_plan(ctx: Context, planner: Planner, plan_hash: str) -> list[str]:
    changed = [op for op in planner.ops.values() if op.op != "unchanged"]
    for op in changed:
        if read_file(op.path) != op.before:
            raise ConnectError("destination-changed", f"{op.path} changed after review; preview again")
    written: list[tuple[Path, bytes | None, bytes | None, int]] = []
    created_dirs: list[Path] = []

    def put(path: Path, before: bytes | None, after: bytes | None, mode: int) -> None:
        if read_file(path) != before:
            raise ConnectError("destination-changed", f"{path} changed during apply")
        if after is None:
            path.unlink()
        else:
            ensure_parent(path, created_dirs)
            write_atomic(path, after, mode)
        written.append((path, before, after, mode))
        if read_file(path) != after:
            raise ConnectError("verify-failed", f"{path} does not match the written content")

    try:
        backups = [op for op in changed if op.backup and op.before is not None]
        if backups:
            directory = backup_dir(ctx, plan_hash)
            index = {}
            for number, op in enumerate(backups, 1):
                name = f"{number:03d}-{op.path.name}"
                index[name] = str(op.path)
                if read_file(directory / name) != op.before:
                    put(directory / name, None, op.before, 0o600)
            index_data = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if read_file(directory / "backup-index.json") != index_data:
                put(directory / "backup-index.json", None, index_data, 0o600)
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
    return reasons


def evaluate(record: dict[str, Any]) -> list[dict[str, str]]:
    """Why a recorded connection no longer matches this installed package; reads only."""
    reasons: list[dict[str, str]] = []
    root = record.get("packageRoot")
    if root != str(PACKAGE_ROOT):
        reasons.append({"code": "package-root-mismatch", "detail": f"connected from {root}, running {PACKAGE_ROOT}"})
    if not (Path(str(root)) / LAUNCHER).is_file():
        reasons.append({"code": "package-root-missing", "detail": f"{root} no longer contains {LAUNCHER}"})
    interpreter = record.get("interpreter")
    if not interpreter or not os.access(interpreter, os.X_OK):
        reasons.append({"code": "interpreter-missing", "detail": str(interpreter)})
    elif Path(interpreter) != Path(os.path.abspath(sys.executable)):
        reasons.append({"code": "interpreter-mismatch", "detail": f"connected with {interpreter}, running {sys.executable}"})
    if record.get("version") != package_version():
        reasons.append({"code": "version-mismatch", "detail": f"connected with {record.get('version')}, installed {package_version()}"})
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
    return reasons


def _agent_status(adapter: Adapter | None, record: dict[str, Any] | None, home: Path) -> dict[str, Any]:
    detected = None
    if adapter is not None:
        try:
            detected = any(locate(location, home).is_dir() for location in adapter.detect)
        except Refusal:
            detected = False
    reasons: list[dict[str, str]] = []
    if record is not None:
        reasons = evaluate(record)
        state = "stale" if reasons else "connected"
    elif adapter is None or adapter.mcp is None:
        state = "manual-only"
        reasons = [{"code": "no-automatic-target", "detail": "this agent has no user-level MCP config connect can merge safely"}]
    else:
        state = "absent"
        if detected:
            try:
                target = adapter.mcp
                path = locate(target.path, home)
                raw = read_file(path)
                merge_config(target.format, None if raw is None else raw.decode("utf-8"), tuple(target.key_path), target.name,
                             target.entry(mcp_command(PACKAGE_ROOT), {"PYTHON": sys.executable}), None, raw is None, path)
            except (Refusal, UnicodeError) as exc:
                state = "manual-only"
                reasons = [{"code": getattr(exc, "code", "config-unparseable"), "detail": str(exc)}]
    return {"title": adapter.title if adapter else None, "state": state, "detected": detected,
            "verification": adapter.verification if adapter else None, "adapterRegistered": adapter is not None,
            "version": (record or {}).get("version"), "reasons": reasons}


def status(config_file: Path, home: Path | None = None) -> dict[str, Any]:
    """Read-only connect state for doctor and the list view: connected, stale, absent or manual-only."""
    home = home or Path.home()
    manifest_path = config_file.parent / MANIFEST_NAME
    manifest, error = load_manifest(manifest_path)
    registered, adapter_errors = load_adapters(strict=False)
    records = {} if error else manifest["agents"]
    agents = {adapter.name: _agent_status(adapter, records.get(adapter.name), home) for adapter in registered}
    for name, record in records.items():
        if name not in agents:
            agents[name] = _agent_status(None, record, home)
    return {"manifest": {"path": str(manifest_path), "exists": manifest_path.is_file(), "error": error},
            "agents": agents, "adapterErrors": adapter_errors}


def generic_snippets(interpreter: str, config_file: Path) -> dict[str, Any]:
    from connect_adapters.spec import standard_entry
    return {profile: standard_entry(mcp_command(PACKAGE_ROOT, profile), mcp_env(interpreter, config_file)) for profile in GENERIC_PROFILES}


# -- command line -----------------------------------------------------------------------------------------------

def public_plan(ctx: Context, planner: Planner, core: dict[str, Any], plan_hash: str) -> dict[str, Any]:
    files = []
    for item in core["files"]:
        op = planner.ops[Path(item["path"])]
        shown = dict(item)
        shown.update({key: value for key, value in op.detail.items() if key in ("entry", "block", "root")})
        files.append(shown)
    return {**core, "files": files, "planHash": plan_hash,
            "backupDir": str(backup_dir(ctx, plan_hash)) if any(item["backup"] for item in core["files"]) else None}


def adapter_extras(adapters: list[Adapter], ctx: Context) -> tuple[list[dict[str, str]], list[str]]:
    body = rules_body(ctx.vault, ctx.config_file)
    snippets = [{"agent": adapter.name, "title": snippet.title, "destination": snippet.destination,
                 "content": snippet.content.replace("{{rules}}", body)} for adapter in adapters for snippet in adapter.snippets]
    notes = [f"{adapter.name}: {note}" for adapter in adapters for note in adapter.notes]
    return snippets, notes + ctx.notes


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
    skills: dict[str, list[dict[str, Any]]] = {}
    for item in plan["files"]:
        if item["op"] == "unchanged":
            continue
        if item["role"] == "skill" and item["op"] == "create":
            skills.setdefault(item["root"], []).append(item)
            continue
        print(f"{item['op'].capitalize()} {item['role']}: {item['path']}" + (" (original is backed up first)" if item["backup"] else ""))
        if "entry" in item:
            print("  entry: " + json.dumps(item["entry"], ensure_ascii=False))
        if "block" in item:
            print("  rules block:\n" + "\n".join("    " + line for line in item["block"].splitlines()))
    for root, items in skills.items():
        print(f"Create skill copies ({len(items)} files, JSON lists each): {root}")
    if plan["backupDir"]:
        print(f"Backups: {plan['backupDir']}")
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
            + (["--python", plan["interpreter"]] if args.python else []) + ["--apply", "--confirm", output["planHash"]]
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
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    if args.confirm is not None and not args.apply:
        parser.error("--confirm requires --apply")
    if args.apply and args.confirm is None:
        parser.error("--apply requires --confirm <planHash> from a reviewed preview")
    if not args.agents and (args.apply or args.remove or args.force or args.python):
        parser.error("--apply, --remove, --force and --python require at least one agent")
    if args.remove and args.python:
        parser.error("--python does not apply to --remove")
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
        ctx, planner, adapters = build_plan(names, remove=args.remove, force=args.force, python=args.python,
                                            home=home, config_file=config_file)
        core = core_plan(ctx, planner, names)
        plan_hash = plan_digest(core)
        if args.apply:
            if args.confirm != plan_hash:
                return fail("stale-plan", f"stale or unconfirmed plan; preview again (hash {plan_hash})")
            apply_plan(ctx, planner, plan_hash)
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
