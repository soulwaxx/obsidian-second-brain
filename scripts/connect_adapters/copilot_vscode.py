"""GitHub Copilot in VS Code adapter (deprecated Local harness) for `obsidian-second-brain connect`.

For VS Code's Copilot harness, connect `copilot-cli` instead: the Copilot harness reads `<COPILOT_HOME or ~/.copilot>/mcp-config.json`
itself (that adapter's file), so one connection covers Copilot CLI and VS Code. This adapter is for the Local harness, which VS Code
deprecates (removal announced for February 2027), when Copilot CLI discovery (`chat.mcp.discovery.enabled`, source `copilot`, on by
default) is off. With discovery on, Local also reads `~/.copilot/mcp-config.json`, and VS Code forwards eligible profile entries to the
Copilot harness, so connecting both registers the server twice; `duplicates_with` makes the preview say so.

Every target below was checked against the current official documentation (see docs/agents.md):
- MCP entry: the VS Code user-profile `mcp.json` (`<User data>/mcp.json`), top-level `servers`, stdio entries with `type: "stdio"`:
  the Local harness's editor-managed format. The User data folder is `%APPDATA%\\Code\\User`, `~/Library/Application Support/Code/User` or
  `~/.config/Code/User` (the docs state no XDG override, so none is honored; VS Code Insiders uses `Code - Insiders` and is not wired).
  https://code.visualstudio.com/docs/agents/reference/mcp-configuration
  https://code.visualstudio.com/docs/configure/settings#_settings-file-locations
- Rules: `<COPILOT_HOME or ~/.copilot>/copilot-instructions.md`, the Copilot harness's general user instructions file (the file
  `copilot-cli` also writes, so the framework writes the block once and removes it with the last owner). The Local harness keeps user
  instructions in the VS Code profile, created through the Agent Customizations editor, a location the docs do not name; connect
  never writes it, and the repository-level rules are printed as a snippet instead.
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
    rules=RulesTarget(path=lambda home, env: copilot_home(home, env) / "copilot-instructions.md"),
    skills=SkillsTarget(path=lambda home, env: copilot_home(home, env) / "skills"),
    snippets=(
        Snippet("Repository rules (also read by the VS Code Local harness)", ".github/copilot-instructions.md", "{{rules}}"),
    ),
    notes=(
        "For VS Code's Copilot harness connect `copilot-cli` instead: it reads `~/.copilot/mcp-config.json`, so one connection covers Copilot CLI and VS Code. Use `copilot-vscode` only for the deprecated Local harness (removal announced for February 2027) when Copilot CLI discovery is off (`chat.mcp.discovery.enabled`, source `copilot`); with it on, Local also reads `~/.copilot/mcp-config.json`, so do not connect both.",
        "Reload the VS Code window, then start a new chat so it loads the MCP server, the instructions and the skills.",
        "The rules block goes to `copilot-instructions.md` in the Copilot configuration directory, which the Copilot harness reads. The Local harness keeps user instructions in the VS Code profile (create them in the Agent Customizations editor; connect does not write that location), so for Local paste the rules there or use the repository snippet.",
        "`COPILOT_HOME` moves the rules and skills only when VS Code's own environment has it; an editor launched from the Dock or Finder does not inherit your shell's variables, so it reads `~/.copilot`. Whether VS Code honors it is not live-verified (#72).",
        "VS Code asks before it calls an MCP tool unless you allow it (Allow all, Autopilot, or Always Allow for the server). Keep `wiki_batch_apply` on approval; do not trust the whole `obsidian-wiki` server.",
    ),
    duplicates_with=("copilot-cli",),
)
