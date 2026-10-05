# Update Mode

Surgical updates to an existing wiki.

**Procedure:**

1. **Inspect existing docs.** Read the relevant wiki pages first. Read the
   backlog in the vault's entry-point page (see Entry Point & Documentation
   Layout in [authoring-standards.md](authoring-standards.md)).

2. **Scope the change.** Build a proportional diff plan (in working context,
   or a temporary `_plan.md` for a major update; see Planning Discipline in
   [authoring-standards.md](authoring-standards.md)). Scope what changed with
   Git Discipline in
   [research-discipline.md](research-discipline.md)):

   ```
   ## Diff Plan
   - Source change: <file> — <what changed>
   - Docs affected: <wiki/page.md> — <what section needs updating>
   - Edit: <specific sentence or section> — <new content>
   - Rationale: <why this edit is correct>
   ```

3. **Promote relevant backlog entries** when the approved update supplies the
   missing evidence. Leave unrelated backlog work for a separate task.

4. **Edit surgically.** Preserve useful structure and wording from the existing
   page. Do not refresh every page. Do not make formatting-only edits. On every
   substantive edit, set the page's `last_updated:` frontmatter to today's date
   (ISO 8601). If the page has legacy `updated`, migrate it while making the
   substantive edit. This stamp records a content revision, not proof that all
   claims are current. Git history can be rewritten; compare source evidence
   and freshness metadata rather than blanket-restamping old pages.

5. **Soft diff budget.** For a small source change, prefer 1-2 new pages and
   surgical canonical updates. Do not skip relevant cross-page corrections
   merely to meet a page quota. If nothing meaningful changed in the wiki's
   domain, say the wiki is already current and do nothing.

6. **Cleanup.** Delete any temporary `_plan.md`. On a major update, run the Coverage
   Self-Check in [authoring-standards.md](authoring-standards.md). No-op is
   acceptable.
