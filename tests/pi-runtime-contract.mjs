import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { pathToFileURL } from "node:url";
import { installedPackage, resolveRuntimeDependencies } from "./pi-runtime-paths.mjs";

const packageRoot = installedPackage();
if (!packageRoot) {
  const diagnostic = "Pi SDK/runtime unavailable; set PI_CODING_AGENT_PACKAGE to run the real-runtime contract.";
  if (process.argv.includes("--require-runtime")) throw new Error(diagnostic);
  console.log(`UNVERIFIED: ${diagnostic}`);
  process.exit(0);
}

const sdkUrl = pathToFileURL(path.join(packageRoot, "dist/index.js")).href;
const [aiUrl, typeboxUrl] = resolveRuntimeDependencies(packageRoot);
const { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } = await import(sdkUrl);
const { createAssistantMessageEventStream } = await import(aiUrl);
const { Check: checkSchema } = await import(new URL("./value/index.mjs", typeboxUrl));
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-pi-runtime-"));
function processGroup(pid) {
  if (!pid) return null;
  const result = spawnSync("ps", ["-o", "pgid=", "-p", String(pid)], { encoding: "utf8" });
  return Number(result.stdout.trim()) || null;
}
function liveGroupMembers(group) {
  if (!group || process.platform === "win32") return false;
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
  assert.equal(liveGroupMembers(group), false, `${label} exited before tool completion`);
}
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
    } else if (scenario === "native-wiki-tools") {
      if (requests === 1) {
        const call = { type: "toolCall", id: "m1-native-check", name: "m1_native_wiki_check", arguments: {} };
        message.content.push(call);
        stream.push({ type: "toolcall_start", contentIndex: 0, partial: message });
        stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: call, partial: message });
        message.stopReason = "toolUse";
        stream.push({ type: "done", reason: "toolUse", message });
      } else {
        const results = context.messages.filter((entry) => entry.role === "toolResult");
        const check = results.find((entry) => entry.toolName === "m1_native_wiki_check");
        assert.ok(check, "model-called deterministic helper executes native tool checks");
        assert.match(JSON.stringify(check), /all four native wiki tools/i);
        message.content.push({ type: "text", text: "All four read-only native wiki tools returned structured results." });
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
  const { Type } = await import(typeboxUrl);
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
  const nativeVault = path.join(tmp, "native tools vault");
  fs.mkdirSync(path.join(nativeVault, "wiki"), { recursive: true });
  const nativePage = path.join(nativeVault, "wiki/native-tool.md");
  const nativeSource = "---\ntype: note\n---\n# Synthetic native wiki\nA local fixture.\n\n## Details\nSecond section.\n";
  fs.writeFileSync(nativePage, nativeSource);
  process.env.PI_NATIVE_TEST_HASH = createHash("sha256").update(nativeSource).digest("hex");
  process.env.OBSIDIAN_VAULT_PATH = "";
  fs.writeFileSync(config, JSON.stringify({ vaultPath: nativeVault, features: { guard: true } }));
  execFileSync("python3", [path.resolve("scripts/bm25-index.py"), "build", "--vault", nativeVault], { stdio: "ignore" });
  const pendingState = path.join(nativeVault, ".vault-meta/lifecycle/state.json");
  fs.mkdirSync(path.dirname(pendingState), { recursive: true });
  const captureRoot = path.join(nativeVault, ".raw/agent-captures");
  fs.mkdirSync(captureRoot, { recursive: true });
  fs.writeFileSync(path.join(captureRoot, "pending-fixture.txt"), "Synthetic pending capture bytes.\n");
  fs.writeFileSync(path.join(captureRoot, "pending-fixture.md"), "Synthetic pending capture record.\n");
  fs.writeFileSync(pendingState, JSON.stringify({ version: 1, vault: nativeVault, pending: { "wiki/pending-native.md": { changed: true }, ".raw/agent-captures/pending-fixture.md": { changed: true } } }));
  const nativeSnapshot = () => Object.fromEntries([...fs.readdirSync(nativeVault, { recursive: true })].map((entry) => path.join(nativeVault, entry)).filter((entry) => fs.statSync(entry).isFile()).map((entry) => [path.relative(nativeVault, entry), [fs.readFileSync(entry).toString("base64"), fs.statSync(entry, { bigint: true }).mtimeNs.toString()]]).concat([["config", [fs.readFileSync(config).toString("base64"), fs.statSync(config, { bigint: true }).mtimeNs.toString()]]]));
  const beforeNativeTools = nativeSnapshot();
  let nativeChecksPassed = false;
  const nativeCheck = (pi) => {
    pi.registerTool({
    name: "m1_native_wiki_check", label: "Native wiki check",
    description: "Deterministic runtime contract for native read-only wiki tools.",
    parameters: Type.Object({}),
    execute: async (_id, _params, _signal, _update, ctx) => {
      const names = ["wiki_search", "wiki_outline", "wiki_read", "wiki_doctor"];
      const info = pi.getAllTools().filter((tool) => names.includes(tool.name));
      assert.equal(info.length, 4, "Pi discovers exactly the four extension-native tools");
      assert.ok(info.every((tool) => tool.exposure === "direct"), "native tools default to direct model exposure");
      assert.ok(info.every((tool) => tool.annotations?.readOnlyHint === true && tool.annotations?.destructiveHint === false && tool.annotations?.idempotentHint === true && tool.annotations?.openWorldHint === false));
      assert.ok(names.every((name) => pi.getActiveTools().includes(name)), "native tools are default active without replacing other selections");
      const definitions = nativeResources.getExtensions().extensions.flatMap((extension) => [...extension.tools.values()].map((tool) => tool.definition));
      for (const name of names) {
        assert.ok(ctx.tools.some((entry) => entry.name === name), `${name} is directly discoverable and callable in the installed Pi runtime`);
        const tool = definitions.find((entry) => entry.name === name);
        assert.equal(tool.annotations.readOnlyHint, true);
        assert.equal(tool.annotations.destructiveHint, false);
        assert.equal(tool.annotations.idempotentHint, true);
        assert.equal(tool.annotations.openWorldHint, false);
        assert.ok(tool.outputSchema?.properties?.operation, `${name} declares the shared result envelope schema`);
      }
      const readTool = info.find((entry) => entry.name === "wiki_read");
      assert.match(readTool.description, /section.*(?:ID|id).*wiki_outline/);
      const readProperties = readTool.parameters.properties;
      assert.match(readProperties.section.description, /entry.*id.*wiki_outline/);
      assert.match(readProperties.section.description, /metadata.*preamble.*heading:<offset>/);
      assert.match(readProperties.section.description, /Not a heading title/);
      assert.match(readProperties.section.description, /Mutually exclusive with start\/end/);
      assert.match(readProperties.expectedHash.description, /sourceHash.*wiki_outline.*wiki_search.*readRef/);
      for (const name of ["start", "end"]) {
        assert.match(readProperties[name].description, /source character/);
        assert.match(readProperties[name].description, /wiki_search.*readRef/);
        assert.match(readProperties[name].description, /Mutually exclusive with section/);
      }
      assert.match(readProperties.start.description, /[Zz]ero-based/);
      assert.match(readProperties.end.description, /[Ee]xclusive/);
      const checkResult = (outcome, operation) => {
        const value = outcome.result.structuredContent;
        assert.equal(value.operation, operation);
        assert.equal(typeof value.ok, "boolean");
        const toolSchema = definitions.find((entry) => entry.name === `wiki_${operation}`).outputSchema;
        assert.equal(checkSchema(toolSchema, value), true, `${operation} structured result matches its declared output schema`);
        return value;
      };
      const search = checkResult(await ctx.executeTool("wiki_search", { query: "synthetic native wiki" }), "search");
      assert.equal(search.ok, true);
      assert.equal(search.payload.results[0].path, "wiki/native-tool.md");
      const outlineFirst = checkResult(await ctx.executeTool("wiki_outline", { page: "wiki/native-tool.md", limit: 1 }), "outline");
      assert.equal(outlineFirst.ok, true);
      assert.equal(outlineFirst.payload.nextOffset, 1);
      assert.equal(outlineFirst.payload.continuation.expectedHash, process.env.PI_NATIVE_TEST_HASH);
      const outlineNext = checkResult(await ctx.executeTool("wiki_outline", { page: "wiki/native-tool.md", limit: 1, offset: outlineFirst.payload.nextOffset, expectedHash: outlineFirst.payload.continuation.expectedHash }), "outline");
      assert.equal(outlineNext.ok, true);
      const section = outlineNext.payload.entries.find((entry) => entry.kind === "heading");
      assert.ok(section);
      const sectionRead = checkResult(await ctx.executeTool("wiki_read", { page: "wiki/native-tool.md", expectedHash: process.env.PI_NATIVE_TEST_HASH, section: section.id }), "read");
      assert.equal(sectionRead.ok, true);
      assert.match(sectionRead.payload.text, /Synthetic native wiki/);
      const readStart = nativeSource.indexOf("Synthetic native wiki");
      const readEnd = nativeSource.length;
      const partial = checkResult(await ctx.executeTool("wiki_read", { page: "wiki/native-tool.md", expectedHash: process.env.PI_NATIVE_TEST_HASH, start: readStart, end: readEnd, maxChars: 12 }), "read");
      assert.equal(partial.ok, true);
      assert.ok(partial.payload.continuation);
      const continued = checkResult(await ctx.executeTool("wiki_read", { page: "wiki/native-tool.md", expectedHash: partial.payload.continuation.expectedHash, start: readStart, end: readEnd, cursor: partial.payload.continuation.cursor, maxChars: 80 }), "read");
      assert.equal(continued.ok, true);
      assert.ok(continued.payload.returned.start >= partial.payload.returned.end);
      const stale = await ctx.executeTool("wiki_outline", { page: "wiki/native-tool.md", expectedHash: "0".repeat(64), offset: 1 });
      assert.equal(stale.isError, true);
      assert.equal(stale.result.structuredContent.code, "source-changed");
      const extra = await ctx.executeTool("wiki_doctor", { rawArgs: ["doctor"] });
      assert.equal(extra.isError, true, "closed-world parameter schema rejects passthrough fields");
      const doctor = checkResult(await ctx.executeTool("wiki_doctor", {}), "doctor");
      assert.equal(doctor.ok, true);
      assert.equal(doctor.payload.vault, fs.realpathSync(nativeVault));

      const fakePython = path.join(tmp, "term-ignoring-python.sh");
      const launcherPidFile = path.join(tmp, "cancel-launcher.pid");
      const descendantPidFile = path.join(tmp, "cancel-descendant.pid");
      fs.writeFileSync(fakePython, `#!/bin/sh\nprintf '%s\\n' "$PPID" > '${launcherPidFile}'\nsh -c 'trap "" TERM; echo $$ > "${descendantPidFile}"; while :; do sleep 1; done' &\nwait\n`);
      fs.chmodSync(fakePython, 0o755);
      const previousPython = process.env.PYTHON;
      process.env.PYTHON = fakePython;
      let cancelPromise;
      let activeController;
      let launcherCancelGroup;
      let descendantCancelGroup;
      const controller = new AbortController();
      try {
        controller.abort();
        const preAborted = await ctx.executeTool("wiki_search", { query: "synthetic native wiki" }, { signal: controller.signal });
        assert.equal(preAborted.isError, true);
        assert.match(JSON.stringify(preAborted.result), /aborted/i, "Pi may cancel before the tool can return a structured envelope");
        assert.equal(fs.existsSync(launcherPidFile), false, "pre-aborted call does not spawn the launcher/Python");

        activeController = new AbortController();
        cancelPromise = ctx.executeTool("wiki_search", { query: "synthetic native wiki" }, { signal: activeController.signal });
        const startDeadline = Date.now() + 3000;
        while (!fs.existsSync(descendantPidFile) && Date.now() < startDeadline) await new Promise((resolve) => setTimeout(resolve, 10));
        assert.ok(fs.existsSync(descendantPidFile), "registered tool started TERM-ignoring Python descendant");
        const launcherPid = Number(fs.readFileSync(launcherPidFile, "utf8"));
        const descendantPid = Number(fs.readFileSync(descendantPidFile, "utf8"));
        launcherCancelGroup = processGroup(launcherPid);
        descendantCancelGroup = processGroup(descendantPid);
        activeController.abort();
        const cancelled = await cancelPromise;
        assert.equal(cancelled.isError, true);
        assert.match(JSON.stringify(cancelled.result), /aborted/i, "Pi may replace the tool envelope with its own cancellation result");
        await assertGroupGone(launcherCancelGroup, "extension-launched node launcher");
        await assertGroupGone(descendantCancelGroup, "extension-launched Python descendant/process group");
      } finally {
        activeController?.abort();
        for (const group of [launcherCancelGroup, descendantCancelGroup]) {
          if (group && process.platform !== "win32" && liveGroupMembers(group)) {
            try { process.kill(-group, "SIGKILL"); } catch { /* already exited */ }
          }
        }
        if (cancelPromise) await cancelPromise.catch(() => undefined);
        if (previousPython === undefined) delete process.env.PYTHON; else process.env.PYTHON = previousPython;
      }
      nativeChecksPassed = true;
      return { content: [{ type: "text", text: "All four native wiki tools passed structured-result, schema, hash, continuation, and cancellation checks." }], details: undefined };
    },
  });
  };
  const nativeResources = new DefaultResourceLoader({ cwd: nativeVault, agentDir: path.join(home, ".pi", "agent"), additionalExtensionPaths: [path.resolve("extensions/obsidian.ts")], extensionFactories: [nativeCheck] });
  await nativeResources.reload();
  assert.deepEqual(nativeResources.getExtensions().errors, []);
  const { session: nativeSession } = await createAgentSession({ cwd: nativeVault, agentDir: path.join(home, ".pi", "agent"), model, modelRuntime, resourceLoader: nativeResources, sessionManager: SessionManager.inMemory(nativeVault), settingsManager: SettingsManager.inMemory({ retry: { enabled: false } }) });
  try {
    await nativeSession.bindExtensions({});
    scenario = "native-wiki-tools";
    requests = 0;
    await nativeSession.prompt("Run the deterministic native wiki tools contract.");
    assert.equal(nativeChecksPassed, true, `native wiki helper must finish all checks without a tool error: ${JSON.stringify(nativeSession.messages.filter((entry) => entry.role === "toolResult"))}`);
    assert.equal(requests, 2, "native-tool calls complete in one tool turn and one deterministic response");
    assert.deepEqual(nativeSnapshot(), beforeNativeTools, "startup, native reads, pending freshness, and doctor leave vault/config bytes and mtimes unchanged");
  } finally {
    nativeSession.dispose();
  }
  console.log("Pi installed-SDK runtime contract PASS: lifecycle regressions plus all four native read-only wiki tools and structured outputs.");
} finally {
  session.dispose();
  fs.rmSync(tmp, { recursive: true, force: true });
}
