import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { execFileSync } from "node:child_process";
import { pathToFileURL } from "node:url";

function installedPackage() {
  if (process.env.PI_CODING_AGENT_PACKAGE) return path.resolve(process.env.PI_CODING_AGENT_PACKAGE);
  try {
    const cli = fs.realpathSync(execFileSync("which", ["pi"], { encoding: "utf8" }).trim());
    for (let dir = path.dirname(cli); dir !== path.dirname(dir); dir = path.dirname(dir)) {
      if (fs.existsSync(path.join(dir, "package.json")) && JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8")).name === "@earendil-works/pi-coding-agent") return dir;
    }
  } catch { /* Pi is optional for this package. */ }
  return null;
}

const packageRoot = installedPackage();
if (!packageRoot) {
  console.log("UNVERIFIED: installed Pi SDK/runtime unavailable; set PI_CODING_AGENT_PACKAGE to run the real-runtime contract.");
  process.exit(0);
}

const sdkUrl = pathToFileURL(path.join(packageRoot, "dist/index.js")).href;
const aiEventStreamPath = path.join(packageRoot, "node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js");
const { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } = await import(sdkUrl);
const { createAssistantMessageEventStream } = await import(pathToFileURL(aiEventStreamPath).href);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-pi-runtime-"));
const vault = path.join(tmp, "vault");
const home = path.join(tmp, "home");
fs.mkdirSync(path.join(vault, "wiki", "runtime"), { recursive: true });
fs.mkdirSync(home, { recursive: true });
const config = path.join(tmp, "properties.json");
fs.writeFileSync(config, JSON.stringify({ vaultPath: vault, features: { guard: true } }));
process.env.HOME = home;
process.env.OBSIDIAN_AGENT_CONFIG = config;
process.env.OBSIDIAN_VAULT_PATH = "";
process.env.WIKI_MIDDLEWARE_DIR = path.resolve("skills/wiki/scripts/okf_mw");

let requests = 0;
let scenario = "invalid";
let siblingRequests = 0;
let expectedLocator = "";
let locatorRequests = 0;
let nestedRequests = 0;
const modelRuntime = await ModelRuntime.create({ authPath: path.join(tmp, "auth.json"), modelsPath: null, refreshOnCreate: false });
modelRuntime.registerProvider("m1-stub", {
  api: "m1-stub-api",
  baseUrl: "http://127.0.0.1/stub",
  apiKey: "deterministic-test-only",
  models: [{ id: "stub", name: "M1 deterministic stub", api: "m1-stub-api", input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, reasoning: false, contextWindow: 4096, maxTokens: 256 }],
  streamSimple(model, context) {
    const stream = createAssistantMessageEventStream();
    requests += 1;
    const message = { role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }, stopReason: "toolUse", timestamp: Date.now() };
    stream.push({ type: "start", partial: message });
    if (scenario === "nested-repair-read") {
      nestedRequests += 1;
      if (nestedRequests === 1) {
        const call = { type: "toolCall", id: "m1-nested-repair-read", name: "m1_repair_read", arguments: {} };
        message.content.push(call);
        stream.push({ type: "toolcall_start", contentIndex: 0, partial: message });
        stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: call, partial: message });
        message.stopReason = "toolUse";
        stream.push({ type: "done", reason: "toolUse", message });
      } else {
        assert.doesNotMatch(JSON.stringify(context.messages), /Action required before considering wiki work complete/,
          "successful nested navigation finalization suppresses obsolete settle diagnostics");
        message.content.push({ type: "text", text: "The repaired page was published and the invalidation was resolved." });
        message.stopReason = "stop";
        stream.push({ type: "text_start", contentIndex: 0, partial: message });
        stream.push({ type: "text_delta", contentIndex: 0, delta: message.content[0].text, partial: message });
        stream.push({ type: "text_end", contentIndex: 0, content: message.content[0].text, partial: message });
        stream.push({ type: "done", reason: "stop", message });
      }
    } else if (scenario === "locator") {
      locatorRequests += 1;
      assert.ok(JSON.stringify(context.messages).includes(expectedLocator), `current selected-vault locator reaches the same session model context: ${expectedLocator}`);
      message.content.push({ type: "text", text: "I received the current wiki locator." });
      message.stopReason = "stop";
      stream.push({ type: "text_start", contentIndex: 0, partial: message });
      stream.push({ type: "text_delta", contentIndex: 0, delta: message.content[0].text, partial: message });
      stream.push({ type: "text_end", contentIndex: 0, content: message.content[0].text, partial: message });
      stream.push({ type: "done", reason: "stop", message });
    } else if (scenario === "siblings") {
      siblingRequests += 1;
      if (siblingRequests <= 2) {
        const page = siblingRequests === 1 ? "sibling-one.md" : "sibling-two.md";
        const title = siblingRequests === 1 ? "Sibling One" : "Sibling Two";
        const call = { type: "toolCall", id: `m1-${page}`, name: "write", arguments: { path: `wiki/runtime/${page}`, content: `---\ntype: note\ntitle: ${title}\n---\n# ${title}\n` } };
        message.content.push(call);
        stream.push({ type: "toolcall_start", contentIndex: 0, partial: message });
        stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: call, partial: message });
        message.stopReason = "toolUse";
        stream.push({ type: "done", reason: "toolUse", message });
      } else {
        assert.match(JSON.stringify(context.messages), /Action required before considering wiki work complete|validation/i,
          "unresolved invalid batch diagnostic remains in context after valid sibling writes");
        message.content.push({ type: "text", text: "The invalid page is still retained; sibling writes did not clear its repair budget." });
        message.stopReason = "stop";
        stream.push({ type: "text_start", contentIndex: 0, partial: message });
        stream.push({ type: "text_delta", contentIndex: 0, delta: message.content[0].text, partial: message });
        stream.push({ type: "text_end", contentIndex: 0, content: message.content[0].text, partial: message });
        stream.push({ type: "done", reason: "stop", message });
      }
    } else if (requests === 1) {
      const call = { type: "toolCall", id: "m1-invalid-write", name: "write", arguments: { path: "wiki/runtime/invalid.md", content: "# Invalid page without required type\n" } };
      message.content.push(call);
      stream.push({ type: "toolcall_start", contentIndex: 0, partial: message });
      stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: call, partial: message });
      message.stopReason = "toolUse";
      stream.push({ type: "done", reason: "toolUse", message });
    } else {
      const toolResult = context.messages.find((entry) => entry.role === "toolResult");
      const text = toolResult?.content?.map((part) => part.text ?? "").join(" ") ?? "";
      const modelText = context.messages.filter((entry) => entry.role === "assistant").flatMap((entry) => entry.content ?? []).map((part) => part.text ?? "").join(" ");
      assert.match(`${text} ${modelText}`, /validation\/sync failed|validation\/finalization failed|Action required before considering wiki work complete/i, "Pi runtime sends lifecycle diagnostic to the model");
      assert.match(`${text} ${modelText}`, /bytes were retained/i, "model-visible failure states retained repairable bytes");
      message.content.push({ type: "text", text: "I saw the validation failure; the page remains repairable and is not finalized." });
      message.stopReason = "stop";
      stream.push({ type: "text_start", contentIndex: 0, partial: message });
      stream.push({ type: "text_delta", contentIndex: 0, delta: message.content[0].text, partial: message });
      stream.push({ type: "text_end", contentIndex: 0, content: message.content[0].text, partial: message });
      stream.push({ type: "done", reason: "stop", message });
    }
    stream.end();
    return stream;
  },
});
const model = modelRuntime.getModel("m1-stub", "stub");
assert.ok(model);
const resourceLoader = new DefaultResourceLoader({
  cwd: tmp,
  agentDir: path.join(home, ".pi", "agent"),
  additionalExtensionPaths: [path.resolve("extensions/obsidian.ts")],
});
await resourceLoader.reload();
const { session } = await createAgentSession({
  cwd: vault,
  agentDir: path.join(home, ".pi", "agent"),
  model,
  modelRuntime,
  resourceLoader,
  sessionManager: SessionManager.inMemory(vault),
  settingsManager: SettingsManager.inMemory({ retry: { enabled: false } }),
  tools: ["write"],
});
try {
  await session.bindExtensions({});
  await session.prompt("Create a wiki page at wiki/runtime/invalid.md with the supplied content.");
  const page = path.join(vault, "wiki/runtime/invalid.md");
  assert.equal(fs.readFileSync(page, "utf8"), "# Invalid page without required type\n", "failed validation preserves the model-written bytes");
  assert.equal(requests, 3, "one deterministic tool call, one actionable settle continuation, and one bounded final model turn");
  const replies = session.messages.filter((message) => message.role === "assistant");
  assert.ok(replies.some((message) => message.content.some((part) => part.type === "text" && /not finalized/.test(part.text))), "runtime completed with a model response after the validation error");

  scenario = "siblings";
  siblingRequests = 0;
  await session.prompt("Also create two valid sibling wiki pages; leave the earlier invalid page unchanged.");
  assert.equal(siblingRequests, 3, "two successful sibling tool calls do not reset the unresolved batch continuation budget");
  assert.equal(fs.readFileSync(page, "utf8"), "# Invalid page without required type\n", "valid siblings do not overwrite invalid page bytes");
  for (const name of ["sibling-one.md", "sibling-two.md"]) {
    assert.match(fs.readFileSync(path.join(vault, "wiki/runtime", name), "utf8"), /^---\ntype: note/m);
  }

  const otherVault = path.join(tmp, "other-vault");
  fs.mkdirSync(path.join(otherVault, "wiki"), { recursive: true });
  fs.writeFileSync(path.join(otherVault, "wiki", "B locator.md"), "---\ntype: note\ntitle: B Locator\n---\n# B locator\n");
  scenario = "locator";
  expectedLocator = otherVault;
  process.env.OBSIDIAN_VAULT_PATH = otherVault;
  await session.prompt("Read the selected vault locator after config changes to B.");
  expectedLocator = vault;
  process.env.OBSIDIAN_VAULT_PATH = vault;
  await session.prompt("Read the selected vault locator after config changes back to A.");
  assert.equal(locatorRequests, 2, "same real Pi session receives B then A locators after A-B-A reselection");

  const repairVault = path.join(tmp, "nested-repair-vault");
  fs.mkdirSync(path.join(repairVault, "wiki", "runtime"), { recursive: true });
  fs.writeFileSync(config, JSON.stringify({ vaultPath: repairVault, features: { guard: true } }));
  process.env.OBSIDIAN_VAULT_PATH = repairVault;
  const typeboxPath = path.join(packageRoot, "node_modules/typebox/build/index.mjs");
  const { Type } = await import(pathToFileURL(typeboxPath).href);
  const orchestrationExtension = (pi) => pi.registerTool({
    name: "m1_repair_read",
    label: "Repair and read wiki",
    description: "For isolated runtime regression: write a broken wiki page, repair it, then read generated navigation.",
    parameters: Type.Object({}),
    execute: async (_id, _params, _signal, _onUpdate, ctx) => {
      const invalid = await ctx.executeTool("write", { path: "wiki/runtime/repaired.md", content: "# Invalid fixture bytes\n" });
      assert.ok(invalid, "nested invalid write reached the actual Pi tool runtime");
      await ctx.executeTool("write", { path: "wiki/runtime/repaired.md", content: "---\ntype: note\ntitle: Repaired\n---\n# Repaired\n" });
      await ctx.executeTool("read", { path: "wiki/runtime/index.md" });
      return { content: [{ type: "text", text: "Completed nested invalid-write, repair-write, and navigation-read sequence." }], details: undefined };
    },
  });
  const repairResources = new DefaultResourceLoader({
    cwd: repairVault,
    agentDir: path.join(home, ".pi", "agent"),
    additionalExtensionPaths: [path.resolve("extensions/obsidian.ts")],
    extensionFactories: [orchestrationExtension],
  });
  await repairResources.reload();
  const { session: repairSession } = await createAgentSession({
    cwd: repairVault,
    agentDir: path.join(home, ".pi", "agent"),
    model,
    modelRuntime,
    resourceLoader: repairResources,
    sessionManager: SessionManager.inMemory(repairVault),
    settingsManager: SettingsManager.inMemory({ retry: { enabled: false } }),
  });
  try {
    await repairSession.bindExtensions({});
    scenario = "nested-repair-read";
    nestedRequests = 0;
    await repairSession.prompt("Use the repair-and-read regression tool.");
    assert.equal(nestedRequests, 2, `same-turn navigation recovery completes without an obsolete settle continuation; messages=${JSON.stringify(repairSession.messages)}`);
    assert.match(fs.readFileSync(path.join(repairVault, "wiki/runtime/index.md"), "utf8"), /\[Repaired\]/,
      "nested navigation read publishes the repaired page before the tool completes");
    assert.equal(fs.readFileSync(path.join(repairVault, "wiki/runtime/repaired.md"), "utf8"), "---\ntype: note\ntitle: Repaired\n---\n# Repaired\n");
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(repairVault, ".vault-meta/lifecycle/state.json"), "utf8")).pending, {},
      "nested successful navigation finalization clears all pending lifecycle state");
    assert.equal(JSON.stringify(repairSession.messages).includes("obsidian-finalization-diagnostic"), false,
      "no obsolete finalization diagnostic is appended after successful navigation repair");
  } finally {
    repairSession.dispose();
  }
  console.log("Pi installed-SDK runtime contract PASS: bounded invalid/sibling repair, same-session A-B-A context, and nested same-turn invalid→repair→index-read finalization.");
} finally {
  session.dispose();
  fs.rmSync(tmp, { recursive: true, force: true });
}
