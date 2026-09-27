# Markdown Standards

Wiki pages are OKF v0.2 concept documents authored in Markdown that Obsidian
also renders. The one conformance-critical rule is the internal-link syntax;
the rest is optional Obsidian presentation sugar.

## Internal links (canonical: Markdown links)

Concept-to-concept links use **standard Markdown links** so any OKF consumer —
not just Obsidian — can resolve them and build the backlink graph. Prefer
bundle-root-relative targets, keep the `.md` suffix, and write a meaningful
label:

- **Canonical:** `[Retrieval-Augmented Generation](/concepts/retrieval-augmented-generation.md)`
- **File-relative** is also valid: `[RAG](../concepts/retrieval.md)`,
  `[Beta](beta.md)`.
- **Fragment to a heading:** `[the ownership model](/concepts/architecture.md#ownership-model)`.
- **Factual citations** use Markdown footnotes keyed to a `sources` entry `id`
  (see `frontmatter.md`), e.g. `…per the recognition policy.[^rev-policy]`.

**Legacy wikilinks** (`[[concepts/retrieval]]`, `[[concepts/retrieval|RAG]]`)
remain readable — `lint.py` resolves them and they still produce backlinks —
but do not author new ones. To bulk-convert an existing vault, run
`scripts/migrate_wikilinks.py <vault-root>` (dry run by default; `--apply` to
write). It rewrites body wikilinks to Markdown links, skips embeds and code,
and reports any target it cannot resolve.

## Obsidian extensions (optional)

These render in Obsidian and are tolerated by OKF consumers as plain text; use
them for presentation, never for concept-to-concept links:

- **Embeds:** `![[file.png]]`, `![[document.pdf]]`, `![[page#heading]]`.
  Prefer `![alt](/assets/file.png)` for portability.
- **Callouts:**

  ```
  > [!note] Title
  > Body text.
  ```

  Types: `note`, `tip`, `warning`, `danger`, `info`, `abstract`, `question`,
  `example`, `quote`.
- **Properties:** YAML frontmatter only (described in `references/frontmatter.md`).
- **Tags:** Obsidian tags inline like `#tag` or in frontmatter `tags:` list.
- **Highlighting:** `==highlighted text==`.
- **Math:** `$$ LaTeX $$` for blocks, `$ LaTeX $` for inline.
- **Checklists:** `- [ ]` and `- [x]` in list items.
- **Comments:** `%% comment %%` (not visible in reading mode).
- **Tables:** Standard GFM tables with `|` pipes.
- **Code blocks:** Fenced with language tags: ` ```python `.
- **Escaping:** Use `\` before literal underscores, asterisks, or brackets
  in prose that should not be treated as formatting.
