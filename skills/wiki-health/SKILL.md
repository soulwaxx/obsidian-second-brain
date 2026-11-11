---
name: wiki-health
description: Run read-only diagnostics on the configured Obsidian wiki and report findings without repairing anything.
---

# Read-only Wiki Health

Follow [focused workflows](../wiki/references/focused-workflows.md) and the vault-selection/safety rules in [the wiki skill](../wiki/SKILL.md).
Apply the shared [trust boundary](../wiki/SKILL.md#trust-boundary) and
[wiki-first answering](../wiki/SKILL.md#wiki-first-answering) rules; this focused
intent does not create separate permissions or evidence guarantees.

In Pi, the `wiki_doctor` native tool provides the same read-only diagnostic operation; see [Pi wiki tools](../../docs/pi-tools.md). For CLI workflows, run `doctor --json` through the package-relative launcher procedure in [focused workflows](../wiki/references/focused-workflows.md), bound to the currently loaded package; do not use an `obsidian-second-brain` bin on `PATH`, which may be a different installed copy or version. Summarize its reported config, dependencies, vault, cache exclusions, lifecycle, and retrieval status, then `readiness.stage`/`reason`, any non-ready `capabilities`, and `readiness.nextActions`. Present next actions as suggestions only: never run them, and treat any action with `mutates` or `requiresReview` as needing a separate explicit request through the reviewed setup or build workflow. A `supported` capability was not probed, and `appState: not-probed` is not evidence that Obsidian Git is syncing. It is diagnostic only: do not repair configs or exclusions, build indexes, edit pages, or finalize lifecycle state. If a content/link audit is requested, run the existing read-only middleware lint and report findings separately. Explain that suggested repairs require a separate explicit request and follow the existing reviewed setup or wiki write workflow.
