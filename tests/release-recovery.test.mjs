import assert from "node:assert/strict";
import { test } from "node:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { assertNpmRelease, githubCommand, npmCommand, recoverRelease, releaseVersion } from "../scripts/release-recovery.mjs";

const input = { tag: "v2.0.1", sha: "a".repeat(40), name: "@soulwaxx/obsidian-second-brain", repository: "soulwaxx/obsidian-second-brain" };
function fixture(npmExists, githubExists, latest = "2.0.0") {
  const state = { info: npmExists ? { version: "2.0.1", gitHead: input.sha } : undefined,
    release: githubExists ? { tag_name: input.tag } : undefined, latest, calls: [] };
  const deps = {
    npm(command, ...args) {
      state.calls.push(["npm", command, ...args]);
      if (command === "view") return args.includes("dist-tags") ? { latest: state.latest } : state.info;
      if (command === "publish") state.info = { version: "2.0.1", gitHead: input.sha };
      if (command === "dist-tag") state.latest = "2.0.1";
    },
    github(method, route, fields) {
      state.calls.push(["github", method, route, fields]);
      if (method === "GET") return state.release;
      if (route.endsWith("generate-notes")) return { body: "Recovered release notes" };
      state.release = fields;
      return fields;
    },
    prepareArtifact() { state.calls.push(["prepare"]); },
  };
  return { state, deps };
}

for (const npmExists of [false, true]) for (const githubExists of [false, true]) {
  test(`recover npm=${npmExists}, GitHub=${githubExists} without overwriting outputs`, async () => {
    const { state, deps } = fixture(npmExists, githubExists);
    await recoverRelease(input, deps);
    assert.equal(state.calls.filter((call) => call[0] === "prepare").length, npmExists ? 0 : 1);
    assert.equal(state.calls.filter((call) => call[1] === "publish").length, npmExists ? 0 : 1);
    assert.equal(state.calls.filter((call) => call[1] === "POST" && call[2].endsWith("/releases")).length, githubExists ? 0 : 1);
    assert.equal(state.latest, "2.0.1");
    state.calls = [];
    await recoverRelease(input, deps);
    assert.ok(state.calls.every((call) => call[1] === "view" || call[1] === "GET"), "repeat recovery must be read-only");
  });
}

test("recovering an older release never downgrades latest", async () => {
  const { state, deps } = fixture(false, false, "3.0.0");
  await recoverRelease(input, deps);
  assert.equal(state.latest, "3.0.0");
  assert.deepEqual(state.calls.find((call) => call[1] === "publish").slice(-2), ["--tag", "recovered"]);
  assert.ok(!state.calls.some((call) => call[1] === "dist-tag"));
});

test("existing npm source mismatch refuses all writes", async () => {
  const { state, deps } = fixture(true, false);
  state.info.gitHead = "b".repeat(40);
  await assert.rejects(recoverRelease(input, deps), /different source/);
  assert.ok(!state.calls.some((call) => call[1] === "publish" || call[1] === "POST"));
});

test("artifact-check failure prevents publication", async () => {
  const { state, deps } = fixture(false, false);
  deps.prepareArtifact = () => { throw new Error("smoke failed"); };
  await assert.rejects(recoverRelease(input, deps), /smoke failed/);
  assert.ok(!state.calls.some((call) => call[1] === "publish" || call[1] === "POST"));
});

test("network/auth failure is not treated as a missing package", async () => {
  const { state, deps } = fixture(false, false);
  deps.npm = () => { throw new Error("registry unavailable"); };
  await assert.rejects(recoverRelease(input, deps), /registry unavailable/);
  assert.deepEqual(state.calls, []);
});

test("CLI adapters treat only explicit 404s as missing outputs", (t) => {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-recovery-cli-"));
  const originalPath = process.env.PATH;
  const originalFailure = process.env.RECOVERY_TEST_FAILURE;
  t.after(() => {
    process.env.PATH = originalPath;
    if (originalFailure === undefined) delete process.env.RECOVERY_TEST_FAILURE;
    else process.env.RECOVERY_TEST_FAILURE = originalFailure;
    fs.rmSync(work, { recursive: true, force: true });
  });
  for (const command of ["npm", "gh"]) {
    fs.writeFileSync(path.join(work, command), `#!${process.execPath}\nprocess.stderr.write(process.env.RECOVERY_TEST_FAILURE); process.exit(1);\n`, { mode: 0o755 });
  }
  process.env.PATH = `${work}${path.delimiter}${originalPath}`;
  process.env.RECOVERY_TEST_FAILURE = "npm error code E404";
  assert.equal(npmCommand("view", "fixture@1.0.0", "--json"), undefined);
  assert.throws(() => npmCommand("publish", "--json"));
  for (const failure of ["npm error code E401", "npm error code ECONNRESET"]) {
    process.env.RECOVERY_TEST_FAILURE = failure;
    assert.throws(() => npmCommand("view", "fixture@1.0.0", "--json"));
  }
  process.env.RECOVERY_TEST_FAILURE = "gh: Not Found (HTTP 404)";
  assert.equal(githubCommand("GET", "fixture"), undefined);
  assert.throws(() => githubCommand("POST", "fixture", {}));
  process.env.RECOVERY_TEST_FAILURE = "gh: Forbidden (HTTP 403)";
  assert.throws(() => githubCommand("GET", "fixture"));
});

test("exact stable tags and source identities are mandatory", () => {
  for (const tag of ["main", "--help", "v01.0.0", "v2.0.1-beta.1", "v2.0.1;touch injected"]) {
    assert.throws(() => releaseVersion(tag));
  }
  assert.equal(releaseVersion("v2.0.1"), "2.0.1");
  assert.throws(() => assertNpmRelease({ version: "2.0.2", gitHead: input.sha }, "2.0.1", input.sha));
});
