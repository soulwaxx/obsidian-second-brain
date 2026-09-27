# Update Mode

Surgical updates to an existing wiki.

**Procedure:**

1. **Inspect existing docs.** Read the relevant wiki pages first. Read the
   backlog in the vault's entry-point page (see Entry Point & Documentation
   Layout in [authoring-standards.md](authoring-standards.md)).

2. **Scope the change.** Build a diff plan (in a fresh `_plan.md`, see Planning
   Discipline in [authoring-standards.md](authoring-standards.md); scope what
   changed with Git Discipline in
   [research-discipline.md](research-discipline.md)):

   ```
   ## Diff Plan
   - Source change: <file> — <what changed>
   - Docs affected: <wiki/page.md> — <what section needs updating>
   - Edit: <specific sentence or section> — <new content>
   - Rationale: <why this edit is correct>
   ```

3. **Promote backlog entries** if the update touches an area that has deferred
   entries. If you are updating a page next to a backlogged area, cover the
   backlogged material too.

4. **Edit surgically.** Preserve useful structure and wording from the existing
   page. Do not refresh every page. Do not make formatting-only edits. On every
   substantive edit, set the page's `last_updated:` frontmatter to today's date
   (ISO 8601). If the page has legacy `updated`, migrate it while making the
   substantive edit. This stamp — not git history — is the authoritative
   staleness signal: a vault history rewrite can flatten per-file git dates, so
   freshness is judged by `last_updated:` vs `created`/`created_date`.

5. **Soft diff budget.** If fewer than 5 source files changed, create at most
   1-2 wiki pages. If nothing meaningful changed in the wiki's domain, say
   the wiki is already current and do nothing.

6. **Cleanup.** Delete `_plan.md`. On a major update, run the Coverage
   Self-Check in [authoring-standards.md](authoring-standards.md). No-op is
   acceptable.
