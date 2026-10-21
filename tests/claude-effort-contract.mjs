import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { createServer } from "node:http";

const cli = process.env.CLAUDE_EFFORT_TEST_CLI ?? "claude";
let version;
try {
  version = execFileSync(cli, ["--version"], { encoding: "utf8", timeout: 10_000 }).trim();
} catch {
  console.log("UNVERIFIED: Claude Code CLI unavailable; native effort dispatch contract was not run.");
  process.exit(0);
}
const versionMatch = version.match(/(?:^|\s)(\d+)\.(\d+)\.(\d+)(?:\s|$)/);
if (!versionMatch || Number(versionMatch[1]) < 2 || (Number(versionMatch[1]) === 2 && Number(versionMatch[2]) < 1)
  || (Number(versionMatch[1]) === 2 && Number(versionMatch[2]) === 1 && Number(versionMatch[3]) < 292)) {
  console.log(`UNVERIFIED: Claude Code ${version} is older than 2.1.292; native per-invocation effort dispatch was not run.`);
  process.exit(0);
}

const root = process.cwd();
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-claude-effort-"));
const home = path.join(tmp, "home");
const caller = path.join(tmp, "caller");
const vault = path.join(tmp, "vault");
fs.mkdirSync(home, { recursive: true });
fs.mkdirSync(caller, { recursive: true });
fs.mkdirSync(path.join(vault, "wiki"), { recursive: true });
const observed = [];
let observedTools = [];
let fixtureAgentEffort;
let delegationSent = false;
let fixtureFailure;
let activeChild;
let requestNumber = 0;
const server = createServer((req, res) => {
  const chunks = [];
  req.on("data", (chunk) => chunks.push(chunk));
  req.on("end", () => {
    if (req.method !== "POST") { res.writeHead(404).end(); return; }
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    requestNumber += 1;
    if (requestNumber > 6) {
      fixtureFailure = `provider request ceiling exceeded; tools=${(body.tools ?? []).map((tool) => tool.name).join(",")}`;
      res.writeHead(500).end(JSON.stringify({ error: { type: "api_error", message: fixtureFailure } }));
      activeChild?.kill("SIGTERM");
      return;
    }
    observed.push(body);
    observedTools = (body.tools ?? []).map((tool) => tool.name);
    const agentTool = (body.tools ?? []).find((tool) => ["Agent", "Task"].includes(tool.name));
    const events = [["message_start", { type: "message_start", message: { id: `msg_${requestNumber}`, type: "message", role: "assistant", model: body.model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 10, output_tokens: 1 } } }]];
    if (agentTool && !delegationSent) {
      delegationSent = true;
      const agentInput = { subagent_type: "obsidian-second-brain:wiki-vault", description: "Effort contract fixture", prompt: "Read the configured wiki contract and report its title." };
      if (fixtureAgentEffort === "low") agentInput.effort = "low";
      events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "tool_use", id: "toolu_effort_fixture", name: agentTool.name, input: {} } }]);
      events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: JSON.stringify(agentInput) } }]);
      events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
      events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "tool_use", stop_sequence: null }, usage: { output_tokens: 10 } }]);
    } else {
      events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } }]);
      events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Fixture complete." } }]);
      events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
      events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 10 } }]);
    }
    events.push(["message_stop", { type: "message_stop" }]);
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
    for (const [event, data] of events) res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    res.end();
  });
});

const cleanEnv = () => ({
  PATH: process.env.PATH ?? "/usr/bin:/bin",
  HOME: home,
  CLAUDE_CONFIG_DIR: path.join(home, ".claude"),
  TMPDIR: tmp,
  LANG: "C.UTF-8",
  ANTHROPIC_BASE_URL: `http://127.0.0.1:${server.address().port}`,
  ANTHROPIC_API_KEY: "sk-ant-deterministic-test-only",
  DISABLE_AUTOUPDATER: "1",
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
  OBSIDIAN_VAULT_PATH: vault,
});

async function runScenario({ label, agentEffort, envConflict = false, expectedChildEffort }) {
  observed.length = 0;
  observedTools = [];
  fixtureAgentEffort = agentEffort;
  delegationSent = false;
  fixtureFailure = undefined;
  requestNumber = 0;
  const env = cleanEnv();
  if (envConflict) env.CLAUDE_CODE_EFFORT_LEVEL = "high";
  const args = ["-p", "--output-format", "json", "--model", "claude-sonnet-4-6", "--permission-mode", "bypassPermissions", "--no-session-persistence", "--plugin-dir", root, "--effort", "high", "/obsidian-second-brain:wiki-agent delegate the request to the packaged wiki-vault agent and report its result."];
  const child = spawn(cli, args, { cwd: caller, env, stdio: ["ignore", "pipe", "pipe"] });
  activeChild = child;
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
  child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
  const status = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error(`${label}: Claude runtime fixture timed out; requests=${observed.length}; tools=${observedTools.join(",")}; stderr=${stderr}\nstdout=${stdout}`)); }, 20_000);
    child.on("error", (error) => { clearTimeout(timer); reject(error); });
    child.on("close", (code) => { clearTimeout(timer); resolve(code); });
  });
  activeChild = undefined;
  assert.equal(status, 0, `${label}: ${fixtureFailure ?? `Claude exited unsuccessfully: ${stderr}\n${stdout}`}`);
  assert.ok(observed.length >= 3 && observed.length <= 6, `${label}: expected bounded parent/child dispatch, got ${observed.length} requests`);
  const parentRequests = observed.filter((request) => !JSON.stringify(request.system).includes("cc_is_subagent=true"));
  const childRequests = observed.filter((request) => JSON.stringify(request.system).includes("cc_is_subagent=true"));
  assert.ok(parentRequests.length >= 2, `${label}: parent made requests before and after child dispatch`);
  assert.ok(parentRequests.every((request) => request.output_config?.effort === "high"), `${label}: parent remains high before and after child dispatch`);
  assert.ok(childRequests.length >= 1, `${label}: packaged child made a provider request`);
  assert.ok(childRequests.every((request) => request.output_config?.effort === expectedChildEffort), `${label}: child requests use expected native effort`);
  assert.match(JSON.stringify(childRequests[0].system), /vault specialist/i, `${label}: packaged wiki-vault agent identity reached child request`);
  assert.match(JSON.stringify(childRequests[0].system), /Resolve the configured vault independently/i, `${label}: packaged wiki-vault agent body reached child request`);
  assert.match(stdout, /Fixture complete/);
  console.log(`PASS ${label}: parent high; child effort ${expectedChildEffort}; actual packaged agent via isolated local provider.`);
}

try {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  await runScenario({ label: "explicit low", agentEffort: "low", expectedChildEffort: "low" });
  await runScenario({ label: "inherited effort", expectedChildEffort: "high" });
  await runScenario({ label: "environment override", agentEffort: "low", envConflict: true, expectedChildEffort: "high" });
  console.log("Claude native effort contract PASS: scripted fake-provider requests verify runtime effort dispatch only; they do not test model compliance or live-provider acceptance.");
} finally {
  await new Promise((resolve) => server.close(resolve));
  fs.rmSync(tmp, { recursive: true, force: true });
}
