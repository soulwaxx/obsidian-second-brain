"""OpenCode adapter for `obsidian-second-brain connect` (docs/agents.md).

Locations and formats verified against OpenCode's current official documentation and its source
(anomalyco/opencode, stable 1.19.6, which npm and GitHub mark "latest", and 2.3.2):
- Config: the global config directory is `~/.config/opencode` (`$XDG_CONFIG_HOME/opencode`, through xdg-basedir in
  packages/core/src/global.ts); it holds `opencode.json` or `opencode.jsonc` (https://opencode.ai/docs/config/,
  "Locations"). OpenCode merges `config.json`, `opencode.json` and `opencode.jsonc` there (jsonc last) and its own
  writes go to the first existing of `opencode.jsonc`, `opencode.json`, `config.json`; connect follows that order
  and so never creates a second global config next to an existing `opencode.jsonc`. `OPENCODE_CONFIG` and
  `OPENCODE_CONFIG_DIR` only add override layers on top of the global config and are not targets.
- MCP: a `local` server is `{"type": "local", "command": [argv...], "environment": {...}, "enabled": true}` under `mcp`
  (https://opencode.ai/docs/mcp-servers/, "Local"; the 1.x docs are `packages/web/src/content/docs/mcp-servers.mdx`
  at v1.19.6). The 2.x docs name servers under `mcp.servers` with `disabled`, but 2.x still reads this direct shape
  and migrates it (packages/core/src/config/normalize.ts, `normalizeMcp`), so one entry serves both lines.
- Rules: `~/.config/opencode/AGENTS.md` is the global rules file; `~/.claude/CLAUDE.md` is read only while it is
  absent (https://opencode.ai/docs/rules/, "Global" and "Claude Code Compatibility").
- Skills: `~/.config/opencode/skills/<name>/SKILL.md` (https://opencode.ai/docs/skills/, "Discovery"); OpenCode also
  reads `~/.claude/skills` and `~/.agents/skills`, and the OpenCode directory takes precedence over both.
- Approval: MCP tool calls are permission-checked as `<server>_<tool>` and default to allow
  (https://opencode.ai/docs/permissions/, "Defaults"), so the printed snippet adds an `ask` rule for the write tools.
Detection is the config directory only; the `opencode` binary is never run.
"""
import os
from pathlib import Path
from typing import Mapping

from connect_adapters.spec import FIXTURE_TESTED, Adapter, McpTarget, RulesTarget, SkillsTarget, Snippet


def _config_dir(home: Path, env: Mapping[str, str]) -> Path:
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"


def _config_file(home: Path, env: Mapping[str, str]) -> Path:
    # OpenCode's own order: an existing opencode.jsonc wins, so a comment-bearing file is refused with a snippet
    # instead of connect adding a second global config.
    jsonc = _config_dir(home, env) / "opencode.jsonc"
    return jsonc if os.path.lexists(jsonc) else _config_dir(home, env) / "opencode.json"


def _entry(command: list[str], env: dict[str, str]) -> dict:
    return {"type": "local", "command": command, "environment": env, "enabled": True}


ADAPTER = Adapter(
    name="opencode",
    title="OpenCode",
    detect=(_config_dir,),
    mcp=McpTarget(path=_config_file, format="json", key_path=("mcp",), entry=_entry),
    rules=RulesTarget(path=lambda home, env: _config_dir(home, env) / "AGENTS.md"),
    skills=SkillsTarget(path=lambda home, env: _config_dir(home, env) / "skills"),
    snippets=(
        Snippet(
            "Ask before the wiki write tools run (OpenCode allows MCP tool calls by default)",
            "the `permission` object of ~/.config/opencode/opencode.json (merge by hand; OpenCode 2.x reads this form too)",
            '{\n'
            '  "permission": {\n'
            '    "obsidian-wiki_wiki_batch_apply": "ask",\n'
            '    "obsidian-wiki_wiki_batch_recover": "ask"\n'
            '  }\n'
            '}\n',
        ),
    ),
    notes=(
        "OpenCode runs MCP tools without asking by default, so connect alone does not put a prompt in front of `wiki_batch_apply`. "
        "Add the printed `permission` rule (or an equivalent `permissions` rule in OpenCode 2.x) to keep wiki writes user-approved.",
        "connect writes the `mcp.obsidian-wiki` entry in the shape OpenCode 1.x documents; OpenCode 2.x still reads it and migrates it to `mcp.servers`. "
        "Existing `mcp.servers` entries are left as they are.",
        "OpenCode reads ~/.claude/CLAUDE.md only while ~/.config/opencode/AGENTS.md does not exist; once connect creates that file, "
        "move any guidance you keep in ~/.claude/CLAUDE.md into it.",
        "OpenCode also reads skills from ~/.claude/skills and ~/.agents/skills, and connect does not look there: same-named skills that another adapter "
        "or you placed in those directories can duplicate ours. OpenCode 2.x documents the ~/.config/opencode copy as taking precedence; 1.x asks for unique names, so remove one copy.",
        "Restart OpenCode so it loads the new `obsidian-wiki` server. A project `opencode.json`, `OPENCODE_CONFIG` or `OPENCODE_CONFIG_DIR` "
        "that defines its own `obsidian-wiki` entry overrides this one.",
    ),
    verification=FIXTURE_TESTED,
)
