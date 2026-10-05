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

## Recover a partial publication

semantic-release creates its tag before publishing, so a registry/API failure
can leave a tag with missing npm or GitHub outputs. Normal publishing detects
an incomplete latest tag and requires recovery rather than silently skipping it.

Run the **Release** workflow on **main**, providing its existing stable tag:

```sh
gh workflow run release.yml --ref main -f tag=vX.Y.Z
```

Replace `vX.Y.Z` with the exact existing tag. Recovery verifies the tagged source
against the same PR evidence, then checks out that source. It:

1. Refuses an existing npm version whose `gitHead` does not match the tag.
2. Rebuilds and smoke-tests only if npm publication is missing.
3. Creates a GitHub Release only if missing, using GitHub-generated notes.
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

Release dependencies belong in `.github/release-tools/`, with a committed
lockfile separate from the dependency-free shipped package. Both PR CI and
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

**Temporary approved audit policy:** print the full audit report and block
critical findings. High/moderate findings remain visible and unresolved; a
passing gate is not a vulnerability-free claim. Authentication or audit-service
failures still fail the job. Revisit this policy when compatible upstream fixes
are available; do not run `npm audit fix --force` just to make the report green.

The initial locked toolset reports 11 affected package entries (10 high,
1 moderate), stemming from these upstream dependencies:

| Dependency | Findings and relevance |
| --- | --- |
| `braces` through `micromatch` | Nested-pattern stack exhaustion. Release branch/rule patterns are repository-owned constants, reducing exposure to attacker-supplied patterns. |
| npm-bundled `brace-expansion` | Pattern-expansion denial of service. Package fetching/packing still warrants upstream remediation. |
| npm-bundled `http-cache-semantics` | Cache disclosure through `max-stale`. Disposable runners/caches reduce cross-user reuse, but are not a proof of safety. |
| npm-bundled `undici` | WebSocket denial of service and retry-interceptor response splitting. The workflow does not intentionally use WebSockets; applicability still needs upstream review. |
| npm-bundled `ip-address` | Address-classification/subnet and diagnostic-size issues. Do not assume these establish a vulnerability in the shipped plugin. |

npm cannot automatically fix its bundled dependencies, and the current `braces`
report suggests an obsolete semantic-release downgrade rather than a compatible
patch. These findings are not suppressed, patched locally, or declared harmless.
Renovate maintains the direct tool pins and lockfile through reviewed PRs. The
preset remains below v10 until the changelog writer is compatible.

## Renovate credentials

The scheduled self-hosted workflow needs `RENOVATE_TOKEN` with access to this
repository and permission to update workflow files and open PRs. A dedicated
identity lets its PRs trigger CI; the ordinary `GITHUB_TOKEN` is not a drop-in
replacement. Keep credentials out of the repository. Major updates and all
merges remain reviewed. Inspect the workflow log after token configuration to
confirm repository discovery and authorization; a stored secret alone is not
proof of a working maintenance loop.
