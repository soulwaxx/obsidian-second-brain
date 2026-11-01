# Selected source observations and reconciliation baselines

Source observations and page/asset reconciliation baselines are optional evidence
records. They are not required metadata, do not migrate older pages, and do not
change existing source/claim IDs or support links. A page with only `type` stays
valid. Select an asset and page pair deliberately; observations never imply that
a page was reconciled, that its claims are true, or that a human review occurred.

## Selected workflow

Use an absolute local source-root mapping only for the invocation. The persisted
root ID and asset ID are explicit portable identities; locators are relative
POSIX paths under that root. The mapping itself is not persisted as an absolute
path. Source snapshot IDs continue to identify immutable locator-plus-byte
hash material, independently of asset IDs.

1. Write a selection JSON file for only the asset being inspected. Timestamps
   are explicit timezone-aware ISO strings, so inspect plans are deterministic.
2. Inspect, review the returned summary and exact `planHash`, then apply the
   observation using that exact approval. The source file is hashed as raw bytes
   (including dirty worktree bytes); its content is not copied or archived.
3. If the page needs editing, prepare and inspect a separate version-1
   `wiki-page` batch bundle. Review the page operations and apply with that
   page batch's exact plan hash. Keep its returned `batchId`.
4. Select the observation and completed page batch in a reconciliation JSON
   file. Inspect the `wiki-ledger` baseline plan, then apply with its separate
   exact plan hash. Publication requires the exact completed page-batch receipt
   and current page bytes to match its postimage. Use `outcome: "no-change"`
   only when a completed page batch records identical preimage/postimage bytes;
   this is a reviewed reconciliation receipt and need not make a cosmetic page
   edit.

For example, `observation.json` contains:

```json
{"version":1,"rootId":"research-notes","assetId":"asset-report","locator":"reports/current.pdf","observedAt":"2026-06-01T12:00:00Z"}
```

Run from the package CLI (the active package-relative launcher may replace the
bin name):

```sh
obsidian-second-brain observe-inspect --selection /absolute/observation.json --source-root /absolute/selected/root --json
obsidian-second-brain observe-apply --selection /absolute/observation.json --source-root /absolute/selected/root --plan-hash OBSERVATION_PLAN_HASH --json
```

For a deliberately reviewed locator move, add `--allow-move` to both
`observe-inspect` and `observe-apply`; the approval hash binds that selection.

The reviewed page-batch input is the ordinary version-1 `operations` bundle
used by `batch-inspect`/`batch-apply`, with `--authority wiki-page`. Its plan
hash and batch ID are separate from both source and baseline approvals.

`reconciliation.json` selects the observation and page-batch receipt:

```json
{"version":1,"reconciliations":[{"page":"wiki/reports.md","observationId":"observation-...","pageBatchId":"BATCH_ID","reconciledAt":"2026-06-01T13:00:00Z","outcome":"updated"}]}
```

For an explicit same-byte receipt, use `"outcome":"no-change"` and a completed
wiki-page batch whose operation leaves those exact page bytes unchanged. Inspect
and apply the baseline independently:

```sh
obsidian-second-brain baseline-inspect --selection /absolute/reconciliation.json --json
obsidian-second-brain baseline-apply --selection /absolute/reconciliation.json --plan-hash BASELINE_PLAN_HASH --json
```

`observedAt` and `reconciledAt` must be timezone-aware ISO timestamps. Do not
invent historical timestamps. A second observation of the same selected asset
and locator after bytes change creates a distinct immutable source snapshot and
observation; it does not advance any page baseline. Only selected page/asset
pairs advance, and each baseline retains its prior history. A locator move
changes the explicitly identified asset's locator only with `--allow-move` on
both reviewed observation commands; review that move deliberately. Existing
unknown record fields are preserved when records are updated. Review status is
not promoted by observing, moving, or reconciling; reconciliation is not factual
verification.

Exact repeats are idempotent. Re-inspecting a published observation or
baseline selection can return a no-op plan; apply only with the exact hash from
that inspection. Do not substitute a prior approval after inputs, source-root
mapping, configuration, page batch, or records change. Concurrent drift is
refused, not overwritten.

## Publication and recovery

Page edits and ledger records have separate authorities and approvals. A
baseline cannot be published until its matching page batch is complete and the
page bytes still match the exact page-batch postimage. Generic ledger batches
also validate baseline dependencies. If an operation is interrupted, recover or
roll back using the original batch ID and generic reviewed-batch recovery
commands, not a newly invented replacement approval:

```sh
obsidian-second-brain batch-recover --batch-id ORIGINAL_BATCH_ID --json
obsidian-second-brain batch-rollback --batch-id ORIGINAL_BATCH_ID --json
```

Rollback is available only before the journal's conservative finalization
cutoff. After finalization may have published lifecycle artifacts, recover to
completion; any reversal is a separately reviewed change. Retain validated
`.vault-meta/lifecycle/batches/` journals: they contain local publication
receipts and preimages needed to verify dependencies, exact repeats, and
recovery. A metadata/JSON ledger copied to another vault remains portable data,
but copied or imported records do **not** recreate local publication authority.
Missing, corrupt, unsafe, or deleted journals limit recovery and may make prior
publication unverifiable. Recovery refuses page/ledger drift rather than
silently replacing it. Lifecycle receipts are local derived state and excluded
from normal Git tracking; do not copy them as portable provenance.

## What observations establish

The observation records the SHA-256 of the exact selected regular-file bytes,
including dirty worktree contents saved on disk. Git context is advisory only: it may
report the captured HEAD and whether the selected bytes match that commit's
blob, but it does not control the byte hash or publication. Git absence, shallow
history, missing objects, rewritten/non-ancestor history, and unavailable blobs
are reported as limitations; never substitute `HEAD~1` or another nearby
commit for unavailable historical bytes. `source-history --observation-id`
checks recorded-commit availability/ancestry against the explicitly mapped
current root:

```sh
obsidian-second-brain source-history --observation-id OBSERVATION_ID --source-root /absolute/selected/root --json
```

This is not source-drift detection. It does not rehash a source during report
reading or infer that an old page corresponds to current source truth. No source
content is stored: there is no implicit source archive. Historical dirty or
unavailable bytes cannot be diffed later unless separately retained under a
separately authorized preservation workflow. Binary regular files can be
observed. Git blob byte-comparison output is capped at 64 MiB; above that, Git
comparison context may be unknown even though the current file's exact byte hash
is recorded.

## Reports, lint, and adoption candidates

`evidence-report` remains read-only. It can report optional asset and observation
records plus each page/asset baseline history and latest declared reconciliation
without opening mapped external roots, querying Git, rehashing sources, writing,
or asserting factual currentness. A recorded observation is not a declared
reconciliation; a declared reconciliation is not factual review. Its page review
metadata is reported separately and is not automatically promoted. Missing
optional provenance is not an error; type-only pages remain valid. The report's
structural and reference findings, including baseline arrays, may help a user
select candidate pages/assets for review, but do not establish historic
reconciliation or authorize backfill.

The existing middleware lint is read-only structural/link diagnostics. Its
provenance hook is disabled; it does not identify historically reconciled
content or authorize ledger population. Any adoption requires a fresh user-
selected observation and reviewed reconciliation against an eligible completed
page batch. Never fabricate history, bulk-backfill automatically, or infer
source truth from links, hashes, timestamps, or lint findings. No upstream
source is vendored and no new dependency is required.
