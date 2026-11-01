import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { assertNpmRelease, npmCommand } from "../.github/release-tools/release-recovery.mjs";

const info = { version: "1.2.3", gitHead: "a".repeat(40) };

function fixture(t) {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-npm-view-"));
  const originalPath = process.env.PATH;
  t.after(() => {
    process.env.PATH = originalPath;
    fs.rmSync(work, { recursive: true, force: true });
  });
  process.env.PATH = `${work}${path.delimiter}${originalPath}`;
  return (result) => fs.writeFileSync(path.join(work, "npm"),
    `#!${process.execPath}\nprocess.stdout.write(${JSON.stringify(JSON.stringify(result))});\n`, { mode: 0o755 });
}

for (const [version, wrap] of [["npm 11", (value) => value], ["npm 12", (value) => [value]]]) {
  test(`npm view normalizes ${version} metadata and dist-tags`, (t) => {
    const reply = fixture(t);
    reply(wrap(info));
    const result = npmCommand("view", "fixture@1.2.3", "--json");
    assert.deepEqual(result, info);
    assertNpmRelease(result, "1.2.3", info.gitHead);
    reply(wrap({ latest: "2.0.0", recovered: "1.2.3" }));
    assert.deepEqual(npmCommand("view", "fixture", "dist-tags", "--json"),
      { latest: "2.0.0", recovered: "1.2.3" });
  });
}

for (const result of [[], [info, info], [null], [[info]]]) {
  test(`npm view refuses missing, ambiguous, or malformed results: ${JSON.stringify(result)}`, (t) => {
    fixture(t)(result);
    assert.throws(() => npmCommand("view", "fixture@1.2.3", "--json"));
  });
}

test("npm 12 normalization preserves version and source mismatch refusal", (t) => {
  const reply = fixture(t);
  reply([{ ...info, gitHead: "b".repeat(40) }]);
  assert.throws(() => assertNpmRelease(npmCommand("view", "fixture@1.2.3", "--json"),
    "1.2.3", info.gitHead), /different source/);
  reply([{ ...info, version: "9.9.9" }]);
  assert.throws(() => assertNpmRelease(npmCommand("view", "fixture@1.2.3", "--json"),
    "1.2.3", info.gitHead));
});
