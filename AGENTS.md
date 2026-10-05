# Repository instructions

This repository packages an Open Knowledge Format (OKF) v0.2 wiki skill for Claude Code and Pi. It supplies one destination-based vault lifecycle hook, middleware that validates pages and generates navigation, a Pi extension, and reviewed setup and retrieval tools. Obsidian Git is the configured workflow's prerequisite and sole commit/pull/push owner. Keep Markdown, middleware, and local retrieval usable while the app is closed; Ollama and other plugins remain optional.

## Architecture and ownership

- `hooks/hooks.json` registers Claude lifecycle events. `extensions/obsidian.ts` registers Pi events; both call `hooks/obsidian-session.sh`. The hook resolves the selected vault independently of caller cwd and protects file-tool destinations. Ordinary relative coding paths retain caller-cwd semantics. Startup and unrelated read-only shutdown perform no vault writes; explicit cross-directory wiki changes receive lifecycle maintenance.
- `skills/wiki/SKILL.md` defines agent behavior. `skills/wiki/references/` holds its operating guides. `skills/wiki/scripts/okf_mw/` owns path guarding, frontmatter validation, index generation, and link linting.
- `scripts/bootstrap-vault.py` separates a reviewed minimal scaffold from explicit `--configure` settings/profile migration. Retrieval uses package-owned scripts with an explicit vault root; `scripts/provision-retrieval.py` remains a legacy helper-copy workflow. Derived cache and lifecycle state belong under `<vault>/.vault-meta/retrieval/` and `<vault>/.vault-meta/lifecycle/`. Setup never initializes Git or configures remotes/authentication.
- `tests/` owns lifecycle, middleware, bootstrap, retrieval, Pi extension, and isolated install checks. `.claude-plugin/` owns Claude manifests; `.github/workflows/test.yml` defines the macOS/Linux CI checks. Keep user instructions in `README.md` and `docs/` aligned with behavior.

## Rules that protect vault data

- OKF reserves `index.md` and `log.md` at any level under `wiki/`, with case-insensitive matching. Middleware generates indexes; the lifecycle hook owns the log. Never write either through an agent file tool or reintroduce the retired `wiki/toc.md`. Normal wiki pages require YAML frontmatter with `type`.
- File-tool guards do not sandbox shell writes. Follow `skills/wiki/SKILL.md` for guard, validate, and sync order when changing vault pages outside agent file tools. Do not weaken path, symlink, or collision checks without tests that cover existing-vault data.
- Preserve preview, explicit `--apply --confirm <planHash>`, conflict refusal, and repeat-run behavior in setup scripts. Scaffold-only setup preserves existing configs. Explicit reviewed configuration sets deprecated `features.autoCommit` to `false` while preserving unrelated fields; even legacy `true` or omitted values never authorize runtime Git writes. Preserve existing plugin schedules and disabled-push intent. Settings replacement/rollback must not overwrite concurrent user/app edits.
- Retrieval is local by default. Keep derived cache and lifecycle state effectively ignored in Git, including linked worktrees, negation rules, and already-tracked files. Preserve existing ownership journals and generated-index collision verification; do not migrate/delete them merely to remove agent commits. Ollama embeddings require an explicit rerank option and a loopback endpoint; failed reranking keeps BM25 results.

## Toolchain and verification

`package.json` defines source, release, and archive/install tests. Its `version` hook synchronizes the embedded Claude plugin manifest during semantic-release npm preparation; `prepublishOnly` checks version agreement. The Git-backed Claude catalog uses an unpinned npm plugin source so both clients consume the same release artifact. `.github/workflows/test.yml` runs pre-merge macOS/Linux tests and records the tested tree; `.github/workflows/pr-title.yml` validates PR titles without rerunning code checks on metadata edits. Require `CI passed` and `PR title passed`. On a main push, `.github/workflows/release.yml` verifies matching successful PR evidence, smoke-tests the prepared version, and publishes via npm OIDC. Recovery of existing partial tags uses the same verification and preserves published outputs. There is no repeated main matrix, manual client-verification gate, or approval environment. Pi loads the shipped TypeScript directly. The shipped package has no lockfile or build, format, or lint script; `.github/release-tools/` separately locks publishing dependencies. Its audit prints all findings and temporarily blocks critical severity; high/moderate findings remain unresolved and documented in `docs/ci-cd.md`. CI uses Node.js 24/Python 3.14; these are CI versions, not declared minimum supported versions. `.github/workflows/renovate.yml` maintains SHA-pinned actions and CI/release tool versions through reviewed PRs.

Python needs PyYAML, and the lifecycle hook needs `jq` when a config exists. Git is used by installation and owner-controlled Obsidian Git version control. Runtime may query Git read-only for collision/exclusion checks but must never stage, commit, pull, or push. See `README.md` for a Python virtual environment and agent installation.

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

## Configuration

`properties.example.json` documents feature names but has `vaultPath: null`, which leaves the integration inactive. The reviewed `--configure` step selects or migrates `~/.config/obsidian-second-brain/properties.json` unless `OBSIDIAN_AGENT_CONFIG` selects another path. The hook also accepts `OBSIDIAN_VAULT_PATH` as an invocation override.

Keep credentials and personal vault data outside this repository; normal BM25 does not need a model or network service. Consult `docs/setup.md` before changing onboarding commands.
