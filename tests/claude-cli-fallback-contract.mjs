import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
function exists(command) {
  try { execFileSync("which", [command], { stdio: "ignore" }); return true; } catch { return false; }
}

if (!exists("claude")) {
  console.log("UNVERIFIED: Claude Code CLI unavailable; package-relative CLI skill runtime was not run.");
  process.exit(0);
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-claude-cli-fallback-"));
const home = path.join(tmp, "home");
const caller = path.join(tmp, "caller");
const vault = path.join(tmp, "vault");
const config = path.join(tmp, "properties.json");
fs.mkdirSync(path.join(home, ".claude"), { recursive: true });
fs.mkdirSync(caller, { recursive: true });
fs.mkdirSync(path.join(vault, "wiki"), { recursive: true });
fs.writeFileSync(path.join(vault, "wiki", "coffee.md"), "# Coffee\n\nEspresso notes.\n");
fs.writeFileSync(config, JSON.stringify({ vaultPath: vault }));

const scenarios = [
  { skill: "wiki-health", command: "doctor --json", type: "health" },
  { skill: "wiki-query", command: 'search "espresso" --json', type: "query" },
];
let active = null;
let requestCount = 0;
const seenReports = new Map();
const contextFor = (body) => {
  const system = typeof body.system === "string" ? body.system : JSON.stringify(body.system ?? "");
  return `${system}\n${JSON.stringify(body.messages ?? [])}`;
};
const toolResultText = (body) => (body.messages ?? [])
  .flatMap((message) => Array.isArray(message.content) ? message.content : [])
  .filter((part) => part.type === "tool_result")
  .map((part) => typeof part.content === "string" ? part.content : JSON.stringify(part.content))
  .join("\n");
const shellQuote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
const sendEvents = (res, events) => {
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
  for (const [event, data] of events) res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  res.end();
};
const server = createServer((req, res) => {
  const chunks = [];
  req.on("data", (chunk) => chunks.push(chunk));
  req.on("end", () => {
    if (req.method !== "POST") {
      res.writeHead(404).end();
      return;
    }
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    requestCount += 1;
    const events = [
      ["message_start", {
        type: "message_start",
        message: { id: `cli_msg_${requestCount}`, type: "message", role: "assistant", model: body.model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 10, output_tokens: 1 } },
      }],
    ];
    if (requestCount === 1) {
      assert.ok((body.tools ?? []).some((tool) => tool.name === "Bash"), "normal Claude runtime exposes Bash for the dispatched skill");
      const context = contextFor(body);
      const instruction = active.type === "health" ? "Summarize its reported config" : "For an existing-knowledge question";
      assert.ok(context.includes(instruction), `Claude did not expand ${active.skill}: ${context.slice(-12000)}`);
      const baseMatch = context.match(/Base directory for this skill:\s*(\/[^\s"\\]+)/);
      assert.ok(baseMatch, `Claude did not supply the loaded skill base directory: ${context.slice(-12000)}`);
      const skillDirectory = path.resolve(baseMatch[1]);
      assert.equal(skillDirectory, path.join(root, "skills", active.skill), "path must be for the active --plugin-dir package skill");
      const packageRoot = path.resolve(skillDirectory, "../..");
      assert.equal(packageRoot, root, "package root must derive from the actual loaded skill directory");
      const launcherCommand = [
        `SKILL_DIR=${shellQuote(skillDirectory)}`,
        'PACKAGE_ROOT="$(cd -P -- "$SKILL_DIR/../.." && pwd)"',
        `node "$PACKAGE_ROOT/scripts/obsidian-second-brain.mjs" ${active.command} --vault ${shellQuote(vault)} --config ${shellQuote(config)}`,
      ].join("; ");
      assert.ok(!launcherCommand.includes("CLAUDE_PLUGIN_ROOT"), "launcher path is independent of plugin-root environment variable");
      const shellCommand = [
        'test -z "${CLAUDE_PLUGIN_ROOT:-}" || { echo "CLAUDE_PLUGIN_ROOT unexpectedly exported to Bash" >&2; exit 97; }',
        launcherCommand,
      ].join("; ");
      const input = JSON.stringify({ command: shellCommand, description: `read-only ${active.type} via loaded package skill path`, timeout: 30_000 });
      events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "tool_use", id: `toolu_${active.skill}`, name: "Bash", input: {} } }]);
      events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: input } }]);
      events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
      events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "tool_use", stop_sequence: null }, usage: { output_tokens: 10 } }]);
    } else {
      const output = toolResultText(body);
      assert.notEqual(output, "", "Claude Bash tool must actually return the package CLI result");
      const jsonLine = output.split(/\r?\n/).find((line) => line.trimStart().startsWith("{"));
      assert.ok(jsonLine, `package CLI output was not returned through Bash: ${output}`);
      const report = JSON.parse(jsonLine);
      assert.equal(report.vault, fs.realpathSync(vault));
      if (active.type === "health") {
        assert.equal(report.config.valid, true, output);
        assert.equal(report.retrieval.indexExists, false, "doctor did not build an index");
      } else {
        assert.equal(report.query, "espresso");
        assert.equal(report.fallback, "navigation");
        assert.deepEqual(report.results, []);
      }
      seenReports.set(active.skill, report);
      events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: `Completed read-only ${active.type} using the active package.` } }]);
      events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: `Completed read-only ${active.type} using the active package.` } }]);
      events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
      events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 10 } }]);
    }
    events.push(["message_stop", { type: "message_stop" }]);
    sendEvents(res, events);
  });
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
const env = {
  ...process.env,
  HOME: home,
  CLAUDE_CONFIG_DIR: path.join(home, ".claude"),
  OBSIDIAN_AGENT_CONFIG: config,
  OBSIDIAN_VAULT_PATH: "",
  ANTHROPIC_BASE_URL: `http://127.0.0.1:${address.port}`,
  ANTHROPIC_API_KEY: "sk-ant-deterministic-test-only",
  DISABLE_AUTOUPDATER: "1",
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
};
delete env.CLAUDE_PLUGIN_ROOT;
assert.equal(env.CLAUDE_PLUGIN_ROOT, undefined);
const initialFiles = () => [...fs.readdirSync(path.join(vault, "wiki"))].sort();
const cliArgs = (skill) => [
  "-p", "--output-format", "json", "--model", "claude-3-5-haiku-20241022",
  "--permission-mode", "bypassPermissions", "--allowedTools", "Bash",
  "--no-session-persistence", "--plugin-dir", root,
  `/obsidian-second-brain:${skill.skill} Run the read-only ${skill.type} workflow now.`,
];
try {
  for (const scenario of scenarios) {
    active = scenario;
    requestCount = 0;
    const before = initialFiles();
    const child = spawn("claude", cliArgs(scenario), { cwd: caller, env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
    child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
    const status = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error(`Claude ${scenario.skill} runtime timed out`)); }, 90_000);
      child.on("error", (error) => { clearTimeout(timer); reject(error); });
      child.on("close", (code) => { clearTimeout(timer); resolve(code); });
    });
    assert.equal(status, 0, `Claude ${scenario.skill} runtime failed: ${stderr}\n${stdout}`);
    assert.ok(requestCount >= 2 && requestCount <= 3, `bounded ${scenario.skill} provider requests: ${requestCount}`);
    assert.ok(stdout.includes(`Completed read-only ${scenario.type}`), `Claude did not complete ${scenario.type}: ${stdout}`);
    assert.deepEqual(initialFiles(), before, `${scenario.skill} must not write to the vault`);
    assert.equal(fs.existsSync(path.join(vault, ".vault-meta")), false, `${scenario.skill} must not create cache/lifecycle state`);
  }
  assert.ok(seenReports.has("wiki-health") && seenReports.has("wiki-query"));
  console.log(`Claude CLI skill fallback PASS: real health/query skill dispatch and Bash execution with CLAUDE_PLUGIN_ROOT unset; ${scenarios.length} isolated local-provider sessions; no paid prompt.`);
} finally {
  await new Promise((resolve) => server.close(resolve));
  fs.rmSync(tmp, { recursive: true, force: true });
}
