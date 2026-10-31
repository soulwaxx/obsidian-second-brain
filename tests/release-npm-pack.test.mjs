import assert from "node:assert/strict";
import { test } from "node:test";
import { parseNpmArchive } from "./npm-pack.mjs";

const archive = { filename: "fixture-1.0.0.tgz", files: [{ path: "package.json" }] };

for (const [version, result] of [
  ["npm 11", [archive]],
  ["npm 12", { "@fixture/package": archive }],
]) {
  test(`archive parsing accepts ${version} output`, () => {
    assert.deepEqual(parseNpmArchive(JSON.stringify(result)), archive);
  });
}

for (const result of [[], {}, [archive, archive], { first: archive, second: archive },
  [{ files: [] }], [{ filename: archive.filename }]]) {
  test(`archive parsing refuses missing, ambiguous, or incomplete metadata: ${JSON.stringify(result)}`, () => {
    assert.throws(() => parseNpmArchive(JSON.stringify(result)));
  });
}

test("archive parsing refuses malformed JSON", () => {
  assert.throws(() => parseNpmArchive("not JSON"), SyntaxError);
});
