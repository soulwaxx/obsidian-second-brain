import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fsMutable from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { installedPackage, resolveRuntimeDependencies } from "./pi-runtime-paths.mjs";
import { canonicalHash, createPiRunner } from "../experiments/knowledge-maintenance/pi-runner.mjs";

const packageRoot = installedPackage();
if (!packageRoot) {
  const message = "Pi SDK/runtime unavailable; set PI_CODING_AGENT_PACKAGE to run the installed-SDK knowledge-maintenance contract.";
  if (process.argv.includes("--require-runtime")) throw new Error(message);
  console.log(`UNVERIFIED: ${message}`);
  process.exit(0);
}
const sdkUrl = pathToFileURL(path.join(packageRoot, "dist/index.js")).href;
const [aiUrl] = resolveRuntimeDependencies(packageRoot);
const { ModelRuntime } = await import(sdkUrl);
const { AuthStorage } = await import(pathToFileURL(path.join(packageRoot, "dist/core/auth-storage.js")).href);
const { createAssistantMessageEventStream } = await import(aiUrl);
const ROOT = process.cwd();
const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "knowledge-maintenance-pi-")));
const runnerHome = path.join(tmp, "home");
fs.mkdirSync(runnerHome);
const sentinelRoots = [path.join(runnerHome, ".agents", "skills"), path.join(tmp, "valid-workspace", ".agents", "skills")];
for (const [index, root] of sentinelRoots.entries()) {
  const directory = path.join(root, `sentinel-${index}`);
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(directory, "package.json"), JSON.stringify({ name: `sentinel-${index}`, metadataMustNotBeRead: true }));
  fs.writeFileSync(path.join(directory, "SKILL.md"), `---\\nname: sentinel-${index}\\ndescription: SECRET_SENTINEL_SKILL_${index}\\n---\\nDo not load this skill.\\n`);
}
const forbiddenReads = [];
const originalReadFileSync = fsMutable.readFileSync;
fsMutable.readFileSync = function (file, ...args) {
  const target = path.resolve(file instanceof URL ? fileURLToPath(file) : String(file));
  if (sentinelRoots.some((root) => target === root || target.startsWith(`${root}${path.sep}`))) {
    forbiddenReads.push(target);
    throw new Error("sentinel-resource-read");
  }
  return originalReadFileSync.call(this, file, ...args);
};
syncBuiltinESMExports();
const priorHome = process.env.HOME;
const priorEnv = Object.fromEntries(["OBSIDIAN_AGENT_CONFIG", "OBSIDIAN_VAULT_PATH", "WIKI_MIDDLEWARE_DIR"].map((key) => [key, process.env[key]]));
process.env.HOME = runnerHome;

const hash = (text) => createHash("sha256").update(text).digest("hex");
let runtime;
let mode = "invalid-maintenance";
let requests = 0;
let deniedWikiOnlySource = false;
const providerContexts = [];
const validPage = "---\ntype: note\ntitle: Acorn retention\n---\n# Acorn retention\n\nAcorn retains requests for 30 days.\n";
const validPageHash = hash(validPage);

function textOf(message) {
  const content = message?.content;
  if (typeof content === "string") return content;
  return Array.isArray(content) ? content.map((part) => part.text ?? "").join("") : "";
}

function toolResultNames(messages) {
  return messages.filter((message) => message.role === "toolResult").map((message) => message.toolName);
}

function parseCurrentQuestion(messages) {
  const prompt = messages.filter((message) => message.role === "user").map(textOf).findLast((text) => text.includes("Question: ")) ?? "";
  return { prompt, question: prompt.match(/Question: ([^\n]+)/)?.[1] ?? "" };
}

function fakeResponse(model, context) {
  const stream = createAssistantMessageEventStream();
  requests += 1;
  const message = { role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
    usage: { input: 1, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 3, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    stopReason: "stop", timestamp: Date.now() };
  const calls = [];
  const emitCall = (name, arguments_, id = `${mode}-${requests}-${name}`) => calls.push({ type: "toolCall", id, name, arguments: arguments_ });
  const messages = context.messages ?? [];
  const current = parseCurrentQuestion(messages);
  const promptText = messages.filter((message) => message.role === "user").map(textOf).join("\n");
  const results = toolResultNames(messages);
  providerContexts.push({ mode, prompt: promptText, messages: structuredClone(messages), toolResults: results });

  if (mode === "invalid-maintenance" && promptText.includes("Chronological checkpoint")) {
    if (!results.length) {
      emitCall("write", { path: "wiki/index.md", content: "not allowed\n" }, "reserved-index");
      emitCall("write", { path: "wiki/log.md", content: "not allowed\n" }, "reserved-log");
      emitCall("write", { path: "wiki/invalid.md", content: "# Missing type frontmatter\n" }, "invalid-page");
      emitCall("write", { path: path.join(activeVault, "wiki", "absolute.md"), content: "no\n" }, "absolute-write");
      emitCall("write", { path: "wiki/../wiki/traversal.md", content: "no\n" }, "traversal-write");
    } else {
      message.content.push({ type: "text", text: "Invalid-page diagnostics were received." });
    }
  } else if (mode === "valid-maintenance" && promptText.includes("Chronological checkpoint")) {
    if (!results.includes("experiment_source_read")) emitCall("experiment_source_read", { path: "2025-01-10/sources/policy.md" });
    else if (!results.includes("write")) emitCall("write", { path: "wiki/retention.md", content: validPage });
    else message.content.push({ type: "text", text: "Maintenance complete." });
  } else if (mode === "native-sequence" && current.question) {
    if (!results.includes("wiki_search")) emitCall("wiki_search", { query: "Acorn retention", limit: 5 });
    else if (!results.includes("wiki_outline")) emitCall("wiki_outline", { page: "wiki/retention.md", limit: 10 });
    else if (!results.includes("wiki_read")) emitCall("wiki_read", { page: "wiki/retention.md", expectedHash: validPageHash, start: 0, end: [...validPage].length });
    else {
      assert.ok(JSON.stringify(messages).includes("wiki_search"));
      assert.ok(JSON.stringify(messages).includes("wiki_outline"));
      assert.ok(JSON.stringify(messages).includes("wiki_read"));
      assert.match(JSON.stringify(messages), /Acorn retains requests for 30 days/);
      message.content.push({ type: "text", text: "Acorn retains requests for 30 days. [wiki/retention.md]" });
    }
  } else if (mode === "native-denials" && current.question) {
    if (!results.length) {
      if (current.question.includes("retained")) emitCall("wiki_search", { query: "Acorn", vault: "/tmp/host-vault" });
      else if (current.question.includes("response target")) emitCall("wiki_search", { query: "Acorn", hybrid: true, ollamaUrl: "http://127.0.0.1:11434" });
      else emitCall("wiki_read", { page: "wiki/../secrets.md", expectedHash: "a".repeat(64), start: 0, end: 10 });
    } else message.content.push({ type: "text", text: "Denied native access did not expose data." });
  } else if (mode === "cancel" && !message._delayed) {
    message._delayed = true;
    stream.push({ type: "start", partial: message });
    setTimeout(() => {
      message.content.push({ type: "text", text: "late deterministic response" });
      message.stopReason = "stop";
      stream.push({ type: "text_start", contentIndex: 0, partial: message });
      stream.push({ type: "text_delta", contentIndex: 0, delta: "late deterministic response", partial: message });
      stream.push({ type: "text_end", contentIndex: 0, content: "late deterministic response", partial: message });
      stream.push({ type: "done", reason: "stop", message });
      stream.end();
    }, 120);
    return stream;
  } else if (mode === "full-matrix") {
    if (promptText.includes("Chronological checkpoint")) {
      assert.doesNotMatch(promptText, /What retention rule, if any|What is the response target|Which weekday is the maintenance window/);
      assert.doesNotMatch(promptText, /expectedAnswers|agent-authored development fixture; not human-reviewed/);
      message.content.push({ type: "text", text: "Maintenance phase complete." });
    } else {
      const checkpoint = promptText.match(/Checkpoint: (\d{4}-\d{2}-\d{2})/)?.[1];
      assert.ok(checkpoint);
      for (const later of allCheckpoints.filter((id) => id > checkpoint)) assert.ok(!promptText.includes(`${later}/sources/`), `future source ${later} leaked`);
      assert.doesNotMatch(promptText, /expectedAnswers|agent-authored development fixture; not human-reviewed/);
      if (promptText.includes("Declared knowledge availability: wiki-only") && !deniedWikiOnlySource) {
        deniedWikiOnlySource = true;
        emitCall("experiment_source_read", { path: `${checkpoint}/sources/policy.md` });
      } else {
        const condition = promptText.match(/Declared knowledge availability: ([^\.]+)/)?.[1] ?? "?";
        message.content.push({ type: "text", text: `Answer ${checkpoint} ${condition} ${current.question}` });
      }
    }
  } else {
    message.content.push({ type: "text", text: `Answer ${current.question}` });
  }

  if (calls.length) {
    message.content.push(...calls);
    message.stopReason = "toolUse";
  }
  stream.push({ type: "start", partial: message });
  if (message.stopReason === "toolUse") {
    calls.forEach((call, contentIndex) => {
      stream.push({ type: "toolcall_start", contentIndex, partial: message });
      stream.push({ type: "toolcall_end", contentIndex, toolCall: call, partial: message });
    });
    stream.push({ type: "done", reason: "toolUse", message });
  } else {
    const text = message.content.at(-1)?.text ?? "";
    stream.push({ type: "text_start", contentIndex: 0, partial: message });
    stream.push({ type: "text_delta", contentIndex: 0, delta: text, partial: message });
    stream.push({ type: "text_end", contentIndex: 0, content: text, partial: message });
    stream.push({ type: "done", reason: "stop", message });
  }
  stream.end();
  return stream;
}

let activeVault = "";
let allCheckpoints = [];
let createCount = 0;
async function makeRunner(runtime, model, label, options = {}) {
  const workspaceRoot = path.join(tmp, `${label}-workspace`);
  const vault = path.join(workspaceRoot, "vault");
  fs.mkdirSync(path.join(vault, "wiki"), { recursive: true });
  fs.mkdirSync(workspaceRoot, { recursive: true });
  if (label === "valid") {
    fs.mkdirSync(path.join(workspaceRoot, ".agents", "skills"), { recursive: true });
    fs.writeFileSync(path.join(workspaceRoot, ".agents", "skills", "sentinel-valid.md"), "SECRET_ANCESTOR_SKILL_MUST_NOT_BE_READ\n");
  }
  fs.writeFileSync(path.join(vault, ".knowledge-maintenance-synthetic"), "obsidian-second-brain synthetic knowledge-maintenance vault v1\n");
  execFileSync("git", ["init", "--quiet", vault], { cwd: tmp, env: process.env });
  fs.writeFileSync(path.join(vault, ".gitignore"), ".vault-meta/\n");
  activeVault = vault;
  createCount += 1;
  const runner = await createPiRunner({ vaultRoot: vault, workspaceRoot, modelRuntime: runtime, model, sdkPackage: packageRoot, maxOutputTokens: 256, ...options });
  return { runner, vault, workspaceRoot };
}

function buildBm25(vault) {
  const out = execFileSync("python3", [path.join(ROOT, "scripts/bm25-index.py"), "build", "--vault", vault], { cwd: tmp, encoding: "utf8", env: process.env });
  return out;
}

function canonicalSourceHashFromPython(sources) {
  const script = "import json,sys; sys.path.insert(0,'experiments/knowledge-maintenance'); import evaluation; print(evaluation.canonical_hash(json.load(sys.stdin)))";
  return execFileSync("python3", ["-c", script], { cwd: ROOT, input: JSON.stringify(sources), encoding: "utf8", env: process.env }).trim();
}

try {
  runtime = await ModelRuntime.create({ credentials: AuthStorage.inMemory(), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
  runtime.registerProvider("km-stub", {
    api: "km-stub-api", baseUrl: "http://127.0.0.1/never-called", apiKey: "offline-test-only",
    models: [{ id: "deterministic", name: "Deterministic test model", api: "km-stub-api", input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, reasoning: false, contextWindow: 16384, maxTokens: 256 }],
    streamSimple: fakeResponse,
  });
  const model = runtime.getModel("km-stub", "deterministic");
  assert.ok(model);
  const fixture = JSON.parse(fs.readFileSync(path.join(ROOT, "experiments/knowledge-maintenance/fixtures.json"), "utf8"));
  allCheckpoints = fixture.checkpoints.map((checkpoint) => checkpoint.id);

  // Keep negative write diagnostics in their own session; successful lifecycle is tested below.
  mode = "invalid-maintenance";
  let { runner, vault } = await makeRunner(runtime, model, "invalid");
  const envBefore = { OBSIDIAN_AGENT_CONFIG: process.env.OBSIDIAN_AGENT_CONFIG, OBSIDIAN_VAULT_PATH: process.env.OBSIDIAN_VAULT_PATH, WIKI_MIDDLEWARE_DIR: process.env.WIKI_MIDDLEWARE_DIR };
  const invalid = await runner.maintain("2025-01-10");
  assert.equal(fs.existsSync(path.join(vault, "wiki/index.md")), false, "model cannot write generated index");
  assert.equal(fs.existsSync(path.join(vault, "wiki/log.md")), false, "model cannot write lifecycle log");
  assert.equal(fs.readFileSync(path.join(vault, "wiki/invalid.md"), "utf8"), "# Missing type frontmatter\n");
  assert.match(JSON.stringify(invalid.result.messages), /frontmatter/);
  assert.match(JSON.stringify(invalid.result.messages), /Action required before considering wiki work complete/);
  assert.deepEqual(invalid.unauthorizedWriteAttempts, ["write", "write", "host-path", "out-of-scope"]);
  assert.deepEqual(invalid.accessDenials, []);
  assert.deepEqual({ OBSIDIAN_AGENT_CONFIG: process.env.OBSIDIAN_AGENT_CONFIG, OBSIDIAN_VAULT_PATH: process.env.OBSIDIAN_VAULT_PATH, WIKI_MIDDLEWARE_DIR: process.env.WIKI_MIDDLEWARE_DIR }, envBefore);

  // Actual SDK maintenance reads source, writes one valid page through the shipped lifecycle,
  // and proves that validation/finalization generates index/log before explicit host BM25.
  mode = "valid-maintenance";
  ({ runner, vault } = await makeRunner(runtime, model, "valid"));
  const maintained = await runner.maintain("2025-01-10");
  assert.match(JSON.stringify(maintained.result.messages), /The fictional Acorn service keeps requests for 30 days/);
  assert.equal(fs.readFileSync(path.join(vault, "wiki/retention.md"), "utf8"), validPage);
  assert.match(fs.readFileSync(path.join(vault, "wiki/index.md"), "utf8"), /retention/);
  assert.ok(fs.readFileSync(path.join(vault, "wiki/log.md"), "utf8").trim().length > 0, "actual lifecycle generated the hook-owned log");
  assert.equal(fs.existsSync(path.join(vault, ".vault-meta/retrieval")), false, "lifecycle retrieval refresh is disabled until host preparation");
  buildBm25(vault);
  const prepared = runner.wikiHash();
  assert.equal(prepared, canonicalHash(maintained.wiki.map(({ path: pagePath, sha256: digest }) => ({ path: pagePath, sha256: digest }))));

  // Real installed-SDK native search -> outline -> hash-bound read, and actual native argument denials.
  mode = "native-sequence";
  const sequenceRow = runner.rows.find((row) => row.checkpoint === "2025-01-10" && row.taskId === "retention-current" && row.language === "en" && row.condition === "sources+wiki");
  const sequence = await runner.answerCell(sequenceRow, prepared);
  assert.deepEqual(sequence.toolNames.filter((name) => name.startsWith("wiki_")), ["wiki_search", "wiki_outline", "wiki_read"]);
  assert.deepEqual(toolResultNames(sequence.messages), ["wiki_search", "wiki_outline", "wiki_read"]);
  assert.match(sequence.answer, /30 days/);
  assert.equal(sequence.unauthorizedWriteAttempts.length, 0);
  assert.equal(runner.sessionLog.at(-1).readonlyUnchanged, true);
  await assert.rejects(runner.answerCell(sequenceRow, "f".repeat(64)), /wiki hash does not match/);
  await assert.rejects(runner.answerCell({ ...sequenceRow, question: "forged question" }, prepared), /not an exact frozen matrix cell/);

  mode = "native-denials";
  const denialRows = [
    sequenceRow,
    runner.rows.find((row) => row.checkpoint === "2025-01-10" && row.taskId === "response-qualified" && row.language === "en" && row.condition === "sources+wiki"),
    runner.rows.find((row) => row.checkpoint === "2025-01-10" && row.taskId === "maintenance-day" && row.language === "en" && row.condition === "sources+wiki"),
  ];
  const expectedAccessCodes = ["vault-override", "network-option", "out-of-scope"];
  for (let index = 0; index < denialRows.length; index += 1) {
    const denied = await runner.answerCell(denialRows[index], prepared);
    assert.deepEqual(denied.accessDenials, [expectedAccessCodes[index]]);
    assert.deepEqual(denied.unauthorizedWriteAttempts, [], "native read denials are not scorer write attempts");
  }

  // Unsafe candidate wiki entries are metadata-only in snapshots and never enter a manifest/hash.
  const outside = path.join(tmp, "outside.md");
  fs.writeFileSync(outside, "SECRET_OUTSIDE_BYTES_must_not_leak\n");
  fs.symlinkSync(outside, path.join(vault, "wiki", "symlink-page.md"));
  fs.linkSync(outside, path.join(vault, "wiki", "hardlink-page.md"));
  const safePages = runner.snapshotWiki();
  assert.ok(!safePages.some((page) => page.path.includes("symlink-page") || page.path.includes("hardlink-page")));
  assert.ok(!JSON.stringify(safePages).includes("SECRET_OUTSIDE_BYTES_must_not_leak"));
  await assert.rejects(runner.answerCell(sequenceRow, prepared), /unsafe-wiki-entry/);
  fs.unlinkSync(path.join(vault, "wiki", "symlink-page.md"));
  fs.unlinkSync(path.join(vault, "wiki", "hardlink-page.md"));

  // Cross-language scorer hash compatibility for the exact source history returned by the runner.
  mode = "no-tools";
  const sourceRow = runner.rows.find((row) => row.checkpoint === "2025-02-10" && row.taskId === "retention-current" && row.language === "en" && row.condition === "sources");
  const sourceAnswer = await runner.answerCell(sourceRow, null);
  const sourceList = fixture.checkpoints.filter((cp) => cp.id <= sourceRow.checkpoint).flatMap((cp) => cp.sources.map((source) => ({ checkpoint: cp.id, path: source.path, sha256: source.sha256 })));
  assert.equal(sourceAnswer.sourceHistoryHash, canonicalSourceHashFromPython(sourceList));
  assert.equal(sourceAnswer.toolNames.some((name) => name.startsWith("wiki_")), false);

  // Every cell executes through a fresh installed-SDK session, with four independent
  // maintenance histories followed by the 144 answer cells and host BM25 prep each checkpoint.
  mode = "full-matrix";
  const full = await makeRunner(runtime, model, "matrix");
  const built = [];
  const experiment = await full.runner.runExperiment({ buildRetrieval: ({ checkpoint, vaultRoot }) => {
    built.push(checkpoint);
    assert.equal(vaultRoot, fs.realpathSync(full.vault));
    buildBm25(vaultRoot);
  } });
  assert.equal(experiment.matrixSize, 144);
  assert.equal(experiment.answers.length, 144);
  assert.equal(experiment.checkpoints.length, 4);
  assert.deepEqual(built, allCheckpoints);
  assert.equal(experiment.timings.length, 148);
  assert.equal(new Set(experiment.timings.map((record) => record.sessionId)).size, 148, "all four maintenance and 144 answer sessions are distinct");
  const fullContexts = providerContexts.filter((context) => context.mode === "full-matrix");
  assert.equal(fullContexts.length, 149, "144 answer and four maintenance sessions, plus the one deliberate denied read retry");
  assert.ok(fullContexts.every((context) => context.messages.filter((entry) => entry.role === "user").filter((entry) => textOf(entry).includes("Question: ")).length <= 1), "each Pi session has at most its own answer prompt, never another answer's history");
  assert.equal(new Set(experiment.answers.map((answer) => answer.id)).size, 144);
  assert.ok(experiment.answers.every((answer) => answer.status === "success" && answer.readOnly && answer.attemptCount === 1));
  for (const checkpoint of allCheckpoints) {
    const rows = experiment.answers.filter((answer) => answer.checkpoint === checkpoint);
    assert.equal(rows.length, 36);
    const wikiHashes = new Set(rows.filter((row) => row.condition !== "sources").map((row) => row.wikiHash));
    assert.equal(wikiHashes.size, 1, `wiki arms must bind to byte-identical checkpoint ${checkpoint}`);
    for (const task of fixture.visibleTasks) for (const language of ["en", "it"]) {
      const paired = rows.filter((row) => row.taskId === task.id && row.language === language);
      assert.equal(paired.length, 3);
      assert.equal(new Set(paired.map((row) => row.question)).size, 1);
      assert.equal(new Set(paired.map((row) => row.model)).size, 1);
      assert.equal(new Set(paired.map((row) => row.effort)).size, 1);
      assert.equal(paired.find((row) => row.condition === "sources").wikiHash, null);
      assert.equal(paired.find((row) => row.condition === "sources").sourceHistoryHash, paired.find((row) => row.condition === "sources+wiki").sourceHistoryHash);
    }
  }
  assert.ok(experiment.answers.find((answer) => answer.condition === "wiki-only").accessDenials.includes("source-unavailable"));
  for (const session of experiment.timings.filter((record) => record.phase.startsWith("answer:"))) assert.equal(session.readonlyUnchanged, true);
  assert.equal(experiment.unauthorizedWriteAttempts.length, 0);

  // Abort during a live request and make an injected setup failure both dispose and restore env.
  const abortController = new AbortController();
  let cancelledSessionDisposed = false;
  mode = "cancel";
  const cancelWorkspace = await makeRunner(runtime, model, "cancel", { signal: abortController.signal, onSession: ({ session }) => {
    const dispose = session.dispose.bind(session);
    session.dispose = () => { cancelledSessionDisposed = true; dispose(); };
    setTimeout(() => abortController.abort(), 15);
  } });
  const cancelRow = cancelWorkspace.runner.rows.find((row) => row.condition === "sources" && row.checkpoint === "2025-01-10");
  const cancelled = await cancelWorkspace.runner.answerCell(cancelRow, null);
  assert.equal(cancelled.cancelled, true);
  assert.equal(cancelWorkspace.runner.sessionLog.at(-1).readonlyUnchanged, true);
  assert.equal(cancelledSessionDisposed, true);

  let failedSessionDisposed = false;
  mode = "full-matrix";
  const setupRunner = await makeRunner(runtime, model, "setup-failure", { onSession: ({ session }) => {
    const dispose = session.dispose.bind(session);
    session.dispose = () => { failedSessionDisposed = true; dispose(); };
    throw new Error("injected setup failure");
  } });
  const setupRow = setupRunner.runner.rows.find((row) => row.condition === "sources" && row.checkpoint === "2025-01-10");
  await assert.rejects(setupRunner.runner.answerCell(setupRow, null), /injected setup failure/);
  assert.equal(failedSessionDisposed, true);
  assert.deepEqual({ OBSIDIAN_AGENT_CONFIG: process.env.OBSIDIAN_AGENT_CONFIG, OBSIDIAN_VAULT_PATH: process.env.OBSIDIAN_VAULT_PATH, WIKI_MIDDLEWARE_DIR: process.env.WIKI_MIDDLEWARE_DIR }, priorEnv);
  assert.equal(createCount, 5);
  assert.deepEqual(forbiddenReads, [], "synthetic HOME and ancestor .agents skill files/metadata are not read by explicit resource loading");
  assert.doesNotMatch(JSON.stringify(providerContexts), /SECRET_SENTINEL_SKILL|SECRET_ANCESTOR_SKILL/u);
  console.log(`Pi knowledge-maintenance installed-SDK contract PASS: ${experiment.timings.length} distinct orchestration sessions, ${requests} fake provider turns, actual lifecycle and native tool roundtrip.`);
} finally {
  fsMutable.readFileSync = originalReadFileSync;
  syncBuiltinESMExports();
  runtime?.dispose?.();
  if (priorHome === undefined) delete process.env.HOME; else process.env.HOME = priorHome;
  for (const [key, value] of Object.entries(priorEnv)) if (value === undefined) delete process.env[key]; else process.env[key] = value;
  fs.rmSync(tmp, { recursive: true, force: true });
}
