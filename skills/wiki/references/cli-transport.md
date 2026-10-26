# Obsidian CLI Transport

`obsidian-cli` is an optional command-line client available with supported
Obsidian releases. Installation and command names vary by platform; consult the
official Obsidian documentation and probe the command available on this
machine. It talks to the **running Obsidian app**, so it can answer questions the filesystem cannot:
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

Probe once per session before relying on it. Try the documented command name
for the installed version (`obsidian` or `obsidian-cli`). Native CLI commands
without a vault selector may use the vault containing the terminal's current
directory or the currently active vault; neither is a safe substitute for the
configured vault. Use `vaults verbose` to find the entry whose path exactly
matches the configured vault root, then select that entry by its exact name or
ID before every command. Verify the selection with `vault info=path` and
compare the returned path exactly with the configured root. If there is no
unique exact path match, the path check fails, or the CLI is unavailable, do
not run native reads; use the filesystem or package retrieval instead. Never
fall back to whichever vault happens to be active.

```bash
obsidian-cli vaults verbose || exit 1
# Set this only to the name or ID for the entry whose reported path is the configured root.
NATIVE_VAULT='<verified vault name or ID>'
CONFIGURED_VAULT='<configured absolute vault root>'
selected=$(obsidian-cli vault="$NATIVE_VAULT" vault info=path) || exit 1
[ "$selected" = "$CONFIGURED_VAULT" ] || { echo "Native CLI vault mismatch; use filesystem" >&2; exit 1; }
obsidian-cli vault="$NATIVE_VAULT" files total
```

---

## Syntax

Arguments are `key=value` pairs, not positionals. The vault selector must
precede the command:

```
obsidian-cli vault="<verified name or ID>" <command> [key=value ...]
```

- `path=` is an exact vault-relative path (`wiki/argocd.md`).
- `file=` resolves by name the way a wikilink does (`file=argocd`), including
  aliases.
- `vault=<name-or-id>` targets a specific vault and must come before the command. Do not omit it, even when only one vault appears open.
- Quote values containing spaces: `query="gateway api"`.

---

## Recipes

### Full-text search (fallback: `rg --type=md`)

```bash
obsidian-cli vault="$NATIVE_VAULT" search query="gateway api" format=json limit=10
obsidian-cli vault="$NATIVE_VAULT" search:context query="gateway api" limit=10   # with matching lines
```

Returns matching file paths. This is a substring search, **not** a ranked
retrieval — it does not replace `scripts/retrieve.py`, which is the ranked
path used by Wiki-First Answering. Reach for `search` when you know the exact
string; reach for `retrieve.py` when you know the topic.

### Backlinks (fallback: `rg "\[\[<basename>"`)

```bash
obsidian-cli vault="$NATIVE_VAULT" backlinks path="wiki/argocd.md" counts
obsidian-cli vault="$NATIVE_VAULT" links path="wiki/argocd.md"        # outgoing
```

Better than ripgrep here: it resolves aliases and heading/block subpaths, so it
catches `[[argocd|ArgoCD]]` and `[[argocd#Sync]]` that a naive grep for the
basename would miss or double-count.

### Link-graph health (no cheap filesystem equivalent)

```bash
obsidian-cli vault="$NATIVE_VAULT" orphans     # files with no incoming links
obsidian-cli vault="$NATIVE_VAULT" deadends    # files with no outgoing links
```

Use during the Coverage Self-Check (see `references/authoring-standards.md`)
to find pages the wiki never links to. Both list the whole vault, including
`_templates/`, `bin/`, and `AGENTS.md` — filter to `wiki/` before acting.

### Read a note (fallback: Read tool)

```bash
obsidian-cli vault="$NATIVE_VAULT" read path="wiki/argocd.md"
obsidian-cli vault="$NATIVE_VAULT" read file="argocd"     # alias-aware resolution
```

Prefer the Read tool for pages you intend to edit — it satisfies the
read-before-edit guard. Use `read file=` when you only have a wikilink target
and need to resolve which page it points at.

### Frontmatter properties (fallback: Read tool)

```bash
obsidian-cli vault="$NATIVE_VAULT" property:read path="wiki/argocd.md" key=type
obsidian-cli vault="$NATIVE_VAULT" properties                                  # vault-wide key list
```

To change a property, use a guarded Edit tool call, then run `validate.py`.
Do not use `property:set`: it bypasses the lifecycle hooks.

### Bases (fallback: read the `.base` YAML directly)

```bash
obsidian-cli vault="$NATIVE_VAULT" bases
obsidian-cli vault="$NATIVE_VAULT" base:query path="wiki/meta/dashboard.base" format=json
```

Returns the resolved row set. There is no way to resolve a Bases view without
the running app.

### Outline and file metadata

```bash
obsidian-cli vault="$NATIVE_VAULT" outline path="wiki/argocd.md"   # headings only, cheap skim
obsidian-cli vault="$NATIVE_VAULT" file path="wiki/argocd.md"      # size, dates, link counts
obsidian-cli vault="$NATIVE_VAULT" aliases verbose                 # vault-wide alias map
```

`outline` is a cheap way to decide whether a page is worth a full read.

---

## What not to use

- `property:set`, `create`, `append`, `prepend`, `delete`, `move`, `rename` on `wiki/` pages —
  they bypass `guard.py` and `validate.py`. Use the filesystem tools.
- `daily:*` — this vault has no Daily Notes plugin convention; worklogs are
  ordinary pages under the naming rules in `references/authoring-standards.md`.
- `plugin:*`, `restart`, `reload` — they mutate the user's app, not the wiki.
