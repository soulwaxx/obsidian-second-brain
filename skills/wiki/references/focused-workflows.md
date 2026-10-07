# Focused Wiki Workflows

The general `wiki` skill remains the entry point for vault setup, broad
maintenance, and requests that do not match a focused intent. These focused
skills are small task selectors, not independent safety contracts. Each follows
[the shared wiki skill](../SKILL.md) and its relevant references; in particular,
all writes use its destination guard, validation, lifecycle finalization, and
provenance rules. Do not copy or weaken those rules here.

## Run the package CLI

The npm `obsidian-second-brain` bin may not be on `PATH` after installing this
package as a Claude or Pi extension. Use the bin only when
`command -v obsidian-second-brain` finds it. Otherwise invoke the currently installed
package's shipped Node launcher; do not install another copy or guess an npm
cache path.

In Claude Code, the loaded skill context supplies its absolute base directory.
Use that exact active skill directory (not a variable in the Bash environment):

```sh
SKILL_DIR="/absolute/installed/package/skills/wiki-health"
PACKAGE_ROOT="$(cd -P -- "$SKILL_DIR/../.." && pwd)"
node "$PACKAGE_ROOT/scripts/obsidian-second-brain.mjs" doctor --json
```

In Pi, use the absolute path Pi reports for the currently loaded skill's
`SKILL.md` (replace the example path below with that exact path):

```sh
SKILL_FILE="/absolute/installed/package/skills/wiki-query/SKILL.md"
PACKAGE_ROOT="$(cd -P -- "$(dirname -- "$SKILL_FILE")/../.." && pwd)"
node "$PACKAGE_ROOT/scripts/obsidian-second-brain.mjs" doctor --json
```

Each shipped skill is directly under `<package-root>/skills/`; moving up two
levels from its skill directory yields the active package root. This keeps
invocation bound to the version the client loaded and works independently of
caller cwd and selected vault. Quote the resolved absolute paths as shown.
Replace `doctor --json` with `search "query terms" --json` when needed. Never
rely on `CLAUDE_PLUGIN_ROOT` in ordinary Bash, install a global CLI, add a vault
wrapper/copy, or change package versions merely to query.

## Query

For a question answered by existing wiki knowledge, search first, then read the
most relevant pages and answer with honest page citations. When the bin is on
`PATH`, run `obsidian-second-brain search "query terms" --json`; otherwise use
the package-relative launcher procedure above, replacing `doctor --json` with
`search "query terms" --json`.

The CLI resolves the same selected vault as the integration. Use `--vault
<absolute-vault-root>` only when an invocation-specific explicit selection is
needed. Search is read-only and reports fallback/freshness; it does not build an
index. If unavailable or no useful results appear, follow the entry point and
generated `index.md` hierarchy in [authoring standards](authoring-standards.md),
then read candidate pages directly. Do not write unless asked to file an
answer or insight.

## Explicit selected-conversation save

A save request must identify the answer, decision, or insight to preserve. Save
only that selected material, not the full transcript or unrelated context.
Separate the assistant's synthesis from user statements and cited evidence;
preserve uncertainty, disagreement, and limits. Never invent citations or
claim the conversation itself is an external source.

Before creating a page, query the wiki and inspect likely existing pages. Update
the best canonical page when the material belongs there; create a new page only
when it contributes distinct durable knowledge and the user authorized saving
it. If the material is already represented or would not improve a canonical
page, make no changes and report that it is already covered. If the target or
requested excerpt is ambiguous, ask rather than capturing a broader conversation. This workflow creates ordinary curated wiki pages. Small writes continue
through the normal guard, validation, and lifecycle path. A larger coordinated
multi-page edit may use the package's optional reviewed, recoverable batch
workflow; do not batch every small edit. Batches preflight all unique
vault-relative destinations before writing, bind the ordered UTF-8 operations
and selected config to an exact plan hash, publish recoverably, then let the
lifecycle own wiki navigation and logging. Use the package CLI:

```sh
obsidian-second-brain batch-inspect --bundle /absolute/path/to/bundle.json --json
obsidian-second-brain batch-apply --bundle /absolute/path/to/bundle.json --plan-hash EXACT_HASH --json
obsidian-second-brain batch-recover --batch-id BATCH_ID --json
```

Each bundle is a JSON object with `version: 1` and a nonempty `operations`
array. Each operation has exactly `path`, `expectedHash`, and `content`; the
path is canonical and vault-relative, `expectedHash` is the prior SHA-256 or
`null` for a new destination, and `content` is the complete UTF-8 replacement.
Use one operation per unique destination. `batch-rollback` is available only
before the journal's `finalizing` phase; that phase is persisted before
lifecycle publication begins and marks the conservative rollback cutoff. Once
finalization may have published navigation/log changes, recover the batch to
completion; a reversal requires a separately reviewed inverse change.
Publication is ordered and recoverable, not simultaneous filesystem-wide
atomic visibility. Keep concurrent Obsidian edits; recovery refuses drift
instead of overwriting it. If the CLI bin is not on `PATH`, invoke it through
the active package launcher described above. Do not stage or commit vault data;
Obsidian Git owns sync.

An explicitly selected local source can also be preserved separately as an
immutable text capture. Select one or more absolute paths to local UTF-8 text files and, optionally,
existing wiki-relative Markdown pages to link:

```sh
obsidian-second-brain capture-inspect --source /absolute/path/to/source.txt --page wiki/topic.md --json
obsidian-second-brain capture-apply --source /absolute/path/to/source.txt --page wiki/topic.md --plan-hash EXACT_HASH --json
```

Repeat `--source` and `--page` to select several; each selected page link set
applies to each selected source. Sources must be regular UTF-8 text without
NUL bytes. Review the inspect summary, which reports
hashes and bounded counts without disclosing source paths/content, and apply
only with that exact hash. The capture writes a byte-preserving
SHA-256-addressed `.txt` payload plus a metadata `.md` record under
`.raw/agent-captures/`; metadata records source-content and source-identity
hashes and selected page links/content hashes. Capture is create-only; distinct
source identities with identical bytes have separate metadata records and share
the content-addressed payload when an exact canonical completed reference and
completed unchanged-payload evidence are retained in lifecycle history; the two
may come from separate completed batches. A damaged older reference does not
invalidate a later valid reference, but damaged-only or unpublished evidence is
refused. Ordinary spaces and Unicode page names are supported; syntax-sensitive
names use percent-encoded Markdown link targets. An exact unchanged repeat is a
no-op; drift, unverified lone payloads, or incomplete/damaged pairs are refused.
Identity reuse also checks the retained, validated lifecycle batch journals under
`.vault-meta/lifecycle/batches/`: known unfinished captures must be recovered or
rolled back through their original batch, and completed captures with missing or
drifted postimages are not recreated. These journals are read-only evidence and
are not created by preview/no-op or removed/migrated by capture. Exact identity
replay and shared reuse rely on retained valid journals; if a journal is deleted
entirely, its former identity cannot be inferred from history alone and the
remaining record/payload is treated as unverified rather than replay authority.
Missing, corrupt, or unsafe journal history cannot authorize capture reuse.
Historical page hashes are preserved as
record evidence; current page existence/content is not required when reusing a
reference that was valid when captured.
Limits are 20 files, 1 MiB per source, 4 MiB total, and 50 linked pages (each
up to 16 MiB). Only local UTF-8 text is supported: no web fetch, crawl, PDF,
audio, video, OCR, or media extraction. Source capture does not promote a
source into wiki claims or replace ordinary write authorization. If the CLI is
not on `PATH`, use the active package-relative launcher above.

## Optional evidence ledgers and read-only reports

Evidence records are optional canonical wiki data, not derived cache state or a
migration. Existing pages remain valid with only `type`; do not add fields or
create ledgers merely to modernize a vault. When useful for a user-selected
research or ingest task, records may be stored as JSON under
`wiki/meta/evidence/` through the existing reviewed batch workflow and its
fixed `wiki-ledger` authority. Prepare a version-1 batch bundle containing the
record JSON at `wiki/meta/evidence/<record-id>.json`, inspect it with
`batch-inspect --authority wiki-ledger`, then apply only with the returned exact
`--plan-hash` and same authority. The batch enforces record schema and filename
identity; it does not establish truth or source quality. Do not use arbitrary
prefixes, create ledgers on startup/query, or backfill/fabricate records for
legacy pages.

A source record identifies a selected source and its SHA-256 content hash;
leave freshness `unknown` unless there is a defensible basis to mark it
`current` or `stale`. A claim record can list source IDs in `support` and
`contradictions`, preserve descriptive `uncertainty`, and record review status.
Claims without linked source IDs remain `provisional`; linked means only that
IDs are recorded, not that a claim is true or verified. Mark review status as
`reviewed` only when an actual review occurred. Preserve unknown extension
fields when updating records. A capture under `.raw/agent-captures/` preserves
selected bytes but does not by itself create source/claim records or establish
that the captured material supports a claim.

Run the read-only report with `obsidian-second-brain evidence-report --json`
(or `evidence-report --vault <vault-root> --as-of YYYY-MM-DD --json` for a
repeatable date). It reports OKF structure separately from declared freshness,
review metadata, references, and ledger relationships. A missing ledger is a
normal legacy state and is not created by reporting. `stale_after` dates and
ISO timestamps are evaluated at calendar-day precision. The report checks
metadata and structure only: it does not fetch sources, recompute source hashes, verify
claims, or attest that a human review happened. Contradictions mean
contradictory evidence was recorded, not that a statement is false.

Apply the common authoring and provenance rules in
[authoring standards](authoring-standards.md),
[research discipline](research-discipline.md), and
[update mode](update-mode.md). Use the guard, validation, and sync order in
[the wiki skill](../SKILL.md).

## Ingest

Use this intent only for a bounded source or material the user explicitly
provided or selected. Read that material, identify supported claims and
uncertainties, and make only an approved, useful update to canonical wiki
pages. Search for an existing home before creating one. This filing workflow
is not itself a raw-source capture: do not archive whole files/transcripts by
default, fetch a crawl, or imply that capture creates wiki claims. If the user
separately requests preservation of a selected local text source, use only the
explicit capture workflow above. Do not imply web/media adapters. Optional
source/claim ledger records are available only through the reviewed `wiki-ledger`
batch path described below; use them only when useful for the selected task,
never as a prerequisite or fabricated retrofit. For repository changes, use
[update mode](update-mode.md); for other user-selected material, use
[research discipline](research-discipline.md) and
[authoring standards](authoring-standards.md). Ask before expanding beyond the
selected source/topic or making destructive changes.

## Research

Research is a bounded investigation of a user-requested question or topic.
Use only evidence accessible in the current task (the wiki, selected local
sources, and any research tools actually available to the client). The package
does not provide web search, fetching, or web/media capture; explicitly selected
local UTF-8 text can be captured only through the separate reviewed workflow
above. Distinguish evidence from inference, retain contradictions and unknowns, and
cite only sources actually inspected. Synthesize into the wiki only when filing
was explicitly requested; otherwise answer without writes. Source/claim ledger
records are optional; when helpful and authorized, record only inspected source
identities, support, contradictions, uncertainty, and actual review state using
the shared evidence workflow. Do not mark unknown freshness as current or call
linked claims verified. Follow [research discipline](research-discipline.md) and
[authoring standards](authoring-standards.md).

## Health

Health checks are read-only. Run `obsidian-second-brain doctor --json` when
the bin is on `PATH`; otherwise use the package-relative launcher procedure
above.

It reports config, dependency, selected-vault, cache-exclusion, lifecycle, and
retrieval diagnostics; it does not repair them. Review any reported paths and
errors without exposing secrets. For content/link structure, run the existing
read-only middleware lint described in [the wiki skill](../SKILL.md) when the
user asks for that audit. Report findings and suggested next steps; never edit
configuration, exclusions, indexes, logs, caches, or pages as an implicit
health-check side effect. Repairs require a separate explicit request and must
follow their existing setup or wiki-write approval workflow.
