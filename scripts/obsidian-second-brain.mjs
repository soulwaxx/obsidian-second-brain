#!/usr/bin/env node
import { spawn } from "node:child_process";
import { readFileSync, realpathSync, statSync } from "node:fs";
import os from "node:os";
import { fileURLToPath } from "node:url";
import path from "node:path";

const script = fileURLToPath(new URL("./obsidian-second-brain.py", import.meta.url));
const bridge = fileURLToPath(new URL("./pi_wiki_tools.py", import.meta.url));
const args = process.argv.slice(2);
const native = args[0] === "--pi-tool";
const mcp = args[0] === "mcp";
const target = native ? bridge : script;
const python = process.env.PYTHON || "python3";
const childArgs = native ? ["-B", target, ...args.slice(1)] : ["-B", target, ...args];
const MAX_STDOUT = 256 * 1024;
const MAX_STDERR = 8 * 1024;
const MAX_RESULT = 64 * 1024;
// Batch operations read their JSON parameters from stdin (never argv). Once an apply has saved its journal the
// bridge prints this marker on stderr and the 30 s deadline stops, so the publication is not killed mid-way;
// cancellation or EOF still terminates it, and `batch-recover` resumes from the journal.
const STDIN_OPERATIONS = new Set(["batch-inspect", "batch-apply", "batch-recover"]);
const JOURNAL_PERSISTED = "osb-journal-persisted";
const requestedDeadline = Number(process.env.OSB_PI_TOOL_DEADLINE_MS);
const DEADLINE = Number.isFinite(requestedDeadline) && requestedDeadline > 0 ? Math.min(requestedDeadline, 30_000) : 30_000;

function terminate(child) {
  if (!child.pid) return;
  try {
    if (process.platform === "win32") child.kill("SIGKILL");
    else process.kill(-child.pid, "SIGKILL");
  } catch { /* already exited */ }
}

if (!native && !mcp) {
  // OSB_NODE lets `connect` pin this exact node executable in the MCP entries it writes.
  const child = spawn(python, childArgs, { stdio: "inherit", detached: process.platform !== "win32", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", OSB_NODE: process.execPath } });
  let stopping = false;
  const stop = () => { if (!stopping) { stopping = true; terminate(child); } };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
  let startFailed = false;
  child.on("error", (error) => {
    if (child.pid === undefined) {
      startFailed = true;
      console.error(`obsidian-second-brain: cannot start Python interpreter ${JSON.stringify(python)} (${error.code ?? error.message}). `
        + "Python 3.14+ with the package requirements is required: activate that environment so python3 resolves to it, "
        + "or set PYTHON=/path/to/python3.14. See docs/setup.md (Prepare tools and select paths).");
    } else {
      console.error(`obsidian-second-brain: ${error.message}`);
    }
    process.exitCode = 1;
  });
  child.on("close", (code) => { if (!startFailed) process.exitCode = code ?? 1; });
} else if (native) {
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
    child = spawn(python, childArgs, { stdio: [STDIN_OPERATIONS.has(args[1]) ? "inherit" : "ignore", "pipe", "pipe"], detached: process.platform !== "win32", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  } catch (error) {
    finish(failure("process-start", "Unable to start the Python tool process", null));
  }
  if (child) {
    child.stdout.on("data", (chunk) => {
      if (stdout.length + chunk.length > MAX_STDOUT) { overflow = true; abort(); return; }
      stdout = Buffer.concat([stdout, chunk]);
    });
    let markerTail = "";
    let persisted = false;
    child.stderr.on("data", (chunk) => {
      if (stderr.length < MAX_STDERR) stderr = Buffer.concat([stderr, chunk.subarray(0, MAX_STDERR - stderr.length)]);
      if (STDIN_OPERATIONS.has(args[1]) && !persisted) {
        markerTail = (markerTail + chunk.toString("latin1")).slice(-4096);
        if (markerTail.includes(JOURNAL_PERSISTED)) {
          persisted = true;
          clearTimeout(timer);
          process.stderr.write(`${JOURNAL_PERSISTED}\n`); // lets the MCP server's hung-launcher watchdog stand down too
        }
      }
    });
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

// `mcp` subcommand: stdio MCP server (newline-delimited JSON-RPC 2.0). Every tool call
// runs `--pi-tool` in a child launcher, so validation, the deadline and the output caps
// are the Pi tools' own. stdout carries protocol messages only.
const MCP_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]; // newest first; tested in tests/mcp-server.mjs
const MCP_MAX_MESSAGE = 64 * 1024;
const MCP_LOCATOR_DEADLINE = 3000;
const MCP_CONFIG_HELPER = fileURLToPath(new URL("./config_contract.py", import.meta.url)); // the helper extensions/obsidian.ts uses
// Per-profile opening and write policy. The `read` tools cannot change the vault, so it does not forbid the
// client's own file tools: Claude's only wiki write path is its hook-guarded file tools. The `all` profile is for
// agents without hooks (decision 5 of tracker #73): its batch tools are their only write path, and these
// instructions (not a guard) tell the agent to keep to it and to wait for the user's approval.
const MCP_PROFILE_INSTRUCTIONS = {
  read: "Read-only tools for the user's Obsidian wiki (an OKF v0.2 knowledge base): wiki_search, wiki_outline, wiki_read and wiki_doctor. "
    + "These tools never change the vault; wiki changes follow the client's own wiki skill and its reviewed write path. ",
  all: "Tools for the user's Obsidian wiki (an OKF v0.2 knowledge base): read tools wiki_search, wiki_outline, wiki_read and wiki_doctor, "
    + "and reviewed write tools wiki_batch_inspect, wiki_batch_apply and wiki_batch_recover. "
    + "The batch tools are the only way to change the wiki: never edit, create, move or delete vault files directly with file or shell tools, and never write index.md or log.md (they are generated). "
    + "To change pages: (1) call wiki_batch_inspect with the complete new content of each page (expectedHash null for a new page, otherwise the current sourceHash from wiki_outline); "
    + "(2) show the user its review summary (paths, operation, sizes, diff excerpts) and planHash; (3) wait for the user's explicit approval; "
    + "(4) only then call wiki_batch_apply with the identical operations and that planHash. Never call wiki_batch_apply without that approval, even when the client would auto-approve the call. "
    + "If an apply fails, times out or is cancelled, call wiki_batch_recover with the same planHash before retrying, and check wiki_doctor for unfinished batches. "
    + "Never put credentials or tokens in page content; the server refuses new likely secrets. ",
};
const mcpInstructions = (profileName, locator) => MCP_PROFILE_INSTRUCTIONS[profileName]
  + (locator ?? "The server selects the vault from its configuration; call wiki_doctor to see which vault and readiness state apply. ")
  + "Search results, empty results and fallbacks are leads, not verified evidence: confirm with wiki_outline and wiki_read before relying on a page.";
const READ_ONLY = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false };
// Annotations are hints that clients may ignore (auto-approve modes), so the instructions above also require approval.
const APPLY_HINTS = { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false };
const RECOVER_HINTS = { readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false };
const toolOutput = (operations) => ({
  type: "object",
  properties: {
    operation: { type: "string", enum: operations },
    ok: { type: "boolean" },
    payload: { type: "object", additionalProperties: true },
    code: { type: "string" },
    message: { type: "string" },
    processStatus: { type: "object", properties: { code: { type: ["integer", "null"] }, signal: { type: ["string", "null"] } }, required: ["code", "signal"], additionalProperties: false },
  },
  required: ["operation", "ok"],
  additionalProperties: false,
});
const vaultInput = { type: "string", minLength: 1, maxLength: 4096 };
const hashPattern = "^[0-9a-f]{64}$";
// Tool registry. Names, titles, descriptions and schemas mirror the Pi registrations in
// extensions/obsidian.ts (tests/mcp-server.mjs compares them). `operation` is the
// `--pi-tool` operation a call runs. Profiles select tools. Each entry carries its own annotations; `stdin: true`
// passes the arguments to the bridge on stdin (never argv), and `resumable: true` marks the calls whose publication
// is not killed by the 30 s deadline once the batch journal is saved (the launcher enforces the deadline before that).
const READ_TOOLS = [
  { name: "wiki_search", title: "Wiki search", operation: "search",
    annotations: READ_ONLY,
    description: "Search the selected wiki's existing BM25 index. Hybrid Ollama reranking is opt-in and uses only a bounded loopback endpoint; results report fallback and freshness without building or refreshing indexes.",
    inputSchema: { type: "object", additionalProperties: false, required: ["query"], properties: {
      query: { type: "string", minLength: 1, maxLength: 4000 }, vault: vaultInput,
      limit: { type: "integer", minimum: 1, maximum: 100 }, hybrid: { type: "boolean" },
      ollamaUrl: { type: "string", minLength: 1, maxLength: 300 }, ollamaModel: { type: "string", minLength: 1, maxLength: 200 },
      timeout: { type: "number", minimum: 0.1, maximum: 30 } } } },
  { name: "wiki_outline", title: "Wiki outline", operation: "outline",
    annotations: READ_ONLY,
    description: "Return hash-bound outline labels and source coordinates for one visible wiki Markdown page; use expectedHash for pagination.",
    inputSchema: { type: "object", additionalProperties: false, required: ["page"], properties: {
      page: { type: "string", minLength: 6, maxLength: 1024 }, vault: vaultInput,
      expectedHash: { type: "string", pattern: hashPattern },
      limit: { type: "integer", minimum: 1, maximum: 100 }, offset: { type: "integer", minimum: 0, maximum: 1000000 } } } },
  { name: "wiki_read", title: "Wiki read", operation: "read",
    annotations: READ_ONLY,
    description: "Read a bounded hash-verified raw or current-projected section/span from one visible wiki Markdown page. Section reads require an entry id from wiki_outline, not a heading title; continuation never extends the original selection.",
    inputSchema: { type: "object", additionalProperties: false, required: ["page", "expectedHash"], properties: {
      page: { type: "string", minLength: 6, maxLength: 1024 }, vault: vaultInput,
      expectedHash: { type: "string", pattern: hashPattern, description: "sourceHash from wiki_outline, or expectedHash from wiki_search readRef. Must match the source snapshot." },
      section: { type: "string", minLength: 1, maxLength: 200, description: "Outline entry id returned by wiki_outline (metadata, preamble, or heading:<offset>). Not a heading title. Mutually exclusive with start/end." },
      start: { type: "integer", minimum: 0, maximum: 100000000, description: "Zero-based decoded Unicode source character start coordinate, for example from wiki_search readRef.start. Requires end. Mutually exclusive with section." },
      end: { type: "integer", minimum: 0, maximum: 100000000, description: "Exclusive decoded Unicode source character end coordinate, for example from wiki_search readRef.end. Requires start. Mutually exclusive with section." },
      view: { type: "string", enum: ["raw", "current"] }, maxChars: { type: "integer", minimum: 1, maximum: 12000 },
      cursor: { type: "integer", minimum: 0, maximum: 100000000 } } } },
  { name: "wiki_doctor", title: "Wiki doctor", operation: "doctor",
    annotations: READ_ONLY,
    description: "Inspect selected integration configuration, vault, retrieval freshness, lifecycle status, capability readiness, and suggested next steps without writing, repairing, indexing, settling, or executing any suggestion.",
    inputSchema: { type: "object", additionalProperties: false, properties: { vault: vaultInput } } },
];
// Batch write tools (`all` profile only): the existing reviewed wiki-page batch flow, nothing else. They are never
// Pi tools. Limits mirror scripts/pi_wiki_tools.py: 20 pages, 48 KiB of serialized operations.
const batchOperations = { type: "array", minItems: 1, maxItems: 20, description: "Pages to write, at most 20 and 48 KiB serialized in total. Each entry is the complete new content of one Markdown page under wiki/.",
  items: { type: "object", additionalProperties: false, required: ["path", "expectedHash", "content"], properties: {
    path: { type: "string", minLength: 6, maxLength: 1024, description: "Vault-relative POSIX path of a Markdown page under wiki/, for example wiki/topic/page.md. index.md, log.md, _plan.md, hidden paths and symlinks are refused." },
    expectedHash: { type: ["string", "null"], pattern: hashPattern, description: "null to create a page that does not exist; otherwise the page's current sourceHash from wiki_outline (the apply is refused if the page changed)." },
    content: { type: "string", maxLength: 49152, description: "Complete page text including YAML frontmatter with a type field." } } } };
const planHashInput = { type: "string", pattern: hashPattern, description: "planHash returned by wiki_batch_inspect for exactly these operations." };
const BATCH_TOOLS = [
  { name: "wiki_batch_inspect", title: "Wiki batch inspect", operation: "batch-inspect", stdin: true, annotations: READ_ONLY,
    description: "Review a proposed wiki page change without writing anything. Validates paths, OKF frontmatter, likely secrets and current page hashes, then returns a bounded review summary (per page: operation, byte sizes, hashes, diff excerpt, validation) and the planHash. Show the summary to the user and wait for explicit approval before wiki_batch_apply.",
    inputSchema: { type: "object", additionalProperties: false, required: ["operations"], properties: { operations: batchOperations } } },
  { name: "wiki_batch_apply", title: "Wiki batch apply", operation: "batch-apply", stdin: true, resumable: true, annotations: APPLY_HINTS,
    description: "Write the pages the user approved after wiki_batch_inspect: resend the identical operations with that planHash. Refused if the operations, any target page or the selected config changed since inspection. Then updates navigation, the lifecycle log and the retrieval index. Never call without the user's explicit approval of the inspect summary. If the call fails, times out or is cancelled, call wiki_batch_recover with the same planHash before retrying.",
    inputSchema: { type: "object", additionalProperties: false, required: ["operations", "planHash"], properties: { operations: batchOperations, planHash: planHashInput } } },
  { name: "wiki_batch_recover", title: "Wiki batch recover", operation: "batch-recover", stdin: true, resumable: true, annotations: RECOVER_HINTS,
    description: "Resume an interrupted wiki_batch_apply from its saved journal, by planHash (or batchId of an unfinished batch from wiki_doctor batches that is not cliOnly). Completes only the plan the user approved and refuses, preserving current bytes, if a target page changed since. Repeating it after completion is a no-op.",
    inputSchema: { type: "object", additionalProperties: false, properties: { planHash: planHashInput,
      batchId: { type: "string", pattern: "^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", description: "Alternative to planHash: an unfinished batchId listed by wiki_doctor whose entry is not cliOnly." } } } },
];
const PROFILES = { read: READ_TOOLS, all: [...READ_TOOLS, ...BATCH_TOOLS] };

function serveMcp() {
  const flags = args.slice(1);
  const usage = (message) => { console.error(`obsidian-second-brain mcp: ${message}\nUsage: obsidian-second-brain mcp [--tools read|all]`); process.exitCode = 2; };
  let profileName = "read";
  if (flags.length === 2 && flags[0] === "--tools") profileName = flags[1];
  else if (flags.length === 1 && flags[0].startsWith("--tools=")) profileName = flags[0].slice("--tools=".length);
  else if (flags.length > 0) return usage("unsupported arguments");
  if (!Object.hasOwn(PROFILES, profileName)) return usage(`unknown --tools profile ${JSON.stringify(profileName)}; use read or all`);
  const tools = new Map(PROFILES[profileName].map((tool) => [tool.name, tool]));
  const outputSchema = toolOutput([...new Set([...tools.values()].map((tool) => tool.operation))]);
  const toolList = [...tools.values()].map(({ operation, stdin, resumable, ...tool }) => ({ ...tool, outputSchema }));
  const serverInfo = { name: "obsidian-second-brain", version: JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version };
  const launcher = fileURLToPath(import.meta.url);
  const calls = new Map(); // request id -> { child, cancelled }
  let closing = false;

  const send = (message) => { if (!closing) process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", ...message })}\n`); };
  const fail = (id, code, message) => send({ id, error: { code, message } });
  const idOf = (message) => (typeof message.id === "string" || typeof message.id === "number" ? message.id : null);
  const terminateCall = (call) => {
    call.child.kill("SIGTERM"); // the child launcher terminates its Python process group
    setTimeout(() => call.child.kill("SIGKILL"), 1500).unref();
  };
  const stop = (call) => {
    call.cancelled = true;
    terminateCall(call);
  };
  // Wiki locator for `initialize`, resolved once with the shared config helper: it only reads the selected
  // config and the vault path, writes nothing, and never reaches stdout. null means "fall back to wiki_doctor".
  let locator;
  let locatorChild;
  const resolveLocator = () => locator ??= new Promise((resolve) => {
    const home = (value) => (value === "~" ? os.homedir() : value?.startsWith("~/") ? path.join(os.homedir(), value.slice(2)) : value);
    const configPath = path.resolve(home(process.env.OBSIDIAN_AGENT_CONFIG?.trim()) || path.join(os.homedir(), ".config/obsidian-second-brain/properties.json"));
    let output = "";
    const done = (value) => { clearTimeout(timer); resolve(value); };
    const timer = setTimeout(() => { locatorChild?.kill("SIGKILL"); done(null); }, MCP_LOCATOR_DEADLINE);
    try {
      locatorChild = spawn(python, ["-B", MCP_CONFIG_HELPER, configPath], { stdio: ["ignore", "pipe", "ignore"], env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
    } catch { return done(null); }
    locatorChild.stdout.on("data", (chunk) => { if (output.length < MAX_RESULT) output += chunk; });
    locatorChild.on("error", () => done(null));
    locatorChild.on("close", (code) => {
      try {
        const { config, error } = JSON.parse(output);
        const selected = process.env.OBSIDIAN_VAULT_PATH?.trim() || home(config?.vaultPath);
        if (code !== 0 || error || typeof selected !== "string" || !selected) return done(null);
        const vault = realpathSync(selected);
        if (!statSync(vault).isDirectory()) return done(null);
        // Paths are quoted so a directory name with newlines cannot inject instruction text.
        done(`Configured Obsidian wiki: ${JSON.stringify(`${vault}/wiki`)} (OKF v0.2; selected integration config: ${JSON.stringify(configPath)}). `
          + "Load wiki/WIKI.md and the relevant wiki/index.md only when a task needs the wiki; call wiki_doctor for readiness state. ");
      } catch { done(null); }
    });
  });
  const shutdown = () => {
    if (closing) return;
    closing = true;
    locatorChild?.kill("SIGKILL");
    for (const call of calls.values()) stop(call);
    process.stdin.destroy(); // the event loop ends once every child has closed
  };
  // On stdin EOF running calls end at once, but a pending `initialize` reply is still delivered (bounded by the
  // locator deadline) before the server exits; signals and stdout errors call shutdown() directly.
  let initializing = 0;
  let draining = false;
  const finishInitialize = () => { initializing -= 1; if (draining && initializing === 0) shutdown(); };
  const onEof = () => {
    draining = true;
    for (const call of calls.values()) stop(call);
    if (initializing === 0) shutdown();
  };

  function runTool(id, tool, toolArgs) {
    const failed = (code, message) => ({ operation: tool.operation, ok: false, code, message });
    const call = { cancelled: false };
    let output = Buffer.alloc(0);
    let overflow = false;
    let timedOut = false;
    const settle = (result) => {
      clearTimeout(watchdog);
      calls.delete(id);
      if (!call.cancelled) send({ id, result: { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result, isError: result.ok !== true } });
    };
    // The child launcher enforces the 30 s deadline itself; this only covers a hung launcher. A resumable call's
    // launcher lifts the deadline once the batch journal is saved (and, for recover, the writer lock is held) and
    // forwards its marker on stderr; the watchdog stands down then. Cancellation or EOF still stop the call.
    const watchdog = setTimeout(() => { timedOut = true; if (call.child) terminateCall(call); }, DEADLINE + 1500);
    try {
      call.child = spawn(process.execPath, [launcher, "--pi-tool", tool.operation, ...(tool.stdin ? [] : [JSON.stringify(toolArgs)])],
        { stdio: [tool.stdin ? "pipe" : "ignore", "pipe", tool.resumable ? "pipe" : "ignore"], env: process.env });
    } catch {
      return settle(failed("process-start", "Unable to start the packaged wiki tool"));
    }
    calls.set(id, call);
    if (tool.stdin) { // batch parameters travel on stdin, not argv (argv is limited to 128 KiB on Linux)
      call.child.stdin.on("error", () => { /* the launcher exited first; its result is reported on close */ });
      call.child.stdin.end(JSON.stringify(toolArgs));
    }
    if (tool.resumable) {
      let markerTail = "";
      call.child.stderr.on("data", (chunk) => {
        markerTail = (markerTail + chunk.toString("latin1")).slice(-256);
        if (markerTail.includes(JOURNAL_PERSISTED)) clearTimeout(watchdog);
      });
    }
    call.child.stdout.on("data", (chunk) => {
      if (overflow) return;
      if (output.length + chunk.length > MAX_RESULT + 1) { overflow = true; terminateCall(call); return; }
      output = Buffer.concat([output, chunk]);
    });
    call.child.on("error", () => { if (call.child.pid === undefined) settle(failed("process-start", "Unable to start the packaged wiki tool")); });
    call.child.on("close", () => {
      if (!calls.has(id)) return;
      if (timedOut) return settle(failed("timeout", "Wiki operation exceeded the 30-second deadline"));
      if (overflow) return settle(failed("output-limit", "Tool result exceeded 64 KiB; request fewer results or a smaller read"));
      let result;
      try { result = JSON.parse(output.toString("utf8")); } catch { /* reported below */ }
      settle(result && typeof result === "object" && result.operation === tool.operation && typeof result.ok === "boolean"
        ? result : failed("invalid-process-output", "Packaged wiki tool returned an invalid result envelope"));
    });
  }

  function handle(line) {
    let message;
    try { message = JSON.parse(line); } catch { return fail(null, -32700, "Parse error"); }
    if (!message || typeof message !== "object" || Array.isArray(message) || message.jsonrpc !== "2.0") return fail(idOf(message ?? {}), -32600, "Invalid Request: expected one JSON-RPC 2.0 object (batches are not supported)");
    const hasId = Object.hasOwn(message, "id");
    if (hasId && typeof message.id !== "string" && typeof message.id !== "number") return fail(null, -32600, "Invalid Request: id must be a string or number");
    if (typeof message.method !== "string") return hasId && (Object.hasOwn(message, "result") || Object.hasOwn(message, "error")) ? undefined : fail(hasId ? message.id : null, -32600, "Invalid Request: method is required");
    const { id, method } = message;
    const params = message.params;
    if (!hasId) { // notifications never receive a response; initialized and unknown ones are ignored
      if (method === "notifications/cancelled" && params && typeof params === "object" && calls.has(params.requestId)) stop(calls.get(params.requestId));
      return;
    }
    if (method === "ping") return send({ id, result: {} });
    if (method === "initialize") {
      if (!params || typeof params !== "object" || typeof params.protocolVersion !== "string") return fail(id, -32602, "Invalid params: protocolVersion is required");
      const protocolVersion = MCP_VERSIONS.includes(params.protocolVersion) ? params.protocolVersion : MCP_VERSIONS[0];
      initializing += 1;
      return void resolveLocator().then((found) => {
        send({ id, result: { protocolVersion, capabilities: { tools: { listChanged: false } }, serverInfo, instructions: mcpInstructions(profileName, found) } });
        finishInitialize();
      });
    }
    if (method === "tools/list") return send({ id, result: { tools: toolList } });
    if (method === "tools/call") {
      const toolArgs = params?.arguments ?? {};
      if (!params || typeof params !== "object" || typeof params.name !== "string") return fail(id, -32602, "Invalid params: name is required");
      if (!toolArgs || typeof toolArgs !== "object" || Array.isArray(toolArgs)) return fail(id, -32602, "Invalid params: arguments must be an object");
      if (!tools.has(params.name)) return fail(id, -32602, `Unknown tool: ${params.name}`);
      if (calls.has(id)) return fail(id, -32600, "Invalid Request: id is already in use by a running call");
      return runTool(id, tools.get(params.name), toolArgs);
    }
    return fail(id, -32601, `Method not found: ${method}`);
  }

  let pending = Buffer.alloc(0);
  let discarding = false;
  process.stdin.on("data", (chunk) => {
    for (let data = chunk; data.length > 0;) {
      const end = data.indexOf(0x0a);
      const piece = end === -1 ? data : data.subarray(0, end);
      if (!discarding && pending.length + piece.length > MCP_MAX_MESSAGE) {
        discarding = true;
        pending = Buffer.alloc(0);
        fail(null, -32600, "Invalid Request: message exceeds 64 KiB");
      } else if (!discarding) pending = Buffer.concat([pending, piece]);
      if (end === -1) break;
      const line = discarding ? "" : pending.toString("utf8").trim();
      pending = Buffer.alloc(0);
      discarding = false;
      if (line) handle(line);
      data = data.subarray(end + 1);
    }
  });
  process.stdin.on("end", onEof);
  process.stdout.on("error", shutdown);
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

if (mcp) serveMcp();
