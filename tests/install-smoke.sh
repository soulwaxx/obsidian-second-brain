#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export HOME="$work/home"
export XDG_CONFIG_HOME="$work/xdg"
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
for resource in ("extensions/obsidian.ts", "skills/wiki/SKILL.md", "hooks/obsidian-session.sh", "agents/pi/wiki-vault.md"):
    assert (package / resource).is_file(), f"missing Pi-installed resource: {resource}"
print(f"Pi local package and resources verified: {package}")
PY

claude plugin marketplace add "$repo"
claude plugin install obsidian-second-brain@obsidian-second-brain --scope user --yes --json
python3 - "$CLAUDE_CONFIG_DIR/plugins/installed_plugins.json" "$repo" <<'PY'
import json, pathlib, sys
registry = json.loads(pathlib.Path(sys.argv[1]).read_text())
entry = registry["plugins"]["obsidian-second-brain@obsidian-second-brain"][0]
installed = pathlib.Path(entry["installPath"]).resolve()
repo = pathlib.Path(sys.argv[2]).resolve()
assert installed.is_dir(), f"Claude installed resource directory is missing: {installed}"
for resource in ("hooks/obsidian-session.sh", "skills/wiki/SKILL.md", "agents/claude/wiki-vault.md", ".claude-plugin/plugin.json"):
    assert (installed / resource).is_file(), f"missing Claude installed resource: {resource}"
print(f"Claude plugin install and resources verified: {installed}")
PY

# An outside-vault lifecycle invocation must not touch even the temporary vault.
config="$work/agent-config.json"
printf '{"vaultPath":"%s"}\n' "$work/vault" >"$config"
for hook in "$repo/hooks/obsidian-session.sh" "$work"/claude/plugins/cache/obsidian-second-brain/obsidian-second-brain/*/hooks/obsidian-session.sh; do
  test -f "$hook"
  before=$(find "$work/vault" -mindepth 1 -print | sort)
  (cd "$work/outside" && OBSIDIAN_AGENT_CONFIG="$config" bash "$hook" session-start)
  after=$(find "$work/vault" -mindepth 1 -print | sort)
  [[ "$before" == "$after" ]]
done
printf 'Outside-vault no-op verified for source and installed Claude lifecycle hooks.\n'
