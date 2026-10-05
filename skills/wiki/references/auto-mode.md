# Auto Mode

Autonomous research and filing loop, invoked when the user asks for a deep
dive or research pass. Follows the Karpathy-style iterative research pattern.

**Startup:**

1. Clarify the topic if not already specified. Identify 3-5 search angles.
2. Plan the research, sources to find, and expected concept graph. Use a
   temporary `wiki/_plan.md` when the scope benefits from it (see Planning
   Discipline in [authoring-standards.md](authoring-standards.md)).

**Research loop (usually 1-3 rounds):**

Stop when the scoped question has a grounded answer, or when the agreed budget
is exhausted. The rounds below are guidance rather than a fixed ceiling.

- Round 1: broad web search across angles. Fetch top results.
- Round 2: gap fill — identify missing or contradictory findings, search
  specifically for those.
- Round 3: synthesis check — one more targeted pass if major gaps remain.

**Filing.** An approved research pass authorizes relevant, evidence-backed
updates to existing canonical pages as well as new source and synthesis pages.
Preserve useful content and contradictory evidence. Ask separately before bulk
deletion, destructive restructuring, or expanding the approved topic.

- Follow the vault contract and existing layout. `sources/`, `concepts/`,
  `entities/`, and `questions/` are suggestions, not mandatory directories.
- Create source pages for major references and substantive concept/entity
  pages only when they add a useful canonical home.
- File the synthesis and open questions in a page appropriate to the vault.
- Add meaningful links without a numeric quota. Page quality follows
  [authoring-standards.md](authoring-standards.md). Evidence gathering follows
  Investigation Discipline in [research-discipline.md](research-discipline.md).

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

- Delete any temporary `_plan.md`.
- Let the lifecycle finalize validated changes, generated navigation, and the
  update log; Obsidian Git owns commits and synchronization. Follow the manual
  middleware workflow in `SKILL.md` when the integration is unavailable.
  Never write `wiki/log.md` yourself. Put research narrative and unresolved
  questions in the synthesis page, not in an agent-generated commit.
