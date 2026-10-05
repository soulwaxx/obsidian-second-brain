# Set up or migrate a vault

The configured workflow uses Obsidian Git for commit, pull, and push. The agent
can work from any folder; the selected vault is independent of caller cwd.
Setup is preview-first and never installs plugin binaries, initializes Git,
configures remotes/authentication, or synchronizes notes as an incidental step.

## 1. Prepare tools and select paths

Install Claude Code or Pi, Python 3 with PyYAML, `jq`, Git, and Obsidian. Use the
[README installation commands](../README.md#install-the-agent-package). Setup
scripts ship with the package but are not added to the shell path; these
examples use a repository checkout:

```sh
PLUGIN_DIR="$(pwd -P)" # Run from the checkout root, or use its absolute path.
VAULT="$HOME/Notes/MyVault" # Replace with the intended absolute vault path.
```

Keep the variables in the same shell. Set `OBSIDIAN_AGENT_CONFIG` before preview
if selecting a non-default integration config. Do not guess or retarget a
config that points to a different vault.

## 2. Scaffold a new wiki

For an existing wiki, preserve its contract and entry point and skip to the
configuration step. For a new directory or an empty vault, preview:

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT"
```

Review the proposed directories, file writes, prerequisite diagnostics, and
`planHash`. The scaffold creates missing `.obsidian/`, `wiki/quickstart.md`, and
generated navigation. It does not create/migrate the integration config, enable
plugins, or initialize Git. Obsidian Git can be installed after the scaffold
exists; its absence is reported rather than preventing this first step.

Apply only the reviewed plan:

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT" --apply --confirm 'PASTE_SCAFFOLD_HASH_HERE'
```

Scaffolding leaves existing notes alone and refuses incompatible indexes,
logs, symlinks, or changed inputs. Preserve and review a conflict rather than
deleting a file to force setup through.

## 3. Install and enable Obsidian Git

Open the folder as a vault in Obsidian. Install and enable Obsidian Git from
Settings > Community Plugins. The configuration step checks the installed
manifest and enabled list; it does not download or enable plugin code for you.

Use the owner-controlled workflow to initialize/use the Git repository and set
up identity, remote, and authentication. Inspect existing repositories instead
of automatically creating a nested one. See
[Git setup](../skills/wiki/references/git-setup.md) for ignore rules and privacy.
No personal credentials, remote URL, or identity belongs in shipped templates.

## 4. Preview the configuration profile

Use the explicit configuration option for a new integration or existing-vault
migration:

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT" --configure
```

Review the selected settings differences and ignore additions. Unrelated
configuration values/private preimages are bound to the hash but not dumped in
the preview. Setup must preserve existing notes, vault conventions, and unknown
settings. A minimal settings profile supplies missing values; it does not reset
existing schedules or override an existing choice to disable pushing.

| Setting | Default/proposal |
| --- | --- |
| Agent vault selection | The explicitly selected vault |
| Deprecated agent auto-commit | `false`; the runtime never commits even if a legacy value is `true` |
| Obsidian Git commit/sync | Five-minute debounce after file edits stop |
| Separate commit/push intervals | Disabled; combined commit-and-sync |
| Separate push interval | Zero; this does not disable combined pushing |
| Automatic pull | Eight minutes and on app startup |
| Commit squashing | Proposed `false`, explicitly shown for review |
| Derived state exclusions | Retrieval, lifecycle tracking, and any retained non-Git ownership journal |

Confirm with this plan's exact hash:

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT" --configure --apply --confirm 'PASTE_CONFIGURE_HASH_HERE'
```

Configuration apply refuses unmet plugin prerequisites and stale/concurrent
changes. An open Obsidian instance can rewrite settings; re-preview if inputs
change. Setup does not implicitly pause/reload the app or run commit/pull/push.
A repeated completed configuration preview should propose no changes.

### Selected config and repair

The default config is `~/.config/obsidian-second-brain/properties.json`.
`OBSIDIAN_AGENT_CONFIG` selects another path; `OBSIDIAN_VAULT_PATH` is an
invocation override. Runtime paths may be absolute; internal authored links
remain portable.

Null/missing vault selection requires an explicit choice. A config targeting
another vault is preserved, not silently retargeted. For malformed JSON or
invalid field types, stop the affected wiki writes, propose the exact minimal
repair, and obtain approval of those changes before editing only the selected
config. Preserve unrelated fields and revalidate in the same session. Do not
repeat an unchanged repair proposal or reset other agent/plugin settings.

## 5. Use the wiki from the source workspace

Launch Claude Code or Pi where the main task belongs, including a repository
outside the vault. Invoke `/obsidian-second-brain:wiki` or `/skill:wiki`. The
startup locator identifies the selected vault; the skill reads its existing
contract and relevant pages rather than loading the entire index into every
unrelated conversation. A pre-existing flat wiki remains flat.

Supported file writes are guarded by destination. Normal Markdown pages need
YAML frontmatter with `type`; generated indexes and the lifecycle log are never
agent-authored. Curated sources and settings remain outside normal wiki-write
permissions. Shell writes bypass pre-tool protection, so follow the manual
middleware order in the skill when needed.

The lifecycle validates changed pages and finalizes a writing batch with
navigation, logging, and optional retrieval refresh. An invalid changed page
keeps its batch pending for repair; unrelated historical defects are reported
without preventing normal repairs. Startup and unrelated read-only shutdown
perform no vault writes. Keep one wiki writer at a time.

Obsidian Git runs independently. Its enabled settings do not prove that it is
currently loaded, unpaused, authenticated, or successfully pushing. Local wiki
work can continue while the app is closed, but automatic backups cannot. Git
history is recovery support, not a multi-page transaction or write sandbox.

## Local retrieval

Use package-owned helpers from any working directory; no scripts are copied
into the vault by new setup:

```sh
python3 "$PLUGIN_DIR/scripts/bm25-index.py" build --vault "$VAULT"
python3 "$PLUGIN_DIR/scripts/retrieve.py" "query terms" --vault "$VAULT"
```

Keep `.vault-meta/retrieval/` and `.vault-meta/lifecycle/` effectively ignored.
Later negations or already-tracked files require review; neither runtime nor
setup silently stages/untracks them. Existing helper copies and ownership
journals remain untouched. The legacy provisioning command still has its own
preview/hash-confirmed apply and overwrite refusal.

The lifecycle refreshes the package BM25 cache after finalized writing batches
when retrieval refresh is enabled. Missing/corrupt cache falls back to generated
navigation. Exact topic-title matches are boosted; ISO-date queries stay lexical
and prioritize matching dated notes, then relevant dated sections. Additional
qualifiers refine dated-section relevance. Read a pending new page by path
rather than assuming search has already indexed it.

Embedding reranking is separate and opt-in:

```sh
ollama pull nomic-embed-text
python3 "$PLUGIN_DIR/scripts/retrieve.py" "query terms" --vault "$VAULT" --rerank
```

Start Ollama and install the model yourself. Reranking sends the query and
candidate content only to a loopback endpoint, ignores proxies, rejects
redirects, and falls back to BM25 when unavailable. Normal retrieval makes no
model/network request. The legacy contextual-prefix helper is a no-op, not
model-augmented indexing.

## Configure the vault agent

The packaged specialist resolves the same vault from any caller cwd. Claude
Code supports `@agent-obsidian-second-brain:wiki-vault`. Pi users can optionally
install `pi-subagents` and run
`/run obsidian-second-brain.wiki-vault "your task"`. The runner is not required
for the skill or lifecycle. Neither specialist pins a model or effort.

Claude Code session-wide subagent settings can be selected at launch:

```sh
CLAUDE_CODE_SUBAGENT_MODEL=sonnet CLAUDE_CODE_EFFORT_LEVEL=high claude
```

The effort setting also affects the main session. For a local agent-specific
choice, copy the packaged agent into the appropriate `.claude/agents/` directory,
add model/effort fields, and replace the plugin-only skill path with an explicit
Skill-tool invocation of `/obsidian-second-brain:wiki` (add `Skill` to its tools).
The local copy will not automatically track package changes. Available models
and effort levels depend on the client/account policy.

For Pi, merge an override into user settings without replacing existing fields:

```json
{
  "subagents": {
    "agentOverrides": {
      "obsidian-second-brain.wiki-vault": {
        "model": "your-provider/your-model",
        "thinking": "high"
      }
    }
  }
}
```

These settings affect the specialist, not the parent. Keep one wiki writer;
multiple read-only agents can consult the vault concurrently.

## Check a problem

| Symptom | Check |
| --- | --- |
| Missing/ambiguous vault | Inspect the selected config and invocation override; do not create a wiki in caller cwd. |
| Configuration apply refuses plugin readiness | Install/enable the supported Obsidian Git version, then preview again. Scaffold creation can precede this. |
| Existing index/log conflict | Preserve the reported file; review ownership/content before any migration. |
| Invalid integration config | Use the exact, approved selected-config repair; unrelated repository work remains independent. |
| Pending batch does not finalize | Repair/remove its invalid changed pages and retry; do not edit the log/index directly. |
| Exclusion diagnostic | Check effective ignores and already-tracked state; review fixes before applying. |
| Search misses a recent page | Ensure its batch finalized; explicitly rebuild if needed, or use generated navigation. |
| No automatic backup/push | Check the running app, paused automation, credentials, and plugin status; configuration alone is not proof of sync. |

Package verification commands are in the [README](../README.md#verify-the-package).
Other optional Obsidian plugins and appearance choices remain owner-controlled;
see the [plugin guide](../skills/wiki/references/plugins.md) and
[visual guide](../skills/wiki/references/css-snippets.md).
