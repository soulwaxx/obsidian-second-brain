"""GitHub Copilot in VS Code adapter for `obsidian-second-brain connect`.

Every target below was checked against the current official documentation (see docs/agents.md):
- MCP entry: the VS Code user-profile `mcp.json` (`<User data>/mcp.json`), top-level `servers`, stdio entries with
  `type: "stdio"`. VS Code now calls this editor-managed format the Local harness's; the Copilot harness reads
  `~/.copilot/mcp-config.json` itself (the `copilot-cli` adapter's file) and takes eligible profile definitions by
  forwarding, which this entry is eligible for (no `${input:...}`). The User data folder is
  `%APPDATA%\\Code\\User`, `~/Library/Application Support/Code/User` or `~/.config/Code/User` (the docs state no
  XDG override, so none is honored; VS Code Insiders uses `Code - Insiders` and is not wired).
  https://code.visualstudio.com/docs/agents/reference/mcp-configuration
  https://code.visualstudio.com/docs/configure/settings#_settings-file-locations
- Rules: `<COPILOT_HOME or ~/.copilot>/instructions/*.instructions.md`, the Copilot harness's user instruction folder.
  The Local harness keeps user instructions in an undocumented profile folder that the VS Code maintainers say not to
  write by hand, so repository-level rules are printed as a snippet instead.
  https://code.visualstudio.com/docs/agents/reference/custom-instructions-reference#_user-instructions
- Skills: `~/.copilot/skills/` (personal skills, Copilot and Local harnesses), the same folder as `copilot-cli`.
  https://code.visualstudio.com/docs/agent-customization/agent-skills
Detection only looks at the User data folder; VS Code and `code` are never run.
"""
import sys
from pathlib import Path

from connect_adapters.copilot_cli import copilot_home
from connect_adapters.spec import Adapter, McpTarget, RulesTarget, SkillsTarget, Snippet


def _platform() -> str:
    return sys.platform


def user_data(home, env):
    """VS Code's user data folder for the default (stable) build."""
    platform = _platform()
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User"
    if platform == "win32":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "Code" / "User"
    return home / ".config" / "Code" / "User"


WORKSPACE_MCP = """{
  "servers": {
    "obsidian-wiki": {
      "type": "stdio",
      "command": "node",
      "args": ["<package root>/scripts/obsidian-second-brain.mjs", "mcp", "--tools", "all"],
      "env": { "PYTHON": "/path/to/python3.14" }
    }
  }
}
"""

ADAPTER = Adapter(
    name="copilot-vscode",
    title="GitHub Copilot in VS Code",
    detect=(user_data,),
    mcp=McpTarget(
        path=lambda home, env: user_data(home, env) / "mcp.json",
        format="json",
        key_path=("servers",),
        entry=lambda command, env: {"type": "stdio", "command": command[0], "args": command[1:], "env": env},
    ),
    rules=RulesTarget(path=lambda home, env: copilot_home(home, env) / "instructions" / "obsidian-second-brain.instructions.md",
                      header='---\napplyTo: "**"\n---'),
    skills=SkillsTarget(path=lambda home, env: copilot_home(home, env) / "skills"),
    snippets=(
        Snippet("Repository rules (also read by the VS Code Local harness)", ".github/copilot-instructions.md", "{{rules}}"),
        Snippet("Workspace MCP entry (optional; it holds machine-specific paths, so do not commit it)", ".vscode/mcp.json", WORKSPACE_MCP),
    ),
    notes=(
        "Reload the VS Code window, then start a new chat so it loads the MCP server, the instructions and the skills.",
        "VS Code asks before it calls an MCP tool unless you allow it (Allow all, Autopilot, or Always Allow for the server). Keep `wiki_batch_apply` on approval; do not trust the whole `obsidian-wiki` server.",
        "VS Code's profile `mcp.json` belongs to the Local harness, which VS Code deprecates (removal announced for February 2027); the Copilot harness reads `~/.copilot/mcp-config.json`, which `connect copilot-cli` wires.",
    ),
)
