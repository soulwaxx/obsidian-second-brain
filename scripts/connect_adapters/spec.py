"""Declarative adapter interface for `obsidian-second-brain connect`.

An adapter is one module in this package that exposes `ADAPTER`, an `Adapter`
built from the pieces below. It only declares *where* an agent keeps its
user-level files and *how* its MCP entry is shaped; the framework in
`scripts/connect.py` does all reading, merging, hashing, backups, atomic writes,
rollback, manifest bookkeeping and doctor reporting. See docs/agents.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

LIVE_VALIDATED = "live-validated"
FIXTURE_TESTED = "fixture-tested (live-unverified)"
SNIPPET_ONLY = "snippet-only"
SERVER_NAME = "obsidian-wiki"

# A location is a path relative to the user's home directory, or a function
# `(home, env) -> Path` of the home directory and the process environment (use it for
# XDG, `CODEX_HOME` or other `<AGENT>_HOME` overrides, e.g.
# `lambda home, env: Path(env.get("CODEX_HOME") or home / ".codex") / "config.toml"`).
# It must return an absolute, user-level path; the framework refuses locations
# inside the selected vault and writes nothing outside the returned paths. Symlinked
# parent directories are resolved (the preview shows the real path); a symlinked
# final file is refused.
Location = str | Callable[[Path, Mapping[str, str]], Path]


def standard_entry(command: list[str], env: dict[str, str]) -> dict:
    """The common `mcpServers` shape: {command, args, env}. Override per agent."""
    return {"command": command[0], "args": command[1:], "env": env}


@dataclass(frozen=True)
class ExtraEntry:
    """One more value connect sets in the MCP config file (json or yaml), next to the server entry.

    `key_path` is the object that holds it and `name` its key (OpenCode: `("permission",)`,
    `"obsidian-wiki_wiki_batch_apply"`, `"ask"`). A new key is appended at the end of its object, which matters to agents
    whose rules are ordered (the last matching rule wins). An existing key whose value is not `value` is left alone when
    that value is in `accept` (the user's own, already as safe as ours: not owned, never removed) and otherwise refuses
    the agent with the exact snippet; a key connect wrote is replaced or removed only while it still matches the
    manifest hash. If the container at `key_path` is a string in `accept`, the intent already holds and nothing is written.
    """
    key_path: tuple[str, ...]
    name: str
    value: Any
    accept: tuple[Any, ...] = ()


@dataclass(frozen=True)
class McpTarget:
    """The MCP server entry the framework merges into one user-level config file."""
    path: Location
    format: str                                   # "json" (strict), "toml" or "yaml"
    key_path: tuple[str, ...]                     # object/table holding named servers, e.g. ("mcpServers",)
    name: str = SERVER_NAME                       # key of our entry inside that object
    # Build the entry from the argv list (`<absolute node> <root>/scripts/obsidian-second-brain.mjs mcp --tools all`)
    # and the env mapping (`PYTHON`, plus `OBSIDIAN_AGENT_CONFIG` when it is set at connect time). Any
    # JSON-like shape works: the command may stay an array and the env may use another key, for example
    # OpenCode's `{"type": "local", "command": command, "environment": env, "enabled": True}`.
    entry: Callable[[list[str], dict[str, str]], dict] = standard_entry
    # Extra values merged into the same file with the same set/remove/ownership rules (json and yaml only), each with
    # its own hash in the manifest. Use for settings that keep the entry safe, for example an approval rule.
    extra: tuple[ExtraEntry, ...] = ()


@dataclass(frozen=True)
class RulesTarget:
    """A user-level Markdown rules file that receives the marked `obsidian-second-brain` block."""
    path: Location
    # Text (for example YAML front matter) written before the marked block, only when
    # connect creates the file. It is recorded in the manifest: `--remove` deletes the file only when it still
    # holds just this header and the block, otherwise it removes only the block.
    header: str | None = None
    # A path that, when it exists while the rules file does not, stops connect from creating the rules file: the agent
    # reads that path only while the rules file is absent (OpenCode: `~/.claude/CLAUDE.md` versus `AGENTS.md`), so creating
    # the file would hide it. The preview prints the block as a snippet instead and records nothing. An existing rules
    # file is updated as always.
    create_unless: Location | None = None


@dataclass(frozen=True)
class SkillsTarget:
    """A user-level skills directory that receives the six portable skill folders as plain copies."""
    path: Location
    # Directories the agent also reads skills from. A skill name that a `<dir>/<name>/SKILL.md` already provides there is
    # not installed for this agent (the agent would list it twice): the preview names the directory, the manifest records
    # it (`skillsElsewhere`) and doctor reports it stale when that copy disappears. A connect copy of such a name in this
    # agent's own directory is removed on the next connect (a customized one is refused unless --force) and doctor reports
    # `skill-duplicated-elsewhere` until then; a copy connect never recorded is never touched.
    also_read: tuple[Location, ...] = ()


@dataclass(frozen=True)
class Snippet:
    """Project-level or manual content that connect prints and never writes.

    `destination` is advisory text for the user (for example `.cursor/rules/wiki.mdc`).
    The token `{{rules}}` in `content` is replaced by the shared rules block body.
    """
    title: str
    destination: str
    content: str


@dataclass(frozen=True)
class Adapter:
    name: str                                     # lowercase CLI id: `connect <name>`
    title: str                                    # display name
    detect: tuple[Location, ...]                  # config directories; any existing directory means "detected"
    mcp: McpTarget | None = None                  # None: no safe automatic target (state `manual-only`)
    rules: RulesTarget | None = None
    skills: SkillsTarget | None = None
    snippets: tuple[Snippet, ...] = ()
    notes: tuple[str, ...] = ()                   # manual steps printed with the preview
    # Names of adapters whose MCP entry reaches some of the same clients, so connecting both registers the server twice.
    # The preview carries a duplicate-registration note when this adapter is selected and one of them is selected or connected.
    duplicates_with: tuple[str, ...] = ()
    verification: str = FIXTURE_TESTED
