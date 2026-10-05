#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export HOME="$work/home"
export XDG_CONFIG_HOME="$work/xdg"
export OBSIDIAN_AGENT_CONFIG="$work/agent-config.json"
unset OBSIDIAN_VAULT_PATH WIKI_MIDDLEWARE_DIR
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$work/vault" "$work/outside"

# Both package managers are redirected to temporary state; no user config is read/written.
export PI_CODING_AGENT_DIR="$work/pi-agent"
export CLAUDE_CONFIG_DIR="$work/claude"
pi install "$repo" --no-approve

python3 - "$PI_CODING_AGENT_DIR/settings.json" "$PI_CODING_AGENT_DIR" "$repo" <<'PY'
import json, pathlib, sys
settings = pathlib.Path(sys.argv[1])
agent_dir = pathlib.Path(sys.argv[2])
repo = pathlib.Path(sys.argv[3]).resolve()
data = json.loads(settings.read_text())
packages = data.get("packages", [])
assert len(packages) == 1, f"unexpected Pi package declarations: {packages!r}"
# Keep this lexical: macOS /var is a symlink to /private/var, and resolving
# before applying ../ components incorrectly redirects an otherwise valid path.
package = pathlib.Path(__import__("os").path.abspath(agent_dir / packages[0]))
assert package == repo, f"Pi package resolved to {package}, expected {repo}"
for resource in ("extensions/obsidian.ts", "skills/wiki/SKILL.md", "hooks/obsidian-session.sh", "scripts/wiki_lifecycle.py", "agents/pi/wiki-vault.md"):
    assert (package / resource).is_file(), f"missing Pi-installed resource: {resource}"
print(f"Pi local package and resources verified: {package}")
PY

node "$repo/tests/npm-plugin.mjs" "$work"
IFS= read -r installed < "$work/claude-installed-path"

# Startup from another cwd supplies a locator without touching the temporary vault.
config="$work/agent-config.json"
printf '{"vaultPath":"%s"}\n' "$work/vault" >"$config"
for hook in "$repo/hooks/obsidian-session.sh" "$installed/hooks/obsidian-session.sh"; do
  test -f "$hook"
  before=$(find "$work/vault" -mindepth 1 -print | sort)
  locator=$(cd "$work/outside" && OBSIDIAN_AGENT_CONFIG="$config" bash "$hook" start)
  [[ "$locator" == *"Configured Obsidian wiki:"* ]]
  denied=$(cd "$work/outside" && printf '{"tool_name":"Write","tool_input":{"file_path":"%s/wiki/index.md"}}\n' "$work/vault" | bash "$hook" prewrite)
  printf '%s\n' "$denied" | jq -e '.hookSpecificOutput.permissionDecision == "deny"' >/dev/null
  after=$(find "$work/vault" -mindepth 1 -print | sort)
  [[ "$before" == "$after" ]]
done
printf 'Cross-directory locator, no startup writes, and destination guard verified for source and installed Claude hooks.\n'
