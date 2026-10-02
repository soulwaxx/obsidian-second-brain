# Repository instructions

This repository packages an Open Knowledge Format (OKF) v0.2 wiki skill for Claude Code and Pi. It supplies one vault-scoped lifecycle hook, middleware that validates pages and generates navigation, a Pi extension, and optional vault setup and retrieval tools. Keep the wiki usable without Git, Obsidian plugins, Ollama, or the retrieval helpers.

## Architecture and ownership

- `hooks/hooks.json` registers Claude lifecycle events. `extensions/obsidian.ts` registers Pi events; both call `hooks/obsidian-session.sh`. The hook reads the vault config, checks the working directory, and calls the middleware. Outside the configured vault it does no vault work.
- `skills/wiki/SKILL.md` defines agent behavior. `skills/wiki/references/` holds its operating guides. `skills/wiki/scripts/okf_mw/` owns path guarding, frontmatter validation, index generation, and link linting.
- `scripts/bootstrap-vault.py` creates a minimal wiki from a reviewed plan. `scripts/provision-retrieval.py` installs optional helpers into `<vault>/scripts/`; the BM25 cache belongs under `<vault>/.vault-meta/retrieval/`. Neither setup command initializes Git.
- `tests/` owns lifecycle, middleware, bootstrap, retrieval, Pi extension, and isolated install checks. `.claude-plugin/` owns Claude manifests; `.github/workflows/test.yml` defines the macOS/Linux CI checks. Keep user instructions in `README.md` and `docs/` aligned with behavior.

## Rules that protect vault data

- OKF reserves `index.md` and `log.md` at any level under `wiki/`, with case-insensitive matching. Middleware generates indexes; the lifecycle hook owns the log. Never write either through an agent file tool or reintroduce the retired `wiki/toc.md`. Normal wiki pages require YAML frontmatter with `type`.
- File-tool guards do not sandbox shell writes. Follow `skills/wiki/SKILL.md` for guard, validate, and sync order when changing vault pages outside agent file tools. Do not weaken path, symlink, or collision checks without tests that cover existing-vault data.
- Preserve preview, explicit `--apply --confirm <planHash>`, conflict refusal, and repeat-run behavior in setup scripts. New bootstrap configs set `features.autoCommit` to `false`; existing configs keep their values, including the true default when the field is absent.
- Retrieval is opt-in and local by default. Keep derived cache files out of Git, including in linked worktrees. Ollama embeddings require an explicit rerank option and a loopback endpoint; failed reranking keeps BM25 results.

## Toolchain and verification

`package.json` defines source, release, and archive/install tests. Its `version` hook synchronizes the two Claude manifests; `prepublishOnly` checks version agreement and runs all three test commands. `.github/workflows/release.yml` checks version tags and recorded live-client passes, calls the macOS/Linux test workflow at the exact release commit, then publishes npm and creates a GitHub Release through the `release` environment. Pi loads the shipped TypeScript directly. There is no lockfile or package build, format, or lint script. CI tests Node.js 22/Python 3.12 and Node.js 24/Python 3.14 on both operating systems; these are CI versions, not declared minimum supported versions. `.github/workflows/renovate.yml` maintains SHA-pinned actions and direct CI tool versions through reviewed PRs.

Python needs PyYAML, and the lifecycle hook needs `jq` when a config exists. Git is used by installation and optional vault version control. See `README.md` for a Python virtual environment and agent installation.

Run the relevant commands from the repository root before calling a change complete (sources: `package.json` and `.github/workflows/test.yml`):

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

The install smoke uses temporary agent settings and a disposable vault; do not replace them with your real profile. ShellCheck, Claude CLI, and Pi CLI must be installed for their respective checks. Report unavailable commands as unverified, not passed.

The authenticated macOS/Linux live-client checks in `docs/release-verification.md` remain a separate release gate; CI smoke does not satisfy them.

## Configuration

`properties.example.json` documents feature names but has `vaultPath: null`, which leaves the integration inactive. Bootstrap writes `~/.config/obsidian-second-brain/properties.json` unless `OBSIDIAN_AGENT_CONFIG` selects another path. The hook also accepts `OBSIDIAN_VAULT_PATH` as an invocation override.

Keep credentials and personal vault data outside this repository; normal BM25 does not need a model or network service. Consult `docs/setup.md` before changing onboarding commands.
