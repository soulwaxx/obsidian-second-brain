# Research Discipline

How to gather evidence before writing. Use this when Init or Auto mode
investigates a repository, and when Update mode scopes a change.

---

## Git Discipline

Use git history to explain why code exists. During Init Mode, inspect recent
commits with `git log --oneline -20` and `git show` for relevant commits.
Use `git log -- <path>` to trace a file's history and `git blame` or
`git log -p --follow -- <path>` for evolutionary context.

During Update Mode, use `git diff HEAD~1 -- <path>` to scope what changed in
a specific area. Use `git log --oneline --since=<date>` for timeboxed scoping.

Do not over-index on ancient history. Focus on the last ~3 months or the
most recent 20 commits unless investigating a specific historical question.

---

## Investigation Discipline

Investigate one bounded area at a time:

- Start with a specific file, directory, or question.
- Inspect and summarize before writing or editing wiki files.
- Keep investigation notes internal. Synthesize the evidence into user-facing
  responses and wiki pages.
- Do not invoke middleware during evidence gathering.

---

## Provenance Discipline

Wiki-First Answering makes the wiki authoritative, which is exactly what makes
an untraceable claim expensive later. Four rules:

- `unsupported` is a legitimate and preferred outcome. A grounded refusal beats
  a confident invention.
- Preserve contradictory evidence on the page. Do not silently pick a winner.
- Never fabricate quotations, dates, page numbers, or evidence locators.
- A `type: source` page must carry a resolvable `source_url` and the date it
  was retrieved.
