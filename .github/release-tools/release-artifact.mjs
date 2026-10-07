import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import { assertNpmRelease, githubCommand, npmCommand, waitForNpmRelease } from "./release-recovery.mjs";

export function verifyConditions() {
  const tags = execFileSync("git", ["tag", "--merged", "HEAD", "--sort=-v:refname"], { encoding: "utf8" });
  const tag = tags.split("\n").find((value) => /^v\d+\.\d+\.\d+$/.test(value));
  if (!tag) return;
  const { name } = JSON.parse(fs.readFileSync("package.json", "utf8"));
  const sha = execFileSync("git", ["rev-parse", `${tag}^{commit}`], { encoding: "utf8" }).trim();
  const info = npmCommand("view", `${name}@${tag.slice(1)}`, "--json");
  const release = githubCommand("GET", `repos/${process.env.GITHUB_REPOSITORY}/releases/tags/${tag}`);
  assert.ok(info && release, `Incomplete ${tag}: run the Release workflow's tag recovery before publishing again`);
  assertNpmRelease(info, tag.slice(1), sha);
}

// semantic-release runs this after @semantic-release/npm has prepared the real version,
// and before it creates a tag or publishes. Source tests have already passed on both OSes.
export function prepare() {
  execFileSync("npm", ["run", "release:check"], { stdio: "inherit" });
  execFileSync("claude", ["plugin", "validate", "."], { stdio: "inherit" });
  execFileSync("bash", ["tests/install-smoke.sh"], { stdio: "inherit" });
}

export async function success(_config, { nextRelease }) {
  const { name } = JSON.parse(fs.readFileSync("package.json", "utf8"));
  await waitForNpmRelease(npmCommand, name, nextRelease.version, nextRelease.gitHead);
  assert.ok(githubCommand("GET", `repos/${process.env.GITHUB_REPOSITORY}/releases/tags/${nextRelease.gitTag}`), "GitHub Release is missing");
}
