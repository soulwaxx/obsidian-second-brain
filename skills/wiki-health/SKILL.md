---
name: wiki-health
description: Run read-only diagnostics on the configured Obsidian wiki and report findings without repairing anything.
---

# Read-only Wiki Health

Follow [focused workflows](../wiki/references/focused-workflows.md) and the vault-selection/safety rules in [the wiki skill](../wiki/SKILL.md).
Apply the shared [trust boundary](../wiki/SKILL.md#trust-boundary) and
[wiki-first answering](../wiki/SKILL.md#wiki-first-answering) rules; this focused
intent does not create separate permissions or evidence guarantees.

Use the read-only `wiki_doctor` tool when it is listed (native in Pi; from the wiki MCP server in Claude Code and MCP-only agents, where the client may prefix the name; see [tool surface by client](../wiki/references/tool-surface.md)). Only when it is not listed, Claude Code and Pi may run `doctor --json` through the package-relative launcher procedure in [focused workflows](../wiki/references/focused-workflows.md), bound to the currently loaded package; do not use an `obsidian-second-brain` bin on `PATH`, which may be a different installed copy or version. An MCP-only agent has no launcher, package path, or CLI fallback: if the tool is not listed, say the wiki server is not connected. Summarize its reported config, dependencies, vault, cache exclusions, lifecycle, and retrieval status, then `readiness.stage`/`reason`, any non-ready `capabilities`, and `readiness.nextActions`. Present next actions as suggestions only: never run them, and treat any action with `mutates` or `requiresReview` as needing a separate explicit request through the reviewed setup or build workflow. A `supported` capability was not probed, and `appState: not-probed` is not evidence that Obsidian Git is syncing. It is diagnostic only: do not repair configs or exclusions, build indexes, edit pages, or finalize lifecycle state. If a content/link audit is requested, Claude Code and Pi run the existing read-only middleware lint and report findings separately; for an MCP-only agent it is not available here, so suggest running it from Claude, Pi or the CLI. Explain that suggested repairs require a separate explicit request and follow the existing reviewed setup or wiki write workflow.
