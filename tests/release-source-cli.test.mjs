import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const script = fileURLToPath(new URL("../scripts/release-source.mjs", import.meta.url));

test("record and verify CLI checks a real squash tree through paginated APIs and a ZIP artifact", (t) => {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-source-proof-"));
  t.after(() => fs.rmSync(work, { recursive: true, force: true }));
  const env = { ...process.env, HOME: work, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null",
    GITHUB_REPOSITORY: "soulwaxx/obsidian-second-brain", GITHUB_EVENT_NAME: "pull_request",
    GITHUB_RUN_ID: "123", GITHUB_RUN_ATTEMPT: "2", PR_NUMBER: "7" };
  const exec = (command, args) => execFileSync(command, args, { cwd: work, env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  const git = (...args) => exec("git", args);
  git("init", "-b", "main");
  git("config", "user.name", "CI test");
  git("config", "user.email", "ci@example.invalid");
  git("config", "commit.gpgsign", "false");
  fs.writeFileSync(path.join(work, "page.txt"), "base\n");
  git("add", ".");
  git("commit", "-m", "fix: base");
  env.PR_BASE_SHA = git("rev-parse", "HEAD");
  git("checkout", "-b", "feature");
  fs.writeFileSync(path.join(work, "page.txt"), "tested\n");
  git("commit", "-am", "fix: change");
  env.PR_HEAD_SHA = git("rev-parse", "HEAD");
  git("checkout", "-b", "tested", "main");
  git("merge", "--no-ff", "feature", "-m", "Synthetic PR merge");
  env.GITHUB_SHA = git("rev-parse", "HEAD");
  const proofFile = path.join(work, "source.json");
  exec(process.execPath, [script, "record", proofFile]);
  git("checkout", "main");
  git("merge", "--squash", "feature");
  git("commit", "-m", "fix: change (#7)");
  const sha = git("rev-parse", "HEAD");
  git("update-ref", "refs/remotes/origin/main", sha);
  const proof = JSON.parse(fs.readFileSync(proofFile, "utf8"));
  assert.notEqual(proof.testedSha, sha);
  assert.equal(proof.tree, git("rev-parse", "HEAD^{tree}"));
  exec("python3", ["-c", "import sys,zipfile\nwith zipfile.ZipFile(sys.argv[1],'w') as z: z.write(sys.argv[2],'source.json')", path.join(work, "proof.zip"), proofFile]);
  const archive = fs.readFileSync(path.join(work, "proof.zip"));
  const repository = env.GITHUB_REPOSITORY;
  const pr = { number: 7, merged_at: "2026-10-05", merge_commit_sha: sha,
    base: { ref: "main", repo: { full_name: repository } }, head: { sha: env.PR_HEAD_SHA } };
  const run = { id: 123, run_attempt: 2, path: ".github/workflows/test.yml", repository: { full_name: repository },
    event: "pull_request", head_sha: env.PR_HEAD_SHA, status: "completed", conclusion: "success" };
  const bin = path.join(work, "bin");
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, "gh"), `#!${process.execPath}
const route = process.argv[3];
let data;
if (route.includes('/commits/')) data = ${JSON.stringify([[pr]])};
else if (route.includes('/workflows/')) data = ${JSON.stringify([{ workflow_runs: [run] }])};
else if (route.includes('/runs/')) data = ${JSON.stringify({ artifacts: [{ id: 1, name: "verified-source-2", expired: false, size_in_bytes: archive.length }] })};
else if (route.endsWith('/zip')) { process.stdout.write(Buffer.from('${archive.toString("base64")}', 'base64')); process.exit(0); }
else throw new Error('Unexpected API route: ' + route);
process.stdout.write(JSON.stringify(data));
`, { mode: 0o755 });
  env.PATH = `${bin}${path.delimiter}${env.PATH}`;
  assert.match(exec(process.execPath, [script, "verify", sha]), /Verified PR #7/);
  // An evidence ZIP may never extract files into or outside the workspace.
  const outside = path.join(path.dirname(work), `${path.basename(work)}-outside.json`);
  exec("python3", ["-c", "import sys,zipfile\nwith zipfile.ZipFile(sys.argv[1],'w') as z: z.writestr(sys.argv[2],'{}')", path.join(work, "bad.zip"), `../${path.basename(outside)}`]);
  const bad = fs.readFileSync(path.join(work, "bad.zip"));
  const fakeGh = path.join(bin, "gh");
  fs.writeFileSync(fakeGh, fs.readFileSync(fakeGh, "utf8").replace(archive.toString("base64"), bad.toString("base64")));
  assert.throws(() => exec(process.execPath, [script, "verify", sha]));
  assert.ok(!fs.existsSync(outside));
});
