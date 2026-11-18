"""Cursor adapter for `obsidian-second-brain connect` (docs/agents.md).

Locations and formats verified against Cursor's current documentation:
- MCP: `~/.cursor/mcp.json` holds user-wide servers under `mcpServers`; a stdio server has `type`, `command`,
  `args` and `env` (https://cursor.com/docs/mcp, "Using mcp.json", "STDIO server configuration",
  "Configuration locations").
- Skills: `~/.cursor/skills/<name>/SKILL.md` is a user-level skills directory (it also reads `~/.agents/skills`);
  optional `references/` folders are allowed (https://cursor.com/docs/skills, "Skill directories").
- Rules: User Rules are defined in Cursor's Customize -> Rules settings and are not stored on the file system
  (https://cursor.com/docs/rules, "User Rules"; the skills page repeats it), so there is no user-level rules
  file for `connect` to write. Project rules are `.cursor/rules/*.mdc` files with `description`/`alwaysApply`
  front matter (same page, "Project rules"); they live in a project repository, so they are printed only.
Detection is the config directory only; the `cursor` binary is never run.
"""
from connect_adapters.spec import FIXTURE_TESTED, Adapter, McpTarget, SkillsTarget, Snippet


def _entry(command: list[str], env: dict[str, str]) -> dict:
    return {"type": "stdio", "command": command[0], "args": command[1:], "env": env}


ADAPTER = Adapter(
    name="cursor",
    title="Cursor",
    detect=(".cursor",),
    mcp=McpTarget(path=".cursor/mcp.json", format="json", key_path=("mcpServers",), entry=_entry),
    rules=None,
    skills=SkillsTarget(path=".cursor/skills"),
    snippets=(
        Snippet(
            "Project rule (optional; the server's MCP instructions already carry this guidance)",
            ".cursor/rules/obsidian-second-brain.mdc",
            "---\n"
            "description: Use the obsidian-wiki MCP tools to read and change the Obsidian wiki\n"
            "alwaysApply: true\n"
            "---\n"
            "\n"
            "{{rules}}\n",
        ),
    ),
    notes=(
        "Cursor user rules live in its settings (Customize > Rules), not in a file, so connect writes no rules block; "
        "the MCP server's initialize instructions carry the wiki rules. Add the project rule snippet to a project only if you want them repeated there.",
        "Restart Cursor or refresh the MCP servers in Customize so it loads the new `obsidian-wiki` server; Cursor asks for approval before each MCP tool call.",
        "If you turn on 'Sync Skills for Cloud Agents' in Cursor, it moves ~/.cursor/skills into its synced storage; rerun `doctor` afterwards, because the local copies are then reported missing.",
    ),
    verification=FIXTURE_TESTED,
)
