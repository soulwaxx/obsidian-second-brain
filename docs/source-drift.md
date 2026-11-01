# Read-only selected-source drift check

`source-check` compares explicitly recorded local source assets against all
recorded byte observations and reports potentially affected pages and claims.
The asset state can be `changed` when current bytes differ from any historical
snapshot; that alone does not mean every page needs review. Page/asset baselines
are assessed separately using each baseline's latest history entry. This is a
review signal only: a changed source does not establish a contradiction, truth,
retraction, or required page edit. Review both support and
contradiction relationships and explicit page references; declared provenance
cannot establish exhaustive impact. Unlinked pages/claims and unsupported
references remain visible as coverage limits.

## Select and run

Provide one portable root ID and one absolute local directory mapping for this
invocation. All recorded assets with that `rootId` are selected; no path or
source discovery is performed. The absolute mapping is not persisted and
portable records are data, not authority or proof that they were reviewed. A
valid portable declared source/observation/baseline graph is enough; review
status and a local lifecycle journal are not required. Invalid graph edges and
invalid ledger/page data are reported as issues rather than silently repaired.

```sh
obsidian-second-brain source-check --root-id research-notes \
  --source-root "/absolute/path/to/research notes" --json
```

Use the normal selected-vault options when necessary: `--vault <vault-root>`
or `--config <config-file>`. The invocation mapping is distinct from vault
selection. From an installed Claude/Pi package without the npm bin on `PATH`,
invoke the active package's `scripts/obsidian-second-brain.mjs` launcher as
described in [focused workflows](../skills/wiki/references/focused-workflows.md#run-the-package-cli).

Output uses schema `okf-source-drift-v1`, with stable ordering, a
`snapshotValidity` of `stable` or `unstable`, and a `complete` flag. Asset,
page, source-reference, baseline, claim/evidence, coverage, unchecked, and issue
rows use explicit states and reason codes. JSON is intended for inspection and
automation; human output summarizes selection, coverage, states, and issues.
Neither form includes source bytes or claim statements by default. Raw bytes are
hashed and compared separately from recorded observations. Page/asset baseline
history is immutable and only its latest appended entry is compared; a newer
observation does not reconcile an older page baseline. A dirty worktree's
current bytes are checked directly, without relying on Git diffs. The recorded
locator alone is checked: renames are not searched for.

Exit status is 0 for a completed selected-root check, including changed or
missing sources, and 1 for invalid input/configuration, an incomplete result,
or unsafe/unstable inspection. Outside-root assets and external/unmapped
references are reported as unchecked, not checked. Safe inspection refuses
symlink traversal, non-regular files, hardlink aliases, and path collisions;
source files, pages, ledger bytes, and mappings are revalidated before return.
This is not an atomic filesystem-wide snapshot: mutation during observation can
be detected as instability, but a stable report is not a transaction.

The check is read-only: it does not write, restamp, cache, refresh lifecycle
state, invoke models or networks, run Git commands, or reconcile records. It is
not a native Pi tool and is not scheduled at startup. The check cannot recover
historical dirty bytes unless an authorized archive exists, verify factual
claims or publication, or provide evidence of review/model quality. Outside
mappings and unsupported source types remain unchecked. Inspect relevant source
and page evidence separately; edit pages or reconcile baselines only through
the independently authorized reviewed workflows in [source baselines](source-baselines.md).

This documentation describes the source-check interface and limits; it does
not claim a release or review-complete status.
