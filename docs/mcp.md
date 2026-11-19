# Stdio MCP server for the wiki tools

`obsidian-second-brain mcp` starts a stdio [Model Context Protocol](https://modelcontextprotocol.io) server that gives any MCP-capable agent the same four read-only tools that Pi registers natively: `wiki_search`, `wiki_outline`, `wiki_read`, and `wiki_doctor`. Tool names, titles, descriptions, input schemas, and read-only annotations match the Pi registrations (a test compares them). With `--tools all` it also offers three reviewed batch write tools (see [Reviewed batch writes](#reviewed-batch-writes---tools-all)). The server is a subcommand of the package's Node launcher, `scripts/obsidian-second-brain.mjs`; it has no extra dependency, daemon, port, or network listener. The default `read` profile never writes to the vault, configuration, caches, or lifecycle state.

Each `tools/call` runs the existing bounded `--pi-tool` bridge, so validation, the 30-second deadline, and the 256 KiB transport / 64 KiB result caps are the ones described in [the Pi tool contract](pi-tools.md). The call result's `structuredContent` is the bridge's result object, byte-for-byte the `--pi-tool` JSON (`operation`, `ok`, `payload`, and on failure `code`/`message`, plus `processStatus`); the same JSON is the single `text` content item. `ok: false` results set `isError: true` and keep their diagnostics. Hybrid search may contact only the bounded loopback Ollama endpoint described there, and only when requested.

## Configure a client

Use an absolute path to the installed package and pin the Python interpreter that has [`requirements.txt`](../requirements.txt) installed (Python 3.14 or newer):

```json
{
  "mcpServers": {
    "obsidian-wiki": {
      "command": "node",
      "args": ["<package root>/scripts/obsidian-second-brain.mjs", "mcp"],
      "env": { "PYTHON": "/path/to/python3.14" }
    }
  }
}
```

`<package root>` is the directory that contains `package.json`, for example `…/node_modules/@soulwaxx/obsidian-second-brain` for an npm install, or the Claude plugin/Pi package checkout. The key names above are the common `mcpServers` shape; consult your client's documentation for its own file location and schema.

A version-pinned `npx` form works when no local install exists:

```json
{ "command": "npx", "args": ["-y", "@soulwaxx/obsidian-second-brain@<version>", "mcp"], "env": { "PYTHON": "/path/to/python3.14" } }
```

Do not use an unpinned `npx -y @soulwaxx/obsidian-second-brain mcp`: it contacts npm every time the agent starts and may run a version you have not reviewed. The absolute-path form makes no network call and runs exactly the installed, reviewed version.

`obsidian-second-brain connect` writes this entry (with `--tools all`) for the agents it supports, previews it first, and `doctor` flags it when the package moves; see [Connect agents to the wiki](agents.md). Generic `read` and `all` snippets for other clients are in [the same guide](agents.md#generic-mcp-snippets).

## Claude Code plugin

The Claude Code plugin declares this server inline in `.claude-plugin/plugin.json` (`mcpServers.obsidian-wiki`), with no `.mcp.json`:

```json
{ "command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/obsidian-second-brain.mjs", "mcp", "--tools", "read"] }
```

Claude Code substitutes `${CLAUDE_PLUGIN_ROOT}` with the installed plugin directory, so the server is the exact reviewed copy the plugin shipped: no `npx`, no `PATH` bin, no network call at agent start. The server inherits Claude's environment, so `OBSIDIAN_AGENT_CONFIG`, `OBSIDIAN_VAULT_PATH`, and `PYTHON` behave as described below and match the hooks' `python3` default. Claude names the tools `mcp__plugin_obsidian-second-brain_obsidian-wiki__wiki_search` (and `_outline`, `_read`, `_doctor`); the `wiki-vault` agent's `tools` allowlist includes them. The profile is fixed to `read`: the plugin never exposes write tools, because Claude writes through hook-guarded file tools. Skills and the agent fall back to the package-relative CLI when the server is disabled in `/mcp` or unavailable. Pi and omp keep their native tools and do not use MCP.

## Client-neutral skills

The six portable skills (`wiki`, `wiki-query`, `wiki-save`, `wiki-ingest`, `wiki-research`, `wiki-health`) name the tools above by their plain `wiki_*` names; a client may prefix them with the server name (Claude Code shows `mcp__plugin_obsidian-second-brain_obsidian-wiki__wiki_search`), so match the trailing name. The skills branch on client capability, as described in `skills/wiki/references/tool-surface.md`:

- Hook-guarded clients (Claude Code, Pi, omp) read with the `wiki_*` tools (Claude's `read` profile; Pi native) and write with their hook-guarded file tools. Without the tools, they fall back to the package-relative CLI.
- MCP-only clients (no hook) read with the same tools and write only with `--tools all`: `wiki_batch_inspect`, the user's approval of its summary, `wiki_batch_apply`, and `wiki_batch_recover` after a failure; never with their own file or shell tools. Without the batch tools the session is read-only. They have no package path, so the CLI-only operations (setup, index builds, capture, baselines, evidence ledgers/reports, lint, migration, `batch-rollback`) are reported as not available there.

A copy of the six skill folders (each `SKILL.md` plus `skills/wiki/references/*.md`) is self-contained: no relative link leaves it, and it needs no package root, plugin variable, or `PATH` bin. `tests/test_skill_contracts.py` enforces the copied-set links and `tests/skill-tool-names.mjs` checks every `wiki_*` name against the Pi registration and the MCP `tools/list` of both profiles.

## Vault selection and working directory

The server resolves the vault exactly as the CLI and Pi tools do: an optional absolute `vault` tool input, then `OBSIDIAN_VAULT_PATH`, then the selected configuration's `vaultPath`; `OBSIDIAN_AGENT_CONFIG` selects the configuration file, otherwise `~/.config/obsidian-second-brain/properties.json`. Put these in the server's `env` when the client does not pass your shell environment. MCP clients may start the server in any working directory, and relative `OBSIDIAN_AGENT_CONFIG`, `OBSIDIAN_VAULT_PATH`, or config `vaultPath` values resolve against that directory, so use absolute values. Call `wiki_doctor` to see which vault, config, and readiness state the server selected.

## Tool profiles

`--tools read` is the default: exactly the four read-only tools above, and the Claude Code plugin always uses it. `--tools all` adds `wiki_batch_inspect`, `wiki_batch_apply`, and `wiki_batch_recover` for agents that have no write guard of their own. Any other value is refused with an error (exit status 2, nothing on stdout). Under `read` the batch tools are absent from `tools/list` and a call to one is refused with `-32602` (unknown tool). The write tools are never registered as Pi tools.

## Reviewed batch writes (`--tools all`)

The three tools wrap the existing reviewed wiki batch flow of `scripts/wiki_batch.py` (the same code and locks as the CLI's `batch-inspect`, `batch-apply`, and `batch-recover`), `wiki-page` authority only. `source-capture`, `wiki-ledger`, and `batch-rollback` remain CLI-only. There is no second write path: the server never writes a file itself, never touches `index.md` or `log.md` (the lifecycle generates the indexes and owns the log), and runs no Git write command; Obsidian Git stays the only committer.

| Tool | Batch operation | Annotations |
| --- | --- | --- |
| `wiki_batch_inspect` | `batch-inspect` (review summary) | read-only, idempotent |
| `wiki_batch_apply` | `batch-apply` | **destructive, non-idempotent** |
| `wiki_batch_recover` | `batch-recover` | destructive, idempotent |

Flow:

1. **Inspect.** The agent calls `wiki_batch_inspect` with `operations`: up to 20 entries of `{ path, expectedHash, content }`, the complete new text of each Markdown page under `wiki/`. `expectedHash` is `null` for a new page, otherwise the page's current `sourceHash` from `wiki_outline`. Nothing is written. The server validates the paths (no `index.md`, `log.md`, `_plan.md`, hidden, out-of-`wiki/`, symlinked, or case-colliding destinations), the OKF frontmatter, the write-path secret check (new likely secrets are refused, as in file tools), and the current page hashes. It returns a bounded review summary and the `planHash`: per path the operation (`create`, `update`, `no-change`), byte sizes, before/after hashes, validation status, and a diff excerpt of at most 1,500 characters (dropped last page first if needed to fit). The plan hash is the CLI's: it binds the bundle, the vault, the current page hashes, and the selected config file's identity.
2. **Review.** The agent must show that summary and the `planHash` to the user and wait for explicit approval. The server's `instructions` say so, and tell the agent never to edit vault files with its own file or shell tools. Annotations are hints: clients may skip prompts (auto-approve modes), and instructions bind only an agent that follows them. Neither is a guard; the server enforces paths, validation, and staleness, not that a human looked.
3. **Apply.** After approval the agent calls `wiki_batch_apply` with the identical `operations` and the `planHash`. The server recomputes the plan and refuses, without writing a page, when the plan hash does not match (changed bundle or config), when a target page changed since inspection (precondition mismatch, with the concurrent edit preserved), or when validation fails. Otherwise it saves a journal, publishes the pages, records them with the lifecycle, regenerates navigation, and runs the configured retrieval refresh, as `batch-apply` does. Repeating a completed apply is a verified no-op. The result payload is `{ batchId, planHash, phase, noop }`.
4. **Recover.** `wiki_batch_recover` takes the `planHash` (or a `batchId` listed by `wiki_doctor`) and resumes the saved journal: it completes only the plan the user approved, refuses and preserves current bytes if a target changed since, and is a no-op once complete. If an apply fails after its journal was saved, the error code is `batch-interrupted` and its payload carries the `planHash`. A timeout or cancellation gives no such reply, so the instructions tell the agent to call `wiki_batch_recover` with the same `planHash` before retrying. If no journal exists, nothing was published and inspecting and applying again is safe. Recover refuses batches that were not created with `wiki-page` authority (for example a CLI `source-capture` journal), by `batchId` as well as by `planHash`, with the error code `authority-cli-only`; recover those with the CLI. Before resuming, recover deletes this batch's leftover `.batch-<batchId>-<hex>.tmp` publication copies from its target directories (exact batch-ID matches only, never following symlinks), so an interrupted publish does not leave hidden files for Obsidian Git to commit. `wiki_doctor` reports `payload.batches` (`unfinished` entries with `batchId`, `planHash`, `phase`, `authority`, `cliOnly`, and paths, plus `unfinishedCount` and `unreadable`) without writing; only entries with `cliOnly: false` are recoverable through `wiki_batch_recover`.

   **Recovery conflict.** When a target page was edited after the interruption, `wiki_batch_recover` fails with `batch-refused` ("recovery preflight conflict ... preserving current bytes"), the edit is kept, and the batch stays unfinished: retrying the tool cannot succeed, and rollback is not an MCP tool. The user must decide and run the CLI themselves: `obsidian-second-brain batch-rollback --batch-id <batchId> --json` (batchId from `wiki_doctor`) restores the original page bytes where they are still the approved ones, or reports `rollback-conflict` for pages that differ. Rollback is refused once the batch reached its `finalizing` phase; then keep the current pages and resolve the conflict by hand. Agents should report the conflict and stop rather than retry.

Failures are tool errors (`isError: true`) with the bridge's bounded `code`/`message`: `invalid-request` (bad arguments or over a size cap), `authority-cli-only` (recover was asked for a batch that is not a `wiki-page` batch), `batch-refused` (any batch-code refusal, including path, validation, secret, stale-plan and config errors), `batch-interrupted`, `invalid-config`, and `no-vault`. Refusals write no page. A refused apply may still create the empty batch journal directory and lock file under `.vault-meta/lifecycle/batches/`.

Limits. Writes always target the configured vault (there is no `vault` input), selected by `OBSIDIAN_VAULT_PATH` or the config as for reads. The serialized `operations` may be at most **48 KiB** (and 20 pages), which sits below the 64 KiB MCP message, bridge-stdin, and result limits; a larger bundle is refused with `invalid-request` before any write, so split big changes into several batches. The parameters travel to the bridge on stdin, never argv. `wiki_batch_inspect` output is bounded to 56 KiB regardless of content.

Deadline. The 30-second deadline applies until the apply's journal is saved, or, for recover, until the writer lock is held, so waiting for another writer's lock ends in a clean `timeout` error instead of a hang. After that point the launcher stops its deadline timer (the bridge prints `osb-journal-persisted` on stderr and the launcher forwards it) so a slow publication or retrieval refresh is not killed mid-way, and the server's hung-launcher watchdog stands down on the same marker. Cancelling the call (`notifications/cancelled`), closing stdin, or a signal still terminates the process group; that is the interruption `wiki_batch_recover` repairs.

Accepted limitation (owner decision; revisit if live validation, issue #72, shows hung applies): there is no hard cap after the journal is saved. A hung post-journal apply or recover keeps holding the writer lock until the client cancels it. Cancelling is safe, because `wiki_batch_recover` can then complete the batch or, after a conflict, the user can roll it back with the CLI.

## Protocol behavior

- Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout; stdout carries protocol messages only. Messages over 64 KiB, malformed JSON (`-32700`), JSON-RPC batches, and invalid requests (`-32600`), and unknown methods (`-32601`) get standard errors; an error answering an invalid request echoes its `id` when that is a string or number, otherwise `null`. A malformed `tools/call` (missing `name`, non-object `arguments`) or an unknown tool name gets `-32602`. Unknown or invalid tool *arguments* are not protocol errors: the call returns a tool result with `isError: true` and the bridge's diagnostics.
- `initialize` echoes the requested `protocolVersion` when it is one of `2025-11-25`, `2025-06-18`, `2025-03-26`, or `2024-11-05`; otherwise it answers with `2025-11-25` and the client decides whether to continue. The result advertises only the `tools` capability and includes `instructions`: the wiki locator (`Configured Obsidian wiki: "<vault>/wiki"`, the quoted selected config path, and a pointer to load `wiki/WIKI.md` and the relevant `wiki/index.md` only when a task needs the wiki), a profile-specific policy (for `read`: the tools are read-only and wiki changes follow the client's own wiki skill and reviewed write path; file tools are not forbidden, because Claude's only write path is its hook-guarded file tools; for `all`: the batch tools are the only way to change the wiki, never edit vault files or write `index.md`/`log.md` directly, show the inspect summary, wait for explicit user approval before apply, and recover by plan hash after a failed apply), and a pointer to `wiki_doctor` for readiness state.
- The locator is resolved once, at the first `initialize`, by running the same `scripts/config_contract.py` helper the Pi extension uses (with `PYTHON`, else `python3`) under a 3-second deadline. It reads only the selected config and checks that the vault directory exists (`OBSIDIAN_VAULT_PATH` overrides `vaultPath`); it writes nothing and prints nothing to stdout. The vault path and config path are rendered as JSON-quoted single-line strings, so unusual directory names cannot add instruction text. If the helper fails or times out, the config is invalid, or no vault directory resolves (a missing config file is not an error by itself: with `OBSIDIAN_VAULT_PATH` set to an existing directory the locator is still emitted), `instructions` omit the locator and tell the agent to call `wiki_doctor` to see which vault and readiness state apply. The handshake never waits longer than that deadline, and the locator is not refreshed while the server runs.
- Also handled: `notifications/initialized`, `ping`, `tools/list` (one page, no cursor), `tools/call`, and `notifications/cancelled` for a running call (the call is terminated and not answered). `outputSchema` and `structuredContent` are part of the newer protocol versions; the text content item carries the same JSON for clients that negotiate `2024-11-05`. Resources, prompts, and HTTP/SSE transports are not provided, and write tools exist only under `--tools all`.
- Calls may overlap; each runs in its own process tree.
- On stdin EOF, `SIGINT`, or `SIGTERM` the server terminates every running call (the launcher kills its Python process group, with a forced kill after 1.5 s; the hung-launcher watchdog uses the same terminate-then-kill sequence) and exits. After EOF the server still delivers a pending `initialize` reply (waiting at most the 3-second locator deadline) before exiting with status 0; `SIGINT` and `SIGTERM` exit immediately. Process-group termination is verified on macOS/Linux only; Windows is unverified.

## Verification status

Fixture-tested with a scripted stdio client (`tests/mcp-server.mjs` for the read tools and protocol, `tests/mcp-batch.mjs` for the batch tools, both against synthetic disposable vaults; `npm run test:package` also starts the archive-installed server). The batch tests cover inspect → apply, CLI plan-hash parity, stale/wrong-hash/changed-config refusal, secret refusal (fake values assembled at runtime), protected and symlinked paths, the size cap, the lifted deadline, cancellation during publication followed by recovery through MCP only, and that no Git write command runs. The Claude plugin declaration is fixture-tested by `tests/claude-mcp-contract.mjs` (also run against the Claude-installed copy by `tests/install-smoke.sh`): with a temporary Claude config and a loopback model provider, real Claude Code 2.1.322 connected the plugin server, listed exactly the four tools, routed a `wiki_doctor` call, and offered them to the `wiki-vault` agent (skipped and reported unverified when the `claude` CLI is absent). No other live MCP client has been validated.
