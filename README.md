# obsidian-second-brain

Obsidian/OKF wiki skill, lifecycle hook, and Pi extension in one package for Claude Code and Pi. Adapted upstream material and license details are in [NOTICE.md](NOTICE.md) and [LICENSE](LICENSE). Hooks are active only inside the selected vault. Requires Python 3 with PyYAML, jq, and git; Obsidian CLI and vault retrieval scripts are optional.

## Install

- Pi: `pi install git:github.com/soulwaxx/obsidian-second-brain`
- Claude Code: `claude plugin marketplace add soulwaxx/obsidian-second-brain`, then `claude plugin install obsidian-second-brain@obsidian-second-brain --scope user`.

Copy `properties.example.json` to `~/.config/obsidian-second-brain/properties.json` and set `vaultPath` to an absolute vault directory. `null` disables the integration. The `features` booleans default to true when omitted: `guard` checks pre-write paths, `toc` injects the wiki index on session start, `autoCommit` commits validated page edits, and `retrievalRefresh` rebuilds the vault retrieval index at shutdown. `OBSIDIAN_AGENT_CONFIG` selects a different property file. An `OBSIDIAN_VAULT_PATH` environment variable can override the configured vault for hook invocations.

Claude hooks are bundled in `hooks/hooks.json`; Pi loads `extensions/obsidian.ts`. The `wiki` skill and middleware live under `skills/wiki`. Use `npm test` for lifecycle, middleware, and Pi guard checks; `claude plugin validate .` validates the Claude manifest.
