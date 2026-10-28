---
name: wiki-query
description: Answer a question from the configured Obsidian wiki using read-only local search and targeted page reads.
---

# Query the Wiki

Follow the common workflow contract in [focused workflows](../wiki/references/focused-workflows.md) and the vault selection, reading, citation, and safety rules in [the wiki skill](../wiki/SKILL.md).
Apply the shared [trust boundary](../wiki/SKILL.md#trust-boundary) and
[wiki-first answering](../wiki/SKILL.md#wiki-first-answering) rules; this focused
intent does not create separate permissions or evidence guarantees.

For an existing-knowledge question in Pi, use the native read-only `wiki_search`, `wiki_outline`, and `wiki_read` tools when appropriate (see [Pi wiki tools](../../docs/pi-tools.md)). For CLI workflows, run `obsidian-second-brain search "query terms" --json` when its bin is on `PATH`; otherwise use the package-relative launcher procedure in [focused workflows](../wiki/references/focused-workflows.md), bound to the currently loaded package. Search results include hash-bound `readRef` coordinates when a source snapshot was verified; use the package's `outline` and `read` commands for bounded targeted reads (documented in `docs/retrieval-reads.md`) rather than loading whole pages. Read the best matching pages and answer from them; cite the wiki paths and separate sourced facts from inference. If search is unavailable, stale, empty, or reports a fallback, use the wiki entry point and generated index hierarchy to locate pages. Do not modify the wiki unless filing was explicitly requested.
