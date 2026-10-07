---
name: wiki-query
description: Answer a question from the configured Obsidian wiki using read-only local search and targeted page reads.
---

# Query the Wiki

Follow the common workflow contract in [focused workflows](../wiki/references/focused-workflows.md) and the vault selection, reading, citation, and safety rules in [the wiki skill](../wiki/SKILL.md).

For an existing-knowledge question, run `obsidian-second-brain search "query terms" --json` when its bin is on `PATH`. If not, use the package-relative launcher procedure in [focused workflows](../wiki/references/focused-workflows.md), bound to the currently loaded package. Read the best matching pages and answer from them; cite the wiki paths and separate sourced facts from inference. If search is unavailable, stale, empty, or reports a fallback, use the wiki entry point and generated index hierarchy to locate pages. Do not modify the wiki unless filing was explicitly requested.
