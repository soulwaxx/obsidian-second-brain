import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

export const defaultLauncher = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../scripts/obsidian-second-brain.mjs");

// Scripted stdio MCP client: one JSON-RPC message per line, as the MCP stdio transport defines.
export function startMcp({ launcher = defaultLauncher, args = ["mcp"], cwd, env = {} } = {}) {
  const child = spawn(process.execPath, [launcher, ...args], { cwd, env: { ...process.env, ...env }, stdio: ["pipe", "pipe", "pipe"] });
  const messages = [];
  const rawLines = [];
  let stderr = "";
  let buffer = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.stdout.on("data", (chunk) => {
    buffer += chunk;
    for (let end = buffer.indexOf("\n"); end !== -1; end = buffer.indexOf("\n")) {
      const line = buffer.slice(0, end);
      buffer = buffer.slice(end + 1);
      rawLines.push(line);
      try { messages.push(JSON.parse(line)); } catch { messages.push({ unparsable: line }); }
    }
  });
  const closed = new Promise((resolve) => child.once("close", (code, signal) => resolve({ code, signal })));
  const send = (message) => child.stdin.write(`${typeof message === "string" ? message : JSON.stringify(message)}\n`);
  const waitFor = async (predicate, timeoutMs = 20_000) => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const found = messages.find(predicate);
      if (found) return found;
      if (Date.now() > deadline) throw new Error(`timed out waiting for an MCP message; got ${JSON.stringify(messages)}; stderr: ${stderr}`);
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  };
  let nextId = 1;
  const request = async (method, params, timeoutMs) => {
    const id = nextId++;
    send({ jsonrpc: "2.0", id, method, ...(params === undefined ? {} : { params }) });
    return await waitFor((message) => message.id === id, timeoutMs);
  };
  const notify = (method, params) => send({ jsonrpc: "2.0", method, ...(params === undefined ? {} : { params }) });
  const initialize = async (protocolVersion = "2025-06-18") => {
    const response = await request("initialize", { protocolVersion, capabilities: {}, clientInfo: { name: "test", version: "0" } });
    notify("notifications/initialized");
    return response;
  };
  const callTool = (name, args) => request("tools/call", { name, arguments: args });
  const close = async () => { child.stdin.end(); return await closed; };
  return { child, messages, rawLines, send, request, notify, initialize, callTool, waitFor, close, closed, stderr: () => stderr };
}
