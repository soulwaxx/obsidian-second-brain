import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { setTimeout } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { releaseDescriptions, releaseRange } from "./release-description.mjs";
import { prepare } from "./release-artifact.mjs";

export function releaseVersion(tag) {
  assert.match(tag, /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/, "recovery requires an exact stable vX.Y.Z tag");
  return tag.slice(1);
}

export function assertNpmRelease(info, version, sha) {
  assert.equal(info.version, version);
  assert.equal(info.gitHead, sha, "existing npm version belongs to a different source; never overwrite it");
}

const NPM_VISIBILITY_WAIT_MS = 15 * 60 * 1000;
const NPM_VISIBILITY_INTERVAL_MS = 10 * 1000;
const NPM_VISIBILITY_ATTEMPTS = NPM_VISIBILITY_WAIT_MS / NPM_VISIBILITY_INTERVAL_MS;

export async function waitForNpmRelease(npm, name, version, sha, sleep = setTimeout) {
  // npm can acknowledge publication before registry metadata is visible to readers.
  // Keep source checks strict, but allow fifteen minutes of propagation between polls.
  for (let attempt = 0; attempt <= NPM_VISIBILITY_ATTEMPTS; attempt++) {
    const info = npm("view", `${name}@${version}`, "--json");
    if (info) { assertNpmRelease(info, version, sha); return; }
    if (attempt < NPM_VISIBILITY_ATTEMPTS) {
      console.log(`Waiting for npm ${name}@${version} visibility (${attempt + 1}/${NPM_VISIBILITY_ATTEMPTS})`);
      await sleep(NPM_VISIBILITY_INTERVAL_MS);
    }
  }
  throw new Error(`npm ${name}@${version} is still not visible after fifteen minutes; check registry visibility before tag recovery`);
}

export async function recoverRelease({ tag, sha, name, repository }, { npm, github, prepareArtifact, describeRelease }) {
  const version = releaseVersion(tag);
  assert.match(sha, /^[0-9a-f]{40}$/);
  const info = npm("view", `${name}@${version}`, "--json");
  if (info) assertNpmRelease(info, version, sha);
  const release = github("GET", `repos/${repository}/releases/tags/${tag}`);
  if (release) assert.equal(release.tag_name, tag);
  // Preflight missing-release details before creating any missing publication output.
  const description = release ? undefined : await describeRelease();
  const latestRelease = release ? undefined : github("GET", `repos/${repository}/releases/latest`);
  const makeLatest = !latestRelease || (latestRelease.prerelease !== true
    && isNewerStableRelease(version, latestRelease.tag_name));
  if (!info) {
    npm("version", version, "--no-git-tag-version", "--allow-same-version");
    prepareArtifact();
    // Never let recovery of an older tag move latest backwards.
    npm("publish", "--access", "public", "--provenance", "--tag", "recovered");
    await waitForNpmRelease(npm, name, version, sha);
  }
  if (!release) {
    const notes = description.previousTag
      ? github("POST", `repos/${repository}/releases/generate-notes`, {
        tag_name: tag, target_commitish: sha, previous_tag_name: description.previousTag,
      })
      : { body: `# ${tag}` };
    github("POST", `repos/${repository}/releases`, { tag_name: tag, target_commitish: sha, name: tag,
      body: `${notes.body}\n\n${description.notes}`, make_latest: makeLatest ? "true" : "false" });
  }
  const tags = npm("view", name, "dist-tags", "--json") || {};
  const latest = tags.latest;
  if (!latest || compareStable(version, latest) > 0) npm("dist-tag", "add", `${name}@${version}`, "latest");
  console.log(`Recovered ${tag}; existing npm versions and releases were preserved`);
}

function isNewerStableRelease(version, tag) {
  try {
    return compareStable(version, releaseVersion(tag)) > 0;
  } catch {
    // Unknown existing release tags are not grounds to move GitHub's pointer.
    return false;
  }
}

function compareStable(a, b) {
  releaseVersion(`v${b}`);
  const left = a.split(".").map(BigInt), right = b.split(".").map(BigInt);
  for (let i = 0; i < 3; i++) if (left[i] !== right[i]) return left[i] > right[i] ? 1 : -1;
  return 0;
}

export function npmCommand(...args) {
  try {
    const output = execFileSync("npm", args, { encoding: "utf8", timeout: 120000,
      stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, NPM_CONFIG_PROVENANCE: "true" } });
    if (!args.includes("--json")) return;
    const result = JSON.parse(output);
    if (args[0] !== "view" || !Array.isArray(result)) return result;
    // npm 12 wraps even an exact-version metadata/dist-tags query in an array.
    assert.equal(result.length, 1, "Expected exactly one npm view result");
    assert.ok(result[0] && typeof result[0] === "object" && !Array.isArray(result[0]),
      "Expected npm view metadata");
    return result[0];
  } catch (error) {
    // Only an explicit registry 404 means missing. Auth, network, and other failures stop recovery.
    if (args[0] === "view" && /\bE404\b/.test(String(error.stderr))) return undefined;
    throw error;
  }
}

export function githubCommand(method, route, fields) {
  const args = ["api", "--method", method, route];
  if (fields) args.push("--input", "-");
  try {
    return JSON.parse(execFileSync("gh", args, { input: fields ? JSON.stringify(fields) : undefined,
      encoding: "utf8", timeout: 60000, stdio: ["pipe", "pipe", "pipe"] }));
  } catch (error) {
    if (method === "GET" && /HTTP 404\)/.test(String(error.stderr))) return undefined;
    throw error;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [command, tag] = process.argv.slice(2);
  releaseVersion(tag);
  const sha = execFileSync("git", ["rev-parse", `${tag}^{commit}`], { encoding: "utf8" }).trim();
  if (command === "resolve") console.log(sha);
  else if (command === "recover") {
    assert.equal(execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(), sha,
      "recovery must run from the exact verified tagged source");
    const { name } = JSON.parse(fs.readFileSync("package.json", "utf8"));
    await recoverRelease({ tag, sha, name, repository: process.env.GITHUB_REPOSITORY },
      { npm: npmCommand, github: githubCommand, prepareArtifact: prepare,
        describeRelease: async () => {
          const git = (...args) => execFileSync("git", args, { encoding: "utf8" }).trim();
          const { previousTag, commits } = releaseRange(sha, git);
          const notes = await releaseDescriptions({ repository: process.env.GITHUB_REPOSITORY,
            sourceSha: sha, commits });
          return { previousTag, notes };
        },
      });
  } else throw new Error("usage: node .github/release-tools/release-recovery.mjs resolve <tag> | recover <tag>");
}
