import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const [command] = process.argv.slice(2);

function manifests() {
  return ["package.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"]
    .map((file) => ({ file: path.join(root, file), data: JSON.parse(fs.readFileSync(path.join(root, file), "utf8")) }));
}

function checkVersions() {
  const [npm, plugin, marketplace] = manifests();
  assert.match(npm.data.version, /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/, "releases require a stable X.Y.Z version");
  const entry = marketplace.data.plugins.find((item) => item.name === plugin.data.name);
  assert.ok(entry, "Claude marketplace must contain the plugin");
  assert.equal(plugin.data.version, npm.data.version, "Claude plugin and npm versions differ");
  assert.equal(entry.version, npm.data.version, "Claude marketplace and npm versions differ");
  return npm.data.version;
}

function syncVersions() {
  const [npm, plugin, marketplace] = manifests();
  assert.match(npm.data.version, /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/, "releases require a stable X.Y.Z version");
  for (const manifest of [plugin, marketplace]) {
    const text = fs.readFileSync(manifest.file, "utf8");
    assert.equal([...text.matchAll(/"version"\s*:\s*"[^"]*"/g)].length, 1, `expected one version field in ${manifest.file}`);
  }
  for (const manifest of [plugin, marketplace]) {
    const text = fs.readFileSync(manifest.file, "utf8");
    fs.writeFileSync(manifest.file, text.replace(/("version"\s*:\s*")[^"]*(")/, (_match, prefix, suffix) => prefix + npm.data.version + suffix));
  }
}

switch (command) {
  case "check":
    console.log(`Release versions verified: ${checkVersions()}`);
    break;
  case "sync":
    syncVersions();
    console.log(`Release versions synchronized: ${checkVersions()}`);
    break;
  default:
    throw new Error("usage: node scripts/release.mjs check | sync");
}
