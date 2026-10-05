import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

export const conventionalTitle = /^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]+\))?!?: .+/;
const shaPattern = /^[0-9a-f]{40}$/;

export function validateEvidence(proof, { repository, sha, tree, parent, pr, run }) {
  assert.equal(proof.repository, repository);
  assert.equal(proof.event, "pull_request");
  assert.equal(proof.runId, run.id);
  assert.equal(proof.runAttempt, run.run_attempt);
  assert.equal(proof.pr, pr.number);
  assert.equal(proof.headSha, pr.head.sha);
  assert.equal(proof.baseSha, parent, "PR checks did not use the final main parent");
  assert.equal(proof.tree, tree, "merged files differ from the tested PR tree");
  assert.match(proof.testedSha, shaPattern);
  assert.match(sha, shaPattern);
}

export async function verifySource({ repository, sha, git, api, evidence }) {
  assert.match(repository, /^[\w.-]+\/[\w.-]+$/);
  assert.match(sha, shaPattern);
  assert.match(git("show", "-s", "--format=%s", sha), conventionalTitle, "merged commit must have a Conventional Commit title");
  git("merge-base", "--is-ancestor", sha, "origin/main");
  const tree = git("rev-parse", `${sha}^{tree}`);
  const parents = git("show", "-s", "--format=%P", sha).split(" ");
  assert.equal(parents.length, 1, "only squash-merged source is eligible for publishing");
  const prs = await api(`repos/${repository}/commits/${sha}/pulls?per_page=100`, true);
  const pr = prs.find((item) => item.merged_at && item.merge_commit_sha === sha
    && item.base.ref === "main" && item.base.repo.full_name === repository);
  assert.ok(pr, "source must be an associated, merged PR; direct pushes cannot publish");
  const pages = await api(`repos/${repository}/actions/workflows/test.yml/runs?event=pull_request&head_sha=${pr.head.sha}&per_page=100`, true);
  const runs = pages.flatMap((page) => page.workflow_runs);
  const run = runs.sort((a, b) => b.id - a.id)[0];
  assert.ok(run, "no PR CI run exists for this head");
  assert.equal(run.repository.full_name, repository);
  assert.equal(run.path, ".github/workflows/test.yml");
  assert.equal(run.event, "pull_request");
  assert.equal(run.head_sha, pr.head.sha);
  assert.equal(run.status, "completed");
  assert.equal(run.conclusion, "success", "latest PR CI must succeed");
  const proof = await evidence(run);
  validateEvidence(proof, { repository, sha, tree, parent: parents[0], pr, run });
  console.log(`Verified PR #${pr.number}, CI run ${run.id}/${run.run_attempt}, tree ${tree}`);
  return proof;
}

export function githubApi(route, paginate = false) {
  const args = ["api", route];
  if (paginate) args.push("--paginate", "--slurp");
  const data = JSON.parse(execFileSync("gh", args, { encoding: "utf8", timeout: 60000, maxBuffer: 16 * 1024 * 1024 }));
  // List endpoints return arrays; workflow-run endpoints return objects.
  return paginate ? data.flat() : data;
}

export function githubEvidence(repository, run) {
  const name = `verified-source-${run.run_attempt}`;
  const { artifacts } = githubApi(`repos/${repository}/actions/runs/${run.id}/artifacts?per_page=100`);
  const artifact = artifacts.find((item) => item.name === name);
  assert.ok(artifact && !artifact.expired, "verified-source evidence missing or expired; rerun PR CI before merging");
  assert.ok(artifact.size_in_bytes <= 65536, "unexpected evidence archive size");
  const archive = execFileSync("gh", ["api", `repos/${repository}/actions/artifacts/${artifact.id}/zip`],
    { timeout: 60000, maxBuffer: 1024 * 1024 });
  // Inspect just the bounded JSON entry, never extract an archive into the workspace.
  const proof = execFileSync("python3", ["-c", "import io,json,sys,zipfile\nz=zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read()))\nassert z.namelist()==['source.json']\nassert z.getinfo('source.json').file_size <= 8192\nprint(json.dumps(json.loads(z.read('source.json'))))"],
    { input: archive, encoding: "utf8", timeout: 10000 });
  return JSON.parse(proof);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [command, argument] = process.argv.slice(2);
  const git = (...args) => execFileSync("git", args, { encoding: "utf8" }).trim();
  const repository = process.env.GITHUB_REPOSITORY;
  if (command === "record") {
    assert.equal(process.env.GITHUB_EVENT_NAME, "pull_request");
    const proof = { repository, event: "pull_request", runId: Number(process.env.GITHUB_RUN_ID),
      runAttempt: Number(process.env.GITHUB_RUN_ATTEMPT), pr: Number(process.env.PR_NUMBER),
      headSha: process.env.PR_HEAD_SHA, baseSha: process.env.PR_BASE_SHA,
      testedSha: git("rev-parse", "HEAD"), tree: git("rev-parse", "HEAD^{tree}") };
    assert.equal(proof.testedSha, process.env.GITHUB_SHA);
    fs.mkdirSync(path.dirname(argument), { recursive: true });
    fs.writeFileSync(argument, JSON.stringify(proof) + "\n");
  } else if (command === "verify") {
    await verifySource({ repository, sha: argument, git, api: githubApi, evidence: (run) => githubEvidence(repository, run) });
  } else throw new Error("usage: node scripts/release-source.mjs record <file> | verify <sha>");
}
