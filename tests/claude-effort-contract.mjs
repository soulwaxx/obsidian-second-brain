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
const selectedConfig = path.join(tmp, "selected-properties.json");
const wrongVault = path.join(tmp, "default-vault");
const shellQuote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
const snapshot = (directory) => Object.fromEntries(fs.readdirSync(directory, { recursive: true })
  .map((relative) => [relative, path.join(directory, relative)])
  .filter(([, file]) => fs.statSync(file).isFile())
  .map(([relative, file]) => [relative, [fs.readFileSync(file).toString("base64"), fs.statSync(file).mtimeMs]]));
const observed = [];
let observedTools = [];
let fixtureAgentEffort;
let agentSchemaHasEffort;
let fixtureWrites = false;
let fixtureResolve = false;
let doctorSent = false;
let writeSent = false;
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
      // Inject effort only when the client advertises it; Claude Code drops the
      // property from the Agent schema while CLAUDE_CODE_EFFORT_LEVEL is set.
      agentSchemaHasEffort = Object.hasOwn(agentTool.input_schema?.properties ?? {}, "effort");
      const agentInput = { subagent_type: "obsidian-second-brain:wiki-vault", run_in_background: false, description: "Effort contract fixture", prompt: fixtureWrites ? "Create wiki/foreground-write.md with valid note frontmatter." : "Read the configured wiki contract and report its title." };
      if (fixtureAgentEffort === "low" && agentSchemaHasEffort) agentInput.effort = "low";
      events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "tool_use", id: "toolu_effort_fixture", name: agentTool.name, input: {} } }]);
      events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: JSON.stringify(agentInput) } }]);
      events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
      events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "tool_use", stop_sequence: null }, usage: { output_tokens: 10 } }]);
    } else if (fixtureWrites && !writeSent && JSON.stringify(body.system).includes("cc_is_subagent=true")) {
      writeSent = true;
      assert.ok(observedTools.includes("Write"), "foreground specialist has its packaged Write tool");
      const input = { file_path: path.join(vault, "wiki", "foreground-write.md"), content: "---\ntype: note\ntitle: Foreground Write\n---\n# Foreground Write\n" };
      events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "tool_use", id: "toolu_foreground_write", name: "Write", input: {} } }]);
      events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: JSON.stringify(input) } }]);
      events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
      events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "tool_use", stop_sequence: null }, usage: { output_tokens: 10 } }]);
    } else if (fixtureResolve && !doctorSent && JSON.stringify(body.system).includes("cc_is_subagent=true")) {
      doctorSent = true;
      assert.ok(observedTools.includes("Bash"), "specialist has its packaged Bash tool");
      const input = { command: `node ${shellQuote(path.join(root, "scripts", "obsidian-second-brain.mjs"))} doctor --json`, description: "Verify override-aware vault selection", timeout: 10_000 };
      events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "tool_use", id: "toolu_specialist_doctor", name: "Bash", input: {} } }]);
      events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: JSON.stringify(input) } }]);
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

async function runScenario({ label, agentEffort, envConflict = false, expectedChildEffort, writePage = false, resolveSelection = false }) {
  observed.length = 0;
  observedTools = [];
  fixtureAgentEffort = agentEffort;
  agentSchemaHasEffort = undefined;
  fixtureWrites = writePage;
  fixtureResolve = resolveSelection;
  doctorSent = false;
  writeSent = false;
  delegationSent = false;
  fixtureFailure = undefined;
  requestNumber = 0;
  const env = cleanEnv();
  let beforeSelection;
  if (resolveSelection) {
    fs.mkdirSync(path.join(wrongVault, "wiki"), { recursive: true });
    fs.writeFileSync(path.join(wrongVault, "wiki", "sentinel.md"), "Default vault must remain untouched.\n");
    const defaultConfig = path.join(home, ".config", "obsidian-second-brain", "properties.json");
    fs.mkdirSync(path.dirname(defaultConfig), { recursive: true });
    fs.writeFileSync(defaultConfig, JSON.stringify({ vaultPath: wrongVault }));
    fs.writeFileSync(selectedConfig, JSON.stringify({ vaultPath: vault }));
    delete env.OBSIDIAN_VAULT_PATH;
    env.OBSIDIAN_AGENT_CONFIG = selectedConfig;
    beforeSelection = [snapshot(vault), snapshot(wrongVault)];
  }
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
  assert.match(JSON.stringify(childRequests[0].system), /OBSIDIAN_AGENT_CONFIG/, `${label}: child receives the override-aware selection contract`);
  assert.match(JSON.stringify(childRequests[0].system), /doctor --json/, `${label}: child receives resolver verification instructions`);
  assert.match(stdout, /Fixture complete/);
  assert.equal(agentSchemaHasEffort, !envConflict, `${label}: Agent tool schema ${envConflict ? "omits" : "includes"} effort`);
  const delegated = parentRequests.flatMap((request) => request.messages ?? [])
    .flatMap((message) => Array.isArray(message.content) ? message.content : [])
    .find((part) => part.type === "tool_use" && part.id === "toolu_effort_fixture");
  assert.ok(delegated, `${label}: parent echoed the Agent tool_use`);
  assert.equal(delegated.input.effort, agentEffort && !envConflict ? agentEffort : undefined, `${label}: Agent input effort`);
  if (envConflict) {
    assert.match(JSON.stringify(parentRequests[0].messages), /printenv CLAUDE_CODE_EFFORT_LEVEL/, `${label}: skill's environment-override branch reached the parent`);
  }
  if (writePage) {
    assert.ok(writeSent, "scripted foreground specialist dispatched Write");
    assert.ok(fs.existsSync(path.join(vault, "wiki", "foreground-write.md")), "specialist wrote the page");
    assert.match(fs.readFileSync(path.join(vault, "wiki", "index.md"), "utf8"), /foreground-write\.md/);
    assert.match(fs.readFileSync(path.join(vault, "wiki", "log.md"), "utf8"), /Creation.*foreground-write\.md/);
    const state = JSON.parse(fs.readFileSync(path.join(vault, ".vault-meta", "lifecycle", "state.json"), "utf8"));
    assert.deepEqual(state.pending, {}, "native hooks finalize the foreground child without manual recovery");
  }
  if (resolveSelection) {
    assert.ok(doctorSent, "native specialist dispatched the read-only resolver");
    const output = childRequests.flatMap((request) => request.messages ?? [])
      .flatMap((message) => Array.isArray(message.content) ? message.content : [])
      .filter((part) => part.type === "tool_result")
      .map((part) => typeof part.content === "string" ? part.content : JSON.stringify(part.content))
      .join("\n");
    const reportLine = output.split(/\r?\n/).find((line) => line.trimStart().startsWith("{"));
    assert.ok(reportLine, `specialist must receive the actual doctor report: ${output}`);
    const report = JSON.parse(reportLine);
    assert.equal(report.vault, fs.realpathSync(vault));
    assert.equal(report.config.path, selectedConfig);
    assert.equal(report.config.valid, true);
    assert.deepEqual([snapshot(vault), snapshot(wrongVault)], beforeSelection, "selection leaves both vaults byte/mtime-identical");
  }
  console.log(`PASS ${label}: parent high; Agent schema effort ${agentSchemaHasEffort ? "present" : "absent"}; Agent input effort ${delegated.input.effort ?? "omitted"}; child effort ${expectedChildEffort}; actual packaged agent via isolated local provider.`);
}

try {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  await runScenario({ label: "explicit low", agentEffort: "low", expectedChildEffort: "low" });
  await runScenario({ label: "inherited effort", expectedChildEffort: "high" });
  await runScenario({ label: "environment override CLAUDE_CODE_EFFORT_LEVEL=high with saved low launches without effort", agentEffort: "low", envConflict: true, expectedChildEffort: "high" });
  await runScenario({ label: "foreground specialist write and hook finalization", agentEffort: "low", expectedChildEffort: "low", writePage: true });
  await runScenario({ label: "specialist resolves config override instead of conflicting default", expectedChildEffort: "high", resolveSelection: true });
  console.log("Claude native effort contract PASS: scripted fake-provider requests verify runtime effort dispatch only; they do not test model compliance or live-provider acceptance.");
} finally {
  await new Promise((resolve) => server.close(resolve));
  fs.rmSync(tmp, { recursive: true, force: true });
}
