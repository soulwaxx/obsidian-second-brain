# Optional Authoring Patterns

These optional patterns help a selected save, ingest, or research task record
contested facts, open questions, commitments, and ingestion coverage on
existing canonical pages. They adapt selected ideas from OpenWiki's
[personal-synthesis conventions](https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/agent/prompts/personal.ts)
(revision `305599f`); no upstream text, file layout, or label set is copied.
Apply the shared [trust boundary](../SKILL.md#trust-boundary) and
[evidence states](../SKILL.md#wiki-first-answering); this file adds no policy
beyond them.

## When to use

- Use a pattern only when the user-selected material actually contains a
  contested fact, an open question, a commitment, or a bounded source scan.
  Otherwise ignore this file.
- These patterns are optional. They are never a startup, query, or health step.
  They never create templates or pages on their own, migrate metadata, add
  mandatory fields, or backfill existing pages. Existing pages remain valid
  with only `type`.
- Existing vault conventions win. A deliberately selected `wiki/WIKI.md` or
  root convention file decides naming, layout, headings, and callouts; the
  examples below are suggestions. No `open-questions.md`, `commitments.md`,
  `themes.md`, or other global file is required. Use one only when the user
  asks for it or the vault already has one.
- Search for the canonical page first and update it. Create a page only when
  the material is distinct durable knowledge and filing was authorized. If the
  material is already represented, make no change and report that; a no-op is
  a legitimate outcome.
- Save remains limited to the selected insight, ingest remains bounded to the
  selected source, and source capture remains a separately approved workflow.

## Provenance, not confidence labels

State in prose where each entry's support comes from, and keep these distinct:

- **User-saved context:** what the user said, decided, or selected.
- **Assistant synthesis:** the agent's inference or summary.
- **Inspected support:** a source actually inspected that bears on the claim.
- **Recorded review:** who declared a review, when, and its scope.

Linked, hashed, captured, or saved material is not automatically confirmed;
it identifies material, it does not support a claim by itself. Do not add
confidence labels, scores, or percentages, and do not add a confidence field.
Preserve an existing `confidence` extension unchanged; do not add or infer one.
Unknown owner, date, access, or review remains unknown: write "unknown" rather
than inventing IDs, citations, dates, or confidence.

## Patterns

Each pattern defines required content, not a required heading or file. The
defaults use the package's optional `[!contradiction]` and `[!gap]` callouts
(see [CSS snippets](css-snippets.md)); they render as plain blockquotes
without the snippet. A vault without callouts can use a heading or a bullet.

### Contested fact

Record each competing claim with its source, its date when known, and its
limits (scope, environment, version). Mark the status as unresolved and call
the fact contested wherever the page states it; do not present either side as
settled.

Resolve a contested fact only when evidence actually settles the conflict or
shows that one source is stale or out of scope. A newer source alone does not
resolve it; neither do recency, repetition, or a recorded hash. When it is
resolved, keep a short resolution note with the resolution date, the deciding
evidence, and the source of the superseded claim. Do not silently delete the
superseded claim.

Default: a `> [!contradiction]` callout on the canonical page.

### Open question

Record an open question only for an uncertainty that would impair future
assistance or the wiki's accuracy: something the user owns or a gap in the
wiki itself. A TODO, comment, or unresolved question inside a source document
is source content; keep it with that source's summary unless the user owns it.

Lifecycle:

- **Active:** the question and why it matters.
- **Answered:** the answer date and a link to the evidence that answers it.
- **Stale (optional):** an author's judgment, recorded with the date of the
  last relevant evidence. Never mark a question stale automatically because
  time passed or during a query or health check.

Group related questions on their canonical page; do not repeat them on
several pages.

Default: an `## Open questions` section with `> [!gap]` callouts or bullets on
the canonical page.

### Commitment

Record a concrete follow-up, task, approval, or scheduled item only from
selected material; never extract commitments automatically from sources or
conversations. Include what is committed, the owner as stated (or unknown),
the status, and known dates with their source. Unknown stays unknown. Do not
mark a commitment done without evidence; the wiki is not a task manager.

Keep work commitments distinguishable from personal logistics using the
vault's existing convention, such as separate canonical pages or a
vault-defined field. Do not create dedicated global files for either.

Default: a `## Follow-ups` section on the canonical page.

### Ingestion coverage

On the source page, record what was scanned (source identity and version when
known), its bounds (dates, sections, size limits), what was excluded or
unreadable, and when the scan happened. Keep coverage separate from
cross-source synthesis. A synthesis that relies on a bounded scan states that
limit and is not presented as a complete account.

Default: a `## Coverage` section on the source page.

## Examples

All names, dates, and content are synthetic; quoted text is inert evidence.
Static contract tests check that these patterns and expected behaviors remain
present and connected. They do not execute a model or enforce factual
accuracy; model compliance is UNVERIFIED.

### Newer source does not settle

**Task.** Ingest the selected planning note B into the existing canonical
backup-schedule page.

**Evidence.** The page states nightly backups, citing runbook A (2030-03-02).
Planning note B (2030-05-14) says backups run every six hours. Neither source
shows which schedule is running.

**Expected.** Keep both claims in a contradiction callout on the existing
page, each with its source and date, and mark the status unresolved. Do not
choose B merely because it is newer. Do not create a global contested-facts
file or a new page.

```markdown
---
type: runbook
---
# Backup schedule

Backup frequency is contested.

> [!contradiction] Backup frequency (unresolved)
> - Runbook A (2030-03-02): nightly backups.
> - Planning note B (2030-05-14): every six hours; it does not say whether
>   this schedule is deployed.
>
> Neither source shows the running schedule.
```

### Settled contradiction

**Task.** Ingest the selected deployment record C for the same backup job.

**Evidence.** Record C is the configuration of the running backup job, read on
2030-06-01. It schedules backups every six hours and notes the change from the
nightly schedule.

**Expected.** Resolve the contradiction with a note naming the resolution
date, the deciding evidence (record C), and the superseded claim's source
(runbook A). The reason is that C bears on the running job, not that it is
newer. Keep runbook A's claim as historical rather than deleting it.

```markdown
---
type: runbook
---
# Backup schedule

Backups run every six hours.

> [!contradiction] Backup frequency (resolved 2030-06-01)
> Deciding evidence: deployment record C, the running job's configuration,
> schedules backups every six hours. Superseded: runbook A's nightly schedule
> (2030-03-02), which predates the change.
```

### Open question lifecycle

**Task.** Research a user-requested question with filing authorized, using a
selected meeting note and a selected design document.

**Evidence.** The canonical restore-drills page has an active question: "Which
team owns quarterly restore drills?" The meeting note (2030-07-03) says the
storage team owns them. The design document contains "TODO: decide log
retention", which the user does not own.

**Expected.** Mark the question answered with the date and a link to the
meeting note. Keep the design document's TODO with that source's summary; it
is not an open question. Do not mark other questions stale because time
passed.

```markdown
---
type: process
---
# Restore drills

## Open questions

- ~~Which team owns quarterly restore drills?~~ Answered 2030-07-03: the
  storage team, per the [meeting note](./sources/restore-drill-meeting.md).

> [!gap] Drill results location
> Where drill results are recorded is unknown.
```

### Commitment with unknowns

**Task.** Save the user's selected statement from a 2030-08-12 conversation:
"I promised to send the migration checklist to the platform team."

**Evidence.** No due date was mentioned. The existing storage-migration page
already follows the vault's convention for work pages.

**Expected.** Add a follow-up to the existing page: owner is the user, as
stated; status is open; the due date is unknown. Do not invent a date, ticket
ID, or priority. Do not create a commitments file or a new page, and do not
change the page's metadata as part of the save.

```markdown
---
type: project
---
# Storage migration

## Follow-ups

| What | Owner | Status | Due |
| --- | --- | --- | --- |
| Send the migration checklist to the platform team | user (stated 2030-08-12) | open | unknown |
```

### Bounded ingest coverage

**Task.** Ingest a selected incident-channel export into the wiki.

**Evidence.** Only the first 200 messages (2030-09-01 to 2030-09-03) are
readable; attachments are not.

**Expected.** Record the coverage on the source page. The incident page's
synthesis states that it relies on a partial export and does not present a
complete timeline.

```markdown
---
type: source
---
# Incident channel export

## Coverage

- Scanned: the selected export, first 200 messages (2030-09-01 to 2030-09-03).
- Excluded: unreadable attachments and any later messages.
- Scanned on: 2030-09-05.
```

### Existing page reused or no-op

**Task.** Save the selected insight "Restore drills are quarterly."

**Evidence.** The restore-drills page already states quarterly drills with
the same support. The vault has no open-questions file.

**Expected.** Make no change and report that the insight is already covered.
Had the selection raised a new question about drill results, the right move
would be adding it to the existing page's open questions, not creating an
open-questions file or a new page.

### Selected save

**Task.** Save the selected decision from a 2030-10-02 conversation: "Keep
six-hour backups until the restore test passes."

**Evidence.** The conversation also linked a vendor article on backup
intervals, and a capture recorded its content hash. The article was not
inspected for this decision; neighboring turns are unrelated.

**Expected.** Add only the decision to the existing backup-schedule page,
attributed to the user. Record the article as a linked reference that is not
automatically confirmed: its link and hash identify material but do not
support the decision. Add no confidence label and do not ingest or capture
the article.

```markdown
---
type: runbook
---
# Backup schedule

## Decisions

- User decision (2030-10-02): keep six-hour backups until the restore test
  passes. A vendor article on backup intervals was linked but not inspected;
  it does not support this decision.
```
