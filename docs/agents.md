# Connect agents to the wiki

`obsidian-second-brain connect` wires an agent to the wiki the way setup scaffolds a vault: it previews every file change with a plan hash, applies only the plan you confirm, and can undo it. It is for agents that have no package plugin: they get the [MCP server](mcp.md) (`--tools all`), a marked rules block, and the six portable skills as plain copies. Claude Code (plugin) and Pi/omp (package) do not need it.

## Support status

| Agent | Mechanism | Status |
| --- | --- | --- |
| Claude Code | Plugin: hooks, skills, inline read-only MCP server | live-validated (the Claude contract tests run the real CLI when it is installed; see [the MCP guide](mcp.md#verification-status)) |
| Pi | Package: extension with native `wiki_*` tools, hooks, skills | live-validated (`npm run test:pi-runtime`) |
| Codex CLI | `connect` adapter (planned, #66): MCP entry in TOML, rules, skills | planned adapter |
| GitHub Copilot CLI (`copilot-cli`) | `connect` adapter: MCP entry in `~/.copilot/mcp-config.json`, rules, skills ([details](#github-copilot)) | fixture-tested (live-unverified) |
| GitHub Copilot in VS Code (`copilot-vscode`) | `connect` adapter: MCP entry in the VS Code user-profile `mcp.json`, instructions file with `applyTo` front matter, skills ([details](#github-copilot)) | fixture-tested (live-unverified) |
| Cursor | `connect` adapter (planned, #68): MCP entry, skills; project rules as a printed snippet | planned adapter |
| OpenCode | `connect` adapter (planned, #69): MCP entry, rules, skills | planned adapter |
| Gemini CLI, Cline, Zed, Kiro, Continue, Claude Desktop, Windsurf, any other MCP client | [Generic MCP snippet](#generic-mcp-snippets) | snippet-only |

There are exactly three statuses. `planned adapter` in the table is a temporary row label for an adapter whose ticket (#66–#69) has not landed, not a fourth status: it becomes `fixture-tested (live-unverified)` when that adapter's tests pass.

Statuses mean:

- **live-validated**: a real client was run against the package and the result is recorded.
- **fixture-tested (live-unverified)**: the adapter's file changes are covered by tests in a temporary `HOME` against synthetic files; the real agent has not been run. Issue #72 records the live runs.
- **snippet-only**: no adapter and no test; you paste the generic snippet by hand.

omp runs the Pi extension; its runtime behavior is unverified. Windows paths are not verified: adapters may return Windows locations, but `connect` is tested on macOS and Linux only.

## Use `connect`

```sh
obsidian-second-brain connect                       # list agents; writes nothing
obsidian-second-brain connect <agent>...            # preview the exact changes and a plan hash
obsidian-second-brain connect <agent>... --apply --confirm <planHash>
obsidian-second-brain connect <agent>... --remove   # preview the removal; add --apply --confirm to apply it
```

`connect` must be the first argument. The plan hash is the setup command's own (`bootstrap-vault.py`'s digest). It binds the exact agent selection, `--force`/`--remove`, the package root, interpreter, node and version, the selected config, and the before/after digest of every file. A different selection, an edit to any target file after the preview, or a different package makes `--apply` refuse with `stale or unconfirmed plan; preview again (hash …)`, and nothing is written. Add `--json` for machine-readable output (`planHash`, `plan.files[]` with `role`, `path`, `op` of `create`/`update`/`delete`/`unchanged`, and the entry or block that will be written).

| Option | Meaning |
| --- | --- |
| `--python PATH` | Interpreter to pin in the MCP entries. Default: the interpreter running `connect` (the one your `obsidian-second-brain` command uses). It must be Python 3.14 or newer and import `yaml`; one without `markdown-it-py` is accepted with a note, because `wiki_outline` and `wiki_read` need it. The path is stored as given, so a virtual environment stays pinned. |
| `--node PATH` | node executable to pin in the MCP entries. Default: the node that runs the `obsidian-second-brain` launcher (it passes its `process.execPath` as `OSB_NODE`), else `node` on `PATH`. It must be an executable that answers `--version` with a Node.js version. The path is stored as given. |
| `--force` | Replace customized skill copies after backing them up (see below). |
| `--remove` | Plan or apply the removal of what `connect` wrote. |

The config is selected as for setup: `OBSIDIAN_AGENT_CONFIG`, else `~/.config/obsidian-second-brain/properties.json`. When `OBSIDIAN_AGENT_CONFIG` is set, the MCP entry carries it in its `env` so the server selects the same config.

### What it writes

Per agent, only user-level files, never inside the vault or a project repository:

1. **MCP entry** `obsidian-wiki`: `<absolute node> <absolute package root>/scripts/obsidian-second-brain.mjs mcp --tools all` with `env.PYTHON` set to the pinned interpreter. No `npx`, so starting the agent makes no network call and runs the reviewed installed copy, and an agent that starts with a minimal `PATH` still finds node. The node is `--node`, else the one that runs the launcher; when that launcher node is the same executable as the `node` on `PATH`, the `PATH` path is pinned instead (unresolved, for example `/opt/homebrew/bin/node` rather than the version-specific `/opt/homebrew/Cellar/node@24/<version>/bin/node`), so a Homebrew upgrade does not break the entry. If node still moves, `doctor` reports `node-missing` and rerunning `connect` repairs it.
2. **Rules block** between `<!-- obsidian-second-brain:start -->` and `<!-- obsidian-second-brain:end -->` in the agent's user-level rules file: the wiki locator, the rule to use the wiki MCP tools and never edit vault files directly, and the inspect → approval → apply flow. Text outside the markers is never touched. An adapter may declare a `header` (for example the `applyTo` front matter VS Code instruction files need); it is written before the block only when `connect` creates the file (also when the file was deleted after an earlier connect, so `--remove` then deletes it again).
3. **Skill copies**: the six portable skills (`wiki`, `wiki-query`, `wiki-save`, `wiki-ingest`, `wiki-research`, `wiki-health`) as `<skills dir>/<name>/SKILL.md` plus `<skills dir>/wiki/references/*.md`, never `wiki/scripts/`. They are MCP-only skills (see `skills/wiki/references/tool-surface.md`).
4. **Install manifest** `connect-manifest.json` next to the selected integration config (outside the vault).
5. **Backups** of any config or rules file that changes on connect and of customized skill copies that `--force` replaces or removes, under `connect-backups/<planHash prefix>/<agent>/` next to the manifest (directories 0700, files 0600), with a `backup-index.json` per agent that maps each backup to its original path. Backups are kept for as long as the agent stays connected; a successful `--remove` deletes that agent's earlier connect-time backups. The `--force` copies of customized skills are different: they are the only copy of text you wrote, so they go in `<planHash prefix>/<agent>/forced/` (with their own `backup-index.json`), pruning never touches them, and the preview (`plan.forcedBackupDirs`, and a line in the human output) names the directory. Delete them by hand when you no longer need them.

Project-level files are never written. The preview prints them as snippets instead (for example a Cursor `.mdc` rule or `.github/copilot-instructions.md`), together with each adapter's manual notes.

### Write rules

- **Merge only our entry.** Other servers, keys and settings are preserved. JSON is parsed strictly and rewritten with the file's detected indent (layout such as indentation and line breaks is normalized); a file with comments (JSONC), trailing commas, duplicate keys, or any value whose text a rewrite would change (numbers such as `1e5`, `1.50` or `-0`, `NaN`/`Infinity`, string escapes such as `\u00e9` or `\/`) is refused. TOML is never rewritten: our table is appended inside `# obsidian-second-brain:start`/`end` comment markers (replaced or removed by those markers), and the result is parsed again to prove that every other setting is unchanged. YAML is merged with PyYAML only when the file has no comments, anchors, aliases, merge keys or duplicate keys and no plain scalar that a rewrite would respell: YAML 1.1 booleans such as `yes` or `On`, and other non-string values whose dumped text differs (`017`, `0x1F`, `1_000`, `~`, an empty value); quoting and flow style are normalized. Any refusal prints the exact snippet to add by hand and writes nothing for the whole selection.
- **Our entry only.** An existing `obsidian-wiki` entry that differs from ours is refused unless the manifest shows `connect` wrote it and it is unmodified (an upgrade); a modified entry is never removed either.
- **Safe writes.** Each file is written to a temporary file in its directory, flushed, renamed, and re-read to verify. The destination is re-checked just before each write, so an edit made after the preview is refused (and the whole apply rolls back). A symlinked final file is refused. A symlinked parent directory (for example `~/.codex` pointing into a dotfiles repository) is followed: the preview and plan hash show the real path (with `requestedPath` for the path the agent uses), the temporary file and rename happen in that real directory, and retargeting the link after the preview makes the plan stale. A symlink inside the skills directory is refused.
- **Rollback.** If anything fails, every file already written is restored, created directories are removed, and a file edited in the meantime is left as the user wrote it and reported.
- **Customized skill copies.** A destination file that differs from the shipped one is overwritten only when its hash still matches the one the manifest recorded (an older, unmodified copy). Otherwise `connect` refuses and names the files; `--force` backs them up first. Removing customized copies needs `--force` too. Files that two connected agents share (for example one skills directory) stay until the last agent using them is removed.
- **Adopted items.** An MCP entry or skill file that is already identical to what `connect` would write, and that no manifest record (and no earlier agent in the same plan) owns, is recorded with `adopted` and left in place by `--remove`, which reports it. Only what `connect` itself wrote is removed.
- **Shared MCP entries.** Agents whose adapters point at the same file and entry name share it. The second agent records the entry without `adopted` (it inherits the first owner's `adopted` and `createdFile`), removing one agent leaves the entry for the others, and the last agent removed deletes it (and the file, if `connect` created it).
- **Repeat runs.** Applying a plan that changes nothing writes nothing, not even the manifest.

### Manifest

`<config dir>/connect-manifest.json` (mode 0600, no timestamps):

```json
{
  "schemaVersion": 1,
  "agents": {
    "<agent>": {
      "version": "<package version>",
      "packageRoot": "<absolute package root>",
      "interpreter": "<absolute interpreter>",
      "node": "<absolute node>",
      "profile": "all",
      "mcp": { "path": "…", "format": "json|toml|yaml", "keyPath": ["mcpServers"], "name": "obsidian-wiki", "entrySha256": "…", "createdFile": false, "adopted": true },
      "rules": { "path": "…", "blockSha256": "…", "createdFile": false, "header": "" },
      "skills": { "root": "…", "files": { "wiki/SKILL.md": "<sha256>", "wiki/references/tool-surface.md": "<sha256>" }, "adopted": ["wiki/SKILL.md"] }
    }
  }
}
```

`adopted` appears only when true or non-empty; `header` is the text `connect` wrote before the rules block (empty when the file already existed). Paths are the real paths. Every record is validated when the manifest is read; a manifest that is not valid JSON, has another schema version or has a malformed record is `manifest-invalid`: `doctor` reports it, and `connect` (apply and remove) refuses and writes nothing until you fix or delete the file by hand.

## Doctor and stale connections

`doctor` (and the MCP `wiki_doctor` tool) report a `connect` section, additive to the readiness schema, and write nothing:

```json
"connect": {
  "manifest": { "path": "…", "exists": true, "error": null, "code": null },
  "agents": { "<agent>": { "title": "…", "state": "connected", "detected": true, "verification": "…", "adapterRegistered": true, "version": "…", "interpreter": "…", "node": "…", "reasons": [] } },
  "adapterErrors": [],
  "nextActions": []
}
```

| State | Meaning |
| --- | --- |
| `connected` | Recorded in the manifest and everything still matches. |
| `newer-package-available` | Advisory, not a failure and not `stale`: everything recorded still works, but the install that runs doctor has a different package root from the recorded one and a newer version than the package at the recorded root. `reasons[0]` says which; the next action `reconnect-newer-package` previews a reconnect with this install. |
| `stale` | Recorded, but something drifted. `reasons[].code` is one or more of `package-root-missing` (the recorded package no longer contains the launcher, for example after a move or uninstall), `version-mismatch` (the package at the recorded root is not the version that was connected, for example after an upgrade in place), `interpreter-missing`, `interpreter-unsuitable` (the pinned interpreter, run read-only with a 5 s timeout and checked once per interpreter per doctor run, is older than Python 3.14, cannot import `yaml`, or did not answer), `node-missing`, `mcp-entry-missing`, `mcp-entry-modified`, `mcp-config-unreadable`, `rules-block-missing`, `rules-block-modified`, `skill-missing`, `skill-modified` (hash differs from the recorded one), `skill-outdated` (the installed package ships a newer copy). |
| `absent` | Not connected. `detected` says whether the agent's config directory exists. |
| `manual-only` | `connect` cannot wire it safely: the adapter has no user-level MCP target, or the detected config cannot be merged (`reasons[0].code` says why). Use the printed snippet. |

Doctor never repairs. `stale` is judged against the recorded root, never against the install that runs doctor: the plugin cache and an npm global install can both exist, and the entry runs the recorded root (`version-mismatch` compares the recorded version with the package at that root). Only a different, newer doctor install yields the advisory `newer-package-available`; an older or equal one changes nothing. For a `stale` agent it adds a `reconnect-agent` next action (a preview command that is not run for you); it ends with `--python <recorded interpreter>` while that interpreter still works and `--node <recorded node>` while that node exists. Rerun `connect <agent>`, review the update, and apply its plan hash; rerunning from the new package location fixes a moved or upgraded install. The human report adds a `Connect <agent>: <state>` line for connected, stale, manual-only and detected agents.

## Adapters

An adapter is one file, `scripts/connect_adapters/<agent>.py`, that declares where an agent keeps its user-level files and how its MCP entry looks. It contains no file-writing code: reading, merging, hashing, backups, atomic writes, rollback, the manifest and doctor all live in `scripts/connect.py` and work for any adapter.

```python
from connect_adapters.spec import (Adapter, McpTarget, RulesTarget, SkillsTarget, Snippet, FIXTURE_TESTED)

ADAPTER = Adapter(
    name="example",                    # lowercase CLI id: `connect example`
    title="Example Agent",
    detect=(".example",),              # config directories (HOME-relative or functions); any existing one means "detected"
    mcp=McpTarget(
        path=".example/mcp.json",      # HOME-relative, or a function (home, env) -> Path (XDG, <AGENT>_HOME)
        format="json",                 # "json" (strict), "toml" or "yaml"
        key_path=("mcpServers",),      # object/table that holds the named servers
        name="obsidian-wiki",          # key of our entry (default)
        entry=lambda command, env: {"command": command[0], "args": command[1:], "env": env},  # default shape
    ),
    rules=RulesTarget(path=".example/AGENTS.md",      # user-level rules file; None to skip
                      header=None),                   # optional text written before the block when connect creates the file
    skills=SkillsTarget(path=".example/skills"),      # user-level skills directory; None to skip
    snippets=(Snippet("Project rules", ".example/rules/wiki.md", "{{rules}}"),),  # printed, never written
    notes=("Restart Example after connecting.",),     # manual steps printed with the preview
    verification=FIXTURE_TESTED,       # default; the table in this file and doctor show it
)
```

Fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `name`, `title` | yes | CLI id (`[a-z][a-z0-9-]*`, unique) and display name. |
| `detect` | yes | Locations (HOME-relative strings or `(home, env) -> Path` functions) whose existence as a directory marks the agent as detected. Detection never runs an agent binary. |
| `mcp` | no | `McpTarget`. Without it the agent is `manual-only`. `entry(command, env)` receives `command` = `["<absolute node>", "<root>/scripts/obsidian-second-brain.mjs", "mcp", "--tools", "all"]` and `env` = `{"PYTHON": …}` (plus `OBSIDIAN_AGENT_CONFIG` when set) and returns the entry in the agent's own shape: the command may stay an array and the env may use another key, for example OpenCode's `{"type": "local", "command": command, "environment": env, "enabled": True}`, or VS Code's `{"type": "stdio", "command": command[0], "args": command[1:], "env": env}`. Values must be strings, booleans, integers or lists/tables of them. |
| `rules` | no | `RulesTarget(path, header=None)`: the user-level rules file that receives the marked block. `header` (for example `---\napplyTo: "**"\n---`) is written before the block only when `connect` creates the file; the manifest records it so `--remove` deletes the file only when it holds nothing but header and block, and otherwise removes just the block. |
| `skills` | no | `SkillsTarget(path)`: the user-level skills directory that receives the six skill folders. |
| `snippets` | no | `Snippet(title, destination, content)` for project-level or manual content. `{{rules}}` expands to the shared rules text. |
| `notes` | no | Manual notes. |
| `verification` | no | `LIVE_VALIDATED`, `FIXTURE_TESTED` (default) or `SNIPPET_ONLY`. |

Location functions receive the home directory and a copy of the process environment, so `CODEX_HOME`, `XDG_CONFIG_HOME` and similar overrides are a one-liner: `path=lambda home, env: Path(env.get("CODEX_HOME") or home / ".codex") / "config.toml"`. Every returned path must be absolute and user-level; the framework refuses anything inside the selected vault, a symlinked final file, and files it cannot merge, and follows symlinked parent directories as described above. One adapter has one MCP target: an agent family with two config files (Copilot CLI and VS Code) is two adapters.

To register an adapter, uncomment (or add) its single line (the commented lines are `codex`, `copilot_cli`, `copilot_vscode`, `cursor` and `opencode`) in `scripts/connect_adapters/__init__.py`:

```python
REGISTRY: list[str] = [
    "connect_adapters.example",
]
```

Separate comment lines keep each adapter's edit on its own line, so adapter changes merge without conflicts. A module that fails to import or has an invalid `ADAPTER` is reported by `connect` and, as `adapterErrors`, by doctor.

Test an adapter with `tests/connect_fixture_adapter.py` and `tests/test_connect.py` as the model: run the real `connect` code against a temporary `HOME` and `OBSIDIAN_AGENT_CONFIG`, assert the exact paths, entry shape, preservation of unrelated entries, preview-without-writes, apply, repeat no-op, removal, and a doctor round trip. Never touch the real home directory or run the agent. When the tests pass, change the agent's row in the table above and in the README from "planned adapter" to "fixture-tested (live-unverified)"; live validation (#72) is a separate step. `tests/connect_fixture_adapter.py` has fixtures for each target shape (`fixture-custom`: environment-dependent location functions, an OpenCode-like entry and a rules header).

## GitHub Copilot

Copilot is two adapters, because one adapter has one MCP file: `connect copilot-cli` and `connect copilot-vscode`. Each is previewed, applied and removed on its own, and both can be selected together. Both are fixture-tested (live-unverified): `tests/test_connect_copilot.py` runs them in a temporary `HOME`; neither the `copilot` binary nor VS Code is run, and detection only checks that the configuration directory exists. Paths and keys were checked against the official documentation in November 2026 (live runs are recorded by #72).

| | `copilot-cli` | `copilot-vscode` |
| --- | --- | --- |
| Detected by | `$COPILOT_HOME`, else `~/.copilot` | VS Code user data folder: `~/Library/Application Support/Code/User` (macOS), `~/.config/Code/User` (Linux), `%APPDATA%\Code\User` (Windows, untested) |
| MCP file | `<COPILOT_HOME or ~/.copilot>/mcp-config.json`, key `mcpServers` | `<user data>/mcp.json`, key `servers` |
| MCP entry | `{"type": "local", "command": <node>, "args": [<root>/scripts/obsidian-second-brain.mjs, "mcp", "--tools", "all"], "env": {"PYTHON": …}, "tools": ["*"]}` | `{"type": "stdio", "command": <node>, "args": […], "env": {"PYTHON": …}}` |
| Rules file | `<COPILOT_HOME or ~/.copilot>/copilot-instructions.md`, no front matter | `<COPILOT_HOME or ~/.copilot>/instructions/obsidian-second-brain.instructions.md`, with `---`, `applyTo: "**"`, `---` written only when `connect` creates the file |
| Skills | `<COPILOT_HOME or ~/.copilot>/skills/` | the same directory, shared with `copilot-cli` |
| Printed snippets | none (user-level rules are written) | `.github/copilot-instructions.md` (the rules) and an optional `.vscode/mcp.json` entry, never written |

`env` also carries `OBSIDIAN_AGENT_CONFIG` when that variable selects a non-default config; without it `env` holds only `PYTHON`. `tools: ["*"]` makes every tool of the server available and, per the VS Code documentation, does not bypass approvals.

Sources:

- Copilot CLI MCP: <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers> and <https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#mcp-server-configuration> (`mcpServers`; local servers take `type` `local` or `stdio`, `command`, `args` and a required `tools`; `env` supports `$VAR` expansion).
- Copilot CLI instructions: <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions> (`$HOME/.copilot/copilot-instructions.md` and `$HOME/.copilot/instructions/**/*.instructions.md`; `COPILOT_HOME` replaces `$HOME/.copilot` for both).
- Copilot CLI skills and directory: <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills> (`~/.copilot/skills/`) and <https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference> (`COPILOT_HOME` overrides the whole configuration directory, which lists `skills/`). The skills pages themselves do not spell out `COPILOT_HOME`; `connect` applies it to skills by that directory reference.
- VS Code MCP: <https://code.visualstudio.com/docs/agents/reference/mcp-configuration> (`servers`, `type: "stdio"`) and <https://code.visualstudio.com/docs/configure/settings#_settings-file-locations> (the user data folder per OS; the profile `mcp.json` sits in it, and **MCP: Open User Configuration** opens it).
- VS Code instructions and skills: <https://code.visualstudio.com/docs/agents/reference/custom-instructions-reference#_user-instructions> (`~/.copilot/instructions/**/*.instructions.md`, `applyTo`) and <https://code.visualstudio.com/docs/agent-customization/agent-skills> (`~/.copilot/skills/`).

What the documentation changes about "VS Code":

- VS Code now has a **Copilot harness** and a deprecated **Local harness** (removal announced for February 2027). The Copilot harness reads `~/.copilot/mcp-config.json` itself, the file `copilot-cli` wires, so a `copilot-cli` connection already reaches VS Code's Copilot harness. The profile `mcp.json` with `servers` is the Local harness's editor-managed format; VS Code forwards eligible entries from it to the Copilot harness (ours has no `${input:...}`), so the `copilot-vscode` entry works in both. Plan for `copilot-vscode` to become redundant once the Local harness is gone.
- The Local harness keeps user instructions in a profile folder that the documentation does not name (the VS Code team says that folder is internal), so `connect` writes no user instructions there. The Copilot harness's documented user instruction folder is used instead, and the repository-level `.github/copilot-instructions.md` (read by both harnesses) is printed as a snippet for you to paste into a repository.
- No XDG override is documented for VS Code's user data folder, so `copilot-vscode` uses `~/.config/Code/User` on Linux regardless of `XDG_CONFIG_HOME`. VS Code Insiders (`Code - Insiders`) and profiles other than the default are not wired; paste the printed entry there by hand.
- Connecting both adapters puts two copies of the rules in front of Copilot CLI (`copilot-instructions.md` and `instructions/*.instructions.md`); the CLI only removes identical copies of the first kind. They do not conflict.
- `applyTo: "**"` follows the documented syntax; VS Code documents that for Copilot sessions `applyTo` matches files the agent works with, so the rules may not load in a chat that touches no file. The rules are also in the MCP server's own instructions.

JSONC: VS Code opens `mcp.json` as JSON with Comments, so comments and trailing commas are valid there and not rare. `connect` follows the framework rule and refuses a file that is not strict JSON (`config-unparseable`), writes nothing for the whole selection, and prints the exact `servers` entry to add by hand. The official pages above do not state that `mcp.json` accepts comments; this comes from VS Code's JSON-with-Comments file handling and is not live-verified.

Approvals: both clients ask before they call an MCP tool by default (Copilot CLI: MCP tool calls follow the session's permission rules; VS Code: Manual permissions, unless a tool is pre-approved). Apply is therefore guarded by the client prompt as well as the rules block, but a broad approval removes it: `--allow-all`/`--allow-tool` in the CLI, and **Allow all**, **Autopilot**, or trusting the whole `obsidian-wiki` server (**Chat: Manage Tool Approval**) in VS Code. Keep `wiki_batch_apply` on manual approval. Whether the real clients prompt for these tools is the claim of their documentation and is not live-verified (#72).

## Generic MCP snippets

Agents without an adapter can use the same server. Put these values in the client's MCP configuration (consult the client's documentation for the file and its schema; most use an `mcpServers` object with `command`, `args` and `env`, while some, such as Zed and Continue, nest or name them differently). These generic snippets use plain `node`, which the client resolves from its own `PATH`; `connect` pins the absolute node instead, and you can do the same by replacing `node` with the output of `command -v node`. Replace `<package root>` with the directory that contains `package.json` (for example `…/node_modules/@soulwaxx/obsidian-second-brain`) and `/path/to/python3.14` with the interpreter that has [`requirements.txt`](../requirements.txt) installed. `obsidian-second-brain connect --json` prints these two snippets with your real values under `genericSnippets`.

Read-only (`read`): the four `wiki_*` read tools. Use this when the agent writes with its own guarded tools, or when you want a read-only connection.

```json
{
  "mcpServers": {
    "obsidian-wiki": {
      "command": "node",
      "args": ["<package root>/scripts/obsidian-second-brain.mjs", "mcp", "--tools", "read"],
      "env": { "PYTHON": "/path/to/python3.14" }
    }
  }
}
```

Reviewed writes (`all`): adds `wiki_batch_inspect`, `wiki_batch_apply` and `wiki_batch_recover`, the only write path for an MCP-only agent. Add the rules block from a `connect` preview to the agent's rules so it shows you the inspect summary and waits for approval before applying.

```json
{
  "mcpServers": {
    "obsidian-wiki": {
      "command": "node",
      "args": ["<package root>/scripts/obsidian-second-brain.mjs", "mcp", "--tools", "all"],
      "env": { "PYTHON": "/path/to/python3.14" }
    }
  }
}
```

If the client does not pass your shell environment, add `OBSIDIAN_AGENT_CONFIG` (and optionally `OBSIDIAN_VAULT_PATH`) to `env`, with absolute values. These entries are not tested against any client and carry the `snippet-only` status. After a package upgrade or move, update the path by hand; `connect` and `doctor` only track agents they wired.
