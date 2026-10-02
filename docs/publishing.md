# Releases and npm publishing

The public npm name is `@soulwaxx/obsidian-second-brain`. npm distributes the Pi package; Claude Code continues to install the plugin through the GitHub marketplace. Both use the same source and release version.

## What the pipeline does

`.github/workflows/release.yml` uses a protected `release` environment, GitHub-hosted runners, and npm Trusted Publishing with OIDC. It coordinates Claude/npm versions, existing version tags, macOS/Linux verification, and GitHub Releases. Actions use full commit SHAs and publication is serialized across all version tags.

Pushing a stable version tag, such as `v0.1.0`, starts the release workflow. It:

1. Checks that the workflow ref is the requested version tag, its event SHA matches the checkout, and the tag, npm version, Claude plugin version, and marketplace entry version agree. This keeps npm provenance tied to the tested release source.
2. Refuses release while any of the four [live-client verification records](release-verification.md#manual-live-client-checks) is missing, not `PASS`, or lacks evidence.
3. Runs workflow/shell quality checks and the macOS/Ubuntu test workflow against the exact tagged commit, covering Node 22/Python 3.12 and Node 24/Python 3.14. The original runtime pair also runs agent install smoke.
4. Enters the GitHub `release` environment. When required reviewers are configured, publication waits for approval. After approval, re-fetches the remote tag and refuses publication if it no longer points at the tested commit.
5. Publishes the public npm package with provenance. A new version must exceed npm's stable latest version, preventing older tags from downgrading latest. npm's pre-publish hook rechecks versions and runs the source, release, and packed-runtime tests.
6. Creates a GitHub Release for the existing tag with generated notes, or publishes an existing draft.

Normal branch pushes and pull requests run tests but do not publish. The release workflow also supports manual dispatch from an existing version tag, with that same tag supplied as the input. Only `soulwaxx/obsidian-second-brain` can run its release jobs; forks cannot publish through this workflow.

## One-time account setup

### GitHub

In this repository's **Settings → Environments**, create an environment named **release**:

- Add a required reviewer. For a solo project, allow self-review so the maintainer can approve their own release.
- Restrict deployments to version tags matching `v*`. Manual retries must dispatch from the same tag, not main, because GitHub deployment policies and npm provenance use the workflow ref, not just the checked-out source.
- Protect existing `v*` tags against updates and deletion with a tag ruleset.
- Confirm approval covers the recorded live checks for the exact code being released. CI does not perform authenticated model sessions.

The `environment: release` line alone does not enforce approval: GitHub's environment settings must contain the reviewer rule. The workflow grants write permissions only to the publishing job: OIDC identity for npm and repository contents for the GitHub Release. It does not need a personal GitHub token.

### npm: first publication

The npm account must own the `soulwaxx` user scope or have publishing permission in an npm organization named `soulwaxx`. Another npm username does not automatically own that scope.

For the first publication through the pipeline, create a short-lived **granular npm access token** with write permission for the `@soulwaxx` scope and bypass 2FA enabled. Store it as an **environment secret named NPM_TOKEN** under GitHub's `release` environment. Never put the token in source files, a commit, release notes, or chat.

This bootstrap token lets the workflow create the package before its npm Trusted Publisher setting exists. Alternatively, make the first publication interactively after all release checks pass, from the clean tagged checkout:

```sh
npm login --registry=https://registry.npmjs.org/
npm run release:check -- v0.1.0
npm run release:verify
npm publish --access public --registry=https://registry.npmjs.org/
```

The workflow can then finish the GitHub Release for that tag, provided npm's published `gitHead` matches the tagged commit.

### npm: subsequent publications without a stored token

After the package exists, open its **Settings → Trusted Publisher** on npmjs.com and configure GitHub Actions:

| Setting | Value |
| --- | --- |
| Organization or user | `soulwaxx` |
| Repository | `obsidian-second-brain` |
| Workflow filename | `release.yml` |
| Environment name | `release` |
| Allowed actions | Allow direct `npm publish` |

Then delete the GitHub `NPM_TOKEN` secret and revoke the bootstrap token in npm. Subsequent releases use GitHub's short-lived OIDC identity. npm requires CLI 11.5.1 or later and Node 22.14.0 or later for this; the publishing job uses Node 24 and a pinned npm 11 version maintained by Renovate. Dependency caches are disabled for publication.

Account permissions, secrets, and Trusted Publisher settings are external account configuration, not settings a repository commit can supply.

## Preparing the first release

The first version is `0.1.0`; no version bump is needed merely to publish it.

1. Commit and push the packaging and release automation to the default branch.
2. Complete and record the four live-client checks for the release candidate in `docs/release-verification.md`. Keep `UNTESTED` or `FAIL` until genuinely passed. Include date, client/OS versions, commit or tag, consent, and observations.
3. Commit that evidence, verify the manifests, and run the local checks.
4. From the clean, reviewed release commit, create and push the annotated tag:

```sh
npm run release:check
npm run release:verify
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin main refs/tags/v0.1.0
```

GitHub Actions will run the release checks, wait for the configured environment approval, publish to npm, and create the GitHub Release. Do not manually create a second GitHub Release as part of the normal flow.

No package is published merely by editing the manifests or running tests. The current unrecorded live checks intentionally block the release workflow.

## Versioning subsequent releases

Start with a clean working tree on the release branch. Finish the candidate's live verification records, then use npm's version command:

```sh
npm version patch -m "Release v%s"
```

Use `minor` or `major` when appropriate. The npm `version` hook synchronizes:

- `package.json`
- `.claude-plugin/plugin.json`
- The plugin entry in `.claude-plugin/marketplace.json`

npm stages the package manifest; the hook stages the two Claude manifests. npm then creates a version commit and annotated tag together. It does not push automatically, so the generated commit remains reviewable.

For an update from `0.1.0` to `0.1.1`:

```sh
npm run release:check -- v0.1.1
npm run release:verify
git show --stat v0.1.1
git push origin main refs/tags/v0.1.1
```

This pipeline supports stable `X.Y.Z` releases. npm cannot overwrite a published name/version pair. Do not move a published version tag to different code.

## Retry and failure behavior

Use **Actions → Release → Run workflow**, selecting the existing tag in **Use workflow from** and supplying the same tag as the input, or use the GitHub CLI:

```sh
gh workflow run release.yml --ref v0.1.0 -f tag=v0.1.0
```

The workflow checks out the tag and verifies it matches the dispatch event's ref and SHA. Dispatching from a branch or supplying a different tag fails validation. A retry reruns verification and environment approval.

If npm already has that version, publication is skipped only when its recorded `gitHead` matches the exact tagged commit. A different or missing `gitHead`, registry authentication failure, or network error stops the workflow rather than pretending publication succeeded. This allows a retry to finish GitHub Release creation after npm publication succeeded.

A previously unpublished version at or below npm latest is refused rather than moving latest backward. A matching already-published version still permits GitHub Release recovery without altering npm dist-tags.

An npm publishing failure prevents GitHub Release creation. Neither failure moves the tag or bumps versions. GitHub Release creation is attempted only after an API 404 confirms absence; authentication, rate-limit, network, or invalid-response errors stop recovery. Existing public GitHub Releases keep their notes; existing drafts are finalized after npm succeeds.

New tags must contain the updated workflows, CI requirements, and release script. Pipeline changes do not rewrite historical tags or retroactively change the workflow stored in those tags. Prepare a new version rather than moving an existing tag.

## Local checks and archive contents

```sh
npm run release:check
npm test
npm run test:release
npm run test:package
claude plugin validate .
bash tests/install-smoke.sh
bash -n hooks/obsidian-session.sh tests/install-smoke.sh
shellcheck tests/install-smoke.sh
npm pack --dry-run
npm publish --dry-run --access public --registry=https://registry.npmjs.org/
```

A publish dry run tests packaging but does not satisfy authenticated live-client verification. Do not bypass publishing hooks with `--ignore-scripts`.

Pi loads the shipped TypeScript directly; unlike pi-subagents' compiled distribution, no build is needed. The archive includes the extension, skill and references, middleware, hook, agent definitions, Python setup/retrieval scripts, example config, documentation, and upstream license notices. Tests, CI/release automation scripts, Python caches, and vault data are excluded.

Installation does not configure a vault, install Python dependencies, initialize Git, or enable retrieval. The [setup guide](setup.md) keeps bootstrap and retrieval opt-in with reviewed preview/apply operations.

## Install and update

After publication, Pi users install with:

```sh
pi install npm:@soulwaxx/obsidian-second-brain
```

If the GitHub source is already installed, remove that declaration first to avoid loading both sources:

```sh
pi remove git:github.com/soulwaxx/obsidian-second-brain
pi install npm:@soulwaxx/obsidian-second-brain
```

Restart Pi or reload resources after installation changes. The optional specialist keeps the name `obsidian-second-brain.wiki-vault`, independently of the npm scope.

Unpinned installations update with:

```sh
pi update npm:@soulwaxx/obsidian-second-brain
```

A versioned installation such as `npm:@soulwaxx/obsidian-second-brain@0.1.0` stays pinned. Claude Code continues to use the GitHub marketplace; the synchronized plugin version identifies its updates.

## References

- [pi-subagents release workflow](https://github.com/nicobailon/pi-subagents/blob/main/.github/workflows/release.yml)
- [npm Trusted Publishing](https://docs.npmjs.com/trusted-publishers)
- [npm version lifecycle](https://docs.npmjs.com/cli/v11/commands/npm-version)
- [GitHub deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
