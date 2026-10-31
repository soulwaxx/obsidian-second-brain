# CI/CD maintenance

## Merge checks and automatic releases

GitHub's main ruleset must require **CI passed** and **PR title passed**, keep
branches up to date, and allow squash merges. Set the squash title default to
**PR_TITLE**. Retain existing deletion/force-push protections and deliberately
review any bypass permissions separately. Roll out the workflow changes through
a PR that runs the new CI and metadata workflows before merging; the new title
check will not appear on branches that still use the old workflow files.

- `CI` runs the full macOS/Linux source, release, packed-runtime, and isolated
  install checks on PR code changes. It also supports merge-group and manual
  diagnostic runs. After installing the agent CLIs, it requires the real Pi
  SDK runtime contract to pass; missing runtime discovery fails rather than
  silently skipping. It does not repeat the full matrix after a merge.
- `PR metadata` validates Conventional Commit titles. Title/body edits run only
  this inexpensive workflow, without cancelling or restarting the code checks.
- After both OS jobs pass, PR CI records its repository, run/attempt, PR head,
  base, tested commit, and Git tree in `verified-source-<attempt>`.
- On a main push, `Release` requires a merged PR with successful latest PR CI.
  Its recorded tree must match the merged tree and its base must match the
  main commit's parent. Direct pushes, unverified bypasses, and non-squash
  commits cannot publish. Missing/expired evidence fails closed.
- The privileged publishing job checks out that verified SHA, installs locked
  tooling, and prepares the actual release version. Before creating the tag,
  it checks version agreement, plugin validation, fresh npm installation,
  plugin upgrades, legacy-source migration, and destination protection.
- npm trusted publishing uses OIDC with provenance. There is no `NPM_TOKEN`
  dependency, main-branch release commit, approval environment, or manual
  client-verification gate. Confirm npm's trusted publisher authorizes
  `soulwaxx/obsidian-second-brain` and `release.yml`, permits direct `npm publish`,
  and has **Allow npm dist-tag** enabled before rollout. Dist-tag permission is
  independent of publish permission and is required when recovery promotes
  `latest`; see [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/).
  An unused
  old token should be revoked by its owner after confirming OIDC publication.

The GitHub marketplace fetches the plugin from npm `latest`; its entry has no
version pin. Only the npm package and its embedded plugin manifest need version
synchronization. Git source manifests can retain their development version:
Claude no longer installs those files through the hosted catalog.

The current proof format certifies ordinary squash-merged PRs, not a multi-PR
merge queue. Keep merge-queue enforcement disabled unless proof verification is
extended for that topology. The `merge_group` trigger remains available for
pre-merge testing, but does not authorize publication by itself.

Evidence is retained for 30 days. Rerun stale PR checks before merging. Normal
release concurrency coalesces pending updates and does not cancel an active
publish; a superseded main update is refused, and the newest verified update
can release all accumulated commits. There is no guarantee of a separate npm
version for every rapidly merged PR.

## Complete GitHub release descriptions

The GitHub Release description is the changelog; there is no repository
`CHANGELOG.md` or changelog/release commit. Conventional Commit notes still
supply the versioned summary. A second package-owned notes generator appends
the full descriptions of associated squash-merged PRs in the release range,
including coalesced updates. This does not depend on the squash commit retaining
the original commit list or PR body.

Write PR descriptions for publication: include every delivered feature and fix,
compatibility/safety changes, verification, and applicable limitations. These
public descriptions are copied verbatim into releases; do not include personal
vault contents, machine-specific private paths, credentials, or private review
artifacts. The release fails closed if the verified source PR is missing or has
an empty description, or metadata cannot be fetched. A nonempty description is
not an automated proof of completeness: the author and reviewer must check its
coverage. Older PRs without descriptions retain only ordinary summary coverage;
no historical details are fabricated.

Descriptions are mutable GitHub metadata, not extra CI evidence or proof of
factual truth. The existing exact-tree/base verifier remains the publishing
authority. Description-only edits do not rerun the code matrix. Publishing needs
read access to PR metadata in addition to its existing release/OIDC permissions.

## Recover a partial publication

semantic-release creates its tag before publishing, so a registry/API failure
can leave a tag with missing npm or GitHub outputs. Normal publishing detects
an incomplete latest tag and requires recovery rather than silently skipping it.

npm can acknowledge a successful publish before its registry metadata becomes
visible. Post-publication verification checks every ten seconds for up to fifteen
minutes of propagation, while still refusing source mismatches and auth/network
errors immediately. The release job allows thirty minutes to accommodate this
wait and the surrounding publication steps. A visibility timeout does not prove
publication is missing: check the exact npm version, its `gitHead`, and the
GitHub Release first. If both outputs are complete, rerun the failed job; there
is nothing to republish.

Run the **Release** workflow on **main**, providing its existing stable tag:

```sh
gh workflow run release.yml --ref main -f tag=vX.Y.Z
```

Replace `vX.Y.Z` with the exact existing tag. Recovery verifies the tagged source
against the same PR evidence, then checks out that source. The workflow preserves
trusted orchestration from its current main commit separately before the tagged
checkout, verifies those locked tools in the preserved directory, and runs the
current recovery implementation with artifact operations rooted in the exact
verified tag. Pre-relocation tags therefore do not need the newer tooling paths
or silently fall back to an older notes-less recovery implementation. It:

1. Refuses an existing npm version whose `gitHead` does not match the tag.
2. Rebuilds and smoke-tests only if npm publication is missing.
3. Creates a GitHub Release only if missing, combining summary notes with the
   same full PR descriptions. It preflights details before missing-output writes
   and binds the range to the tagged source and its preceding stable ancestor
   tag, not later main changes. A first release has no preceding comparison.
4. Preserves existing outputs and never downgrades GitHub's Latest release or
   npm `latest`. Before creating a missing GitHub Release, recovery reads the
   current `/releases/latest` stable release and explicitly sets `make_latest`
   to `false` unless the recovered version is newer (or no stable release
   exists). Prereleases do not become Latest.

A missing npm package is initially published under `recovered`; npm `latest` is
advanced only when appropriate. A repeat run with complete outputs is read-only.
Authentication/network failures are not interpreted as missing outputs.

This automated recovery requires retained PR evidence and the new release
scripts in the tagged source. Legacy tags or evidence older than 30 days need
an independently reviewed recovery plan; do not bypass the verifier, delete a
published tag, or try to overwrite an npm version. Fix failures before a tag is
created by rerunning the automatic release, not tag recovery.

## Release tooling and security policy

All release tooling lives in `.github/release-tools/`: version synchronization,
source verification, prepared-artifact checks, description generation, and
partial-publication recovery. `scripts/` is reserved for vault/package runtime
tools. Release dependencies use a committed lockfile in the tooling directory,
separate from the dependency-free shipped package. Both PR CI and
publishing run:

```sh
npm ci --prefix .github/release-tools --ignore-scripts --no-fund
node .github/release-tools/check.mjs
npm audit --prefix .github/release-tools --audit-level=critical
```

The tool check loads the actual semantic-release plugins, tests version
selection and release notes, and exercises npm preparation against disposable
manifests without publishing. Dependency lifecycle scripts are disabled during
installation; the owned package's version hook still runs during preparation.

CI and release use the same pinned npm CLI. Archive-result parsing accepts both
npm 11's array output and npm 12's package-name-keyed output; release, packed-runtime,
and isolated plugin-install checks all use the same tested parser. The disposable
loopback registry fixture explicitly permits its tarball downloads for npm 12;
`allow-remote=root` still fails because npm pack treats its second fetch as non-root.
The fixture's temporary environment/npmrc uses `allow-remote=all`, with all tarball
URLs supplied by the loopback server. This permission never changes user settings
or the workflow's script allow-list; revisit it when npm pack propagates root status.
Agent CLI installation uses
`--strict-allow-scripts` with a command-scoped allow-list for Claude Code's
native-binary placement, `@google/genai`'s no-op preinstall, and `protobufjs`'s
version-scheme check. New dependency install scripts fail the job until reviewed;
do not replace the allow-list with blanket approval. The pinned Pi package no
longer requires the previous `esbuild` postinstall. A transitive
`node-domexception` deprecation can still appear during CLI installation; keep
it visible until the upstream dependency is replaced.

CI tests Node 24 with both Python 3.14 (the supported minimum) and Python 3.15
on macOS and Linux. Release uses the newest Python version in that tested matrix.
The workflow guards preserve minimum-version coverage and require the release
runtime to match the newest matrix entry, rather than rejecting every Python
upgrade. Renovate preserves the 3.14 minimum entry while updating newer runtimes.
Setup checks for the latest matching patch rather than using an older
runner-cached version. This can add a download to the job. Release also disables
pip's version-update notice, matching PR CI.

**Deferred high-severity gate (owner-approved):** print the full audit report
and retain the critical threshold while the upstream npm bundle remains
vulnerable. High/moderate findings remain visible and unresolved; a passing gate
is not a vulnerability-free claim. Authentication or audit-service failures
still fail the job. Do not run `npm audit fix --force` just to make the report
green.

The shipped package has no npm runtime dependencies or bundled dependencies;
release tooling is excluded from the npm artifact. The locked release toolset
keeps `handlebars` at 4.7.10 to resolve its earlier critical advisories. The
compatible `undici` copies now use 6.29.1 under `@actions/http-client` and 7.30.1
under `@semantic-release/github`, addressing the current TLS-validation,
redirect-disclosure, and denial-of-service advisories without major upgrades.

The reviewed installed release-tool tree reports **one high and 36 moderate
affected package entries**, including dependent packages. The advisory-bearing
copies below are bundled inside npm 11.21.1:

- `undici` 6.29.0: high, including
  [TLS certificate-validation bypass](https://github.com/advisories/GHSA-2m5v-pch6-74hx).
  The patched 6.x release is 6.29.1.
- `ip-address` 10.7.2: moderate,
  [quadratic-time IPv6 zone parsing](https://github.com/advisories/GHSA-rxp7-6hxx-x8q5).
  The patched release is 10.7.4 or newer.
- `glob` 13.0.6: moderate,
  [cancellation bypass permitting post-timeout filesystem traversal](https://github.com/advisories/GHSA-3g3m-2fjr-6pw4).
- `minimatch` 10.2.5: moderate,
  [denial of service via globparts amplification](https://github.com/advisories/GHSA-g3hv-75wg-m249).
  The patched release is 11.0.1 or newer.

These bundled copies cannot be replaced by an ordinary transitive update or
root npm override. A trial of npm 12.2.1 retained vulnerable bundled copies;
adopting the newer CLI is therefore not a security remediation for these findings.
An `npm audit` suggestion that a fix is available is not proof that `npm ci`
installs a repaired bundle. Updating the global CLI alone
also does not update the locked release tool's npm.

The earlier [`braces` advisory GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
was withdrawn upstream on 2026-10-23 and is no longer an active audit finding;
no local advisory suppression is used.

Revisit the threshold when an upstream npm release ships patched bundled
copies. Update the publishing CLI and locked npm together, verify the actual
installed tree with `npm ci`, and require both the release-plugin check and
`npm audit --prefix .github/release-tools --audit-level=high` to pass before
switching all three audit invocations (PR CI, publishing, and recovery) to high.
Do not suppress findings, adopt unreviewed forks, patch installed files ad hoc,
or declare the remaining issues harmless. Renovate maintains direct tool pins
and the lockfile through reviewed PRs. The preset remains below v10 until the
changelog writer is compatible.

## Renovate credentials

The scheduled self-hosted workflow needs `RENOVATE_TOKEN` with access to this
repository and permission to update workflow files and open PRs. A dedicated
identity lets its PRs trigger CI; the ordinary `GITHUB_TOKEN` is not a drop-in
replacement. The workflow owns the weekly schedule; the repository config has
no second time window that could block branch creation when GitHub starts a
scheduled run late. Manual workflow runs can also create update branches.

Keep credentials out of the repository. Major updates and all merges remain
reviewed. Inspect the workflow log after token configuration to confirm
repository discovery and authorization; a stored secret alone is not proof of
a working maintenance loop.
