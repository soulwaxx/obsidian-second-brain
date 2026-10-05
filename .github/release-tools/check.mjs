import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { prepare as prepareNpm } from "./node_modules/@semantic-release/npm/index.js";
import getConfig from "./node_modules/semantic-release/lib/get-config.js";

const context = { cwd: process.cwd(), env: process.env, logger: {
  log() {}, success() {}, warn() {}, error() {}, scope() { return this; },
} };
const { options, plugins } = await getConfig(context);
for (const [message, expected] of [
  ["feat: add feature", "minor"], ["fix: fix bug", "patch"],
  ["ci: update CI", "patch"], ["docs: update skill", "patch"],
  ["feat!: incompatible change", "major"],
]) {
  assert.equal(await plugins.analyzeCommits({ ...context, options,
    commits: [{ message, hash: "a".repeat(40) }] }), expected);
}
const notes = await plugins.generateNotes({ ...context, options,
  commits: [{ message: "fix: verify release tooling", hash: "a".repeat(40) }],
  branch: { name: "main" }, lastRelease: { gitTag: "v1.0.0" },
  nextRelease: { version: "1.0.1", gitTag: "v1.0.1" },
});
assert.match(notes, /verify release tooling/);
const work = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-release-tools-"));
try {
  for (const file of ["package.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "scripts/release.mjs"]) {
    fs.mkdirSync(path.dirname(path.join(work, file)), { recursive: true });
    fs.copyFileSync(file, path.join(work, file));
  }
  await prepareNpm({ npmPublish: false }, { ...context, cwd: work,
    env: { ...process.env, HOME: work, npm_config_cache: path.join(work, "cache"),
      npm_config_ignore_scripts: "false", npm_config_dry_run: "false" },
    stdout: process.stdout, stderr: process.stderr, nextRelease: { version: "9.8.7" },
  });
  for (const file of ["package.json", ".claude-plugin/plugin.json"]) {
    assert.equal(JSON.parse(fs.readFileSync(path.join(work, file), "utf8")).version, "9.8.7");
  }
} finally {
  fs.rmSync(work, { recursive: true, force: true });
}
console.log("Locked release plugins, version analysis, release notes, and npm preparation PASS");
