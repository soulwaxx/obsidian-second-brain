import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { execFileSync, spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { defaultLauncher as launcher, startMcp } from "./mcp-client.mjs";

const root = path.resolve(path.dirname(launcher), "..");
const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "mcp-server-")));
const python = process.env.PYTHON || "python3";
const READ_TOOLS = ["wiki_search", "wiki_outline", "wiki_read", "wiki_doctor"];
const clients = new Set();
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
function commandsMentioning(marker) {
  return spawnSync("ps", ["-axo", "command="], { encoding: "utf8" }).stdout.split("\n").filter((line) => line.includes(marker));
}
async function waitForFile(file) {
  const deadline = Date.now() + 5000;
  while (!fs.existsSync(file) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(fs.existsSync(file), `${path.basename(file)} was written`);
}
function snapshot(...directories) {
  const entries = [];
  const walk = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const target = path.join(directory, entry.name);
      const info = fs.lstatSync(target, { bigint: true });
      entries.push([target, info.mode.toString(), info.mtimeNs.toString(), info.isFile() ? fs.readFileSync(target).toString("base64") : null]);
      if (info.isDirectory()) walk(target);
    }
  };
  directories.forEach(walk);
  return JSON.stringify(entries);
}

try {
  // Synthetic vault, selected through an isolated config; nothing here touches a real vault or profile.
  const home = path.join(tmp, "home");
  const cwd = path.join(tmp, "client cwd");
  const vault = path.join(tmp, "vault ü space");
  fs.mkdirSync(home);
  fs.mkdirSync(cwd);
  fs.mkdirSync(path.join(vault, "wiki/topic"), { recursive: true });
  const source = "---\ntype: Note\n---\n# Coffee 😀\nEspresso evidence.\n";
  const pageRel = "wiki/topic/coffee ü.md";
  fs.writeFileSync(path.join(vault, pageRel), source);
  const sourceHash = createHash("sha256").update(source).digest("hex");
  const config = path.join(tmp, "properties.json");
  fs.writeFileSync(config, JSON.stringify({ vaultPath: vault }));
  const baseEnv = { HOME: home, OBSIDIAN_AGENT_CONFIG: config, OBSIDIAN_VAULT_PATH: "", OSB_PI_TOOL_DEADLINE_MS: "30000", PYTHONWARNINGS: "error::DeprecationWarning" };
  execFileSync(python, ["-B", path.join(root, "scripts/bm25-index.py"), "build", "--vault", vault], { env: { ...process.env, ...baseEnv }, cwd });

  // Python network guard: every connection or name lookup attempt is recorded and refused.
  const guard = path.join(tmp, "guard");
  const netLog = path.join(tmp, "network.log");
  fs.mkdirSync(guard);
  fs.writeFileSync(path.join(guard, "sitecustomize.py"), `import os, socket
_log = os.environ["OSB_NET_LOG"]
def _record(kind, target):
    with open(_log, "a") as handle:
        handle.write(f"{kind} {target}\\n")
def _block(kind, original):
    def wrapper(*args, **kwargs):
        _record(kind, args[1:2] if kind.startswith("connect") else args[:1])
        raise OSError("network blocked by test")
    return wrapper
socket.socket.connect = _block("connect", socket.socket.connect)
socket.socket.connect_ex = _block("connect_ex", socket.socket.connect_ex)
socket.getaddrinfo = _block("getaddrinfo", socket.getaddrinfo)
`);
  const guardEnv = { PYTHONPATH: guard, OSB_NET_LOG: netLog };
  const probe = spawnSync(python, ["-c", "import socket\ntry:\n    socket.getaddrinfo('example.invalid', 80)\nexcept OSError:\n    pass"], { env: { ...process.env, ...guardEnv } });
  assert.equal(probe.status, 0, probe.stderr?.toString());
  assert.match(fs.readFileSync(netLog, "utf8"), /getaddrinfo/, "the network guard records attempts");
  fs.rmSync(netLog);

  const open = (options = {}) => {
    const client = startMcp({ cwd, ...options, env: { ...baseEnv, ...guardEnv, ...options.env } });
    clients.add(client);
    return client;
  };
  const bridge = (operation, input, env = {}) => {
    const result = spawnSync(process.execPath, [launcher, "--pi-tool", operation, JSON.stringify(input)], { cwd, encoding: "utf8", env: { ...process.env, ...baseEnv, ...env } });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
  };

  // Handshake, version negotiation, instructions and protocol-error handling.
  const watched = [vault, home, cwd];
  const configBefore = fs.readFileSync(config, "utf8");
  const before = snapshot(...watched);
  const client = open();
  const init = await client.initialize();
  assert.equal(init.jsonrpc, "2.0");
  assert.equal(init.result.protocolVersion, "2025-06-18");
  assert.deepEqual(init.result.capabilities, { tools: { listChanged: false } });
  assert.equal(init.result.serverInfo.name, "obsidian-second-brain");
  assert.equal(init.result.serverInfo.version, JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")).version);
  assert.match(init.result.instructions, /never edit vault files with your own file or shell tools/);
  assert.match(init.result.instructions, /wiki_doctor/);
  for (const version of ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]) {
    const negotiating = open();
    assert.equal((await negotiating.request("initialize", { protocolVersion: version })).result.protocolVersion, version, `supported version ${version} is echoed`);
    await negotiating.close();
  }
  const unsupported = open();
  assert.equal((await unsupported.request("initialize", { protocolVersion: "1999-01-01" })).result.protocolVersion, "2025-11-25", "unsupported versions get the newest supported version");
  assert.equal((await unsupported.request("initialize", {})).error.code, -32602);
  assert.equal((await unsupported.request("initialize")).error.code, -32602);
  await unsupported.close();

  assert.deepEqual((await client.request("ping")).result, {});
  const listed = (await client.request("tools/list")).result.tools;
  assert.deepEqual(listed.map((tool) => tool.name), READ_TOOLS);
  for (const tool of listed) {
    assert.deepEqual(tool.annotations, { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false });
    assert.equal(tool.inputSchema.type, "object");
    assert.equal(tool.inputSchema.additionalProperties, false);
    assert.equal(tool.outputSchema.additionalProperties, false);
  }

  // Tool names, descriptions and input schemas match the Pi registrations.
  const optional = Symbol("optional");
  globalThis.__mcpTypeStub = {
    Object: (props, options) => {
      const properties = {};
      const required = [];
      for (const [key, value] of Object.entries(props)) {
        if (value[optional]) properties[key] = value.schema;
        else { properties[key] = value; required.push(key); }
      }
      return { type: "object", properties, ...(required.length ? { required } : {}), ...options };
    },
    String: (options) => ({ type: "string", ...options }), Boolean: () => ({ type: "boolean" }),
    Integer: (options) => ({ type: "integer", ...options }), Number: (options) => ({ type: "number", ...options }),
    Optional: (schema) => ({ schema, [optional]: true }), Literal: (value) => ({ const: value }),
    Union: (schemas) => (schemas.every((item) => "const" in item) ? { type: "string", enum: schemas.map((item) => item.const) } : { anyOf: schemas }),
    Record: () => ({ type: "object", additionalProperties: true }), Unknown: () => ({}), Null: () => ({ type: "null" }),
  };
  const extensionSource = fs.readFileSync(path.join(root, "extensions/obsidian.ts"), "utf8")
    .replace('import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";', "type ExtensionAPI = unknown;")
    .replace('import { Type } from "@earendil-works/pi-ai";', "const Type = (globalThis as any).__mcpTypeStub;");
  const extensionFile = path.join(tmp, "extensions/obsidian.ts");
  fs.mkdirSync(path.dirname(extensionFile));
  fs.writeFileSync(extensionFile, extensionSource);
  const piTools = [];
  (await import(pathToFileURL(extensionFile).href)).default({ on: () => {}, registerTool: (tool) => piTools.push(tool) });
  assert.deepEqual(piTools.map((tool) => tool.name), READ_TOOLS);
  for (const [index, piTool] of piTools.entries()) {
    const tool = listed[index];
    assert.equal(tool.title, piTool.label, `${tool.name} title`);
    assert.equal(tool.description, piTool.description, `${tool.name} description`);
    assert.deepEqual(tool.inputSchema, piTool.parameters, `${tool.name} input schema`);
    assert.deepEqual(tool.annotations, piTool.annotations, `${tool.name} annotations`);
    assert.deepEqual(Object.keys(tool.outputSchema.properties), Object.keys(piTool.outputSchema.properties), `${tool.name} output fields`);
    assert.deepEqual(tool.outputSchema.required, piTool.outputSchema.required);
  }

  // Every tool: structuredContent equals the --pi-tool result for identical input.
  const outline = bridge("outline", { page: pageRel });
  const search = bridge("search", { query: "espresso" });
  assert.equal(search.payload.results[0].path, pageRel);
  const readRef = search.payload.results[0].readRef;
  const calls = [
    ["wiki_search", "search", { query: "espresso" }],
    ["wiki_search", "search", { query: "espresso", limit: 1 }],
    ["wiki_outline", "outline", { page: pageRel }],
    ["wiki_read", "read", { page: pageRel, expectedHash: sourceHash, section: outline.payload.entries.find((entry) => entry.kind === "heading").id }],
    ["wiki_read", "read", { page: readRef.page, expectedHash: readRef.expectedHash, start: readRef.start, end: readRef.end }],
    ["wiki_doctor", "doctor", {}],
    ["wiki_doctor", "doctor", { vault }],
  ];
  assert.equal((await client.callTool("wiki_doctor", {})).result.structuredContent.payload.vault, fs.realpathSync(vault), "vault comes from the selected config");
  for (const [name, operation, input] of calls) {
    const response = await client.callTool(name, input);
    const expected = bridge(operation, input);
    assert.equal(expected.ok, true, `${name} bridge result: ${JSON.stringify(expected)}`);
    assert.equal(response.result.isError, false, name);
    assert.deepEqual(response.result.structuredContent, expected, `${name} parity with --pi-tool`);
    assert.deepEqual(JSON.parse(response.result.content[0].text), expected, `${name} text content mirrors structuredContent`);
    assert.equal(response.result.content.length, 1);
  }

  // Tool failures are tool errors with the bridge's diagnostics; protocol problems are JSON-RPC errors.
  for (const [name, operation, input] of [
    ["wiki_read", "read", { page: pageRel, expectedHash: "0".repeat(64), section: "preamble" }],
    ["wiki_search", "search", { query: "x", unknown: true }],
    ["wiki_outline", "outline", {}],
  ]) {
    const response = await client.callTool(name, input);
    assert.equal(response.result.isError, true, name);
    assert.equal(response.result.structuredContent.ok, false);
    assert.deepEqual(response.result.structuredContent, bridge(operation, input), `${name} failure parity`);
  }
  assert.equal((await client.callTool("wiki_save", {})).error.code, -32602, "unknown tool");
  assert.match((await client.callTool("wiki_save", {})).error.message, /Unknown tool: wiki_save/);
  assert.equal((await client.request("tools/call", { arguments: {} })).error.code, -32602);
  assert.equal((await client.request("tools/call", { name: "wiki_doctor", arguments: [] })).error.code, -32602);
  assert.equal((await client.request("tools/call", { name: "wiki_doctor", arguments: "x" })).error.code, -32602);
  assert.equal((await client.request("tools/call", { name: "wiki_doctor" })).result.isError, false, "absent arguments mean {}");
  assert.equal((await client.request("resources/list")).error.code, -32601, "unknown method");
  assert.equal((await client.request("no/such/method")).error.code, -32601);
  client.notify("notifications/unknown");
  client.send("{not json");
  assert.equal((await client.waitFor((message) => message.error?.code === -32700)).id, null);
  client.send(JSON.stringify([{ jsonrpc: "2.0", id: 900, method: "ping" }]));
  assert.equal((await client.waitFor((message) => message.error?.code === -32600 && /batches/.test(message.error.message))).id, null);
  client.send(JSON.stringify({ id: 901, method: "ping" }));
  client.send(JSON.stringify({ jsonrpc: "2.0", id: { nested: 1 }, method: "ping" }));
  client.send(JSON.stringify({ jsonrpc: "2.0", id: 902 }));
  client.send(JSON.stringify("string"));
  await client.waitFor((message) => message.id === 902 && message.error?.code === -32600);
  client.send(`${JSON.stringify({ jsonrpc: "2.0", id: 903, method: "ping", params: { padding: "x".repeat(70_000) } })}`);
  await client.waitFor((message) => message.error?.code === -32600 && /64 KiB/.test(message.error.message));
  assert.deepEqual((await client.request("ping")).result, {}, "server keeps serving after malformed input");
  client.send(JSON.stringify({ jsonrpc: "2.0", id: 904, result: {} })); // a client response: ignored, never answered
  assert.deepEqual((await client.request("ping")).result, {});
  assert.ok(!client.messages.some((message) => message.id === 904 || message.id === 901), "no response to ignored messages");
  assert.deepEqual((await client.close()), { code: 0, signal: null }, "clean exit on stdin EOF");
  assert.ok(client.rawLines.every((line) => { try { return JSON.parse(line).jsonrpc === "2.0"; } catch { return false; } }), "stdout carries only JSON-RPC messages");
  assert.equal(client.stderr(), "", "no stderr noise during a normal session");

  // The read profile writes nothing (vault, index/cache, lifecycle, config, HOME, cwd) and makes no network request.
  const unchanged = (message) => {
    assert.ok(snapshot(...watched) === before && fs.readFileSync(config, "utf8") === configBefore, message);
  };
  unchanged("read profile leaves vault, cache, lifecycle state, config, HOME and cwd untouched");
  assert.ok(!fs.existsSync(netLog), "default read calls make no network request");
  const hybrid = open();
  await hybrid.initialize();
  const remote = await hybrid.callTool("wiki_search", { query: "espresso", hybrid: true, ollamaUrl: "http://203.0.113.9:11434", timeout: 1 });
  assert.equal(remote.result.structuredContent.ok, true, JSON.stringify(remote.result));
  assert.ok(!fs.existsSync(netLog), "hybrid search never contacts a non-loopback endpoint");
  await hybrid.close();
  unchanged("hybrid attempt writes nothing");

  // Relative config vault values resolve against the server's cwd.
  const relativeCwd = path.join(tmp, "relative cwd");
  fs.mkdirSync(path.join(relativeCwd, "rel-vault/wiki"), { recursive: true });
  const relativeConfig = path.join(tmp, "relative.json");
  fs.writeFileSync(relativeConfig, JSON.stringify({ vaultPath: "rel-vault" }));
  const relative = open({ cwd: relativeCwd, env: { OBSIDIAN_AGENT_CONFIG: relativeConfig } });
  await relative.initialize();
  assert.equal((await relative.callTool("wiki_doctor", {})).result.structuredContent.payload.vault, fs.realpathSync(path.join(relativeCwd, "rel-vault")));
  await relative.close();

  // Profiles: read is the default; all is refused until #63; nothing is written to stdout on refusal.
  for (const [flags, message] of [[["--tools", "all"], /--tools all is not available yet/], [["--tools=all"], /--tools all is not available yet/],
    [["--tools", "write"], /unknown --tools profile/], [["--bogus"], /unsupported arguments/], [["--tools"], /unsupported arguments/]]) {
    const refused = spawnSync(process.execPath, [launcher, "mcp", ...flags], { cwd, encoding: "utf8", timeout: 5000, env: { ...process.env, ...baseEnv } });
    assert.equal(refused.status, 2, flags.join(" "));
    assert.equal(refused.stdout, "");
    assert.match(refused.stderr, message);
  }
  for (const flags of [["--tools", "read"], ["--tools=read"]]) {
    const explicit = open({ args: ["mcp", ...flags] });
    assert.deepEqual((await explicit.request("tools/list")).result.tools.map((tool) => tool.name), READ_TOOLS);
    await explicit.close();
  }

  // Deadline overrun, cancellation and EOF with an unresponsive Python process.
  const shim = path.join(tmp, "python shim");
  const pidFile = path.join(tmp, "descendant.pid");
  fs.writeFileSync(shim, `#!/bin/sh\ncase "$MODE" in\nhang) sh -c 'trap "" TERM; echo $$ > "${pidFile}"; while :; do sleep 1; done' & wait ;;\nlarge) head -c 300000 /dev/zero | tr '\\000' x ;;\nesac\n`);
  fs.chmodSync(shim, 0o755);
  const hang = (extra = {}) => ({ PYTHON: shim, MODE: "hang", ...extra });

  const slow = open({ env: hang({ OSB_PI_TOOL_DEADLINE_MS: "400" }) });
  await slow.initialize();
  const overrun = await slow.callTool("wiki_search", { query: "espresso" });
  assert.equal(overrun.result.isError, true);
  assert.equal(overrun.result.structuredContent.code, "timeout");
  assert.deepEqual(overrun.result.structuredContent.operation, "search");
  await waitForFile(pidFile);
  await assertGroupGone(processGroup(Number(fs.readFileSync(pidFile, "utf8"))), "deadline-overrun Python/descendant");
  assert.equal((await slow.request("ping")).result !== undefined, true, "server survives a deadline overrun");
  await slow.close();
  fs.rmSync(pidFile);

  const oversized = open({ env: hang({ MODE: "large" }) });
  await oversized.initialize();
  assert.equal((await oversized.callTool("wiki_search", { query: "espresso" })).result.structuredContent.code, "output-limit");
  await oversized.close();

  const cancelling = open({ env: hang() });
  await cancelling.initialize();
  cancelling.send({ jsonrpc: "2.0", id: 50, method: "tools/call", params: { name: "wiki_search", arguments: { query: "cancel-marker" } } });
  await waitForFile(pidFile);
  const cancelledGroup = processGroup(Number(fs.readFileSync(pidFile, "utf8")));
  cancelling.notify("notifications/cancelled", { requestId: 50 });
  await assertGroupGone(cancelledGroup, "cancelled Python/descendant");
  assert.deepEqual((await cancelling.request("ping")).result, {});
  assert.ok(!cancelling.messages.some((message) => message.id === 50), "a cancelled call is not answered");
  await cancelling.close();
  fs.rmSync(pidFile);

  const marker = `eof-marker-${process.pid}-${Date.now()}`;
  const eof = open({ env: hang() });
  await eof.initialize();
  eof.send({ jsonrpc: "2.0", id: 60, method: "tools/call", params: { name: "wiki_search", arguments: { query: marker } } });
  await waitForFile(pidFile);
  const eofGroup = processGroup(Number(fs.readFileSync(pidFile, "utf8")));
  assert.ok(commandsMentioning(marker).length > 0, "the in-flight call is running before EOF");
  const exited = await Promise.race([eof.close(), new Promise((resolve) => setTimeout(() => resolve("still running"), 8000))]);
  assert.deepEqual(exited, { code: 0, signal: null }, "server exits on stdin EOF while a call is running");
  await assertGroupGone(eofGroup, "EOF Python/descendant");
  const leftovers = Date.now() + 3000;
  while (commandsMentioning(marker).length > 0 && Date.now() < leftovers) await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(commandsMentioning(marker), [], "no launcher or Python child remains after EOF");
  assert.ok(!eof.messages.some((message) => message.id === 60), "no response is written after EOF");
  console.log("MCP server PASS: handshake, version negotiation, four read tools with --pi-tool parity, protocol errors, read-only profile, deadline, cancellation and EOF cleanup.");
} finally {
  for (const client of clients) { try { client.child.kill("SIGKILL"); } catch { /* already exited */ } }
  for (const group of processGroups) {
    if (!liveGroupMembers(group)) continue;
    try { process.kill(-group, "SIGKILL"); } catch { /* already exited */ }
  }
  fs.rmSync(tmp, { recursive: true, force: true });
}
