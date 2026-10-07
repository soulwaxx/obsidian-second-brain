---
name: wiki-health
description: Run read-only diagnostics on the configured Obsidian wiki and report findings without repairing anything.
---

# Read-only Wiki Health

Follow [focused workflows](../wiki/references/focused-workflows.md) and the vault-selection/safety rules in [the wiki skill](../wiki/SKILL.md).

Run `obsidian-second-brain doctor --json` when its bin is on `PATH`; otherwise use the package-relative launcher procedure in [focused workflows](../wiki/references/focused-workflows.md), bound to the currently loaded package. Summarize its reported config, dependencies, vault, cache exclusions, lifecycle, and retrieval status. It is diagnostic only: do not repair configs or exclusions, build indexes, edit pages, or finalize lifecycle state. If a content/link audit is requested, run the existing read-only middleware lint and report findings separately. Explain that suggested repairs require a separate explicit request and follow the existing reviewed setup or wiki write workflow.
