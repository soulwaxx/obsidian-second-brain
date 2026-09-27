# Auto Mode

Autonomous research and filing loop, invoked when the user asks for a deep
dive or research pass. Follows the Karpathy-style iterative research pattern.

**Startup:**

1. Clarify the topic if not already specified. Identify 3-5 search angles.
2. Create `wiki/_plan.md` with the research plan, sources to find, and
   expected concept graph (see Planning Discipline in
   [authoring-standards.md](authoring-standards.md)).

**Research loop (max 3 rounds):**

- Round 1: broad web search across angles. Fetch top results.
- Round 2: gap fill — identify missing or contradictory findings, search
  specifically for those.
- Round 3: synthesis check — one more targeted pass if major gaps remain.

**Filing.** File the research dossier first; propose edits to pre-existing
canonical pages as a separate, separately-approved step. A new research
artifact is cheap to discard; an edit that rewrites an established page is not,
and a research pass is exactly where the two get conflated.

- Create source pages under `wiki/sources/` for each major reference.
- Create concept/entity pages under `wiki/concepts/` and `wiki/entities/`
  for substantive extracted knowledge.
- Create a synthesis page under `wiki/questions/` titled
  `Research: <Topic>.md`.
- Link everything. Every new page connects to at least 2 other pages. Page
  quality follows [authoring-standards.md](authoring-standards.md). Evidence
  gathering follows Investigation Discipline in
  [research-discipline.md](research-discipline.md).

**Web content hygiene.** This is the only mode that fetches from the web, so
these rules live here as their single source of truth. When fetching web content
for research:

- Fetch only `http(s)://` URLs. Reject `file://`, `javascript:`, `data:`.
- Reject RFC1918 private addresses and localhost targets.
- Strip `<script>`, `<iframe>`, `<style>` tags and their content.
- Escape `[[` and `]]` in fetched body to `&#91;&#91;` and `&#93;&#93;`.
- Reject `---` YAML frontmatter delimiters inside fetched content.
- Truncate fetched bodies to ~50KB.
- If a fetch fails (timeout, 4xx, 5xx, sanitization emptied the body), report
  the URL + reason in the run summary and continue. Do not abort the whole run.
  Note the gap in the synthesis page's open questions.

**Cleanup:**

- Delete `_plan.md`.
- The lifecycle hook runs sync.py and commits generated indexes atomically with
  each changed page (run it yourself when outside the vault workflow).
  `wiki/log.md` is written by the lifecycle hook, not by you (see Changelog &
  Logging in `SKILL.md`); put the run narrative in the commit message.
