# Set up a vault

Use this procedure for a new directory or an existing Obsidian vault. Preview each change from the repository checkout before you apply it. Bootstrap stops when an existing index, log, or config conflicts with the proposed setup.

## 1. Prepare the tools

Install Claude Code or Pi, Python 3, PyYAML, and `jq`. Install Git to clone this repository or to version your vault. If Python cannot import PyYAML, activate a virtual environment and install it as shown in the [README](../README.md#requirements).

Install the package in your agent using the [README commands](../README.md#install-the-agent-package). Keep a separate checkout for `scripts/bootstrap-vault.py` and `scripts/provision-retrieval.py`. The agent package manager does not add these scripts to your shell path.

From the repository checkout, save its path and choose your vault location:

```sh
PLUGIN_DIR="$(pwd -P)"
VAULT="$HOME/Notes/MyVault" # Replace with your absolute vault path.
```

Keep these variables in the same shell for the remaining steps.

The path can name a new directory or an existing vault. Avoid pointing it at a directory that contains unrelated notes until you have reviewed the preview.

## 2. Preview bootstrap

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT"
```

Read the JSON plan. `directories` lists new directories; `writes` lists exact file contents; `configStatus` reports the selected vault and auto-commit state. The `planHash` binds the confirmation to the inspected vault, Obsidian settings, and agent config.

Bootstrap creates only missing setup files:

| Path | Purpose |
| --- | --- |
| `.obsidian/` | Marks the directory for Obsidian. Open it with **Manage Vaults > Open folder as vault** if you use the desktop app. |
| `wiki/quickstart.md` | Gives you an entry page with a backlog. |
| `wiki/index.md` | Provides generated navigation. The middleware creates additional section indexes when pages need them. |
| `~/.config/obsidian-second-brain/properties.json` | Selects the vault for the lifecycle hook. Set `OBSIDIAN_AGENT_CONFIG` before preview if you need another location. |

Bootstrap leaves an existing quickstart page and other notes alone. It refuses a conflicting generated index, a user-owned `log.md`, or a symlink in the paths it inspects. Review the reported conflict instead of deleting or replacing the file to force setup through.

### Existing agent config

If the config already points to this vault, bootstrap preserves it byte for byte. Check its `features.autoCommit` value: an omitted value defaults to `true`. A new config sets it to `false`.

If the existing config has a null or missing `vaultPath`, edit that file in place to set the intended absolute vault path and `features.autoCommit` to `false`. Keep its other settings. If it points to a different vault, choose a separate config path with `OBSIDIAN_AGENT_CONFIG` or decide how you want to manage the existing integration before retrying.

## 3. Apply the reviewed plan

Copy `planHash` from the preview. Replace the placeholder below with that exact value:

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT" --apply --confirm 'PASTE_PLAN_HASH_HERE'
```

If the command reports a stale plan, run preview again and review the new plan. A repeat preview after a successful apply lists no new setup writes. Check the created pages before editing your vault.

## 4. Decide whether to use Git

Git is optional. A vault without its own Git repository still synchronizes indexes at shutdown. Bootstrap does not initialize a repository, stage files, or set up automatic commits.

For a new vault repository, [review the Git procedure](../skills/wiki/references/git-setup.md) before you run `git init`. Add ignore rules before staging notes and stage only paths you intend to commit. If the vault lives inside another repository, do not treat the parent repository as a fresh vault repository. Inspect staged changes before each commit.

For an existing Git vault, keep its `.gitignore` and history. Review the `features.autoCommit` setting separately: it defaults to `true` when omitted from an existing config. Obsidian Git backups and the agent's auto-commit setting are separate controls.

## 5. Start the agent

Change into the vault before starting Claude Code or Pi:

```sh
cd "$VAULT"
claude
```

For Pi, run `pi` in the same directory instead. Run `/obsidian-second-brain:wiki` in Claude Code or `/skill:wiki` in Pi. Ask the skill to build the wiki from your chosen sources, or use the existing `wiki/quickstart.md` as the first page. The agent reads the index at session start when `features.toc` is on.

Write normal pages under `wiki/` with YAML frontmatter. Each non-reserved Markdown page needs a `type` field. The [frontmatter guide](../skills/wiki/references/frontmatter.md) covers optional fields; [Init mode](../skills/wiki/references/init-mode.md) describes the first-page workflow. Leave generated `index.md` files and `wiki/log.md` to the middleware and hook.

The lifecycle hook checks supported agent file writes inside the configured vault. Shell writes bypass that pre-write check, so use the skill's guard and validator if you write wiki pages through a shell. A session launched outside the vault does not run vault lifecycle work.

## Optional local retrieval

You can use `wiki/index.md` without retrieval. To add local text search, preview helper installation with the checkout path saved above:

```sh
python3 "$PLUGIN_DIR/scripts/provision-retrieval.py" --vault "$VAULT"
```

Inspect `writes`, `cacheIgnore`, and the new `planHash`. Provisioning refuses existing `scripts/retrieve.py`, `scripts/bm25-index.py`, and `scripts/contextual-prefix.py`; it does not replace your scripts. In a Git vault with a `.git` directory, it adds `.vault-meta/retrieval/` to the local Git exclusion. In a linked Git worktree, it requires an effective directory exclusion before applying.

Confirm the reviewed retrieval plan, then build and query the index from the vault directory:

```sh
python3 "$PLUGIN_DIR/scripts/provision-retrieval.py" --vault "$VAULT" --apply --confirm 'PASTE_RETRIEVAL_PLAN_HASH_HERE'
cd "$VAULT"
python3 scripts/bm25-index.py build --vault .
python3 scripts/retrieve.py "query terms" --vault .
```

These helpers index vault Markdown locally with BM25 text ranking. `contextual-prefix.py` is a hook-compatible no-op; it does not call a model. The hook refreshes the index at shutdown when `features.retrievalRefresh` is on and the helpers are installed. Generated state stays under `.vault-meta/retrieval/` and the hook does not auto-stage or auto-commit it. If you already track that directory, remove it from tracking deliberately before relying on the Git exclusion.

Embedding reranking is a separate opt-in. Install and start Ollama yourself, download an embedding model, and request reranking for a query:

```sh
ollama pull nomic-embed-text
python3 scripts/retrieve.py "query terms" --vault . --rerank
```

The default reranker sends the query and candidate note content to `http://127.0.0.1:11434/api/embed`. It accepts only localhost or loopback endpoints, ignores proxy settings, and rejects redirects. It does not download models. If Ollama is unavailable or returns invalid embeddings, retrieval keeps the BM25 order; a missing or invalid index sends you back to `wiki/index.md` navigation.

## Check a problem

| Symptom | Check |
| --- | --- |
| Bootstrap refuses an existing config | Check `vaultPath` and `features.autoCommit` in the selected config. Keep other settings; preview again after editing. |
| Bootstrap refuses an index or log | Check the reported path. The command protects existing content rather than replacing it. |
| The skill or hook stays silent | Start the installed agent inside the configured vault. Confirm the correct config path and its `vaultPath`; check `features.toc` for session-start index output. |
| Retrieval finds no matches | Confirm that helper provisioning succeeded, then rebuild the BM25 index from the vault root. Use `wiki/index.md` while you diagnose the cache. |
| Ollama reranking falls back to BM25 | Confirm Ollama is running locally and that `nomic-embed-text` is installed. The fallback leaves local text search available. |
| Linked-worktree provisioning refuses to run | Make Git effectively ignore `.vault-meta/retrieval/`, including custom index filenames. Remove later negation rules, then preview again. |

For package checks and the unverified live-host release gate, read [Release verification](release-verification.md). For optional Obsidian plugins and graph colors, use the [plugin guide](../skills/wiki/references/plugins.md) and [visual guide](../skills/wiki/references/css-snippets.md); neither is required for setup.
