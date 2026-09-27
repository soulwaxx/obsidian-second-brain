# Init Mode

Build the wiki from scratch for a new repository or project.

**Vault setup (new vaults only).** If the vault has no git repository yet,
follow [git-setup.md](git-setup.md) to initialize history and write-protection;
it also covers the Obsidian plugins in [plugins.md](plugins.md). To make the
file explorer colour-coded by folder type and add custom callout styles, apply
the snippets in [css-snippets.md](css-snippets.md). Skip both when the vault is
already established.

**Procedure:**

1. **Discover.** Read the repo's README, SKILL.md, and top-level structure.
   Use `git log --oneline -20` for context (see Git Discipline in
   [research-discipline.md](research-discipline.md)). Identify ~10 key areas:
   architecture, workflows, concepts, data model, integrations, operations,
   tests, extension points. For small repos (10 or fewer primary sources),
   scale to 3-4 areas.

2. **Plan.** Create `wiki/_plan.md` listing intended pages, evidence sources,
   relationship edges, and open questions (see Planning Discipline in
   [authoring-standards.md](authoring-standards.md)).

3. **Quickstart.** Create `wiki/quickstart.md` first. It is the entry point:
   overview, links to all major sections, and a `## Backlog` section.

4. **Section pages.** Create linked section pages. Max 8 pages unless the repo
   is very large. Section directories for major topic areas. Each page: what,
   why, how to start, gotchas, source references. For a starting directory
   layout and the frontmatter extras that suit this source type — website,
   repository, business, personal, research, or book/course — see
   [ingest-recipes.md](ingest-recipes.md).

5. **Middleware.** Run guard.py before each write, then validate.py after the
   resulting write and before commit. Validation failure blocks auto-commit
   until the page is repaired. Indexes regenerate and commit atomically with
   the changed page (run sync.py yourself outside the vault workflow).

6. **Cleanup.** Delete `_plan.md`. Run the Coverage Self-Check in
   [authoring-standards.md](authoring-standards.md). Leave backlog in
   quickstart.md.

Rules:

- Do not silently drop domains. If an area is not documented, backlog it.
- Do not document every file. Document architecture, workflows, concepts,
  data, integrations, operations, tests, and extension points.
- Quickstart first, then linked section pages. Not the reverse.
