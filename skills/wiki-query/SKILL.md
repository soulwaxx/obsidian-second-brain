---
name: wiki-query
description: Answer a question from the configured Obsidian wiki using read-only local search and targeted page reads.
---

# Query the Wiki

Follow the common workflow contract in [focused workflows](../wiki/references/focused-workflows.md) and the vault selection, reading, citation, and safety rules in [the wiki skill](../wiki/SKILL.md).
Apply the shared [trust boundary](../wiki/SKILL.md#trust-boundary) and
[wiki-first answering](../wiki/SKILL.md#wiki-first-answering) rules; this focused
intent does not create separate permissions or evidence guarantees.

For an existing-knowledge question, use the read-only `wiki_search`, `wiki_outline`, and `wiki_read` tools through the available route in [tool surface by client](../wiki/references/tool-surface.md), including Pi discovery when they are not directly listed. Only when the tools cannot be reached, Claude Code and Pi may run `search "query terms" --json` through the package-relative launcher procedure in [focused workflows](../wiki/references/focused-workflows.md), bound to the currently loaded package; do not use an `obsidian-second-brain` bin on `PATH`, which may be a different installed copy or version. An MCP-only agent has no launcher, package path, or CLI fallback: if the tools are not listed, say the wiki server is not connected and do not read vault files with its own tools. Search results include hash-bound `readRef` coordinates when a source snapshot was verified; use `wiki_outline` and `wiki_read` (or the package CLI's `outline` and `read` commands) for bounded targeted reads rather than loading whole pages. Read the best matching pages and answer from them; cite the wiki paths and separate sourced facts from inference. If search is unavailable, stale, empty, or reports a fallback, Claude Code and Pi use the wiki entry point and generated index hierarchy to locate pages; an MCP-only agent refines the query or reports what could not be located. Do not modify the wiki unless filing was explicitly requested.
