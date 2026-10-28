import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawn, spawnSync } from "node:child_process";

const root = process.cwd();
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-tool-launcher-"));
const activeChildren = new Set();
const processGroups = new Set();
function processGroup(pid) {
  const result = spawnSync("ps", ["-o", "pgid=", "-p", String(pid)], { encoding: "utf8" });
  const group = Number(result.stdout.trim());
  if (Number.isFinite(group) && group > 0) processGroups.add(group);
  return group;
}
function liveGroupMembers(group) {
  const result = spawnSync("ps", ["-axo", "pgid=,stat="], { encoding: "utf8" });
  return result.stdout.split("\n").some((line) => {
    const match = line.trim().match(/^(\d+)\s+(\S+)/);
    return match && Number(match[1]) === group && !match[2].startsWith("Z");
  });
}
async function assertGroupGone(group, label) {
  if (!group || process.platform === "win32") return;
  const deadline = Date.now() + 3000;
  while (liveGroupMembers(group) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(liveGroupMembers(group), false, `${label} process group has exited (zombies count as terminated)`);
}
async function assertPidGone(pid, label) {
  if (!pid) return;
  const alive = () => {
    try { process.kill(pid, 0); } catch { return false; }
    const status = spawnSync("ps", ["-o", "stat=", "-p", String(pid)], { encoding: "utf8" }).stdout.trim();
    return status !== "" && !status.startsWith("Z");
  };
  const deadline = Date.now() + 3000;
  while (alive() && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(alive(), false, `${label} has exited (zombies count as terminated)`);
}
try {
  const fake = path.join(tmp, "python shim");
  const pidFile = path.join(tmp, "descendant.pid");
  const largePidFile = path.join(tmp, "large-python.pid");
  fs.writeFileSync(fake, `#!/bin/sh\ncase "$MODE" in\nnormal) printf '%s\\n' '{"operation":"search","ok":true,"payload":{"results":[]}}' ;;\nmalformed) printf '{not-json' ;;\nunknown-field) printf '%s\\n' '{"operation":"search","ok":true,"rawArgs":["secret"]}' ;;\nfailed) printf '%s\\n' '{"operation":"search","ok":true,"payload":{"retained":true}}'; exit 7 ;;\nlarge) echo $$ > '${largePidFile}'; head -c 300000 /dev/zero | tr '\\000' x ;;\ntimeout) sh -c 'trap "" TERM; echo $$ > "${pidFile}"; while :; do sleep 1; done' & wait ;;\nnoisy) head -c 10000 /dev/zero | tr '\\000' x >&2; printf '%s\\n' '{"operation":"search","ok":true}' ;;\nesac\n`);
  fs.chmodSync(fake, 0o755);
  const launcher = path.join(root, "scripts/obsidian-second-brain.mjs");
  const run = (mode, deadline) => spawnSync(process.execPath, [launcher, "--pi-tool", "search", "{}"], {
    cwd: tmp, encoding: "utf8", timeout: 5000,
    env: { ...process.env, PYTHON: fake, MODE: mode, OSB_PI_TOOL_DEADLINE_MS: String(deadline ?? 30000) },
  });
  const normal = run("normal");
  assert.equal(normal.status, 0, normal.stderr);
  assert.deepEqual(JSON.parse(normal.stdout), { operation: "search", ok: true, payload: { results: [] }, processStatus: { code: 0, signal: null } });
  assert.equal(JSON.parse(run("malformed").stdout).code, "invalid-process-output");
  assert.equal(JSON.parse(run("unknown-field").stdout).code, "invalid-process-output");
  const failed = run("failed");
  const retainedFailure = JSON.parse(failed.stdout);
  assert.equal(retainedFailure.ok, false);
  assert.equal(retainedFailure.payload.retained, true);
  assert.equal(retainedFailure.processStatus.code, 7);
  const noisy = run("noisy");
  assert.equal(JSON.parse(noisy.stdout).ok, true, "bounded stderr is not leaked or allowed to corrupt JSON result");
  const missingPython = spawnSync(process.execPath, [launcher, "--pi-tool", "doctor", "{}"], {
    cwd: tmp, encoding: "utf8", timeout: 5000,
    env: { ...process.env, PYTHON: path.join(tmp, "missing-python"), OSB_PI_TOOL_DEADLINE_MS: "1000" },
  });
  assert.equal(JSON.parse(missingPython.stdout).code, "process-start");
  const large = run("large");
  assert.equal(JSON.parse(large.stdout).code, "output-limit");
  assert.ok(fs.existsSync(largePidFile));
  await assertGroupGone(processGroup(Number(fs.readFileSync(largePidFile, "utf8"))), "oversized-output Python/descendant");
  await assertPidGone(large.pid, "oversized-output launcher");
  const timed = run("timeout", 400);
  assert.equal(JSON.parse(timed.stdout).code, "timeout");
  assert.ok(fs.existsSync(pidFile), "timeout fixture launched its TERM-ignoring descendant");
  const descendant = Number(fs.readFileSync(pidFile, "utf8"));
  await assertGroupGone(processGroup(descendant), "timeout Python/descendant");
  await assertPidGone(timed.pid, "timeout launcher");
  fs.rmSync(pidFile, { force: true });
  const cancelled = spawn(process.execPath, [launcher, "--pi-tool", "doctor", "{}"], {
    cwd: tmp, detached: process.platform !== "win32", stdio: ["ignore", "ignore", "ignore"],
    env: { ...process.env, PYTHON: fake, MODE: "timeout", OSB_PI_TOOL_DEADLINE_MS: "30000" },
  });
  activeChildren.add(cancelled);
  const cancelledClose = new Promise((resolve) => cancelled.once("close", resolve));
  const deadline = Date.now() + 3000;
  while (!fs.existsSync(pidFile) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(fs.existsSync(pidFile), "abort fixture launched its TERM-ignoring descendant");
  const cancelledDescendant = Number(fs.readFileSync(pidFile, "utf8"));
  const cancelledPythonGroup = processGroup(cancelledDescendant);
  const cancelledLauncherGroup = processGroup(cancelled.pid);
  cancelled.kill("SIGTERM");
  await cancelledClose;
  activeChildren.delete(cancelled);
  await assertGroupGone(cancelledPythonGroup, "cancelled Python/descendant");
  await assertGroupGone(cancelledLauncherGroup, "cancelled launcher");
  console.log("Pi wiki launcher PASS: argv JSON, output bounds, noisy stderr, cancellation, timeout and macOS/Linux process-group cleanup.");
} finally {
  for (const child of activeChildren) {
    try { if (child.pid && process.platform !== "win32") process.kill(-child.pid, "SIGKILL"); else child.kill("SIGKILL"); } catch { /* already exited */ }
  }
  await Promise.all([...activeChildren].map((child) => child.closePromise ?? new Promise((resolve) => {
    if (child.exitCode !== null || child.signalCode !== null) resolve();
    else child.once("close", resolve);
  })));
  for (const group of processGroups) {
    if (!liveGroupMembers(group)) continue;
    try { process.kill(-group, "SIGKILL"); } catch { /* already exited */ }
  }
  fs.rmSync(tmp, { recursive: true, force: true });
}
