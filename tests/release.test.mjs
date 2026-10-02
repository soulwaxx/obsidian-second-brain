import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function fixture(t) {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-release-"));
  t.after(() => fs.rmSync(work, { recursive: true, force: true }));
  for (const file of ["package.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "scripts/release.mjs"]) {
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
  git("add", ".");
  git("commit", "-m", "Fixture");
  const run = (command) => spawnSync(process.execPath, [path.join(work, "scripts/release.mjs"), command],
    { cwd: work, env, encoding: "utf8" });
  return { work, env, git, run };
}

test("version checks need neither tags nor manual release documents", (t) => {
  const { work, git, run } = fixture(t);
  assert.equal(git("tag"), "");
  assert.ok(!fs.existsSync(path.join(work, "docs")));
  assert.equal(run("check").status, 0);
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

test("semantic-release npm preparation synchronizes all published manifests without a version commit", (t) => {
  const { work, env, git, run } = fixture(t);
  const sha = git("rev-parse", "HEAD");
  execFileSync("npm", ["version", "1.2.3", "--no-git-tag-version", "--allow-same-version"],
    { cwd: work, env, stdio: ["ignore", "pipe", "pipe"] });
  for (const file of ["package.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"]) {
    const manifest = JSON.parse(fs.readFileSync(path.join(work, file), "utf8"));
    assert.equal(file.endsWith("marketplace.json") ? manifest.plugins[0].version : manifest.version, "1.2.3");
  }
  assert.equal(git("rev-parse", "HEAD"), sha);
  assert.equal(git("tag"), "");
  assert.equal(run("check").status, 0);
  const [archive] = JSON.parse(execFileSync("npm", ["pack", "--json"], { cwd: work, env, encoding: "utf8" }));
  const packed = path.join(work, "packed");
  fs.mkdirSync(packed);
  execFileSync("tar", ["-xzf", path.join(work, archive.filename), "-C", packed]);
  for (const file of ["package.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"]) {
    const manifest = JSON.parse(fs.readFileSync(path.join(packed, "package", file), "utf8"));
    assert.equal(file.endsWith("marketplace.json") ? manifest.plugins[0].version : manifest.version, "1.2.3");
  }
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
