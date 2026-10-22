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
  diagnostic runs. It does not repeat the full matrix after a merge.
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
  `soulwaxx/obsidian-second-brain` and `release.yml` before rollout. An unused
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
4. Preserves existing outputs and never downgrades npm `latest`.

A missing npm package is initially published under `recovered`; `latest` is
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

CI and release use the same pinned npm 11.x CLI. Agent CLI installation uses
`--strict-allow-scripts` with a command-scoped allow-list for Claude Code's
native-binary placement, `@google/genai`'s no-op preinstall, and `protobufjs`'s
version-scheme check. New dependency install scripts fail the job until reviewed;
do not replace the allow-list with blanket approval. The pinned Pi package no
longer requires the previous `esbuild` postinstall. A transitive
`node-domexception` deprecation can still appear during CLI installation; keep
it visible until the upstream dependency is replaced.

Node 24 and Python 3.14 setup check for the latest matching patch rather than
using an older runner-cached version. This can add a download to the job. Release
also disables pip's version-update notice, matching PR CI.

**Temporary approved audit policy:** print the full audit report and block
critical findings. High/moderate findings remain visible and unresolved; a
passing gate is not a vulnerability-free claim. Authentication or audit-service
failures still fail the job. Revisit this policy when compatible upstream fixes
are available; do not run `npm audit fix --force` just to make the report green.

The locked toolset keeps `handlebars` at 4.7.10 to resolve the critical
JavaScript-injection advisories affecting 4.7.9. Updating both the publishing CLI
and the lockfile's transitive npm to 11.21.1 replaces vulnerable bundled copies
of `brace-expansion`, `http-cache-semantics`, `undici`, `ip-address`, and
`postcss-selector-parser`. An installed-tree check and the release-plugin check
verify the refresh; updating the global CLI alone does not update the locked
release tool's npm.

The audit now reports seven high-severity affected package entries, with no
moderate or critical entries in the reviewed tree. These seven entries trace
back to [`braces` GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm),
which has no patched upstream release; 3.0.3 remains the latest. Its recursive
AST walkers can exhaust the stack on deeply nested patterns. The finding
propagates through `micromatch` to semantic-release and its plugins; these are
not seven independent root vulnerabilities. Release branch/rule patterns are
repository-owned constants, reducing exposure to attacker-supplied patterns
without proving safety. npm's suggested semantic-release 15.14.0 downgrade is
not a compatible remediation.

Do not suppress these findings, adopt unreviewed forks, patch installed files
ad hoc, or declare them harmless. Keep the full audit visible and reassess both
installed dependency versions and the release-plugin check when upstream fixes
arrive. Renovate maintains the direct tool pins and lockfile through reviewed
PRs. The preset remains below v10 until the changelog writer is compatible.

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
