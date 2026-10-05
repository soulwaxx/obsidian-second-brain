import assert from "node:assert/strict";
import { test } from "node:test";
import { verifySource, validateEvidence } from "../scripts/release-source.mjs";

const repository = "soulwaxx/obsidian-second-brain";
const sha = "a".repeat(40), tree = "b".repeat(40), parent = "c".repeat(40), head = "d".repeat(40);
function fixture() {
  const pr = { number: 7, merged_at: "2026-10-05", merge_commit_sha: sha,
    base: { ref: "main", repo: { full_name: repository } }, head: { sha: head } };
  const run = { id: 123, run_attempt: 2, repository: { full_name: repository },
    path: ".github/workflows/test.yml", event: "pull_request", head_sha: head, status: "completed", conclusion: "success" };
  const proof = { repository, event: "pull_request", runId: 123, runAttempt: 2, pr: 7,
    headSha: head, baseSha: parent, tree, testedSha: "e".repeat(40) };
  const state = { pr, run, proof, parents: parent, title: "fix: tested change (#7)", ancestor: true };
  const git = (command, ...args) => {
    if (command === "merge-base") { assert.ok(state.ancestor); return ""; }
    if (command === "rev-parse") return tree;
    return args.includes("--format=%s") ? state.title : state.parents;
  };
  const api = async (route) => route.includes("/pulls?") ? state.pr ? [state.pr] : [] : [{ workflow_runs: state.run ? [state.run] : [] }];
  return { state, options: { repository, sha, git, api, evidence: async () => { assert.ok(state.proof); return state.proof; } } };
}

test("successful PR CI certifies the exact merged tree and parent", async () => {
  const { options } = fixture();
  assert.equal((await verifySource(options)).tree, tree);
});

for (const [name, mutate] of [
  ["direct push", (s) => { s.pr = null; }],
  ["unmerged PR", (s) => { s.pr.merged_at = null; }],
  ["wrong merge commit", (s) => { s.pr.merge_commit_sha = head; }],
  ["foreign base repository", (s) => { s.pr.base.repo.full_name = "other/repo"; }],
  ["non-main base", (s) => { s.pr.base.ref = "other"; }],
  ["non-squash merge", (s) => { s.parents += " " + head; }],
  ["invalid commit title", (s) => { s.title = "not conventional"; }],
  ["source outside main", (s) => { s.ancestor = false; }],
  ["missing run", (s) => { s.run = null; }],
  ["failed run", (s) => { s.run.conclusion = "failure"; }],
  ["cancelled run", (s) => { s.run.conclusion = "cancelled"; }],
  ["pending run", (s) => { s.run.status = "in_progress"; }],
  ["foreign workflow", (s) => { s.run.path = ".github/workflows/other.yml"; }],
  ["foreign repository", (s) => { s.run.repository.full_name = "other/repo"; }],
  ["wrong head", (s) => { s.run.head_sha = sha; }],
  ["non-PR event", (s) => { s.run.event = "push"; }],
  ["missing evidence", (s) => { s.proof = null; }],
  ["changed merged files", (s) => { s.proof.tree = head; }],
  ["stale PR base", (s) => { s.proof.baseSha = head; }],
  ["different PR", (s) => { s.proof.pr = 8; }],
  ["different evidence repository", (s) => { s.proof.repository = "other/repo"; }],
  ["different evidence event", (s) => { s.proof.event = "push"; }],
  ["different evidence run", (s) => { s.proof.runId = 999; }],
  ["different evidence attempt", (s) => { s.proof.runAttempt = 1; }],
  ["different evidence head", (s) => { s.proof.headSha = sha; }],
]) {
  test(`release refuses ${name}`, async () => {
    const { state, options } = fixture();
    mutate(state);
    await assert.rejects(verifySource(options));
  });
}

test("a newer failed run cannot be masked by an older success", async () => {
  const { state, options } = fixture();
  options.api = async (route) => route.includes("/pulls?") ? [state.pr]
    : [{ workflow_runs: [state.run, { ...state.run, id: 124, conclusion: "failure" }] }];
  await assert.rejects(verifySource(options));
});

test("evidence validates full SHA identities", () => {
  const { state } = fixture();
  assert.throws(() => validateEvidence({ ...state.proof, testedSha: "injected" }, { repository, sha, tree, parent, pr: state.pr, run: state.run }));
});
