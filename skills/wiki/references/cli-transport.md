# Obsidian CLI Transport

`obsidian-cli` ships inside Obsidian 1.12+ and is linked onto PATH as both
`obsidian-cli` and `obsidian` (see `modules/claude-obsidian.nix`). It talks to
the **running Obsidian app**, so it can answer questions the filesystem cannot:
resolved link graphs, alias-aware lookups, and Bases views.

Use it for **reads and graph queries only**. Keep **writes on the filesystem
tools** (Read/Write/Edit) so `validate.py` and `guard.py` stay in the path —
see Middleware Invocation in `SKILL.md`. CLI mutations bypass the file-tool
pre-write checks and post-write lifecycle, so this skill does not use them
for wiki pages.

---

## Precondition

The CLI requires Obsidian to be **running**. When it is not:

```
The CLI is unable to find Obsidian. Please make sure Obsidian is running and try again.
```

That is the only failure mode worth handling. Treat a non-zero exit or that
message as "CLI unavailable" and fall back to the filesystem equivalent listed
with each recipe. Never block work on the CLI; it is an accelerator, not a
dependency.

Probe once per session before relying on it:

```bash
obsidian-cli files total >/dev/null 2>&1 || echo "CLI unavailable; use filesystem"
```

---

## Syntax

Arguments are `key=value` pairs, not positionals:

```
obsidian-cli <command> [key=value ...]
```

- `path=` is an exact vault-relative path (`wiki/argocd.md`).
- `file=` resolves by name the way a wikilink does (`file=argocd`), including
  aliases.
- `vault=<name>` targets a specific vault; omit it when only one is open.
- Quote values containing spaces: `query="gateway api"`.

---

## Recipes

### Full-text search (fallback: `rg --type=md`)

```bash
obsidian-cli search query="gateway api" format=json limit=10
obsidian-cli search:context query="gateway api" limit=10   # with matching lines
```

Returns matching file paths. This is a substring search, **not** a ranked
retrieval — it does not replace `scripts/retrieve.py`, which is the ranked
path used by Wiki-First Answering. Reach for `search` when you know the exact
string; reach for `retrieve.py` when you know the topic.

### Backlinks (fallback: `rg "\[\[<basename>"`)

```bash
obsidian-cli backlinks path="wiki/argocd.md" counts
obsidian-cli links path="wiki/argocd.md"        # outgoing
```

Better than ripgrep here: it resolves aliases and heading/block subpaths, so it
catches `[[argocd|ArgoCD]]` and `[[argocd#Sync]]` that a naive grep for the
basename would miss or double-count.

### Link-graph health (no cheap filesystem equivalent)

```bash
obsidian-cli orphans     # files with no incoming links
obsidian-cli deadends    # files with no outgoing links
```

Use during the Coverage Self-Check (see `references/authoring-standards.md`)
to find pages the wiki never links to. Both list the whole vault, including
`_templates/`, `bin/`, and `AGENTS.md` — filter to `wiki/` before acting.

### Read a note (fallback: Read tool)

```bash
obsidian-cli read path="wiki/argocd.md"
obsidian-cli read file="argocd"     # alias-aware resolution
```

Prefer the Read tool for pages you intend to edit — it satisfies the
read-before-edit guard. Use `read file=` when you only have a wikilink target
and need to resolve which page it points at.

### Frontmatter properties (fallback: Read tool)

```bash
obsidian-cli property:read path="wiki/argocd.md" key=type
obsidian-cli properties                                  # vault-wide key list
```

To change a property, use a guarded Edit tool call, then run `validate.py`.
Do not use `property:set`: it bypasses the lifecycle hooks.

### Bases (fallback: read the `.base` YAML directly)

```bash
obsidian-cli bases
obsidian-cli base:query path="wiki/meta/dashboard.base" format=json
```

Returns the resolved row set. There is no way to resolve a Bases view without
the running app.

### Outline and file metadata

```bash
obsidian-cli outline path="wiki/argocd.md"   # headings only, cheap skim
obsidian-cli file path="wiki/argocd.md"      # size, dates, link counts
obsidian-cli aliases verbose                 # vault-wide alias map
```

`outline` is a cheap way to decide whether a page is worth a full read.

---

## What not to use

- `property:set`, `create`, `append`, `prepend`, `delete`, `move`, `rename` on `wiki/` pages —
  they bypass `guard.py` and `validate.py`. Use the filesystem tools.
- `daily:*` — this vault has no Daily Notes plugin convention; worklogs are
  ordinary pages under the naming rules in `references/authoring-standards.md`.
- `plugin:*`, `restart`, `reload` — they mutate the user's app, not the wiki.
