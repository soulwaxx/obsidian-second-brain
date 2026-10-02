import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const clients = [["macOS", "Claude Code"], ["macOS", "Pi"], ["Linux", "Claude Code"], ["Linux", "Pi"]];

function fixture(t) {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-release-"));
  t.after(() => fs.rmSync(work, { recursive: true, force: true }));
  for (const file of ["package.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "scripts/release.mjs", "docs/release-verification.md"]) {
    fs.mkdirSync(path.dirname(path.join(work, file)), { recursive: true });
    fs.copyFileSync(path.join(root, file), path.join(work, file));
  }
  const env = {
    ...process.env, HOME: work, npm_config_cache: path.join(work, "cache"),
    npm_config_dry_run: "false", npm_config_ignore_scripts: "false",
  };
  const git = (...args) => execFileSync("git", args, { cwd: work, env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  git("init");
  git("config", "user.name", "Release test");
  git("config", "user.email", "release-test@example.invalid");
  git("config", "commit.gpgsign", "false");
  git("config", "tag.gpgsign", "false");
  git("add", ".");
  git("commit", "-m", "Fixture");
  const version = JSON.parse(fs.readFileSync(path.join(work, "package.json"), "utf8")).version;
  const tag = `v${version}`;
  git("tag", "-a", tag, "-m", tag);
  const run = (command, releaseTag, extraEnv = {}) => spawnSync(process.execPath,
    [path.join(work, "scripts/release.mjs"), command, ...(releaseTag === undefined ? [] : [releaseTag])],
    { cwd: work, env: { ...env, ...extraEnv }, encoding: "utf8" });
  return { work, env, git, run, version, tag };
}

function recordPasses(work) {
  fs.writeFileSync(path.join(work, "docs/release-verification.md"),
    clients.map(([os, client]) => `| ${os} | ${client} | PASS | Disposable fixture evidence: date, versions, commit, consent, observations |\n`).join(""));
}

function readyFixture(t) {
  const data = fixture(t);
  recordPasses(data.work);
  data.git("add", "docs/release-verification.md");
  data.git("commit", "-m", "Record fixture live checks");
  data.git("tag", "-f", "-a", data.tag, "-m", data.tag);
  return data;
}

function mockNpm(work) {
  const bin = path.join(work, "bin");
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, "npm"), `#!/usr/bin/env node
import fs from "node:fs";
fs.appendFileSync(process.env.MOCK_LOG, JSON.stringify(process.argv.slice(2)) + "\\n");
if (process.argv[2] === "view") {
  console.log(process.env.MOCK_VIEW);
  process.exit(Number(process.env.MOCK_STATUS));
}
if (process.argv[2] !== "publish") process.exit(99);
process.exit(Number(process.env.MOCK_PUBLISH_STATUS || 0));
`, { mode: 0o755 });
  return { PATH: bin + path.delimiter + process.env.PATH, MOCK_LOG: path.join(work, "npm.log") };
}

test("versions and matching tag pass; wrong or moved tags fail", (t) => {
  const { work, git, run, tag } = fixture(t);
  assert.equal(run("check", tag).status, 0);
  assert.notEqual(run("check", "v99.0.0").status, 0);
  fs.writeFileSync(path.join(work, "change"), "next commit");
  git("add", "change");
  git("commit", "-m", "Next");
  assert.notEqual(run("check", tag).status, 0);
});

test("plugin or marketplace version drift fails", (t) => {
  const { work, run } = fixture(t);
  for (const file of [".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"]) {
    const target = path.join(work, file);
    const original = fs.readFileSync(target, "utf8");
    fs.writeFileSync(target, original.replace(/"version": "[^"]+"/, '"version": "99.0.0"'));
    assert.notEqual(run("check").status, 0);
    fs.writeFileSync(target, original);
  }
});

test("npm version synchronizes all manifests into its commit and annotated tag", (t) => {
  const { work, env, git } = fixture(t);
  execFileSync("npm", ["version", "patch", "--sign-git-tag=false", "--commit-hooks=false", "-m", "Release v%s"],
    { cwd: work, env, stdio: ["ignore", "pipe", "pipe"] });
  const manifest = JSON.parse(fs.readFileSync(path.join(work, "package.json"), "utf8"));
  for (const file of ["package.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"]) {
    const committed = JSON.parse(git("show", `HEAD:${file}`));
    assert.equal(file.endsWith("marketplace.json") ? committed.plugins[0].version : committed.version, manifest.version);
  }
  assert.equal(git("rev-parse", `v${manifest.version}^{commit}`), git("rev-parse", "HEAD"));
  assert.equal(git("cat-file", "-t", `v${manifest.version}`), "tag");
  assert.equal(git("status", "--porcelain", "--untracked-files=no"), "");
});

test("stable releases reject prerelease and malformed versions", (t) => {
  const { work, run } = fixture(t);
  const target = path.join(work, "package.json");
  const data = JSON.parse(fs.readFileSync(target, "utf8"));
  for (const version of ["1.0.0-beta.1", "01.0.0", "not-a-version"]) {
    data.version = version;
    fs.writeFileSync(target, JSON.stringify(data));
    assert.notEqual(run("sync").status, 0);
  }
});

test("live-client gate requires all four unique PASS rows with evidence", (t) => {
  const { work, run } = fixture(t);
  const file = path.join(work, "docs/release-verification.md");
  for (const [os, client] of clients) {
    for (const status of ["UNTESTED", "FAIL"]) {
      recordPasses(work);
      fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace(`| ${os} | ${client} | PASS |`, `| ${os} | ${client} | ${status} |`));
      assert.notEqual(run("verify-live").status, 0);
    }
  }
  recordPasses(work);
  assert.equal(run("verify-live").status, 0);
  fs.appendFileSync(file, "| macOS | Pi | PASS | duplicate |\n");
  assert.notEqual(run("verify-live").status, 0);
  recordPasses(work);
  fs.writeFileSync(file, fs.readFileSync(file, "utf8").split("\n").slice(1).join("\n"));
  assert.notEqual(run("verify-live").status, 0);
  recordPasses(work);
  fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace(/\| PASS \|[^\n]+/, "| PASS | |"));
  assert.notEqual(run("verify-live").status, 0);
});

test("publication refuses incomplete live records before contacting npm", (t) => {
  const { work, run, tag } = fixture(t);
  fs.writeFileSync(path.join(work, "docs/release-verification.md"), clients.map(([os, client]) => `| ${os} | ${client} | UNTESTED | |\n`).join(""));
  const env = mockNpm(work);
  assert.notEqual(run("publish", tag, env).status, 0);
  assert.ok(!fs.existsSync(env.MOCK_LOG));
});

test("publication only starts on E404; an existing identical commit resumes safely", (t) => {
  const { work, run, git, version, tag } = readyFixture(t);
  const env = mockNpm(work);
  const invoke = (status, view) => {
    fs.rmSync(env.MOCK_LOG, { force: true });
    const result = run("publish", tag, { ...env, MOCK_STATUS: String(status), MOCK_VIEW: JSON.stringify(view) });
    const calls = fs.readFileSync(env.MOCK_LOG, "utf8").trim().split("\n").map((line) => JSON.parse(line));
    return { result, calls };
  };
  const absent = invoke(1, { error: { code: "E404" } });
  assert.equal(absent.result.status, 0, absent.result.stderr);
  assert.deepEqual(absent.calls[1], ["publish", "--access", "public", "--provenance", "--registry=https://registry.npmjs.org/"]);
  const existing = invoke(0, { version, gitHead: git("rev-parse", "HEAD") });
  assert.equal(existing.result.status, 0);
  assert.equal(existing.calls.length, 1);
  for (const data of [{ version, gitHead: "different-commit" }, { version }, { version: "99.0.0", gitHead: git("rev-parse", "HEAD") }]) {
    const collision = invoke(0, data);
    assert.notEqual(collision.result.status, 0);
    assert.equal(collision.calls.length, 1);
  }
  for (const code of ["E401", "E403", "ECONNRESET"]) {
    const failure = invoke(1, { error: { code } });
    assert.notEqual(failure.result.status, 0);
    assert.equal(failure.calls.length, 1);
  }
});

test("dirty tracked files prevent publication before contacting npm", (t) => {
  const { work, run, tag } = readyFixture(t);
  fs.appendFileSync(path.join(work, "docs/release-verification.md"), "\nUncommitted change\n");
  const env = mockNpm(work);
  assert.notEqual(run("publish", tag, env).status, 0);
  assert.ok(!fs.existsSync(env.MOCK_LOG));
});

test("npm publication failure is propagated", (t) => {
  const { work, run, tag } = readyFixture(t);
  const env = mockNpm(work);
  const result = run("publish", tag, {
    ...env, MOCK_STATUS: "1", MOCK_VIEW: JSON.stringify({ error: { code: "E404" } }), MOCK_PUBLISH_STATUS: "1",
  });
  assert.notEqual(result.status, 0);
});
