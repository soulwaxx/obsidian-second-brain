"""OpenCode adapter for `obsidian-second-brain connect` (docs/agents.md).

Locations and formats verified against OpenCode's current official documentation and its source
(anomalyco/opencode). Two lines exist: V2 is published as `@opencode/cli` (npm `latest` 2.3.2) and the docs label V2 latest;
V1 is `opencode-ai` (1.19.6). One entry and one rule form serve both:
- Config: the global config directory is `~/.config/opencode` (`$XDG_CONFIG_HOME/opencode`, through xdg-basedir in
  packages/core/src/global.ts); it holds `opencode.json` or `opencode.jsonc` (https://opencode.ai/docs/config/,
  "Locations"). OpenCode merges `config.json`, `opencode.json` and `opencode.jsonc` there (jsonc last) and its own
  writes go to the first existing of `opencode.jsonc`, `opencode.json`, `config.json`; connect follows that order
  and so never creates a second global config next to an existing `opencode.jsonc`. `OPENCODE_CONFIG_DIR` replaces the
  global directory in V2 (`OPENCODE_CONFIG_DIR ?? Path.config`, packages/util/src/global.ts) and is an extra layer loaded
  after the global one in V1 (`opencode.json`/`opencode.jsonc` and skills from it, config.ts); when it is set and not empty
  it is the target for the config, AGENTS.md and skills, which both lines read from there (V1 reads the global AGENTS.md
  only from `~/.config/opencode`, session/instruction.ts, so there the rules block waits for V2; the MCP instructions carry
  the rules either way). `OPENCODE_CONFIG` is an override layer and not a target.
- MCP: a `local` server is `{"type": "local", "command": [argv...], "environment": {...}, "enabled": true}` under `mcp`
  (https://opencode.ai/docs/mcp-servers/, "Local"; the 1.x docs are `packages/web/src/content/docs/mcp-servers.mdx`
  at v1.19.6). The V2 docs name servers under `mcp.servers` with `disabled`, but V2 still reads this direct shape
  and migrates it (packages/core/src/config/normalize.ts, `normalizeMcp`; migration guide), so one entry serves both lines.
- Rules: `~/.config/opencode/AGENTS.md` is the global rules file; `~/.claude/CLAUDE.md` is read only while it is
  absent (https://opencode.ai/docs/rules/, "Global" and "Claude Code Compatibility"), so connect creates AGENTS.md only
  when `~/.claude/CLAUDE.md` does not exist (`create_unless`); otherwise it prints the block.
- Skills: `~/.config/opencode/skills/<name>/SKILL.md` (https://opencode.ai/docs/skills/, "Discovery"); OpenCode also
  reads `~/.claude/skills` and `~/.agents/skills` (where the Codex adapter installs the same six names), so a name found
  there is not installed a second time (`also_read`).
- Approval: MCP tool calls are permission-checked as `<server>_<tool>` and the default policy allows everything
  (https://opencode.ai/docs/permissions/, "Defaults"), so connect writes an `ask` rule for the write tools into the
  `permission` object (V1 shape). V2 folds `permission` into its ordered `permissions` list
  (normalize.ts: tool rules, then `permission`, then native `permissions`) and the last matching rule wins in both lines
  (V1: `fromConfig` keeps key order, `evaluate` uses `findLast`), so the new keys are appended at the end of the object.
Detection is the config directory only; the `opencode` binary is never run.
"""
import os
from pathlib import Path
from typing import Mapping

from connect_adapters.spec import FIXTURE_TESTED, SERVER_NAME, Adapter, ExtraEntry, McpTarget, RulesTarget, SkillsTarget


def _config_dir(home: Path, env: Mapping[str, str]) -> Path:
    if env.get("OPENCODE_CONFIG_DIR"):
        return Path(env["OPENCODE_CONFIG_DIR"])
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"


def _config_file(home: Path, env: Mapping[str, str]) -> Path:
    # OpenCode's own order: an existing opencode.jsonc wins, so a comment-bearing file is refused with a snippet
    # instead of connect adding a second global config.
    jsonc = _config_dir(home, env) / "opencode.jsonc"
    return jsonc if os.path.lexists(jsonc) else _config_dir(home, env) / "opencode.json"


def _entry(command: list[str], env: dict[str, str]) -> dict:
    return {"type": "local", "command": command, "environment": env, "enabled": True}


# An existing `ask` or `deny` for the same key is already safe and stays the user's.
_ASK = tuple(ExtraEntry(("permission",), f"{SERVER_NAME}_{tool}", "ask", accept=("ask", "deny"))
             for tool in ("wiki_batch_apply", "wiki_batch_recover"))

ADAPTER = Adapter(
    name="opencode",
    title="OpenCode",
    detect=(_config_dir,),
    mcp=McpTarget(path=_config_file, format="json", key_path=("mcp",), entry=_entry, extra=_ASK),
    rules=RulesTarget(path=lambda home, env: _config_dir(home, env) / "AGENTS.md", create_unless=".claude/CLAUDE.md"),
    skills=SkillsTarget(path=lambda home, env: _config_dir(home, env) / "skills", also_read=(".claude/skills", ".agents/skills")),
    notes=(
        "connect writes `ask` rules for `wiki_batch_apply` and `wiki_batch_recover` into the `permission` object, because OpenCode allows MCP tool calls by default. "
        "The last matching rule wins, so a rule of yours placed after them (or in a project config, an agent definition or a V2 `permissions` list) still overrides them; "
        "an existing `allow` for one of those keys is refused instead of overridden.",
        "connect writes the `mcp.obsidian-wiki` entry in the shape OpenCode 1.x documents; OpenCode 2.x (`@opencode/cli`) still reads it and migrates it to `mcp.servers`. "
        "Existing `mcp.servers` entries are left as they are.",
        "OpenCode reads ~/.claude/CLAUDE.md only while AGENTS.md does not exist in its config directory; connect therefore creates AGENTS.md only when no ~/.claude/CLAUDE.md exists. "
        "Creating one yourself later hides CLAUDE.md.",
        "OpenCode also reads skills from ~/.claude/skills and ~/.agents/skills: a skill name already there is not installed again, and the preview names the directory. "
        "Rerun connect after that copy goes away (doctor reports it).",
        "Restart OpenCode so it loads the new `obsidian-wiki` server. A project `opencode.json` or `OPENCODE_CONFIG` "
        "that defines its own `obsidian-wiki` entry overrides this one.",
    ),
    verification=FIXTURE_TESTED,
)
