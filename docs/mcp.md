# Stdio MCP server for the read-only wiki tools

`obsidian-second-brain mcp` starts a stdio [Model Context Protocol](https://modelcontextprotocol.io) server that gives any MCP-capable agent the same four read-only tools that Pi registers natively: `wiki_search`, `wiki_outline`, `wiki_read`, and `wiki_doctor`. Tool names, titles, descriptions, input schemas, and read-only annotations match the Pi registrations (a test compares them). The server is a subcommand of the package's Node launcher, `scripts/obsidian-second-brain.mjs`; it has no extra dependency, daemon, port, or network listener, and it never writes to the vault, configuration, caches, or lifecycle state.

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

## Claude Code plugin

The Claude Code plugin declares this server inline in `.claude-plugin/plugin.json` (`mcpServers.obsidian-wiki`), with no `.mcp.json`:

```json
{ "command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/obsidian-second-brain.mjs", "mcp", "--tools", "read"] }
```

Claude Code substitutes `${CLAUDE_PLUGIN_ROOT}` with the installed plugin directory, so the server is the exact reviewed copy the plugin shipped: no `npx`, no `PATH` bin, no network call at agent start. The server inherits Claude's environment, so `OBSIDIAN_AGENT_CONFIG`, `OBSIDIAN_VAULT_PATH`, and `PYTHON` behave as described below and match the hooks' `python3` default. Claude names the tools `mcp__plugin_obsidian-second-brain_obsidian-wiki__wiki_search` (and `_outline`, `_read`, `_doctor`); the `wiki-vault` agent's `tools` allowlist includes them. The profile is fixed to `read`: the plugin never exposes write tools, because Claude writes through hook-guarded file tools. Skills and the agent fall back to the package-relative CLI when the server is disabled in `/mcp` or unavailable. Pi and omp keep their native tools and do not use MCP.

## Vault selection and working directory

The server resolves the vault exactly as the CLI and Pi tools do: an optional absolute `vault` tool input, then `OBSIDIAN_VAULT_PATH`, then the selected configuration's `vaultPath`; `OBSIDIAN_AGENT_CONFIG` selects the configuration file, otherwise `~/.config/obsidian-second-brain/properties.json`. Put these in the server's `env` when the client does not pass your shell environment. MCP clients may start the server in any working directory, and relative `OBSIDIAN_AGENT_CONFIG`, `OBSIDIAN_VAULT_PATH`, or config `vaultPath` values resolve against that directory, so use absolute values. Call `wiki_doctor` to see which vault, config, and readiness state the server selected.

## Tool profiles

`--tools read` is the default and the only profile shipped: exactly the four tools above. `--tools all` is reserved for the batch write tools and is refused with an error (exit status 2, nothing on stdout) until they ship. Any other value is refused too.

## Protocol behavior

- Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout; stdout carries protocol messages only. Messages over 64 KiB, malformed JSON (`-32700`), JSON-RPC batches, and invalid requests (`-32600`), and unknown methods (`-32601`) get standard errors; an error answering an invalid request echoes its `id` when that is a string or number, otherwise `null`. A malformed `tools/call` (missing `name`, non-object `arguments`) or an unknown tool name gets `-32602`. Unknown or invalid tool *arguments* are not protocol errors: the call returns a tool result with `isError: true` and the bridge's diagnostics.
- `initialize` echoes the requested `protocolVersion` when it is one of `2025-11-25`, `2025-06-18`, `2025-03-26`, or `2024-11-05`; otherwise it answers with `2025-11-25` and the client decides whether to continue. The result advertises only the `tools` capability and includes `instructions`: the wiki locator (`Configured Obsidian wiki: "<vault>/wiki"`, the quoted selected config path, and a pointer to load `wiki/WIKI.md` and the relevant `wiki/index.md` only when a task needs the wiki), use these tools for the wiki, never edit vault files with your own file or shell tools, and call `wiki_doctor` for readiness state.
- The locator is resolved once, at the first `initialize`, by running the same `scripts/config_contract.py` helper the Pi extension uses (with `PYTHON`, else `python3`) under a 3-second deadline. It reads only the selected config and checks that the vault directory exists (`OBSIDIAN_VAULT_PATH` overrides `vaultPath`); it writes nothing and prints nothing to stdout. The vault path and config path are rendered as JSON-quoted single-line strings, so unusual directory names cannot add instruction text. If the helper fails or times out, the config is invalid, or no vault directory resolves (a missing config file is not an error by itself: with `OBSIDIAN_VAULT_PATH` set to an existing directory the locator is still emitted), `instructions` omit the locator and tell the agent to call `wiki_doctor` to see which vault and readiness state apply. The handshake never waits longer than that deadline, and the locator is not refreshed while the server runs.
- Also handled: `notifications/initialized`, `ping`, `tools/list` (one page, no cursor), `tools/call`, and `notifications/cancelled` for a running call (the call is terminated and not answered). `outputSchema` and `structuredContent` are part of the newer protocol versions; the text content item carries the same JSON for clients that negotiate `2024-11-05`. Resources, prompts, HTTP/SSE transports, and write tools are not provided.
- Calls may overlap; each runs in its own process tree.
- On stdin EOF, `SIGINT`, or `SIGTERM` the server terminates every running call (the launcher kills its Python process group, with a forced kill after 1.5 s; the hung-launcher watchdog uses the same terminate-then-kill sequence) and exits. After EOF the server still delivers a pending `initialize` reply (waiting at most the 3-second locator deadline) before exiting with status 0; `SIGINT` and `SIGTERM` exit immediately. Process-group termination is verified on macOS/Linux only; Windows is unverified.

## Verification status

Fixture-tested with a scripted stdio client (`tests/mcp-server.mjs`, also exercised from an archive-installed package by `npm run test:package`) against a synthetic vault. The Claude plugin declaration is fixture-tested by `tests/claude-mcp-contract.mjs` (also run against the Claude-installed copy by `tests/install-smoke.sh`): with a temporary Claude config and a loopback model provider, real Claude Code 2.1.322 connected the plugin server, listed exactly the four tools, routed a `wiki_doctor` call, and offered them to the `wiki-vault` agent (skipped and reported unverified when the `claude` CLI is absent). No other live MCP client has been validated.
