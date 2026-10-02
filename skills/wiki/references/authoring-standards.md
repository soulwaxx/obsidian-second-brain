# Authoring Standards

How to plan, model, structure, and finish wiki pages. Reach for this whenever
you are **writing** to the vault — Init, Update, and Auto modes all pass
through here. Chat mode does not.

---

## Planning Discipline

Before writing or updating wiki pages, create a `_plan.md` file in the relevant
wiki directory. The plan lists:

- Intended pages to create or update
- Source evidence for each page (file paths, URLs, git commits)
- Relationship edges between concepts being documented, formatted as:
  `<source concept> -- <relationship meaning> --> <target concept>`
- Any remaining questions or gaps

Use the plan to verify completeness and consistency before writing. Delete
`_plan.md` before finishing. Never leave it in the wiki.

For Update Mode, the plan is a diff plan: what source changed, what docs are
affected, what edit is needed, and why.

---

## OKF Relationship Modeling

Every non-reserved `.md` file is a concept node. Standard Markdown links
between concept documents — `[Beta](/concepts/beta.md)` — are directed edges in
the knowledge graph. Use Markdown links, not wikilinks, for new content (see
`markdown-standards.md`).

Model meaningful relationships. Common relationship types:

- **dependency** — A depends on B
- **data-flow** — A produces data consumed by B
- **ownership** — A owns/manages B
- **lifecycle** — A creates, deploys, or destroys B
- **implementation-of** — A implements the interface/contract defined by B
- **alternative-to** — A is an alternative to B
- **part-of** — A is a component of B

Put links in prose that explains the relationship. Do not just list links. Do
not add reciprocal links without evidence — only link bidirectionally when the
relationship is symmetric or when each direction is independently evidenced.

Each substantive concept should connect to at least 2 other concepts. But do
not mint thin concepts solely to create edges. A concept is worth a page when
it has standalone substance: a definition, rationale, behavioral rules, or
internal structure.

---

## Existing Documentation Discipline

Treat existing READMEs, docs, and SKILL.md files as primary sources. Summarize
and link to them, do not duplicate their content. A wiki page can reference
a README with a single sentence and a relative link.

If existing documentation conflicts with what you discover from source
inspection, flag the conflict and prefer the source. Add a note in the wiki:
"The existing README claims X, but the source code shows Y. This may be stale."

---

## Section Quality Rules

- Do not create a directory unless it represents a real documentation area
  that will contain multiple pages.
- A section directory should have multiple substantive pages. A single-file
  directory is acceptable only when the page is substantial and likely to grow.
- Avoid thin pages. Merge stubs into broader pages rather than creating many
  tiny files.
- Prefer headings inside broader pages over creating many small directories.
- Each page should cover: what it does, why it exists, where to start, what
  to watch for, and key source references.
- For small scopes (10 or fewer primary sources): create a quickstart plus
  at most 1-2 supporting pages.
- Review the wiki tree before finishing. Merge or remove low-value directories
  and stubs.

---

## Entry Point & Documentation Layout

Every wiki maintains one entry-point page: an overview plus links to every
major section. If the vault already has a root convention file (e.g.
`WIKI.md`), that file names the entry point — use whatever it says. When
scaffolding a new vault that has no such convention yet, default to
`wiki/quickstart.md`.

The entry-point page contains:

- A one-paragraph overview of what this vault documents.
- Links to every major section directory with one-line descriptions.
- The `## Backlog` section at the end listing deferred areas.

Navigation rides the generated `index.md` hierarchy: `sync.py` writes a
deterministic `index.md` in every directory listing its concepts and
subdirectories, and the root `wiki/index.md` is the top of the tree. These are
middleware-generated — never author or edit an `index.md` by hand. Section hubs
(pages with `type: index`) remain the human-authored overviews within that
tree. This `index.md` hierarchy is what Wiki-First Answering falls back to when
`retrieve.py` is unavailable.

`index.md` and `log.md` are the only reserved filenames (OKF v0.2). `log.md` is
hook-owned — you never write it. See Changelog & Logging in `SKILL.md`.

---

## Documentation Goals

Someone with zero knowledge of this repository should start at the vault's
entry-point page and come away understanding what the project is, how it is
structured, and where to look for specific information.

Future agents (including yourself in later sessions) should be able to answer
most questions by reading the wiki rather than exploring raw source files.

Every page should capture:

- **What** the thing is.
- **Why** it exists (the decision context, the problem it solves).
- **How** a reader engages with it (where to start, what commands to run).
- **Watch for** — pitfalls, gotchas, stale assumptions.
- **Source references** — file paths, git history markers, upstream docs.

Capture both technical and business logic. Explain why code exists, not just
what it does. Use clear markdown with stable links (vault-relative paths, not
absolute URLs for internal resources).

Keep the wiki concise. One canonical home per concept — do not duplicate
content across pages. Use links to reference instead.

Include change-oriented guidance: when a future agent needs to update this
page, what should they look for? What signals staleness?

---

## Coverage Self-Check

Before finishing any init or major update:

- Verify every identified area is documented or explicitly backlogged.
- Audit the concept graph: every link resolves, cross-domain relationships
  are linked, no orphan pages exist.
- Verify `_plan.md` is deleted.
- Verify that directory indexes are coherent (they will be regenerated by
  sync.py, but the pages they reference should exist).
- Leave deferred areas captured in a `## Backlog` section at the end of the
  vault's entry-point page. Each backlog entry should name what is missing
  and why it was deferred (e.g., "not a priority yet", "waiting on source
  access", "needs more evidence").

**Compounding KPI.** A healthy vault compounds — pages get revisited and
enriched, not written once and abandoned. Track the count of pages with three
or more substantive revisions (proxy: `last_updated:` present and later than
`created`, or three or more `## YYYY-MM-DD` entries naming the page in
`log.md`). Legacy `updated` is accepted as input; migrate it to `last_updated`
when a substantive edit already touches the page. If that count does not grow
over time, the vault is an append-only dump, not a compounding brain — surface
it rather than adding more pages.
