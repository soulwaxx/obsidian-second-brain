import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { startMcp } from "./mcp-client.mjs";

// Contract for the MCP server the Claude plugin bundles (#62). The optional argument is a plugin
// root to check instead of this checkout, e.g. an archive-installed package. Everything runs with
// temporary settings, a synthetic vault, and a loopback model provider; no real profile, vault,
// network service, or paid prompt is used.
const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pluginRoot = fs.realpathSync(path.resolve(process.argv[2] ?? source));
const READ_TOOLS = ["wiki_search", "wiki_outline", "wiki_read", "wiki_doctor"];
const SERVER = "obsidian-wiki";
const plugin = JSON.parse(fs.readFileSync(path.join(pluginRoot, ".claude-plugin/plugin.json"), "utf8"));

// 1. Declaration: plugin-root path, read profile, no npx, no PATH bin, no write profile.
assert.deepEqual(Object.keys(plugin.mcpServers ?? {}), [SERVER], "the plugin declares exactly one MCP server");
const declared = plugin.mcpServers[SERVER];
assert.deepEqual(Object.keys(declared).sort(), ["args", "command"], "no env, cwd, or other launch overrides");
assert.equal(declared.command, "node", "launched by node, not by npx or a PATH bin");
assert.deepEqual(declared.args, ["${CLAUDE_PLUGIN_ROOT}/scripts/obsidian-second-brain.mjs", "mcp", "--tools", "read"]);
assert.doesNotMatch(JSON.stringify(declared), /npx|npm exec|@soulwaxx/, "no network-capable package launcher");
assert.ok(!fs.existsSync(path.join(pluginRoot, ".mcp.json")), "a second declaration file could override the reviewed one");
const launcher = declared.args[0].replace("${CLAUDE_PLUGIN_ROOT}", pluginRoot);
assert.ok(fs.statSync(launcher).isFile(), "the declared launcher ships in the plugin root");

const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "claude-mcp-")));
try {
  const home = path.join(tmp, "home");
  const caller = path.join(tmp, "caller");
  const vault = path.join(tmp, "vault");
  const config = path.join(tmp, "properties.json");
  fs.mkdirSync(path.join(vault, "wiki"), { recursive: true });
  fs.mkdirSync(path.join(home, ".claude"), { recursive: true });
  fs.mkdirSync(caller);
  fs.writeFileSync(config, JSON.stringify({ vaultPath: vault }));
  const env = { HOME: home, XDG_CONFIG_HOME: path.join(home, ".config"), OBSIDIAN_AGENT_CONFIG: config };
  delete process.env.OBSIDIAN_VAULT_PATH;

  // 2. The declared command line, resolved against the plugin root, starts a read-only server from any cwd.
  const client = startMcp({ launcher, args: declared.args.slice(1), cwd: caller, env });
  try {
    const init = await client.initialize();
    assert.match(init.result.instructions, /Configured Obsidian wiki:/);
    assert.match(init.result.instructions, /read-only|never change the vault/i);
    // Claude's only wiki write path is its hook-guarded file tools, so the read profile must not forbid them.
    assert.doesNotMatch(init.result.instructions, /never edit|do not edit|file or shell tools|your own (file|shell)/i, "instructions do not forbid file tools");
    const tools = (await client.request("tools/list")).result.tools;
    assert.deepEqual(tools.map((tool) => tool.name), READ_TOOLS);
    assert.ok(tools.every((tool) => tool.annotations.readOnlyHint === true), "no write tool is exposed");
  } finally {
    assert.equal((await client.close()).code, 0, "the declared server exits on stdin EOF");
  }
  console.log(`Claude plugin MCP declaration PASS (${pluginRoot}): plugin-root node launch, read profile, four tools.`);

  // 3. Real Claude Code loads the plugin, connects the server, lists the tools, and routes one call.
  let hasClaude = true;
  try { execFileSync("which", ["claude"], { stdio: "ignore" }); } catch { hasClaude = false; }
  if (hasClaude) {
    const qualified = (name) => `mcp__plugin_obsidian-second-brain_${SERVER}__${name}`;
    let requests = 0;
    let sawToolList = false;
    let toolResult = "";
    let subagentTools = null;
    const provider = createServer((req, res) => {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        const raw = Buffer.concat(chunks).toString("utf8");
        if (req.method !== "POST" || !raw) { res.writeHead(404, { "content-type": "application/json" }); res.end("{}"); return; }
        const body = JSON.parse(raw);
        requests += 1;
        const names = (body.tools ?? []).map((tool) => tool.name);
        if (READ_TOOLS.every((name) => names.includes(qualified(name)))) sawToolList = true;
        const results = (body.messages ?? []).flatMap((m) => Array.isArray(m.content) ? m.content : []).filter((part) => part.type === "tool_result");
        if (results.length) toolResult = JSON.stringify(results[0].content);
        const isSubagent = !names.includes("Agent") && requests > 1;
        if (isSubagent) subagentTools = names;
        const events = [["message_start", { type: "message_start", message: { id: `msg_${requests}`, type: "message", role: "assistant", model: body.model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 10, output_tokens: 1 } } }]];
        const call = requests === 1 ? ["toolu_wiki_doctor", qualified("wiki_doctor"), {}]
          : results.length === 1 && !isSubagent ? ["toolu_vault_agent", "Agent", { description: "wiki vault", prompt: "Reply with one word.", subagent_type: "obsidian-second-brain:wiki-vault", run_in_background: false }] : null;
        if (call) {
          events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "tool_use", id: call[0], name: call[1], input: {} } }]);
          events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: JSON.stringify(call[2]) } }]);
          events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
          events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "tool_use", stop_sequence: null }, usage: { output_tokens: 5 } }]);
        } else {
          events.push(["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: "" }}]);
          events.push(["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "done" } }]);
          events.push(["content_block_stop", { type: "content_block_stop", index: 0 }]);
          events.push(["message_delta", { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 1 } }]);
        }
        events.push(["message_stop", { type: "message_stop" }]);
        res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
        for (const [event, data] of events) res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
        res.end();
      });
    });
    await new Promise((resolve) => provider.listen(0, "127.0.0.1", resolve));
    try {
      const args = ["-p", "--output-format", "stream-json", "--verbose", "--model", "claude-3-5-haiku-20241022",
        "--permission-mode", "bypassPermissions", "--no-session-persistence", "--plugin-dir", pluginRoot, "Run wiki_doctor and report."];
      const child = spawn("claude", args, { cwd: caller, stdio: ["ignore", "pipe", "pipe"], env: {
        ...process.env, ...env, CLAUDE_CONFIG_DIR: path.join(home, ".claude"),
        ANTHROPIC_BASE_URL: `http://127.0.0.1:${provider.address().port}`, ANTHROPIC_API_KEY: "sk-ant-deterministic-test-only",
        DISABLE_AUTOUPDATER: "1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1" } });
      let stdout = "";
      let stderr = "";
      child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
      child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
      const status = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("Claude MCP fixture timed out")); }, 90_000);
        child.on("error", (error) => { clearTimeout(timer); reject(error); });
        child.on("close", (code) => { clearTimeout(timer); resolve(code); });
      });
      assert.equal(status, 0, `Claude exited unsuccessfully: ${stderr}\n${stdout}`);
      const init = stdout.split("\n").filter(Boolean).map((line) => JSON.parse(line)).find((event) => event.type === "system" && event.subtype === "init");
      assert.ok(init, "Claude emitted its init event");
      assert.deepEqual(init.mcp_servers.map(({ name, status: state, source: origin }) => ({ name, state, origin })),
        [{ name: `plugin:obsidian-second-brain:${SERVER}`, state: "connected", origin: "plugin" }]);
      assert.deepEqual(init.tools.filter((name) => name.startsWith("mcp__")).sort(), READ_TOOLS.map(qualified).sort(), "Claude lists exactly the four read tools");
      assert.ok(sawToolList, "the model request carried the four plugin MCP tools");
      assert.ok(subagentTools, "the packaged wiki-vault subagent ran");
      assert.deepEqual(subagentTools.filter((name) => name.startsWith("mcp__")).sort(), READ_TOOLS.map(qualified).sort(), "the vault specialist's tool allowlist includes the four read tools");
      assert.ok(toolResult.includes(vault) || toolResult.includes(fs.realpathSync(vault)), `the routed wiki_doctor call used the synthetic vault: ${toolResult}`);
      console.log(`Claude plugin MCP runtime PASS: server connected, four read tools listed, wiki_doctor routed (${requests} local-provider requests; no paid prompt).`);
    } finally {
      await new Promise((resolve) => provider.close(resolve));
    }
  } else console.log("UNVERIFIED: Claude Code CLI unavailable; live plugin MCP discovery was not run.");
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
