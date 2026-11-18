# Tool Surface by Client

The wiki skills work in Claude Code, Pi, omp, and MCP-only agents (for example
Codex CLI, GitHub Copilot, Cursor, OpenCode). What a client can read, write, and
run differs, so choose the route by the capabilities you actually have, not by
assumption. This guide is the one place that maps clients to tools; the other
skills and references defer to it. Trust, OKF, selection, and safety rules do not
change with the client: follow [the wiki skill](../SKILL.md) in every case.

The skills are installed either from the package (Claude plugin or Pi package)
or as a self-contained copy of the six skill folders (`wiki`, `wiki-query`,
`wiki-save`, `wiki-ingest`, `wiki-research`, `wiki-health`). A copy holds each
`SKILL.md` and `wiki/references/*.md`; none of their relative links leave that
set. It has no package root, so a copied skill never relies on a package path, a
launcher, an `obsidian-second-brain` bin on `PATH`, or a plugin variable.
Package documents such as `docs/setup.md` or `docs/mcp.md` are named in plain
text because they exist only in the package or its repository.

## Wiki tool names

The portable skills use the plain tool names below. A client may add its own
prefix to MCP tools (the server name, or a plugin-specific prefix); match on the
trailing `wiki_*` name. Claude Code lists the plugin's tools as
`mcp__plugin_obsidian-second-brain_obsidian-wiki__wiki_search` and so on.

| Tool | Purpose | Where it exists |
| --- | --- | --- |
| `wiki_search`, `wiki_outline`, `wiki_read`, `wiki_doctor` | Read-only search, outline, bounded read, diagnostics | Pi native; MCP `read` and `all` profiles |
| `wiki_batch_inspect`, `wiki_batch_apply`, `wiki_batch_recover` | Reviewed, recoverable wiki-page writes | MCP `all` profile only (never Pi, never the Claude plugin) |

## Client classes

| Class | Read | Write |
| --- | --- | --- |
| **Pi / omp** with the package extension | Native `wiki_*` read tools; otherwise the package CLI | The client's file tools, guarded by the vault hook (omp through the Pi extension; runtime unverified) |
| **Claude Code** with the plugin | The plugin's MCP read tools when listed; otherwise the package-relative CLI | The client's file tools, guarded by the vault hook |
| **MCP-only agents** (copied skills, no package hook) | MCP read tools | MCP `wiki_batch_*` tools only, and only when the server runs with `--tools all` |

Classify the session by the client application and how the skill was installed,
never by what is in your context:

- **Package install** means you are Claude Code with this plugin, or Pi/omp with
  this package, and this skill was loaded from the package: its directory is
  `<package>/skills/<name>` and `<package>/scripts/obsidian-second-brain.mjs`
  and `<package>/hooks/obsidian-session.sh` exist beside it. Write with the
  client's file tools when the package hook is active, which the session-start
  context shows with the hook's locator ("writes are guarded and validated").
  When it is not active (no vault is configured yet, as in setup or init, or
  hooks are disabled), you are still a package session and never read-only:
  follow the manual guard, validate, and sync order in the wiki skill, and use
  setup, the CLI, and the package scripts.
- **Copy** means every other client, and any skill installed by
  `obsidian-second-brain connect` or otherwise copied without a package root:
  treat yourself as MCP-only, whatever model you run on.

Your model's identity ("I am Claude") and the MCP server's locator are not
evidence of a package install or an active guard. The MCP server's
`initialize` instructions also begin `Configured Obsidian wiki: "<vault>/wiki"`,
and Copilot, Cursor, and other clients can run a Claude model. Only the
client application with the package hook or extension counts; the hook's
locator says "writes are guarded and validated" and the MCP locator never does.

## Reading

Use `wiki_search`, `wiki_outline`, and `wiki_read` when they are listed, and
`wiki_doctor` to verify the selected vault, config, and readiness. Do not set the
`vault` input unless the user selected a different vault; the server or
extension already uses the configured one.

- Pi, omp, and Claude Code may run the package CLI through the package-relative
  launcher procedure in [focused workflows](focused-workflows.md#run-the-package-cli)
  only when the read tools are not listed (for example the server is disabled).
- An MCP-only agent has no CLI fallback and no package path. If the read tools
  are not listed, say the wiki server is not connected and stop; do not read or
  search vault files with the client's own tools. Reserved `index.md` pages are
  not readable through `wiki_read`, so when search finds nothing, refine the
  query or report what could not be located.

## Writing

**Hook-guarded clients (Claude Code, Pi, omp).** Write with the client's file
tools; the hook authorizes the destination, validates the page, and finalizes
navigation and logging. Shell writes are not guarded and follow the guard,
validate, and sync order in the wiki skill; without an active hook, that
order applies to file-tool writes too. A reviewed batch is optional for a
larger coordinated change, through the package CLI.

**MCP-only agents.** Change the wiki only through the batch tools:

1. Call `wiki_batch_inspect` with up to 20 `{ path, expectedHash, content }`
   operations: the complete new text of each Markdown page under `wiki/`
   (`expectedHash` is `null` for a new page, otherwise the current `sourceHash`
   from `wiki_outline`; at most 48 KiB serialized). Nothing is written.
2. Show the user the inspect summary (paths, operations, sizes, validation,
   diff excerpts) and the `planHash`.
3. Wait for the user's explicit approval. Never apply without it, even when the
   client would auto-approve the call.
4. Call `wiki_batch_apply` with the identical operations and that `planHash`.
5. If an apply fails, times out, or is cancelled, call `wiki_batch_recover` with
   the same `planHash` (or a `batchId` from `wiki_doctor` whose entry is not
   `cliOnly`) before retrying. On a
   `batch-refused` recovery conflict, report it and stop: rollback is not an MCP
   tool.

Never create, edit, move, or delete vault files with the client's own file or
shell tools, and never write `index.md` or `log.md`; the server generates the
indexes and owns the log. Never put secrets in page content; the write-path
secret check refuses them. Keep one wiki writer at a time. If `wiki_batch_*`
are not listed, the session is read-only: say that writing needs the server's
`--tools all` profile and do not look for another write path. Save, ingest,
research filing, and every other write in the focused skills use this flow for
an MCP-only agent.

## Not available to MCP-only agents

These need the package scripts, a lifecycle hook, or a local app. For an
MCP-only agent each is **not available here; run it from Claude, Pi or the CLI**:
vault setup and configuration, retrieval index builds (including semantic
chunks), source capture, source observations and baselines, evidence-ledger
batches, evidence reports, link-graph lint and the link-compatibility audit,
link migration, shell-write guard/validate/capture/record/finalize helpers,
`batch-rollback`, and `obsidian-cli` transport. Say so briefly, suggest those
clients, and continue with the parts of the task the tools do cover.

## Pi/omp and Claude specifics

- Pi and omp keep their native tools and do not use MCP.
- Claude's plugin runs the MCP server with the `read` profile only; its write
  path is the hook-guarded file tools. It never lists `wiki_batch_*`.
- Preference when several routes exist: native or MCP read tools first, then the
  package-relative CLI for Claude and Pi. Never use an `obsidian-second-brain`
  bin on `PATH`; it may be a different installed copy or version.
