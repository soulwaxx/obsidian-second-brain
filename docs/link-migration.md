# Review Obsidian link compatibility

Use the package-owned tools below for an explicitly requested audit or migration. OKF lint keeps its existing bundle-root resolution rules. A successful OKF check does not prove that native Obsidian selects the same file.

Set `PLUGIN_DIR` to the active package root, `VAULT` to the selected vault root, and `CONFIG` to its integration config. Use the [package-relative launcher procedure](../skills/wiki/references/focused-workflows.md#run-the-package-cli) to locate the active package. Keep review artifacts outside the vault:

```sh
REVIEW_DIR="$(mktemp -d)"
CONFIG="${OBSIDIAN_AGENT_CONFIG:-$HOME/.config/obsidian-second-brain/properties.json}"
```

## Run a read-only compatibility report

```sh
python3 "$PLUGIN_DIR/scripts/obsidian_compat.py" \
  --vault "$VAULT" --json > "$REVIEW_DIR/compatibility.json"
```

The report separates authored pages, generated navigation, and lifecycle history. It identifies leading-slash destinations, folder-only navigation, and bare relative paths shadowed by vault-root files. Sparse-page output is diagnostic; it does not authorize adding relationships or enriching archives.

The report uses filesystem resolution unless you supply reviewed native evidence. Its default output says the app was not queried. It never opens Obsidian, invokes a model, or contacts a network service.

Optional native evidence has this shape:

```json
{
  "vault": "/absolute/path/to/vault",
  "links": [
    {
      "source": "wiki/topic.md",
      "destination": "./sources/analysis.md",
      "source_hash": "SHA256_OF_CURRENT_SOURCE_BYTES",
      "resolved_path": "wiki/sources/analysis.md"
    }
  ]
}
```

Use `null` for a link that the native resolver reports as unresolved. Supply observations from a read-only resolver query in the running app, rather than copying the filesystem result and labeling it native:

```sh
python3 "$PLUGIN_DIR/scripts/obsidian_compat.py" \
  --vault "$VAULT" --native-evidence "$REVIEW_DIR/native-evidence.json" \
  --json > "$REVIEW_DIR/compatibility-with-native-evidence.json"
```

Evidence must match the vault, discovered source/destination, and current source hash. Stale or conflicting observations are refused. Unmatched links retain the filesystem-fallback label; an explicit native failure remains `native-unresolved`.

The report sets `native_graph_complete: false`. Its parser-discovered links cannot certify the complete native graph. Reference-style links, HTML anchors, unsupported syntax, and non-Markdown files require separate review. When Obsidian is unavailable, report native verification as unverified, not passed.

## Prepare an authored Markdown migration

Preview destination-only changes and the existing batch approval plan:

```sh
python3 "$PLUGIN_DIR/scripts/wiki_migrate.py" \
  --vault "$VAULT" --config "$CONFIG" --json \
  > "$REVIEW_DIR/authored-preview.json"
jq '.bundle' "$REVIEW_DIR/authored-preview.json" > "$REVIEW_DIR/authored-bundle.json"
```

Use repeatable `--page wiki/topic.md` arguments to restrict the preview to selected authored pages. A no-op preview has an empty bundle and no approval hash; do not submit that empty bundle for publication.

Review every proposed destination and its intended file. The preparation tool preserves labels, surrounding prose, fragments, code, unknown frontmatter fields, and original BOM/newline bytes. It covers supported authored frontmatter links and navigation descriptions. Missing or ambiguous targets and unsupported source spans do not receive automatic corrections.

Publication uses the existing `wiki-page` batch authority. Review its plan, then apply with the exact approved hash:

```sh
node "$PLUGIN_DIR/scripts/obsidian-second-brain.mjs" batch-inspect \
  --vault "$VAULT" --config "$CONFIG" \
  --bundle "$REVIEW_DIR/authored-bundle.json" --json

node "$PLUGIN_DIR/scripts/obsidian-second-brain.mjs" batch-apply \
  --vault "$VAULT" --config "$CONFIG" \
  --bundle "$REVIEW_DIR/authored-bundle.json" \
  --plan-hash HASH_FROM_REVIEWED_PREVIEW --json
```

Save the original bundle and hash for exact retries. Recover an interrupted batch with the existing `batch-recover --batch-id ID` command. Recovery refuses concurrent edits; it does not overwrite them to make a migration finish.

Markdown batches remain Markdown-only. The helper excludes generated indexes, lifecycle logs, and temporary plans at every depth. Base/YAML corrections require their separately supported guarded validation path; this workflow does not grant that authority.

## Review historical log destinations separately

Normal finalization preserves old history. A new generator does not implicitly migrate it, and ordinary agent file tools still cannot write any reserved `log.md`.

The dedicated lifecycle-owned migration command handles only the root `wiki/log.md`. Preview its exact destination replacements:

```sh
python3 "$PLUGIN_DIR/scripts/wiki_history.py" \
  --vault "$VAULT" --config "$CONFIG" --json \
  > "$REVIEW_DIR/history-preview.json"
```

Review the operations, `beforeHash`, `afterHash`, and `planHash`. Apply only after the owner approves those exact changes:

```sh
python3 "$PLUGIN_DIR/scripts/wiki_history.py" \
  --vault "$VAULT" --config "$CONFIG" \
  --apply --confirm HASH_FROM_REVIEWED_PREVIEW --json
```

The command changes only reviewed destination spans. It preserves historical dates, verbs, labels, prose, ordering, fragments, BOM, and line endings. It refuses pending writer captures, changed config/log bytes, unsafe paths, and lifecycle state that is not effectively Git-ignored.

Hash-bound recovery journals stay under `.vault-meta/lifecycle/`. Existing journals remain intact. Resume an interrupted approved migration or verify its exact after-image:

```sh
python3 "$PLUGIN_DIR/scripts/wiki_history.py" \
  --vault "$VAULT" --config "$CONFIG" \
  --recover HASH_FROM_REVIEWED_PREVIEW --json
```

An unchanged completed result is a no-op. Drift is a conflict, not permission to overwrite. The command does not delete notes, change settings, update nested logs, regenerate navigation, or run Git writes.

## Verify the final graph

Rerun the compatibility report and OKF lint with the vault root after a migration:

```sh
python3 "$PLUGIN_DIR/scripts/obsidian_compat.py" --vault "$VAULT" --json
python3 "$PLUGIN_DIR/skills/wiki/scripts/okf_mw/lint.py" "$VAULT" --format json
```

Inspect the full native graph when the running app is available, including links outside the preparation parser's coverage. Retain the native-verification limitation when it is unavailable. Report authored/navigation failures separately from intentionally preserved historical links.

New actual deletions produce `Deletion: Removed` events with escaped plain-text paths, so the new event does not link to a missing file. Earlier creation/update links remain historical evidence and can appear as dangling links in OKF lint. A pending pre-upgrade deletion can retain its old `Update` retry semantics to preserve already-published history; a new deletion uses the explicit event.

A zero-finding active graph does not authorize deleting loose files or changing semantic relationships. Review those actions separately with the owner. Obsidian Git remains the sole owner of vault commits, pulls, and pushes.
