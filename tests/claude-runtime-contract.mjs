import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { createServer } from "node:http";

function exists(command) {
  try { execFileSync("which", [command], { stdio: "ignore" }); return true; } catch { return false; }
}

if (!exists("claude")) {
  console.log("UNVERIFIED: Claude Code CLI unavailable; real Claude hook dispatch contract was not run.");
  process.exit(0);
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-claude-runtime-"));
const vault = path.join(tmp, "vault");
const caller = path.join(tmp, "caller");
const home = path.join(tmp, "home");
fs.mkdirSync(path.join(vault, "wiki", "topic"), { recursive: true });
fs.mkdirSync(caller, { recursive: true });
fs.mkdirSync(home, { recursive: true });
const config = path.join(tmp, "properties.json");
fs.writeFileSync(config, JSON.stringify({ vaultPath: vault, features: { guard: true } }));
const hook = path.resolve("hooks/obsidian-session.sh");
const settings = path.join(tmp, "settings.json");
fs.writeFileSync(settings, JSON.stringify({ hooks: {
  PreToolUse: [{ matcher: "Write", hooks: [{ type: "command", command: `bash "${hook}" prewrite`, timeout: 15 }] }],
  PostToolUse: [{ matcher: "Write", hooks: [{ type: "command", command: `bash "${hook}" postwrite`, timeout: 20 }] }],
  Stop: [{ matcher: "", hooks: [{ type: "command", command: `bash "${hook}" stop`, timeout: 30 }] }],
} }));
let requests = 0;
let toolDiagnosticObserved = false;
const server = createServer((req, res) => {
  const chunks = [];
  req.on("data", (chunk) => chunks.push(chunk));
  req.on("end", () => {
    const rawBody = Buffer.concat(chunks).toString("utf8");
    if (req.method !== "POST" || !rawBody) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { type: "not_found_error", message: `${req.method} ${req.url}` } }));
      return;
    }
    const body = JSON.parse(rawBody);
    requests += 1;
    const messages = body.messages ?? [];
    if (messages.some((message) => message.role === "user" && Array.isArray(message.content)
      && message.content.some((part) => part.type === "tool_result" && /not finalized|retained for repair/i.test(part.content ?? "")))) {
      toolDiagnosticObserved = true;
    }
    const events = [];
    events.push(["message_start", {
      type: "message_start",
      message: { id: `msg_${requests}`, type: "message", role: "assistant", model: body.model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 10, output_tokens: 1 } },
    }]);
    if (requests === 1) {
      const args = JSON.stringify({ file_path: path.join(vault, "wiki/topic/invalid.md"), content: "# Invalid page missing YAML frontmatter\n" });
      events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "tool_use", id: "toolu_m1_invalid", name: "Write", input: {} } }]);
      events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: args } }]);
      events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
      events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "tool_use", stop_sequence: null }, usage: { output_tokens: 10 } }]);
    } else {
      events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: "The page remains invalid and repairable; I will not claim it was finalized." } }]);
      events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "The page remains invalid and repairable; I will not claim it was finalized." } }]);
      events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
      events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 10 } }]);
    }
    events.push(["message_stop", { type: "message_stop" }]);
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
    for (const [event, data] of events) res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    res.end();
  });
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
const env = { ...process.env, HOME: home, CLAUDE_CONFIG_DIR: path.join(home, ".claude"), OBSIDIAN_AGENT_CONFIG: config, OBSIDIAN_VAULT_PATH: vault, WIKI_MIDDLEWARE_DIR: path.resolve("skills/wiki/scripts/okf_mw"), ANTHROPIC_BASE_URL: `http://127.0.0.1:${address.port}`, ANTHROPIC_API_KEY: "sk-ant-deterministic-test-only", DISABLE_AUTOUPDATER: "1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1" };
const args = ["-p", "--output-format", "json", "--model", "claude-3-5-haiku-20241022", "--permission-mode", "bypassPermissions", "--allowedTools", "Write", "--no-session-persistence", "--settings", settings, "Write an invalid wiki page at the requested path, then report any validation failure."];
try {
  const child = spawn("claude", args, { cwd: caller, env, stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
  child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
  const status = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("Claude runtime fixture timed out")); }, 90_000);
    child.on("error", (error) => { clearTimeout(timer); reject(error); });
    child.on("close", (code) => { clearTimeout(timer); resolve(code); });
  });
  assert.equal(status, 0, `Claude runtime exited unsuccessfully: ${stderr}\n${stdout}`);
  const written = path.join(vault, "wiki/topic/invalid.md");
  assert.equal(fs.readFileSync(written, "utf8"), "# Invalid page missing YAML frontmatter\n", "Claude's actual Write tool bytes remain repairable");
  const lifecycleState = JSON.parse(fs.readFileSync(path.join(vault, ".vault-meta/lifecycle/state.json"), "utf8"));
  assert.ok(Object.values(lifecycleState.pending).some((entry) => Object.keys(entry.captures ?? {}).some((owner) => owner.startsWith("claude:"))), "failed Stop retains owner-scoped pending lifecycle data");
  assert.ok(toolDiagnosticObserved, "Claude model received the actual PostToolUse additionalContext failure");
  assert.ok(requests >= 2 && requests <= 4, `bounded Claude provider request count, got ${requests}`);
  assert.match(stdout, /invalid|repair|finaliz/i, "Claude's final model-visible report does not claim success");
  console.log(`Claude installed-runtime contract PASS: actual Write/PostToolUse/Stop dispatch; ${requests} deterministic local-provider requests; no paid prompt.`);
} finally {
  await new Promise((resolve) => server.close(resolve));
  fs.rmSync(tmp, { recursive: true, force: true });
}
