# Markdown Standards

Wiki pages are OKF v0.2 concept documents authored in Markdown that Obsidian
also renders. The one conformance-critical rule is the internal-link syntax;
the rest is optional Obsidian presentation sugar.

## Internal links (standard Markdown)

Use **standard Markdown links** with the `.md` suffix and a meaningful label.
For a configured Obsidian vault, prefer a source-file-relative target: Obsidian
resolves these paths relative to the note, including when the vault contains
folders. Percent-encode spaces and special characters in each path component:

- From `wiki/index.md` to `wiki/concepts/index.md`:
  `[Concepts](./concepts/index.md)`.
- From `wiki/guides/index.md` to `wiki/concepts/retrieval.md`:
  `[RAG](../concepts/retrieval.md)`.
- From `wiki/guides/start.md` to `wiki/guides/space notes.md`:
  `[Space notes](./space%20notes.md)`.
- For special characters, for example:
  `[Research](../concepts/research%20%5B2025%5D.md)`.
- **Fragment to a heading:** `[ownership model](../concepts/architecture.md#ownership-model)`.

Existing OKF bundle-root-relative links such as
`[Retrieval](/concepts/retrieval.md)` remain valid for OKF reading and
conformance. They need not be rewritten solely for that reason; however, an OKF
lint success confirms OKF resolution, not compatibility with native Obsidian
resolution in every vault. For configured Obsidian links, use the source-file-
relative form above. Generated directory navigation likewise targets the
relative `./subdirectory/index.md` file, rather than a directory URL.

**Factual citations** use Markdown footnotes keyed to a `sources` entry `id`
(see `frontmatter.md`), e.g. `…per the recognition policy.[^rev-policy]`.

**Legacy wikilinks** (`[[concepts/retrieval]]`, `[[concepts/retrieval|RAG]]`)
remain readable — `lint.py` resolves them and they still produce backlinks —
but do not author new ones.

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
