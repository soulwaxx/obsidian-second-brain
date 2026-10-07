# obsidian-second-brain

Maintain an [Open Knowledge Format (OKF) v0.2](skills/wiki/SKILL.md) wiki in an
Obsidian vault from Claude Code or Pi, without moving the agent out of its
current repository. The package supplies a wiki skill, destination-based file
protection, lifecycle maintenance, and local retrieval tools.

**Obsidian Git owns commits, pulls, and pushes.** Install and enable it for the
configured workflow; the agent integration never performs those operations.
Markdown, local retrieval, and page maintenance remain usable while Obsidian
is closed. Automatic sync requires the running app and plugin.

The vault remains yours. Setup previews proposed changes before confirmation,
preserves existing notes and unrelated settings, and refuses conflicts. See
[NOTICE.md](NOTICE.md) and [LICENSE](LICENSE) for attribution and license terms.

## Requirements

- Claude Code or Pi; Python 3 with PyYAML; `jq` for lifecycle commands.
- Obsidian with the owner-installed Obsidian Git plugin for version control.
  Setup validates its installed manifest and enabled-plugin list before applying
  the configuration profile; it does not download plugin binaries.
- Git for the vault repository and package installation. Repository creation,
  remote selection, identity, and authentication remain owner-controlled.
- A repository checkout or installed package directory for setup commands.
  Installing the package does not add a setup CLI to the shell path.

If Python cannot import PyYAML:

```sh
python3 -m venv "$HOME/.venvs/obsidian-second-brain"
. "$HOME/.venvs/obsidian-second-brain/bin/activate"
python3 -m pip install PyYAML
```

On Linux, install your distribution's `python3-venv` package if necessary. Keep
the environment active when running Python commands.

## Install the agent package

For Pi, install the published npm release:

```sh
pi install npm:@soulwaxx/obsidian-second-brain
```

To track unreleased source instead, use
`pi install git:github.com/soulwaxx/obsidian-second-brain`.

For Claude Code:

```sh
claude plugin marketplace add soulwaxx/obsidian-second-brain
claude plugin install obsidian-second-brain@obsidian-second-brain --scope user
```

The GitHub marketplace is a catalog; Claude fetches the plugin from the same
versioned npm artifact as Pi. The catalog deliberately does not pin a plugin
version. Claude Code and npm must support npm plugin sources.
Existing Git-backed installations retain the same plugin identity. Refresh the
catalog and update the plugin once to migrate:

```sh
claude plugin marketplace update obsidian-second-brain
claude plugin update obsidian-second-brain@obsidian-second-brain --scope user
```

Restart Claude Code after updating. For unpublished local development, load the
checkout with `claude --plugin-dir /absolute/path/to/obsidian-second-brain`;
adding its marketplace still selects the published npm plugin.

Use an existing checkout or create one for the following setup examples:

```sh
git clone https://github.com/soulwaxx/obsidian-second-brain.git
cd obsidian-second-brain
PLUGIN_DIR="$(pwd -P)"
VAULT="$HOME/Notes/MyVault" # Replace with the selected absolute vault path.
```

## Scaffold, then configure

First preview the minimal wiki scaffold:

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT"
```

Review the plan, then use its exact hash:

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT" --apply --confirm 'PASTE_SCAFFOLD_HASH_HERE'
```

This creates missing `.obsidian/`, `wiki/quickstart.md`, and generated navigation.
It preserves existing notes, logs, and settings. Scaffolding can precede plugin
installation; it does not yet create or migrate the agent integration config.

Open the directory as an Obsidian vault, deliberately create/use its Git
repository, and install/enable Obsidian Git. Review
[Git setup](skills/wiki/references/git-setup.md) before the first backup. Then
preview the supported configuration profile:

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT" --configure
```

Review the selected configuration/plugin differences and ignore additions,
then confirm this separate plan:

```sh
python3 "$PLUGIN_DIR/scripts/bootstrap-vault.py" --vault "$VAULT" --configure --apply --confirm 'PASTE_CONFIGURE_HASH_HERE'
```

The reviewed configuration selects the vault and sets the deprecated agent
`features.autoCommit` field to `false`. It preserves unrelated settings,
existing plugin schedules, and an existing choice to disable pushing. Disabling
plugin commit squashing is an explicit proposal, not a hidden reset. Private
settings/preimages are bound to the plan hash but not dumped in the preview.

The default config is `~/.config/obsidian-second-brain/properties.json`.
`OBSIDIAN_AGENT_CONFIG` selects another file; `OBSIDIAN_VAULT_PATH` selects a
vault for an invocation. Setup never silently retargets a config pointing to a
different vault. Re-preview when inspected files change. See
[the setup guide](docs/setup.md) for migration and prerequisite diagnostics.

## Work from any folder

Start the agent in the repository or folder where the main task belongs.
Use `/obsidian-second-brain:wiki` in Claude Code or `/skill:wiki` in Pi as the
general entry point. Focused intents are available as
`/obsidian-second-brain:wiki-query`, `/obsidian-second-brain:wiki-save`,
`/obsidian-second-brain:wiki-ingest`, `/obsidian-second-brain:wiki-research`,
and `/obsidian-second-brain:wiki-health` in Claude Code, or `/skill:wiki-query`,
`/skill:wiki-save`, `/skill:wiki-ingest`, `/skill:wiki-research`, and
`/skill:wiki-health` in Pi. Query and health are read-only; save only files an
explicitly selected conversation insight; ingest/research do not imply raw
source capture or automatic filing. Every intent follows the shared wiki
write rules. The skill resolves the configured vault and uses absolute runtime
paths when necessary. Ordinary relative coding paths keep their
current-directory meaning. Larger coordinated wiki edits may optionally use the
reviewed recoverable batch CLI; small edits retain the normal lifecycle. An
explicitly selected local UTF-8 text source may also be preserved through a
separate reviewed, immutable capture into `.raw/agent-captures/`. Neither is
mandatory, and capture does not provide web/media extraction or authorize wiki
filing. See the [focused workflow reference](skills/wiki/references/focused-workflows.md)
for exact approval and recovery commands.

Startup supplies a small vault locator rather than injecting the entire
personal index into unrelated work. The skill searches and reads relevant
pages when a task needs them. An approved ingest or research pass may update
relevant canonical pages; ordinary questions remain read-only unless filing
was requested. Keep one wiki writer at a time.

File-tool guards apply to selected-vault destinations regardless of agent cwd.
They protect generated `index.md` and lifecycle-owned `log.md` at every wiki
depth, curated sources, settings, and unsafe paths. Shell writes are not
sandboxed; follow the skill's explicit guard/validation workflow if the file
tool lifecycle is unavailable.

The lifecycle validates changed pages and finalizes writing batches with
navigation, logging, and optional retrieval refresh. Failed changed pages are
retained for repair; an unrelated historical defect is not a vault-wide veto.
Unrelated startup/read-only shutdown does not regenerate the vault. Obsidian
Git handles version control independently, including for legacy configs with
`autoCommit: true`.

## Query and diagnose

When the `obsidian-second-brain` bin is on `PATH`, use the package-owned CLI
for read-only structured search and doctor commands from any folder:

```sh
obsidian-second-brain search "query terms" --json
obsidian-second-brain doctor --json
obsidian-second-brain evidence-report --json
```

Pi and Claude plugin installation does not necessarily put that npm bin on
shell `PATH`. If `command -v obsidian-second-brain` fails, do not install a
second copy: invoke the active package's Node launcher using its package-relative
path. The [shared workflow reference](skills/wiki/references/focused-workflows.md#run-the-package-cli)
shows how to derive an absolute package root from Claude's loaded skill base
directory or Pi's loaded skill location; this works from any caller cwd and selects the
version already loaded by that client.

Search uses the selected vault and does not rebuild the index; doctor reports
configuration, dependencies, vault, cache-exclusion, lifecycle, and retrieval
status without repairing them. `evidence-report` is also read-only; optional
source/claim records are not migrated or required, and its structural,
freshness, and relationship output does not establish factual truth or source
verification. Use the wiki's generated index hierarchy when search is
unavailable or has no useful result. Focused workflow limits and
selected-conversation save rules are documented in
[the shared workflow reference](skills/wiki/references/focused-workflows.md).

## Local retrieval

Retrieval ships with the package; no vault-local helper installation, Ollama,
or network service is required. From any folder:

```sh
python3 "$PLUGIN_DIR/scripts/bm25-index.py" build --vault "$VAULT"
python3 "$PLUGIN_DIR/scripts/retrieve.py" "query terms" --vault "$VAULT"
```

The derived cache lives under `.vault-meta/retrieval/`. Keep it and
`.vault-meta/lifecycle/` effectively ignored in Git. Lifecycle refresh uses
package-owned scripts; a missing/corrupt cache falls back to generated index
navigation. Existing vault-local helpers are left untouched; the legacy
`provision-retrieval.py` preview/apply workflow remains available if needed.

Embedding operations default to `qwen3-embedding:4b`; `--ollama-model` selects
another already-installed model. Changing models does not make old-model cache
vectors compatible: rebuild semantic chunks explicitly after the selected model
is available. Embedding reranking is a separate opt-in. Install/start Ollama and
explicitly download the model before querying:

```sh
ollama pull qwen3-embedding:4b
python3 "$PLUGIN_DIR/scripts/retrieve.py" "query terms" --vault "$VAULT" --rerank
```

Normal retrieval makes no Ollama request. Reranking accepts loopback endpoints,
ignores proxies, rejects redirects, and falls back to BM25 on failure. Exact
topic-title matches receive a ranking boost; ISO-date queries stay lexical and
prefer matching dated notes/relevant dated sections. See
[retrieval details](docs/setup.md#local-retrieval).

## Optional specialist

Claude Code supports `@agent-obsidian-second-brain:wiki-vault`. For Pi, optionally
install `pi-subagents` with `pi install npm:pi-subagents`, then ask the parent to
run `obsidian-second-brain.wiki-vault`, or use
`/run obsidian-second-brain.wiki-vault "your task"`. The specialist resolves the
same configured vault; it does not require the parent to start there.

Neither packaged specialist pins a model or effort. See
[agent configuration](docs/setup.md#configure-the-vault-agent) for client
settings. Pi without the subagent runner still supports the skill and lifecycle.

## Verify the package

Run from the repository checkout:

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

Installation tests use temporary agent settings and a disposable vault, not the
real profile, and do not send a model prompt. CI checks macOS/Linux with Node 24
and Python 3.14. Use Conventional Commit PR titles: `feat:` releases a minor
version, a breaking change a major version, and other types a patch. Full code
checks run before merge; title/body edits run only metadata validation. After a
squash merge, publishing verifies that the merged tree passed PR CI, smoke-tests
the prepared release, and uses npm OIDC to publish with provenance. There is no
repeated main test matrix or manual client-verification gate.

Require **CI passed** and **PR title passed** in the main ruleset. Publishing
refuses direct pushes and mismatched or expired verification evidence. See
[CI/CD maintenance](docs/ci-cd.md) for rollout, locked release tools, the temporary
critical-blocking audit policy, and recovery of partially published tags.
