"""Codex CLI adapter for `obsidian-second-brain connect` (#66).

Targets, checked against OpenAI's Codex documentation on 2026-11-18 (live-unverified; #72 runs the real agent):

* MCP entry: `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`), table `[mcp_servers.<name>]` with
  `command`, `args` and `env` (stdio servers). https://developers.openai.com/codex/mcp and
  https://developers.openai.com/codex/config-basic. An empty `CODEX_HOME` counts as unset
  (https://github.com/openai/codex/blob/9a8730f3/codex-rs/utils/home-dir/src/lib.rs). The framework appends the table
  inside comment markers and re-parses the file, so Codex's other settings are untouched.
* Rules block: `$CODEX_HOME/AGENTS.md`, Codex's global instructions
  (https://developers.openai.com/codex/guides/agents-md). A non-empty `AGENTS.override.md` in the same
  directory replaces it, which the notes below tell the user about.
* Skill copies: `~/.agents/skills`, Codex's user skills location (`$HOME/.agents/skills`; it is not under
  `CODEX_HOME`): https://developers.openai.com/codex/skills.

Detection looks only at the Codex home directory; the `codex` binary is never run. Hooks and plugins are out of scope.
"""
from pathlib import Path

from connect_adapters.spec import Adapter, FIXTURE_TESTED, McpTarget, RulesTarget, SkillsTarget, Snippet


def codex_home(home, env):
    return Path(env.get("CODEX_HOME") or home / ".codex")


ADAPTER = Adapter(
    name="codex",
    title="Codex CLI",
    detect=(codex_home,),
    mcp=McpTarget(path=lambda home, env: codex_home(home, env) / "config.toml", format="toml", key_path=("mcp_servers",)),
    rules=RulesTarget(path=lambda home, env: codex_home(home, env) / "AGENTS.md"),
    skills=SkillsTarget(path=".agents/skills"),
    snippets=(
        Snippet("Project rules (optional)", "AGENTS.md at the root of a project that should always use the wiki",
                "{{rules}}"),
    ),
    notes=(
        "Restart Codex after connecting: it reads config.toml and AGENTS.md when a session starts.",
        "Codex reads only the first non-empty global instructions file: if $CODEX_HOME/AGENTS.override.md exists "
        "and is not empty, it replaces AGENTS.md, so add the rules block there by hand.",
        "The skill copies go to ~/.agents/skills (Codex's user skills directory, not under CODEX_HOME); "
        "other agents that read that directory share them.",
    ),
    verification=FIXTURE_TESTED,
)
