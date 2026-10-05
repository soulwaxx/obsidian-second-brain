# Init Mode

Build the wiki from scratch for a new repository or project.

**Vault setup (optional).** For a new or existing vault, first preview the
scaffold from the plugin checkout with
`python3 scripts/bootstrap-vault.py --vault /absolute/path/to/vault`, review its
plan, then apply with `--apply --confirm <planHash>` using the printed hash.
Bootstrap preserves existing notes/indexes/logs and does not initialize Git.
Scaffolding is separate from the reviewed `--configure` step, which selects the
vault and disables deprecated agent auto-commit after Obsidian Git is installed
and enabled. Preserve unrelated existing configuration. If the active integration reports a config
diagnostic, stop wiki writes and use the exact, explicitly approved repair
workflow in `SKILL.md`; do not guess the target vault or reset its config.
Obsidian Git is the version-control prerequisite; follow
[git-setup.md](git-setup.md) and review ignore rules before enabling backups.
Other optional Obsidian plugins are described in
[plugins.md](plugins.md); optional CSS and Graph View customizations are in
[css-snippets.md](css-snippets.md).

**Procedure:**

1. **Discover.** Resolve the configured vault and read its contract before the
   source repository's README, SKILL.md, and top-level structure.
   Use `git log --oneline -20` for context (see Git Discipline in
   [research-discipline.md](research-discipline.md)). Identify ~10 key areas:
   architecture, workflows, concepts, data model, integrations, operations,
   tests, extension points. For small repos (10 or fewer primary sources),
   scale to 3-4 areas.

2. **Plan.** List intended pages, evidence sources, relationship edges, and
   open questions. Use a temporary `wiki/_plan.md` when it helps track a large
   pass (see Planning Discipline in
   [authoring-standards.md](authoring-standards.md)).

3. **Entry point.** Follow the existing vault's entry-point convention. For a
   new vault, `wiki/quickstart.md` gives an overview, links, and a `## Backlog`.

4. **Content pages.** Create substantive, linked pages in the existing layout.
   Prefer a concise first pass; page counts and directories should follow the
   evidence and vault contract, not a fixed quota. Each page: what, why, how to
   start, gotchas, source references. For a starting directory
   layout and the frontmatter extras that suit this source type — website,
   repository, business, personal, research, or book/course — see
   [ingest-recipes.md](ingest-recipes.md).

5. **Middleware.** Follow pre-write authorization, post-write validation, and
   batch finalization in `SKILL.md`. Supported file tools use the lifecycle;
   without it, run the manual middleware checks. Repair invalid changed pages
   before publishing their batch. Obsidian Git owns version control.

6. **Cleanup.** Delete any temporary `_plan.md`. Run the Coverage Self-Check in
   [authoring-standards.md](authoring-standards.md). Leave backlog in
   quickstart.md.

Rules:

- Do not silently drop domains. If an area is not documented, backlog it.
- Do not document every file. Document architecture, workflows, concepts,
  data, integrations, operations, tests, and extension points.
- Establish the entry point before expanding the wiki; preserve existing hubs.
