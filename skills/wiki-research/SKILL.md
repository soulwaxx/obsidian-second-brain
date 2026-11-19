---
name: wiki-research
description: Conduct a bounded investigation of a requested question using the wiki and evidence accessible to the current client.
---

# Bounded Wiki Research

Follow [focused workflows](../wiki/references/focused-workflows.md) and the evidence and authoring rules in [the wiki skill](../wiki/SKILL.md), [research discipline](../wiki/references/research-discipline.md), and [authoring standards](../wiki/references/authoring-standards.md).
Apply the shared [trust boundary](../wiki/SKILL.md#trust-boundary) and
[wiki-first answering](../wiki/SKILL.md#wiki-first-answering) rules; this focused
intent does not create separate permissions or evidence guarantees.

Define the question and limits, inspect relevant wiki pages first, then use only selected local sources or research tools actually available in the current task. Keep source-backed claims, inference, contradictions, and unanswered questions distinct. Cite only evidence actually inspected. Answer without writing unless the user explicitly requested filing; then make only a relevant, bounded canonical-page update through the client's write path ([tool surface by client](../wiki/references/tool-surface.md)): file tools in a hook-guarded client (Claude Code, Pi, omp), or only `wiki_batch_inspect` → show the user its summary and `planHash` → explicit approval → `wiki_batch_apply` in an MCP-only client, never editing vault files directly with file or shell tools. The package does not provide web search, web fetching, or web/media adapters. For a user-selected local UTF-8 text file, an explicitly requested immutable source capture is available through the separate reviewed capture workflow; it preserves the original bytes and hashes and can link selected existing wiki pages. Capture is optional and does not itself make or file claims; it is not available here for an MCP-only agent (run from Claude, Pi or the CLI). In a hook-guarded client use a reviewed recoverable batch only for larger coordinated wiki edits, not for ordinary small changes. See [focused workflows](../wiki/references/focused-workflows.md) for the available commands and limits.
