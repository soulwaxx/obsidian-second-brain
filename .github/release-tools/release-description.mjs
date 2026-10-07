import assert from "node:assert/strict";
import { githubApi } from "./release-source.mjs";

const shaPattern = /^[0-9a-f]{40}$/;
const stableTag = /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

// PR descriptions, unlike individual commit messages, survive squash merging.
// They are release metadata, not additional evidence of tested code or factual truth.
export async function releaseDescriptions({ repository, sourceSha, commits, api = githubApi }) {
  assert.match(repository, /^[\w.-]+\/[\w.-]+$/);
  assert.match(sourceSha, shaPattern);
  const sections = [], seen = new Set();
  let sourceFound = false;
  for (const { hash } of commits) {
    assert.match(hash, shaPattern);
    const prs = await api(`repos/${repository}/commits/${hash}/pulls?per_page=100`, true);
    const pr = prs.find((item) => item.merged_at && item.merge_commit_sha === hash
      && item.base?.ref === "main" && item.base?.repo?.full_name === repository);
    if (!pr) continue;
    assert.ok(Number.isSafeInteger(pr.number) && pr.number > 0);
    const body = typeof pr.body === "string" ? pr.body.trim() : "";
    if (hash === sourceSha) {
      sourceFound = true;
      assert.ok(body, "verified source PR needs a detailed release description; do not publish only its squash headline");
    }
    // Legacy PRs with no body retain the ordinary generated headline summary.
    if (!body || seen.has(pr.number)) continue;
    seen.add(pr.number);
    sections.push(`### [PR #${pr.number}](https://github.com/${repository}/pull/${pr.number})\n\n${body}`);
  }
  assert.ok(sourceFound, "release descriptions must include the associated verified source PR");
  return `## Detailed changes\n\n${sections.join("\n\n")}`;
}

// Recovery must use the tagged source's range, never later main commits/releases.
export function releaseRange(sourceSha, git) {
  assert.match(sourceSha, shaPattern);
  const previousTag = git("tag", "--merged", `${sourceSha}^`, "--sort=-version:refname")
    .split("\n").find((tag) => stableTag.test(tag));
  const range = previousTag ? `${previousTag}..${sourceSha}` : sourceSha;
  const commits = git("log", "--format=%H", range).split("\n").filter(Boolean).map((hash) => ({ hash }));
  return { previousTag, commits };
}

export async function generateNotes(_config, context) {
  return releaseDescriptions({ repository: context.env.GITHUB_REPOSITORY,
    sourceSha: context.nextRelease.gitHead, commits: context.commits });
}
