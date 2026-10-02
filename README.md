# obsidian-second-brain

Keep an [Open Knowledge Format (OKF) v0.2](skills/wiki/SKILL.md) wiki in an Obsidian vault with Claude Code or Pi. The package supplies a wiki skill, a vault-scoped lifecycle hook, and a Pi extension. You can use the wiki without Git, Obsidian community plugins, or retrieval helpers.

The vault remains yours. Bootstrap shows every proposed write before you confirm it. Existing notes, indexes, logs, and agent settings stay in place; conflicting files stop setup.

See [NOTICE.md](NOTICE.md) and [LICENSE](LICENSE) for upstream attribution and license terms.

## Requirements

- Install Claude Code or Pi for your operating system. Use Python 3 with PyYAML and `jq` for setup. Git clones the checkout; Node.js and npm run the tests.
- Create or choose a vault directory. Obsidian can open the directory as a vault; its desktop app is not required for the wiki scripts.
- Keep a local checkout of this repository for bootstrap and optional retrieval setup. Installing the package in an agent does not put these commands in your shell path.

If Python cannot import PyYAML, create a virtual environment and activate it before using the Python commands:

```sh
python3 -m venv "$HOME/.venvs/obsidian-second-brain"
. "$HOME/.venvs/obsidian-second-brain/bin/activate"
python3 -m pip install PyYAML
```

On Linux, install your distribution's `python3-venv` package if the first command fails. Keep the environment active when running the commands in this guide.

## Install the agent package

For Pi, the npm installation command after the first release is published is:

```sh
pi install npm:@soulwaxx/obsidian-second-brain
```

Until then, or to install from GitHub, use:

```sh
pi install git:github.com/soulwaxx/obsidian-second-brain
```

For Claude Code:

```sh
claude plugin marketplace add soulwaxx/obsidian-second-brain
claude plugin install obsidian-second-brain@obsidian-second-brain --scope user
```

Use an existing repository checkout or create one for the next steps:

```sh
git clone https://github.com/soulwaxx/obsidian-second-brain.git
cd obsidian-second-brain
```

## Set up a vault

Choose an absolute vault path. Run the preview from the repository checkout and inspect its `directories`, `writes`, and `configStatus` fields:

```sh
VAULT="$HOME/Notes/MyVault" # Replace with your chosen absolute path.
python3 scripts/bootstrap-vault.py --vault "$VAULT"
```

Copy the printed `planHash` into the next command only after reviewing the plan:

```sh
python3 scripts/bootstrap-vault.py --vault "$VAULT" --apply --confirm 'PASTE_PLAN_HASH_HERE'
```

Bootstrap creates `.obsidian/`, `wiki/quickstart.md`, generated `wiki/index.md`, and the agent config if they are absent. It leaves existing files untouched and refuses conflicting indexes or logs. If the vault or config changes between preview and apply, preview again.

Bootstrap does not initialize Git or install retrieval helpers.

A new config at `~/.config/obsidian-second-brain/properties.json` points to this vault and sets `features.autoCommit` to `false`. An existing config keeps its values; an omitted `autoCommit` setting still defaults to `true`. If the integration reports a config problem, stop wiki writes. Propose the exact minimal repair to the selected config and get approval for those exact changes before editing only that config. Preserve unrelated fields, never enable `autoCommit` without direction, then revalidate and reload the config in the same session before resuming. Use `OBSIDIAN_AGENT_CONFIG` to select another config path.

For an existing vault, collision rules, and a step-by-step example, read [Set up a vault](docs/setup.md). For optional version control, follow [Git setup](skills/wiki/references/git-setup.md) after reviewing its ignore rules.

## Add retrieval only if you need it

The wiki works through generated indexes without retrieval helpers. To add local text search, preview and confirm provisioning from the repository checkout:

```sh
python3 scripts/provision-retrieval.py --vault "$VAULT"
```

Review its `writes` and copy its new `planHash`. Replace the placeholder before applying:

```sh
python3 scripts/provision-retrieval.py --vault "$VAULT" --apply --confirm 'PASTE_RETRIEVAL_PLAN_HASH_HERE'
cd "$VAULT"
python3 scripts/bm25-index.py build --vault .
python3 scripts/retrieve.py "query terms" --vault .
```

The generated index lives under `.vault-meta/retrieval/`; keep that directory out of Git. Rebuild it after upgrading the package to enable title-aware ranking for existing vaults. Provisioning adds a local exclusion in an ordinary Git vault and checks the effective exclusion in a linked Git worktree. It refuses to replace existing helper scripts.

Embedding reranking requires a running local Ollama service and an embedding model you install yourself. Run `ollama pull nomic-embed-text`, then add `--rerank` to a retrieval query. Normal retrieval makes no Ollama request. Reranking uses note content over a loopback connection and falls back to text search if Ollama fails. Exact title matches are prioritized for topic-only searches; queries containing an ISO date stay lexical, preferring an exact matching dated note and then a relevant dated section in a canonical page. Additional qualifier terms refine dated-section relevance without disabling the date priority. Successful semantic reranks label their output scores `cosine=` rather than BM25. See [Set up a vault](docs/setup.md#optional-local-retrieval) for the exact sequence and privacy limits.

## Use the wiki

Start your installed agent from the vault directory. Invoke `/obsidian-second-brain:wiki` in Claude Code or `/skill:wiki` in Pi. The skill guides you through writing OKF pages; ordinary pages need a `type` field in their YAML frontmatter. Read [Init mode](skills/wiki/references/init-mode.md) for the first-page workflow and [frontmatter rules](skills/wiki/references/frontmatter.md) for page fields.

To delegate a vault task to the packaged specialist, use `@agent-obsidian-second-brain:wiki-vault` in Claude Code. In Pi, optionally install `pi-subagents` with `pi install npm:pi-subagents`, then ask the parent to run `obsidian-second-brain.wiki-vault` (or use `/run obsidian-second-brain.wiki-vault "your task"`). The Pi agent is not required for `/skill:wiki` or the vault lifecycle. Both agents use the bundled wiki skill and leave their model and effort unpinned; see [agent configuration](docs/setup.md#configure-the-vault-agent) for per-client choices.

The lifecycle hook runs only when the agent's working directory is inside the configured vault. It supplies the generated index at session start and checks protected paths before supported file writes. After each successful page write it validates changed Markdown pages and synchronizes generated indexes independently of Git and `autoCommit`; shutdown also runs a convergence sync. Committing is separate and happens only when `features.autoCommit` is true in a Git vault with a `.git` directory. Existing configs with no setting retain the true default. Do not edit generated `index.md` files or the hook-owned `wiki/log.md` by hand.

## Verify the package

Run the repository checks from its checkout:

```sh
npm run release:check
npm test
npm run test:release
npm run test:package
claude plugin validate .
bash tests/install-smoke.sh
bash -n hooks/obsidian-session.sh tests/install-smoke.sh
shellcheck tests/install-smoke.sh
```

The install smoke uses temporary agent settings and a temporary vault. It does not send a model prompt. [Release verification](docs/release-verification.md) records the macOS/Linux continuous integration checks and the separate live-client checks required before a release. Authenticated first runs remain untested until those checks are recorded. Stable version tags trigger the release pipeline: verify all three manifests and live-client records, run macOS/Linux CI, publish to npm through the protected `release` environment, and create a GitHub Release. CI also validates workflows and tests Node 24/Python 3.14 compatibility; its aggregate **CI passed** check covers every job. For coordinated versioning and one-time GitHub/npm setup, see [Releases and npm publishing](docs/publishing.md).
