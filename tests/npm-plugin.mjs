import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as http from "node:http";
import * as path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

// Exercise real Claude installs and updates against a disposable loopback npm registry.
// No public registry, credentials, model prompt, or personal agent settings are used.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const work = path.resolve(process.argv[2]);
const exec = promisify(execFile);
const env = { ...process.env, HOME: path.join(work, "home"), XDG_CONFIG_HOME: path.join(work, "xdg"),
  npm_config_cache: path.join(work, "npm-cache"), npm_config_dry_run: "false",
  npm_config_update_notifier: "false", npm_config_userconfig: path.join(work, "npmrc") };
const run = async (command, args) => {
  const { stdout, stderr } = await exec(command, args, { cwd: root, env, timeout: 120000, maxBuffer: 4 * 1024 * 1024 });
  if (stderr) process.stderr.write(stderr);
  return stdout;
};
const json = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
const manifest = json(path.join(root, "package.json"));
const catalog = json(path.join(root, ".claude-plugin/marketplace.json"));
assert.deepEqual(catalog.plugins[0].source, { source: "npm", package: manifest.name });
assert.ok(!Object.hasOwn(catalog.plugins[0], "version"));
const id = `${catalog.plugins[0].name}@${catalog.name}`;
fs.mkdirSync(work, { recursive: true });
const [archive] = JSON.parse(await run("npm", ["pack", "--json", "--ignore-scripts", "--pack-destination", work]));
const current = fs.readFileSync(path.join(work, archive.filename));
const oldRoot = path.join(work, "old");
fs.mkdirSync(oldRoot);
await run("tar", ["-xzf", path.join(work, archive.filename), "-C", oldRoot]);
for (const file of ["package.json", ".claude-plugin/plugin.json"]) {
  const target = path.join(oldRoot, "package", file);
  const data = json(target);
  data.version = "0.0.0";
  fs.writeFileSync(target, JSON.stringify(data));
}
const [oldArchive] = JSON.parse(await run("npm", ["pack", path.join(oldRoot, "package"), "--json", "--ignore-scripts", "--pack-destination", work]));
const old = fs.readFileSync(path.join(work, oldArchive.filename));
let latest = "0.0.0";
let registry;
const server = http.createServer((req, res) => {
  const pathname = decodeURIComponent(new URL(req.url, registry).pathname);
  if (pathname === `/${manifest.name}` || pathname === `/${manifest.name}/latest`) {
    const versions = Object.fromEntries([["0.0.0", old], [manifest.version, current]].map(([version, bytes]) => [version, {
      name: manifest.name, version,
      dist: { tarball: `${registry}archive/${version}.tgz`, shasum: createHash("sha1").update(bytes).digest("hex") },
    }]));
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Cache-Control", "no-store");
    res.end(JSON.stringify(pathname.endsWith("/latest") ? versions[latest] : { name: manifest.name, "dist-tags": { latest }, versions }));
  } else if (pathname === "/archive/0.0.0.tgz" || pathname === `/archive/${manifest.version}.tgz`) {
    res.end(pathname === "/archive/0.0.0.tgz" ? old : current);
  } else {
    res.statusCode = 404;
    res.end(JSON.stringify({ error: "not found" }));
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
registry = `http://127.0.0.1:${server.address().port}/`;
// HTTP is accepted only when it is the user's default npm registry.
env.npm_config_registry = registry;
fs.writeFileSync(env.npm_config_userconfig, `registry=${registry}\n`);
const catalogRoot = path.join(work, "catalog");
fs.mkdirSync(path.join(catalogRoot, ".claude-plugin"), { recursive: true });
const catalogFile = path.join(catalogRoot, ".claude-plugin/marketplace.json");
const npmCatalog = structuredClone(catalog);
npmCatalog.plugins[0].source.registry = registry;
function installed(expected) {
  const entry = json(path.join(env.CLAUDE_CONFIG_DIR, "plugins/installed_plugins.json")).plugins[id][0];
  const directory = entry.installPath;
  assert.equal(json(path.join(directory, ".claude-plugin/plugin.json")).version, expected);
  assert.equal(json(path.join(directory, "package.json")).version, expected);
  for (const resource of ["hooks/obsidian-session.sh", "scripts/wiki_lifecycle.py", "skills/wiki/SKILL.md", "agents/claude/wiki-vault.md"]) {
    assert.ok(fs.existsSync(path.join(directory, resource)), `missing Claude resource: ${resource}`);
  }
  return directory;
}
try {
  env.CLAUDE_CONFIG_DIR = path.join(work, "claude");
  fs.writeFileSync(catalogFile, JSON.stringify(npmCatalog));
  await run("claude", ["plugin", "marketplace", "add", catalogRoot]);
  await run("claude", ["plugin", "install", id, "--scope", "user", "--yes", "--json"]);
  installed("0.0.0");
  latest = manifest.version;
  await run("claude", ["plugin", "update", id, "--scope", "user", "--yes", "--json"]);
  const directory = installed(manifest.version);
  fs.writeFileSync(path.join(work, "claude-installed-path"), directory + "\n");
  console.log(`Claude npm install and update without a catalog version bump PASS (${manifest.version})`);

  // Existing Git/local-source users keep their marketplace and plugin identities.
  env.CLAUDE_CONFIG_DIR = path.join(work, "claude-legacy");
  const legacy = structuredClone(catalog);
  legacy.plugins[0].source = "./";
  legacy.plugins[0].version = "0.0.0";
  fs.cpSync(path.join(oldRoot, "package"), catalogRoot, { recursive: true });
  fs.writeFileSync(catalogFile, JSON.stringify(legacy));
  await run("claude", ["plugin", "marketplace", "add", catalogRoot]);
  await run("claude", ["plugin", "install", id, "--scope", "user", "--yes", "--json"]);
  installed("0.0.0");
  fs.writeFileSync(catalogFile, JSON.stringify(npmCatalog));
  await run("claude", ["plugin", "marketplace", "update", catalog.name]);
  await run("claude", ["plugin", "update", id, "--scope", "user", "--yes", "--json"]);
  installed(manifest.version);
  console.log("Claude legacy-source migration to the npm artifact PASS");
} finally {
  await new Promise((resolve) => server.close(resolve));
}
