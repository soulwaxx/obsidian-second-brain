import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const registry = "https://registry.npmjs.org/";
const [command, tag] = process.argv.slice(2);

function manifests() {
  return ["package.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"]
    .map((file) => ({ file: path.join(root, file), data: JSON.parse(fs.readFileSync(path.join(root, file), "utf8")) }));
}

function releaseVersions() {
  const [npm, plugin, marketplace] = manifests();
  assert.match(npm.data.version, /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/, "releases require a stable X.Y.Z version");
  const entry = marketplace.data.plugins.find((item) => item.name === plugin.data.name);
  assert.ok(entry, "Claude marketplace must contain the plugin");
  return { npm, plugin, marketplace, entry };
}

function checkVersions(releaseTag) {
  const { npm, plugin, entry } = releaseVersions();
  assert.equal(plugin.data.version, npm.data.version, "Claude plugin and npm versions differ");
  assert.equal(entry.version, npm.data.version, "Claude marketplace and npm versions differ");
  if (releaseTag !== undefined) {
    assert.equal(releaseTag, `v${npm.data.version}`, "release tag must match the manifest version");
    const git = (args) => execFileSync("git", args, { cwd: root, encoding: "utf8" }).trim();
    assert.equal(git(["rev-parse", `refs/tags/${releaseTag}^{commit}`]), git(["rev-parse", "HEAD"]), "release tag must point to the checked-out commit");
  }
  return npm.data;
}

function syncVersions() {
  const { npm, plugin, marketplace } = releaseVersions();
  for (const manifest of [plugin, marketplace]) {
    const text = fs.readFileSync(manifest.file, "utf8");
    const versions = [...text.matchAll(/"version"\s*:\s*"[^"]*"/g)];
    assert.equal(versions.length, 1, `expected one version field in ${manifest.file}`);
  }
  for (const manifest of [plugin, marketplace]) {
    const text = fs.readFileSync(manifest.file, "utf8");
    fs.writeFileSync(manifest.file, text.replace(/("version"\s*:\s*")[^"]*(")/, (_match, prefix, suffix) => prefix + npm.data.version + suffix));
  }
}

function verifyLiveChecks() {
  const text = fs.readFileSync(path.join(root, "docs/release-verification.md"), "utf8");
  const rows = text.split("\n").filter((line) => line.startsWith("|")).map((line) => line.split("|").slice(1, -1).map((cell) => cell.trim()));
  for (const [os, client] of [["macOS", "Claude Code"], ["macOS", "Pi"], ["Linux", "Claude Code"], ["Linux", "Pi"]]) {
    const matches = rows.filter((row) => row[0] === os && row[1] === client);
    assert.equal(matches.length, 1, `missing or duplicate live-client record: ${os} / ${client}`);
    assert.equal(matches[0][2], "PASS", `live-client release gate pending: ${os} / ${client}`);
    assert.ok(matches[0][3], `live-client evidence missing: ${os} / ${client}`);
  }
}

function publish() {
  assert.ok(tag, "publish requires an existing version tag");
  const manifest = checkVersions(tag);
  verifyLiveChecks();
  assert.equal(execFileSync("git", ["status", "--porcelain", "--untracked-files=no"], { cwd: root, encoding: "utf8" }).trim(), "", "publication requires a clean tracked checkout");
  const sha = execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
  const result = spawnSync("npm", ["view", `${manifest.name}@${manifest.version}`, "version", "gitHead", "--json", `--registry=${registry}`], { cwd: root, encoding: "utf8" });
  if (result.error) throw result.error;
  if (result.status === 0) {
    const published = JSON.parse(result.stdout);
    assert.equal(published.version, manifest.version, "unexpected registry version");
    assert.equal(published.gitHead, sha, "npm version already exists from a different commit; refusing to release it");
    console.log(`npm version ${manifest.version} already published from ${sha}; resuming GitHub release`);
    return;
  }
  // Only an absent package/version permits publication; network/auth failures do not.
  const failure = JSON.parse(result.stdout);
  assert.equal(failure.error?.code, "E404", `cannot check npm version: ${result.stderr}`);
  execFileSync("npm", ["publish", "--access", "public", "--provenance", `--registry=${registry}`], { cwd: root, stdio: "inherit" });
}

switch (command) {
  case "check":
    console.log(`Release versions verified: ${checkVersions(tag).version}`);
    break;
  case "sync":
    syncVersions();
    console.log(`Release versions synchronized: ${checkVersions().version}`);
    break;
  case "verify-live":
    verifyLiveChecks();
    console.log("All four live-client release records passed");
    break;
  case "publish":
    publish();
    break;
  default:
    throw new Error("usage: node scripts/release.mjs check [vX.Y.Z] | sync | verify-live | publish vX.Y.Z");
}
