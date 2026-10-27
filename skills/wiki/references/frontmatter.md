# OKF v0.2 Frontmatter Requirements

Every non-reserved `.md` file in `wiki/` is an OKF **concept document** and MUST
carry a YAML frontmatter block delimited by `---`. Under Open Knowledge Format
v0.2, `type` is the **only required field** — a page carrying just `type` is
fully conformant. Everything else is optional; absence carries meaning (an
unverified page is distinguishable from a verified one) and is never an error.

`validate.py` enforces this and, per OKF §11 (permissive conformance), never
rejects a page for unrecognized fields. It reads both v0.2 pages and legacy
v0.1 shapes so an existing vault keeps validating without a rewrite.

## Required

- `type` (string) — the concept's kind. Consumers use it for routing,
  filtering, and presentation, and MUST tolerate unknown values. Not registered
  centrally. Values in active use: `concept`, `reference`, `index`, `project`,
  `meta`, `question`, `entity`, `analysis`, `comparison`, `fold`, `synthesis`,
  `source`. `index` marks a human-authored section-hub page — a naming
  convention only: `sync.py` generates the reserved directory `index.md` files
  purely from directory structure and never reads `type`, so no field value is
  mechanically enforced. Also defined but reserved: `decision`, `domain`.

## Recommended

- `title` (string) — display name. Defaults to the filename stem when absent
  (used by `sync.py` index generation).
- `description` (string) — a single sentence summarizing the concept. Used in
  index listings, search snippets, and previews. This is the canonical preview
  field in v0.2 (legacy `summary` may remain when it carries longer content).
- `resource` (string) — a URI uniquely identifying the underlying asset the
  concept describes (`https://…`, a file path, a package name). Absent for
  concepts that describe abstract ideas. Supersedes legacy `source_url`, which
  remains accepted input; do not add it alongside canonical `resource` merely
  for compatibility.
- `tags` (list of strings) — cross-cutting categorization. Quote numeric-only
  values (`- "2026"`) so YAML keeps them strings; a bare `2026` parses as an int
  and `validate.py` rejects it.

## Optional families (provenance, trust, lifecycle)

These make "where did this come from", "how much should I trust it", and "is it
still current" answerable from frontmatter. All optional; all validated
leniently.

- `sources` (list of mappings) — provenance. Each entry has at least `id` or
  `resource`, plus optional credibility signals `author`, `usage_count`,
  `last_modified`. When an entry carries `usage_count`, add a sibling
  `usage_window: { from: <ISO 8601>, to: <ISO 8601> }` (OKF §5.1) that frames
  the count's observation range; a single entry may override the shared window
  with its own `usage_window`. Per-claim attribution uses Markdown footnotes
  keyed to a source `id` (see `markdown-standards.md`), not a body
  `# Citations` list. Legacy scalar and string-list `sources` remain accepted
  migration inputs; new bundles use mappings rather than a fabricated singleton
  URL.

  ```yaml
  sources:
    - id: fpa-handbook
      resource: https://wiki.acme/finance/fpa-handbook
      title: FP&A reporting handbook
      author: team:finance-fpa
      usage_count: 5000
      last_modified: 2026-04-02
  usage_window: { from: 2026-06-01, to: 2026-06-30 }
  ```

- `generated` (mapping) — how the current content was produced:
  `generated: { by: <actor>, at: <ISO 8601> }`. Records the last content change
  (supersedes legacy `timestamp`).
- `verified` (mapping, or list of mappings) — `{ by: <actor>, at: <ISO 8601> }`.
  Its presence and actor kind describe the declared trust tier (unverified /
  machine-confirmed / human-reviewed), not independently attested review or
  proof of factual support/current truth. Apply the shared
  [evidence-state distinctions](../SKILL.md#wiki-first-answering); a recent
  `generated.at`, `verified.at`, or future `stale_after` alone verifies no claim.
- `status` (string) — lifecycle state, e.g. `stable`, `draft`, `deprecated`.
- `stale_after` (date) — the date past which the concept should be treated as
  stale.

**Actor convention** for `generated.by` and `verified[].by`: `<producer>/<version>`
for agents (`reference_agent/gemini-2.5-pro`), `human:<id>` for people
(`human:ahormati`), `process:<id>` for automated processes
(`process:finance-nightly`).

## Attested Computation (OKF §10) — intentionally out of scope

OKF v0.2 defines a `type: Attested Computation` concept with `runtime`,
`parameters`, `computation`, `executor`, and `attester` fields, plus a
`# Computation` body heading, for values a consumer can re-run and verify. This
is a data-catalog feature; this vault is a personal knowledge brain with no
sanctioned computations, so it is deliberately unimplemented — not an oversight.
`validate.py` accepts such a page (it is a valid `type` with extra keys OKF
requires consumers to tolerate) but enforces none of the §10 contract. If the
vault ever needs attested metrics, add the type here and extend `validate.py`
to require `runtime` for it.

## Vault extensions (producer-defined, preserved)

OKF preserves unknown producer keys, and this vault relies on several. Never
drop a key just because the spec does not name it:

- `last_updated` (ISO date) — the vault's staleness signal. Use it on every new
  page and substantive edit; retrieval and the compounding KPI consume it. A
  recommended extension, no longer spec-required; format-checked when present.
  Legacy `updated` is accepted as input, but when a substantive edit already
  touches such a page, migrate it to `last_updated`.
- `created` / `created_date` — freshness baseline (freshness = `last_updated`
  vs `created`).
- `related`, `aliases`, `recall_triggers`, `category`, `domain`, `status`,
  `confidence`, `source_url` (legacy), `timestamp` (legacy).

### Source dates

These source-date fields are optional, producer-defined conventions, not new
OKF requirements. Apply the stronger [source-authoring guidance](research-discipline.md#provenance-discipline)
when gathering evidence; the validator checks structure, not source quality.

| Field | Meaning |
| --- | --- |
| `retrieved` / `fetched` | When the source was actually accessed or fetched. Preserve either existing spelling; do not duplicate them to satisfy the guide. |
| `ingested` | When material entered the wiki workflow. Retain a historical ingest date only when evidence supports it; a new ingest does not establish an old access date. |
| `observed` | When an event, state, or attributed session statement was observed. |
| `published` | When the source was published, if known. |
| `source_created` | When the underlying source was created, if known; distinct from wiki page `created` / `created_date`. |

Observation, publication, source creation, and wiki page creation/update dates
are not substitutes for retrieval dates. Preserve existing producer field names
and their documented meaning rather than renaming them just to match this table.
For bundles, put source-specific dates on the relevant `sources` entries;
page-level `ingested` describes the bundle's ingest only.

For a missing original access date, omit the date and use
`retrieval_status: unknown`. For a source that cannot be accessed, use
`retrieval_status: unavailable` and explain the limitation in the body.
These statuses record authoring limitations, not automatic freshness or review
verdicts; unknown dates and unavailable evidence are not structural errors.

### Source provenance examples

These examples are synthetic. Each is format-valid without legacy `source_url`;
validation does not establish resolvability, factual support, or complete provenance.

A singleton external source with a known access date:

```yaml
type: source
resource: https://example.org/reference
retrieved: 2026-01-01
```

A bundle with separate source access dates and a known wiki ingest date:

```yaml
type: source
sources:
  - id: reference-a
    resource: https://example.org/a
    retrieved: 2026-01-01
  - id: reference-b
    resource: https://example.org/b
    fetched: 2026-01-02
ingested: 2026-01-03
```

A local artifact, with a portable path rather than an invented external URL.
Describe the inspected version in the body when known; a raw capture is optional.

```yaml
type: source
resource: .raw/example.txt
retrieval_status: unknown
```

A session anchor supplied by the client or user, identifying the selected fourth
turn in this synthetic example. Describe the selected excerpt, speaker, and any
replay limitation in the body; never invent an anchor or archive a transcript
just to fill metadata.

The observation date does not imply external retrieval.

```yaml
type: source
sources:
  - id: example-session-turn-4
observed: 2026-01-01
```

A retained historical source with no recoverable original access date. It remains
format-valid with explicit source-quality debt; do not invent a date to repair it.

```yaml
type: source
resource: https://example.org/historical-reference
retrieval_status: unknown
```

## Reserved files — no concept frontmatter

OKF v0.2 reserves exactly two filenames at any directory level:

- `index.md` — directory listing, generated by `sync.py`. The root
  `wiki/index.md` is the only file that declares `okf_version: "0.2"`.
- `log.md` — chronological update history, written by the vault lifecycle hook.

The reserved set is defined once in `scripts/okf_mw/okf_paths.py` and shared by
`guard.py`, `validate.py`, and `sync.py`. Comparisons casefold: the vault lives
on case-insensitive APFS, where `wiki/Index.md` and `wiki/index.md` are one
file. (The former `toc.md` whole-vault map was retired in the v0.2 migration —
it was a non-conformant extra file; navigation now rides the `index.md`
hierarchy and the entry-point page.)

## Frontmatter template

```yaml
---
type: <Type name>                  # REQUIRED
title: <Optional display name>
description: <Optional one-sentence summary>
resource: <Optional canonical URI for the underlying asset>
tags: [<tag>, <tag>]
status: <Optional lifecycle state>
generated: { by: <actor>, at: <ISO 8601> }
verified: { by: <actor>, at: <ISO 8601> }
stale_after: <YYYY-MM-DD>
sources:
  - id: <source-id>
    resource: <URI or bundle path>
last_updated: <YYYY-MM-DD>         # vault extension: staleness signal
---
```
