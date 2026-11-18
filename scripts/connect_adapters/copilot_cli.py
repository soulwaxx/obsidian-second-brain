"""GitHub Copilot CLI adapter for `obsidian-second-brain connect`.

Every target below was checked against the current official documentation (see docs/agents.md):
- MCP entry: `<COPILOT_HOME or ~/.copilot>/mcp-config.json`, top-level `mcpServers`; a local server needs `command`,
  `args` and `tools` (`["*"]` makes every tool available; it does not bypass approvals); `type` is `local` or `stdio`.
  https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
  https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#mcp-server-configuration
- Rules: `<COPILOT_HOME or ~/.copilot>/copilot-instructions.md` (user-level instructions, no front matter needed).
  https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions
- Skills: `~/.copilot/skills/` (personal skills). The docs name `COPILOT_HOME` as the override of the whole
  configuration directory, which lists `skills/`; the skills pages themselves only spell out `~/.copilot/skills`.
  https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills
  https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
Detection only looks at the configuration directory; the `copilot` binary is never run.
"""
from pathlib import Path

from connect_adapters.spec import Adapter, McpTarget, RulesTarget, SkillsTarget


def copilot_home(home, env):
    """`$COPILOT_HOME`, else `~/.copilot` (the CLI's configuration and state directory)."""
    return Path(env.get("COPILOT_HOME") or home / ".copilot")


ADAPTER = Adapter(
    name="copilot-cli",
    title="GitHub Copilot CLI",
    detect=(copilot_home,),
    mcp=McpTarget(
        path=lambda home, env: copilot_home(home, env) / "mcp-config.json",
        format="json",
        key_path=("mcpServers",),
        entry=lambda command, env: {"type": "local", "command": command[0], "args": command[1:], "env": env, "tools": ["*"]},
    ),
    rules=RulesTarget(path=lambda home, env: copilot_home(home, env) / "copilot-instructions.md"),
    skills=SkillsTarget(path=lambda home, env: copilot_home(home, env) / "skills"),
    notes=(
        "Start a new Copilot CLI session so it loads the MCP server, the instructions and the skills (`/skills reload` loads skills in a running session).",
        "VS Code's Copilot harness reads this same `mcp-config.json`, `copilot-instructions.md` and skills folder, so this one connection also covers VS Code; `connect copilot-vscode` is only for the deprecated Local harness and would register the server twice if Copilot CLI discovery is on.",
        "`COPILOT_HOME` reaches VS Code only when VS Code's own environment has it; an editor launched from the Dock or Finder does not inherit your shell's variables, so it reads `~/.copilot`. Whether VS Code honors it is not live-verified (#72).",
        "Copilot CLI asks before it calls an MCP tool unless you allow it (`--allow-all`, `--allow-tool` or a saved approval). Keep `wiki_batch_apply` on approval; do not allow the whole `obsidian-wiki` server.",
    ),
    duplicates_with=("copilot-vscode",),
)
