import assert from "node:assert/strict";

export function parseNpmArchive(output) {
  const result = JSON.parse(output);
  // npm <=11 returns an array; npm 12 keys archive metadata by package name.
  const archives = Array.isArray(result) ? result : Object.values(result);
  assert.equal(archives.length, 1, "Expected exactly one npm archive");
  const [archive] = archives;
  assert.equal(typeof archive.filename, "string");
  assert.ok(Array.isArray(archive.files), "Expected npm archive file metadata");
  return archive;
}
