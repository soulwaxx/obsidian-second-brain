#!/usr/bin/env node
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const script = fileURLToPath(new URL("./obsidian-second-brain.py", import.meta.url));
const bridge = fileURLToPath(new URL("./pi_wiki_tools.py", import.meta.url));
const args = process.argv.slice(2);
const native = args[0] === "--pi-tool";
const target = native ? bridge : script;
const python = process.env.PYTHON || "python3";
const childArgs = native ? ["-B", target, ...args.slice(1)] : ["-B", target, ...args];
const MAX_STDOUT = 256 * 1024;
const MAX_STDERR = 8 * 1024;
const MAX_RESULT = 64 * 1024;
const requestedDeadline = Number(process.env.OSB_PI_TOOL_DEADLINE_MS);
const DEADLINE = Number.isFinite(requestedDeadline) && requestedDeadline > 0 ? Math.min(requestedDeadline, 30_000) : 30_000;

function terminate(child) {
  if (!child.pid) return;
  try {
    if (process.platform === "win32") child.kill("SIGKILL");
    else process.kill(-child.pid, "SIGKILL");
  } catch { /* already exited */ }
}

if (!native) {
  const child = spawn(python, childArgs, { stdio: "inherit", detached: process.platform !== "win32", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  let stopping = false;
  const stop = () => { if (!stopping) { stopping = true; terminate(child); } };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
  child.on("error", (error) => { console.error(`obsidian-second-brain: ${error.message}`); process.exitCode = 1; });
  child.on("close", (code) => { process.exitCode = code ?? 1; });
} else {
  let stdout = Buffer.alloc(0);
  let stderr = Buffer.alloc(0);
  let overflow = false;
  let settled = false;
  let child;
  const finish = (result) => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    process.removeListener("SIGINT", abort);
    process.removeListener("SIGTERM", abort);
    process.stdout.write(`${JSON.stringify(result)}\n`);
  };
  const abort = () => { terminate(child); };
  const timer = setTimeout(() => { timedOut = true; abort(); }, DEADLINE);
  let timedOut = false;
  process.on("SIGINT", abort);
  process.on("SIGTERM", abort);
  try {
    child = spawn(python, childArgs, { stdio: ["ignore", "pipe", "pipe"], detached: process.platform !== "win32", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  } catch (error) {
    finish(failure("process-start", "Unable to start the Python tool process", null));
  }
  if (child) {
    child.stdout.on("data", (chunk) => {
      if (stdout.length + chunk.length > MAX_STDOUT) { overflow = true; abort(); return; }
      stdout = Buffer.concat([stdout, chunk]);
    });
    child.stderr.on("data", (chunk) => { if (stderr.length < MAX_STDERR) stderr = Buffer.concat([stderr, chunk.subarray(0, MAX_STDERR - stderr.length)]); });
    child.on("error", () => finish(failure("process-start", "Unable to start the Python tool process", null)));
    child.on("close", (code, signal) => {
      if (settled) return;
      if (timedOut) return finish(failure("timeout", "Wiki operation exceeded the 30-second deadline", code));
      if (overflow) return finish(failure("output-limit", "Wiki operation exceeded the bounded output transport; request fewer results or a smaller read", code));
      let payload;
      try { payload = JSON.parse(stdout.toString("utf8")); }
      catch { return finish(failure("invalid-process-output", "Wiki operation returned invalid structured output", code)); }
      if (!payload || typeof payload !== "object" || Array.isArray(payload)
          || Object.keys(payload).some((key) => !["operation", "ok", "payload", "code", "message"].includes(key))
          || payload.operation !== args[1] || typeof payload.ok !== "boolean"
          || (Object.hasOwn(payload, "payload") && (!payload.payload || typeof payload.payload !== "object" || Array.isArray(payload.payload)))
          || (Object.hasOwn(payload, "code") && (typeof payload.code !== "string" || !/^[a-z][a-z0-9-]{0,63}$/.test(payload.code)))
          || (Object.hasOwn(payload, "message") && (typeof payload.message !== "string" || payload.message.length > 1000))) {
        return finish(failure("invalid-process-output", "Wiki operation returned an invalid result envelope", code));
      }
      const result = { ...payload, processStatus: { code, signal } };
      if ((code !== 0 || signal !== null) && result.ok !== false) {
        result.ok = false;
        result.code ??= "process-failed";
        result.message ??= "Wiki operation process exited unsuccessfully";
      }
      if (Buffer.byteLength(JSON.stringify(result), "utf8") > MAX_RESULT) return finish(failure("output-limit", "Wiki result exceeds 64 KiB; request fewer results or a smaller read", code));
      // stderr is deliberately not returned: it may contain paths or exception details.
      finish(result);
    });
  }
}

function failure(code, message, processCode) {
  return { operation: args[1] ?? "unknown", ok: false, code, message,
    processStatus: { code: processCode, signal: null } };
}
