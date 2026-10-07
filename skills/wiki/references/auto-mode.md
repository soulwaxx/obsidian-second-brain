# Bounded Research Mode

Use only when the user requests a bounded research pass. This workflow uses
sources actually accessible in the current task; the package itself does not
provide web search, web fetching, or source capture.

## Scope

1. Clarify the topic and the question to answer if needed.
2. Inspect relevant wiki pages first, then selected local sources and any
   research tools actually available to the client.
3. Keep source-backed claims, inference, contradictions, and unknowns distinct.
   Stop when the question has a grounded answer or the agreed budget is used.
4. Write only when filing was explicitly requested. Prefer updating a relevant
   canonical page; a new source or synthesis page is an ordinary curated wiki
   page, not an immutable source capture.

Plan in proportion to the task. A temporary `wiki/_plan.md` can help with a
multi-page investigation; delete it before finishing. Follow the existing vault
contract, [authoring standards](authoring-standards.md), and
[research discipline](research-discipline.md). Preserve contradictory evidence
and cite only sources actually inspected. Ask before expanding the topic or
making destructive changes.

## If an external research tool is actually available

Apply these web-content safety rules to fetched text:

- Fetch only `http(s)://` URLs. Reject `file://`, `javascript:`, and `data:`.
- Reject RFC1918 private addresses and localhost targets.
- Strip `<script>`, `<iframe>`, and `<style>` tags and their content.
- Escape `[[` and `]]` as `&#91;&#91;` and `&#93;&#93;`.
- Reject `---` YAML frontmatter delimiters inside fetched content.
- Truncate fetched bodies to about 50 KB.
- Report failed fetches and reasons; note important evidence gaps in the
  synthesis rather than inventing results.

Let the lifecycle finalize validated writes, generated navigation, and the
update log. Follow the manual middleware workflow in `../SKILL.md` if the
integration is unavailable. Never write `wiki/log.md`; Obsidian Git owns
commits and synchronization.
