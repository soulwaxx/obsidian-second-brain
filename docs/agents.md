# Connect agents to the wiki

`obsidian-second-brain connect` wires an agent to the wiki the way setup scaffolds a vault: it previews every file change with a plan hash, applies only the plan you confirm, and can undo it. It is for agents that have no package plugin: they get the [MCP server](mcp.md) (`--tools all`), a marked rules block, and the six portable skills as plain copies. Claude Code (plugin) and Pi/omp (package) do not need it.

## Support status

| Agent | Mechanism | Status |
| --- | --- | --- |
| Claude Code | Plugin: hooks, skills, inline read-only MCP server | live-validated (the Claude contract tests run the real CLI when it is installed; see [the MCP guide](mcp.md#verification-status)) |
| Pi | Package: extension with native `wiki_*` tools, hooks, skills | live-validated (`npm run test:pi-runtime`) |
| Codex CLI | `connect` adapter (planned, #66): MCP entry in TOML, rules, skills | planned adapter |
| GitHub Copilot (CLI and VS Code) | `connect` adapter (planned, #67): MCP entry, rules, skills | planned adapter |
| Cursor | `connect` adapter (planned, #68): MCP entry, skills; project rules as a printed snippet | planned adapter |
| OpenCode | `connect` adapter (planned, #69): MCP entry, rules, skills | planned adapter |
| Gemini CLI, Cline, Zed, Kiro, Continue, Claude Desktop, Windsurf, any other MCP client | [Generic MCP snippet](#generic-mcp-snippets) | snippet-only |

Statuses mean:

- **live-validated**: a real client was run against the package and the result is recorded.
- **fixture-tested (live-unverified)**: the adapter's file changes are covered by tests in a temporary `HOME` against synthetic files; the real agent has not been run. Each adapter ticket flips its row from "planned adapter" to this status, and issue #72 records the live runs.
- **snippet-only**: no adapter and no test; you paste the generic snippet by hand.

omp runs the Pi extension; its runtime behavior is unverified. Windows paths are not verified: adapters may return Windows locations, but `connect` is tested on macOS and Linux only.

## Use `connect`

```sh
obsidian-second-brain connect                       # list agents; writes nothing
obsidian-second-brain connect <agent>...            # preview the exact changes and a plan hash
obsidian-second-brain connect <agent>... --apply --confirm <planHash>
obsidian-second-brain connect <agent>... --remove   # preview the removal; add --apply --confirm to apply it
```

`connect` must be the first argument. The plan hash is the setup command's own (`bootstrap-vault.py`'s digest). It binds the exact agent selection, `--force`/`--remove`, the package root, interpreter and version, the selected config, and the before/after digest of every file. A different selection, an edit to any target file after the preview, or a different package makes `--apply` refuse with `stale or unconfirmed plan; preview again (hash …)`, and nothing is written. Add `--json` for machine-readable output (`planHash`, `plan.files[]` with `role`, `path`, `op` of `create`/`update`/`delete`/`unchanged`, and the entry or block that will be written).

| Option | Meaning |
| --- | --- |
| `--python PATH` | Interpreter to pin in the MCP entries. Default: the interpreter running `connect` (the one your `obsidian-second-brain` command uses). It must be Python 3.14 or newer and import `yaml`; one without `markdown-it-py` is accepted with a note, because `wiki_outline` and `wiki_read` need it. The path is stored as given, so a virtual environment stays pinned. |
| `--force` | Replace customized skill copies after backing them up (see below). |
| `--remove` | Plan or apply the removal of what `connect` wrote. |

The config is selected as for setup: `OBSIDIAN_AGENT_CONFIG`, else `~/.config/obsidian-second-brain/properties.json`. When `OBSIDIAN_AGENT_CONFIG` is set, the MCP entry carries it in its `env` so the server selects the same config.

### What it writes

Per agent, only user-level files, never inside the vault or a project repository:

1. **MCP entry** `obsidian-wiki`: `node <absolute package root>/scripts/obsidian-second-brain.mjs mcp --tools all` with `env.PYTHON` set to the pinned interpreter. No `npx`, so starting the agent makes no network call and runs the reviewed installed copy. `node` is resolved from the agent's `PATH`.
2. **Rules block** between `<!-- obsidian-second-brain:start -->` and `<!-- obsidian-second-brain:end -->` in the agent's user-level rules file: the wiki locator, the rule to use the wiki MCP tools and never edit vault files directly, and the inspect → approval → apply flow. Text outside the markers is never touched.
3. **Skill copies**: the six portable skills (`wiki`, `wiki-query`, `wiki-save`, `wiki-ingest`, `wiki-research`, `wiki-health`) as `<skills dir>/<name>/SKILL.md` plus `<skills dir>/wiki/references/*.md`, never `wiki/scripts/`. They are MCP-only skills (see `skills/wiki/references/tool-surface.md`).
4. **Install manifest** `connect-manifest.json` next to the selected integration config (outside the vault).
5. **Backups** of any config or rules file that changes and of customized skill copies, under `connect-backups/<planHash prefix>/` next to the manifest, with a `backup-index.json` that maps each backup to its original path.

Project-level files are never written. The preview prints them as snippets instead (for example a Cursor `.mdc` rule or `.github/copilot-instructions.md`), together with each adapter's manual notes.

### Write rules

- **Merge only our entry.** Other servers, keys and settings are preserved. JSON is parsed strictly and rewritten with the file's detected indent; a file with comments (JSONC), trailing commas or duplicate keys is refused. TOML is never rewritten: our table is appended inside `# obsidian-second-brain:start`/`end` comment markers (replaced or removed by those markers), and the result is parsed again to prove that every other setting is unchanged. YAML is merged with PyYAML only when the file has no comments, because a rewrite would drop them. Any refusal prints the exact snippet to add by hand and writes nothing for the whole selection.
- **Our entry only.** An existing `obsidian-wiki` entry that differs from ours is refused unless the manifest shows `connect` wrote it and it is unmodified (an upgrade); a modified entry is never removed either.
- **Safe writes.** Each file is written to a temporary file in its directory, flushed, renamed, and re-read to verify. The destination is re-checked just before each write, so an edit made after the preview is refused (and the whole apply rolls back). Symlinked targets are refused.
- **Rollback.** If anything fails, every file already written is restored, created directories are removed, and a file edited in the meantime is left as the user wrote it and reported.
- **Customized skill copies.** A destination file that differs from the shipped one is overwritten only when its hash still matches the one the manifest recorded (an older, unmodified copy). Otherwise `connect` refuses and names the files; `--force` backs them up first. Removing customized copies needs `--force` too. Files that two connected agents share (for example one skills directory) stay until the last agent using them is removed.
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
      "profile": "all",
      "mcp": { "path": "…", "format": "json|toml|yaml", "keyPath": ["mcpServers"], "name": "obsidian-wiki", "entrySha256": "…", "createdFile": false },
      "rules": { "path": "…", "blockSha256": "…", "createdFile": false },
      "skills": { "root": "…", "files": { "wiki/SKILL.md": "<sha256>", "wiki/references/tool-surface.md": "<sha256>" } }
    }
  }
}
```

## Doctor and stale connections

`doctor` (and the MCP `wiki_doctor` tool) report a `connect` section, additive to the readiness schema, and write nothing:

```json
"connect": {
  "manifest": { "path": "…", "exists": true, "error": null },
  "agents": { "<agent>": { "title": "…", "state": "connected", "detected": true, "verification": "…", "adapterRegistered": true, "version": "…", "reasons": [] } },
  "adapterErrors": [],
  "nextActions": []
}
```

| State | Meaning |
| --- | --- |
| `connected` | Recorded in the manifest and everything still matches. |
| `stale` | Recorded, but something drifted. `reasons[].code` is one or more of `package-root-mismatch` (the package that runs doctor is not the one that was connected, for example after an upgrade or move), `package-root-missing`, `interpreter-missing`, `interpreter-mismatch` (doctor runs under a different interpreter than the pinned one), `version-mismatch`, `mcp-entry-missing`, `mcp-entry-modified`, `mcp-config-unreadable`, `rules-block-missing`, `rules-block-modified`, `skill-missing`, `skill-modified` (hash differs from the recorded one), `skill-outdated` (the installed package ships a newer copy). |
| `absent` | Not connected. `detected` says whether the agent's config directory exists. |
| `manual-only` | `connect` cannot wire it safely: the adapter has no user-level MCP target, or the detected config cannot be merged (`reasons[0].code` says why). Use the printed snippet. |

Doctor never repairs. For a `stale` agent it adds a `reconnect-agent` next action (a preview command that is not run for you). Rerun `connect <agent>`, review the update, and apply its plan hash; rerunning from the new package location fixes a moved or upgraded install. The human report adds a `Connect <agent>: <state>` line for connected, stale, manual-only and detected agents.

## Adapters

An adapter is one file, `scripts/connect_adapters/<agent>.py`, that declares where an agent keeps its user-level files and how its MCP entry looks. It contains no file-writing code: reading, merging, hashing, backups, atomic writes, rollback, the manifest and doctor all live in `scripts/connect.py` and work for any adapter.

```python
from connect_adapters.spec import (Adapter, McpTarget, RulesTarget, SkillsTarget, Snippet, FIXTURE_TESTED)

ADAPTER = Adapter(
    name="example",                    # lowercase CLI id: `connect example`
    title="Example Agent",
    detect=(".example",),              # config directories under HOME; any existing one means "detected"
    mcp=McpTarget(
        path=".example/mcp.json",      # HOME-relative, or a function Path(home) -> Path (XDG, <AGENT>_HOME)
        format="json",                 # "json" (strict), "toml" or "yaml"
        key_path=("mcpServers",),      # object/table that holds the named servers
        name="obsidian-wiki",          # key of our entry (default)
        entry=lambda command, env: {"command": command[0], "args": command[1:], "env": env},  # default shape
    ),
    rules=RulesTarget(path=".example/AGENTS.md"),     # user-level rules file; None to skip
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
| `detect` | yes | Locations (HOME-relative strings or `Path -> Path` functions) whose existence as a directory marks the agent as detected. Detection never runs an agent binary. |
| `mcp` | no | `McpTarget`. Without it the agent is `manual-only`. `entry(command, env)` receives `command` = `["node", "<root>/scripts/obsidian-second-brain.mjs", "mcp", "--tools", "all"]` and `env` = `{"PYTHON": …}` (plus `OBSIDIAN_AGENT_CONFIG` when set) and returns the entry in the agent's own shape, for example OpenCode's `{"type": "local", "command": command, "environment": env}`. Values must be strings, booleans, integers or lists/tables of them. |
| `rules` | no | `RulesTarget(path)`: the user-level rules file that receives the marked block. |
| `skills` | no | `SkillsTarget(path)`: the user-level skills directory that receives the six skill folders. |
| `snippets` | no | `Snippet(title, destination, content)` for project-level or manual content. `{{rules}}` expands to the shared rules text. |
| `notes` | no | Manual notes. |
| `verification` | no | `LIVE_VALIDATED`, `FIXTURE_TESTED` (default) or `SNIPPET_ONLY`. |

Every returned path must be absolute and user-level; the framework refuses anything inside the selected vault, symlinked targets, and files it cannot merge.

To register an adapter, uncomment (or add) its single line in `scripts/connect_adapters/__init__.py`:

```python
REGISTRY: list[str] = [
    "connect_adapters.example",
]
```

Separate comment lines keep each adapter's edit on its own line, so adapter changes merge without conflicts. A module that fails to import or has an invalid `ADAPTER` is reported by `connect` and, as `adapterErrors`, by doctor.

Test an adapter with `tests/connect_fixture_adapter.py` and `tests/test_connect.py` as the model: run the real `connect` code against a temporary `HOME` and `OBSIDIAN_AGENT_CONFIG`, assert the exact paths, entry shape, preservation of unrelated entries, preview-without-writes, apply, repeat no-op, removal, and a doctor round trip. Never touch the real home directory or run the agent. When the tests pass, change the agent's row in the table above and in the README from "planned adapter" to "fixture-tested (live-unverified)"; live validation (#72) is a separate step.

## Generic MCP snippets

Agents without an adapter can use the same server. Put these values in the client's MCP configuration (consult the client's documentation for the file and its schema; most use an `mcpServers` object with `command`, `args` and `env`, while some, such as Zed and Continue, nest or name them differently). Replace `<package root>` with the directory that contains `package.json` (for example `…/node_modules/@soulwaxx/obsidian-second-brain`) and `/path/to/python3.14` with the interpreter that has [`requirements.txt`](../requirements.txt) installed. `obsidian-second-brain connect --json` prints these two snippets with your real values under `genericSnippets`.

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
