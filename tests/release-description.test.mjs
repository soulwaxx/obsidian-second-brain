import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { generateNotes, releaseDescriptions, releaseRange } from "../.github/release-tools/release-description.mjs";

const repository = "fixture/wiki", sourceSha = "a".repeat(40), priorSha = "b".repeat(40);
const description = ["### M1: reliability", "### M2: shared CLI and focused skills",
  "### M3: semantic chunks and hybrid retrieval", "### M4: recoverable batches and captures",
  "### M5: optional ledgers and reports", "### Verification", "### Limitations"].join("\n\n");
function pr(hash = sourceSha, number = 7, body = description) {
  return { number, body, merged_at: "2026-01-01", merge_commit_sha: hash,
    base: { ref: "main", repo: { full_name: repository } } };
}
function input(api, commits = [{ hash: sourceSha }]) {
  return { repository, sourceSha, commits, api };
}

test("all milestone details survive a one-line squash commit", async () => {
  const calls = [];
  const notes = await releaseDescriptions(input((route, paginate) => {
    calls.push([route, paginate]);
    return [pr()];
  }, [{ hash: sourceSha, message: "feat: shared wiki workflows" }]));
  assert.ok(notes.includes(description));
  assert.match(notes, /https:\/\/github.com\/fixture\/wiki\/pull\/7/);
  assert.deepEqual(calls, [[`repos/${repository}/commits/${sourceSha}/pulls?per_page=100`, true]]);
});

test("coalesced releases preserve every described PR once", async () => {
  const notes = await releaseDescriptions(input((route) => route.includes(sourceSha)
    ? [pr()] : [pr(priorSha, 6, "An earlier fix and its limitations.")],
  [{ hash: sourceSha }, { hash: priorSha }, { hash: sourceSha }]));
  assert.ok(notes.includes(description));
  assert.match(notes, /An earlier fix and its limitations/);
  assert.equal(notes.split("### [PR #7]").length, 2);
});

test("legacy empty descriptions are not fabricated", async () => {
  const notes = await releaseDescriptions(input((route) => route.includes(sourceSha)
    ? [pr()] : [pr(priorSha, 6, null)], [{ hash: sourceSha }, { hash: priorSha }]));
  assert.ok(notes.includes(description));
  assert.doesNotMatch(notes, /PR #6/);
});

for (const body of [null, "", " \n "]) {
  test(`source description ${JSON.stringify(body)} fails closed`, async () => {
    await assert.rejects(releaseDescriptions(input(() => [pr(sourceSha, 7, body)])), /needs a detailed release description/);
  });
}

for (const change of [
  { merged_at: null }, { merge_commit_sha: priorSha },
  { base: { ref: "feature/topic", repo: { full_name: repository } } },
  { base: { ref: "main", repo: { full_name: "other/repository" } } },
]) {
  test(`unrelated/open/wrong-origin PR is not source evidence: ${JSON.stringify(change)}`, async () => {
    await assert.rejects(releaseDescriptions(input(() => [{ ...pr(), ...change }])), /associated verified source PR/);
  });
}

test("source outside the release range and failed API cannot silently produce partial notes", async () => {
  await assert.rejects(releaseDescriptions(input(() => [pr(priorSha)], [{ hash: priorSha }])), /associated verified source PR/);
  await assert.rejects(releaseDescriptions(input(() => { throw new Error("metadata unavailable"); })), /metadata unavailable/);
});

test("Markdown and shell-looking body content remains inert verbatim data", async () => {
  const body = "## Details\n\n$(touch injected)\n\n```sh\necho example\n```";
  const notes = await releaseDescriptions(input(() => [pr(sourceSha, 7, body)]));
  assert.ok(notes.includes(body));
});

test("invalid source/repository identities refuse before API access", async () => {
  const api = () => { throw new Error("API must not be reached"); };
  await assert.rejects(releaseDescriptions({ ...input(api), sourceSha: "--help" }));
  await assert.rejects(releaseDescriptions({ ...input(api), repository: "../private" }));
  await assert.rejects(releaseDescriptions(input(api, [{ hash: "not-a-sha" }])));
});

test("actual semantic-release adapter collects details through a disposable gh fixture", async (t) => {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "osb-description-cli-"));
  const originalPath = process.env.PATH;
  t.after(() => { process.env.PATH = originalPath; fs.rmSync(work, { recursive: true, force: true }); });
  fs.writeFileSync(path.join(work, "gh"), `#!${process.execPath}\nif (process.argv[2] !== "api" || process.argv[3] !== ${JSON.stringify(`repos/${repository}/commits/${sourceSha}/pulls?per_page=100`)}) process.exit(1);\nprocess.stdout.write(${JSON.stringify(JSON.stringify([[pr()]]))});\n`, { mode: 0o755 });
  process.env.PATH = `${work}${path.delimiter}${originalPath}`;
  const notes = await generateNotes({}, { env: { GITHUB_REPOSITORY: repository },
    nextRelease: { gitHead: sourceSha }, commits: [{ hash: sourceSha }] });
  assert.ok(notes.includes(description));
});

test("recovery range uses a preceding stable ancestor, not current/future/prerelease tags", (t) => {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "osb-description-git-"));
  t.after(() => fs.rmSync(work, { recursive: true, force: true }));
  const env = { ...process.env, HOME: work, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null" };
  const git = (...args) => execFileSync("git", args, { cwd: work, env, encoding: "utf8",
    timeout: 10000, stdio: ["ignore", "pipe", "pipe"] }).trim();
  git("init", "--initial-branch=main");
  git("config", "user.name", "Fixture");
  git("config", "user.email", "fixture@example.invalid");
  const commit = (message) => { git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", message); return git("rev-parse", "HEAD"); };
  const first = commit("feat: first source");
  git("tag", "v1.0.0");
  const middle = commit("fix: interim source");
  git("tag", "v2.0.0-beta.1");
  const target = commit("feat: recovery target");
  git("tag", "v1.1.0");
  const future = commit("feat: later source");
  git("tag", "v2.0.0");
  const range = releaseRange(target, git);
  assert.equal(range.previousTag, "v1.0.0");
  assert.deepEqual(range.commits, [{ hash: target }, { hash: middle }]);
  assert.ok(!range.commits.some(({ hash }) => hash === first || hash === future));
  git("tag", "-d", "v1.0.0");
  assert.equal(releaseRange(target, git).previousTag, undefined);
  assert.deepEqual(releaseRange(target, git).commits, [{ hash: target }, { hash: middle }, { hash: first }]);
});
