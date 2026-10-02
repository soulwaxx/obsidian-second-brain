# Init Mode

Build the wiki from scratch for a new repository or project.

**Vault setup (optional).** For a new or existing vault, first preview the
scaffold from the plugin checkout with
`python3 scripts/bootstrap-vault.py --vault /absolute/path/to/vault`, review its
plan, then apply with `--apply --confirm <planHash>` using the printed hash.
Bootstrap does not initialize Git and preserves existing notes/indexes/logs. A
new config uses `autoCommit: false`; existing config values and omitted
feature defaults are unchanged. If the active integration reports a config
diagnostic, stop wiki writes and use the exact, explicitly approved repair
workflow in `SKILL.md`; do not guess the target vault or reset its config.
Git history is a separate opt-in; follow [git-setup.md](git-setup.md) and create
`.gitignore` before staging. Optional Obsidian plugins are described in
[plugins.md](plugins.md); optional CSS and Graph View customizations are in
[css-snippets.md](css-snippets.md).

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
   resulting write and before commit. Validation failure blocks synchronization until the page is repaired.
   The lifecycle validates changed Markdown and synchronizes indexes after each
   successful page write, independently of optional auto-commit. Run sync.py
   yourself outside the vault workflow.

6. **Cleanup.** Delete `_plan.md`. Run the Coverage Self-Check in
   [authoring-standards.md](authoring-standards.md). Leave backlog in
   quickstart.md.

Rules:

- Do not silently drop domains. If an area is not documented, backlog it.
- Do not document every file. Document architecture, workflows, concepts,
  data, integrations, operations, tests, and extension points.
- Quickstart first, then linked section pages. Not the reverse.
