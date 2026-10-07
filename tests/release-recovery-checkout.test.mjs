import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const steps = JSON.parse(execFileSync("python3", ["-c",
  "import json,sys,yaml; print(json.dumps(yaml.load(open(sys.argv[1]),Loader=yaml.BaseLoader)['jobs']['release']['steps']))",
  path.join(root, ".github/workflows/release.yml")], { encoding: "utf8", timeout: 10000 }));

test("trusted current recovery survives a pre-relocation tagged artifact checkout", () => {
  const work = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-recovery-checkout-")));
  try {
    const artifact = path.join(work, "artifact"), bin = path.join(work, "bin");
    fs.mkdirSync(artifact);
    fs.mkdirSync(bin);
    const log = path.join(work, "calls.jsonl"), published = path.join(work, "published.json"), release = path.join(work, "release.json");
    const env = { ...process.env, HOME: work, XDG_CONFIG_HOME: work, GIT_CONFIG_NOSYSTEM: "1",
      GIT_CONFIG_GLOBAL: path.join(work, "absent-gitconfig"), GIT_CONFIG_COUNT: "0",
      PATH: `${bin}${path.delimiter}${process.env.PATH}`, RUNNER_TEMP: work,
      GITHUB_REPOSITORY: "fixture/wiki", RECOVERY_TAG: "v2.0.1",
      FIXTURE_LOG: log, FIXTURE_PUBLISHED: published, FIXTURE_RELEASE: release, FIXTURE_ARTIFACT: artifact };
    delete env.GIT_DIR;
    delete env.GIT_WORK_TREE;
    delete env.GIT_INDEX_FILE;
    const git = (...args) => execFileSync("git", args, { cwd: artifact, env, encoding: "utf8", timeout: 10000 }).trim();
    git("init", "-q");
    git("config", "user.email", "fixture@example.test");
    git("config", "user.name", "Fixture");
    git("config", "commit.gpgsign", "false");
    git("config", "tag.gpgsign", "false");
    git("config", "core.hooksPath", path.join(work, "absent-hooks"));
    fs.mkdirSync(path.join(artifact, "scripts"));
    fs.mkdirSync(path.join(artifact, "tests"));
    fs.writeFileSync(path.join(artifact, "package.json"), JSON.stringify({ name: "fixture-wiki", version: "0.1.0" }));
    fs.writeFileSync(path.join(artifact, "scripts/release-recovery.mjs"), "throw new Error('old notes-less recovery must not run');\n");
    fs.writeFileSync(path.join(artifact, "tests/install-smoke.sh"), 'test "$PWD" = "$FIXTURE_ARTIFACT"\n');
    git("add", ".");
    git("commit", "-qm", "fix: initial fixture");
    git("tag", "v2.0.0");
    fs.writeFileSync(path.join(artifact, "feature.txt"), "historical tagged feature\n");
    git("add", ".");
    git("commit", "-qm", "feat: historical feature");
    env.FIXTURE_SHA = git("rev-parse", "HEAD");
    git("tag", env.RECOVERY_TAG);
    for (const name of ["release-recovery.mjs", "release-description.mjs", "release-source.mjs", "release-artifact.mjs"]) {
      const target = path.join(artifact, ".github/release-tools", name);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.copyFileSync(path.join(root, ".github/release-tools", name), target);
    }
    fs.copyFileSync(path.join(root, ".releaserc.json"), path.join(artifact, ".releaserc.json"));
    fs.mkdirSync(path.join(artifact, ".claude-plugin"));
    fs.writeFileSync(path.join(artifact, ".claude-plugin/plugin.json"), "{}\n");
    git("add", ".");
    git("commit", "-qm", "fix: relocate trusted tools");
    env.ORCHESTRATION_SHA = git("rev-parse", "HEAD");
    git("tag", "v9.0.0");
    const preserve = steps.find((step) => step.name === "Preserve trusted recovery orchestration");
    assert.ok(preserve, "workflow must preserve current tooling before historical checkout");
    execFileSync("bash", ["-eo", "pipefail", "-c", preserve.run], { cwd: artifact, env, timeout: 10000 });
    git("checkout", "-q", env.RECOVERY_TAG);
    assert.ok(!fs.existsSync(path.join(artifact, ".github/release-tools/release-recovery.mjs")));
    for (const command of ["npm", "gh", "claude"]) {
      fs.writeFileSync(path.join(bin, command), `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2), command = ${JSON.stringify(command)};
if (process.cwd() !== process.env.FIXTURE_ARTIFACT) throw new Error('wrong artifact cwd');
fs.appendFileSync(process.env.FIXTURE_LOG, JSON.stringify([command, ...args]) + '\\n');
const read = (file) => fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : undefined;
const output = (data) => process.stdout.write(JSON.stringify(data));
if (command === 'npm') {
  if (args[0] === 'view' && args.includes('dist-tags')) output({latest:'9.0.0'});
  else if (args[0] === 'view') {
    const data = read(process.env.FIXTURE_PUBLISHED);
    if (data) output(data); else {process.stderr.write('npm error code E404'); process.exit(1);}
  } else if (args[0] === 'version') {
    const data = JSON.parse(fs.readFileSync('package.json')); data.version = args[1]; fs.writeFileSync('package.json', JSON.stringify(data));
  } else if (args[0] === 'publish') fs.writeFileSync(process.env.FIXTURE_PUBLISHED, JSON.stringify({version:'2.0.1',gitHead:process.env.FIXTURE_SHA}));
  else if (args.join(' ') !== 'run release:check') throw new Error('unexpected npm call');
} else if (command === 'gh') {
  const route = args[1] === '--method' ? args[3] : args[1];
  if (route.includes('/commits/')) output([[{number:7,merged_at:'2026-01-01',merge_commit_sha:process.env.FIXTURE_SHA,base:{ref:'main',repo:{full_name:'fixture/wiki'}},body:'Historical feature and limitations survive recovery.'}]]);
  else if (args[2] === 'GET') {
    const data = read(process.env.FIXTURE_RELEASE);
    if (data) output(data); else {process.stderr.write('gh: Not Found (HTTP 404)'); process.exit(1);}
  } else {
    const fields = JSON.parse(fs.readFileSync(0,'utf8'));
    if (route.endsWith('/generate-notes')) {
      if (fields.previous_tag_name !== 'v2.0.0' || fields.target_commitish !== process.env.FIXTURE_SHA) throw new Error('wrong historical range');
      output({body:'Historical summary'});
    } else {fs.writeFileSync(process.env.FIXTURE_RELEASE,JSON.stringify(fields)); output(fields);}
  }
} else if (args.join(' ') !== 'plugin validate .') throw new Error('unexpected Claude call');
`, { mode: 0o755 });
    }
    const recovery = steps.find((step) => step.name === "Recover missing publication outputs");
    const run = () => spawnSync("bash", ["-eo", "pipefail", "-c", recovery.run], { cwd: artifact, env, encoding: "utf8", timeout: 20000 });
    let result = run();
    assert.equal(result.status, 0, result.stderr);
    const body = JSON.parse(fs.readFileSync(release)).body;
    assert.match(body, /Historical summary/);
    assert.match(body, /Historical feature and limitations survive recovery/);
    assert.equal(git("rev-parse", "HEAD"), env.FIXTURE_SHA);
    const first = fs.readFileSync(log, "utf8").trim().split("\n").map(JSON.parse);
    assert.equal(first.filter((call) => call[0] === "npm" && call[1] === "publish").length, 1);
    assert.ok(first.some((call) => call[0] === "claude"));
    fs.writeFileSync(log, "");
    result = run();
    assert.equal(result.status, 0, result.stderr);
    const repeat = fs.readFileSync(log, "utf8").trim().split("\n").map(JSON.parse);
    assert.ok(repeat.every((call) => call[1] === "view" || call[3] === "GET"), "existing outputs require read-only recovery");
    assert.equal(JSON.parse(fs.readFileSync(release)).body, body);
    git("restore", "package.json");
    git("checkout", "-q", env.ORCHESTRATION_SHA);
    result = run();
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /exact verified tagged source/);
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
});
