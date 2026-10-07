---
name: wiki-ingest
description: Review one user-selected local or provided source and file a bounded, evidence-grounded wiki update when requested.
---

# Ingest Selected Material

Follow [focused workflows](../wiki/references/focused-workflows.md) and [the wiki skill](../wiki/SKILL.md) for vault selection, page discovery, provenance, authoring, and the mandatory guard/validate/sync order.

Work only from the source or material the user selected. Identify what it supports, what remains uncertain, and whether an existing canonical page is the right home. Make no wiki changes unless filing is authorized. Prefer a normal guarded, validated lifecycle write for a small update; for a larger coordinated multi-page change, an explicit reviewed batch is available but is not required for every edit.

For an explicitly selected local UTF-8 text file that should be preserved as source evidence, the package can create an immutable, content-hashed record under `.raw/agent-captures/`, with optional links and content hashes for selected existing wiki pages. This is a separate opt-in operation: inspect with `capture-inspect`, review the bounded summary and exact `planHash`, then apply with `capture-apply --plan-hash <exact-hash>`. It does not authorize filing or rewriting wiki pages, and it does not extract web pages, PDFs, audio, video, or other media. See [focused workflows](../wiki/references/focused-workflows.md) for command syntax and limits. Without that explicit capture request, do not archive whole sources or transcripts. Ask before expanding the topic or source scope.
