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
from typing import Callable, Mapping

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


@dataclass(frozen=True)
class RulesTarget:
    """A user-level Markdown rules file that receives the marked `obsidian-second-brain` block."""
    path: Location
    # Text (for example YAML front matter) written before the marked block, only when
    # connect creates the file. It is recorded in the manifest: `--remove` deletes the file only when it still
    # holds just this header and the block, otherwise it removes only the block.
    header: str | None = None


@dataclass(frozen=True)
class SkillsTarget:
    """A user-level skills directory that receives the six portable skill folders as plain copies."""
    path: Location


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
