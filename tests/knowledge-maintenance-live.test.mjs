import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { execFileSync, spawnSync } from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { REQUEST_CONTRACT_FIELDS, codeIdentity, createBudget, loadSelectedOpenAIAuth, resolveConfig, validateWorkdir, wrapOpenAIProvider } from "../experiments/knowledge-maintenance/live-support.mjs";
import { buildAnswerJudgePrompt, buildMaintenanceJudgePrompt, dryRun, recordPreflightFailure, runLive, validateRunMetadata } from "../experiments/knowledge-maintenance/run.mjs";
import { installedPackage, resolveRuntimeDependencies } from "./pi-runtime-paths.mjs";
import { createPiRunner } from "../experiments/knowledge-maintenance/pi-runner.mjs";
import { createNativeProviderRuntime, preflightNativeProvider } from "../experiments/knowledge-maintenance/native-bridge.mjs";
import { fileURLToPath, pathToFileURL } from "node:url";

const hash = (value) => createHash("sha256").update(value).digest("hex");
const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "knowledge-maintenance-live-test-")));
const fakeHome = path.join(tmp, "home");
const outside = path.join(tmp, "external");
fs.mkdirSync(fakeHome);
fs.mkdirSync(outside);
const fixture = JSON.parse(fs.readFileSync("experiments/knowledge-maintenance/fixtures.json", "utf8"));
const workdir = path.join(outside, "not-created");
const configPath = path.join(outside, "config.json");
const config = { workdir, solver: { provider: "openai", model: "file-model", effort: "low" }, limits: { calls: 12 } };
fs.writeFileSync(configPath, JSON.stringify(config));

try {
  const resolved = resolveConfig({ argv: ["--config", configPath, "--solver-provider", "openai", "--solver-model", "cli-model", "--solver-effort", "high", "--response-tokens", "32"], env: { OSB_KM_SOLVER_PROVIDER: "offline", OSB_KM_SOLVER_MODEL: "env-model", OSB_KM_RESPONSE_TOKENS: "24" }, cwd: outside });
  assert.equal(resolved.solver.provider, "openai", "CLI overrides env and dedicated file");
  assert.equal(resolved.solver.model, "cli-model", "CLI overrides env and dedicated file");
  assert.equal(resolved.solver.effort, "high");
  assert.equal(resolved.limits.maxOutputTokens, 32);
  assert.equal(resolved.limits.calls, 12);
  const partialConfigPath = path.join(outside, "partial-config.json");
  fs.writeFileSync(partialConfigPath, JSON.stringify({ workdir, solver: { provider: "openai", model: "file-solver" }, judge: { model: "file-judge" } }));
  const partial = resolveConfig({ argv: ["--config", partialConfigPath, "--solver-effort", "high", "--solver-model", "cli-solver", "--judge-effort", "high"],
    env: { OSB_KM_SOLVER_MODEL: "env-solver", OSB_KM_JUDGE_EFFORT: "low" } });
  assert.deepEqual(partial.solver, { provider: "openai", model: "cli-solver", effort: "high" }, "CLI role fields override environment/file while missing fields inherit defaults");
  assert.deepEqual(partial.judge, { provider: "openai", model: "file-judge", effort: "high" }, "judge file/environment/CLI precedence preserves default provider");
  assert.deepEqual(partial.limits, { calls: 750, minutes: 90, outputTokens: 500000, maxOutputTokens: 4096 }, "omitted limits inherit documented defaults");
  fs.writeFileSync(partialConfigPath, JSON.stringify({ workdir, solver: { model: "file-solver" } }));
  const fileDefaults = resolveConfig({ argv: ["--config", partialConfigPath], env: {} });
  assert.deepEqual(fileDefaults.solver, { provider: "openai", model: "file-solver", effort: "medium" });
  assert.deepEqual(fileDefaults.judge, { provider: "openai", model: "gpt-6.1-terra", effort: "medium" });
  fs.writeFileSync(partialConfigPath, JSON.stringify({ workdir }));
  const envDefaults = resolveConfig({ argv: ["--config", partialConfigPath], env: { OSB_KM_SOLVER_MODEL: "env-solver", OSB_KM_JUDGE_EFFORT: "high" } });
  assert.deepEqual(envDefaults.solver, { provider: "openai", model: "env-solver", effort: "medium" });
  assert.deepEqual(envDefaults.judge, { provider: "openai", model: "gpt-6.1-terra", effort: "high" });
  fs.writeFileSync(partialConfigPath, JSON.stringify({ workdir, solver: { model: "" } }));
  assert.throws(() => resolveConfig({ argv: ["--config", partialConfigPath], env: {} }), /invalid-solver/);
  fs.writeFileSync(partialConfigPath, JSON.stringify({ workdir, solver: null }));
  assert.throws(() => resolveConfig({ argv: ["--config", partialConfigPath], env: {} }), /solver-must-be-object/);
  fs.writeFileSync(partialConfigPath, JSON.stringify({ workdir, solver: { typo: "x" } }));
  assert.throws(() => resolveConfig({ argv: ["--config", partialConfigPath], env: {} }), /solver-unknown-key/);
  assert.throws(() => resolveConfig({ argv: ["--config", configPath], env: { OSB_KM_UNKNOWN: "x" } }), /unknown-prefixed-environment-key/);
  fs.writeFileSync(configPath, JSON.stringify({ ...config, live: true }));
  assert.throws(() => resolveConfig({ argv: ["--config", configPath], env: {} }), /config-unknown-key/);
  fs.writeFileSync(configPath, JSON.stringify({ ...config, unexpected: true }));
  assert.throws(() => resolveConfig({ argv: ["--config", configPath], env: {} }), /config-unknown-key/);
  fs.writeFileSync(configPath, JSON.stringify(config));
  assert.throws(() => resolveConfig({ argv: ["--config", configPath, "--response-tokens", "8"], env: {} }), /below-provider-minimum/);
  assert.throws(() => resolveConfig({ argv: ["--live", "false", "--config", configPath], env: {} }), /invalid-command-line/);

  const dry = resolveConfig({ argv: ["--config", configPath], env: {} });
  const manifest = dryRun(dry, fixture);
  assert.equal(manifest.liveEnabled, false);
  assert.equal(manifest.taskIds.length, 144);
  assert.equal(manifest.settings.solver.model, "file-model");
  assert.ok(manifest.identities.sourceHash && manifest.identities.goldHash && manifest.identities.promptHash && manifest.identities.rubricHash && manifest.identities.code.hash);
  assert.equal(fs.existsSync(workdir), false, "dry-run cannot create output paths");
  assert.equal(Object.hasOwn(manifest, "authFile"), false, "manifest must not include credential path");
  const ancestorRepo = path.join(outside, "preflight-ancestor-repo");
  const ancestorVault = path.join(ancestorRepo, "vault");
  const ancestorWorkdir = path.join(outside, "preflight-ancestor-run");
  fs.mkdirSync(path.join(ancestorVault, "wiki"), { recursive: true });
  fs.writeFileSync(path.join(ancestorVault, ".knowledge-maintenance-synthetic"), "obsidian-second-brain synthetic knowledge-maintenance vault v1\n");
  fs.mkdirSync(path.join(ancestorRepo, ".git"));
  const ancestorConfig = { ...dry, live: true, dryRun: false, workdir: ancestorWorkdir, authFile: path.join(outside, "missing-auth.json"), vaultRoot: ancestorVault };
  const ancestorManifest = dryRun({ ...ancestorConfig, live: false, dryRun: true }, fixture);
  let ancestorAuthLoads = 0;
  await assert.rejects(runLive(ancestorConfig, { ...ancestorManifest, liveEnabled: true, mode: "live", externalWorkdir: ancestorWorkdir }, fixture, "owner-ancestor", {
    loadAuth: () => { ancestorAuthLoads++; throw new Error("auth loading must not be reached"); },
  }), /unsupported-git-metadata-layout/);
  assert.equal(fs.existsSync(ancestorWorkdir), false, "ancestor Git discovery is rejected before run output/host preparation");
  assert.equal(ancestorAuthLoads, 0, "ancestor Git discovery is rejected before auth or provider setup");
  const checkpoint = fixture.checkpoints[0];
  const sources = checkpoint.sources.map((source) => ({ checkpoint: checkpoint.id, path: source.path, text: source.text, sha256: source.sha256 }));
  const judgeInput = buildAnswerJudgePrompt({ candidateId: "opaque-candidate-uuid", answerHash: hash("safe answer"), rubric: { fixed: true },
    checkpoint: checkpoint.id, question: fixture.visibleTasks[0].question.en, gold: fixture.gold.expectedAnswers[fixture.visibleTasks[0].id][checkpoint.id],
    topic: fixture.visibleTasks[0].topic, sourceHistory: sources, answer: "safe answer", schema: JSON.parse(fs.readFileSync("experiments/knowledge-maintenance/annotation-schema.json", "utf8")) });
  assert.match(judgeInput, /opaque-candidate-uuid/);
  assert.doesNotMatch(judgeInput, /sources\\+wiki|gpt-6\\.1-sol|reasoning trace|2025-02-10|2025-03-10/);
  const currentPages = [{ path: "wiki/policy.md", content: "Current snapshot text", sha256: hash("Current snapshot text") }];
  const currentSnapshotHash = hash("snapshot");
  const wikiJudgeInput = buildMaintenanceJudgePrompt({ checkpoint: checkpoint.id, snapshotHash: currentSnapshotHash, previousSnapshotHash: null,
    previousSnapshot: null, changeAssessment: "initialization", rubric: { fixed: true }, topics: fixture.gold.topics, gold: {}, sourceHistory: sources,
    pages: currentPages, schema: JSON.parse(fs.readFileSync("experiments/knowledge-maintenance/annotation-schema.json", "utf8")) });
  assert.match(wikiJudgeInput, /changeAssessment=initialization/);
  assert.match(wikiJudgeInput, /Previous frozen candidate snapshot: null/);
  const previousA = { checkpoint: "2024-12-01", status: "complete", contentHash: hash("previous A"), pages: [{ path: "wiki/policy.md", content: "Prior policy A", sha256: hash("Prior policy A") }] };
  const previousB = { checkpoint: "2024-12-01", status: "complete", contentHash: hash("previous B"), pages: [{ path: "wiki/policy.md", content: "Prior policy B", sha256: hash("Prior policy B") }] };
  const revisionPromptA = buildMaintenanceJudgePrompt({ checkpoint: checkpoint.id, snapshotHash: currentSnapshotHash, previousSnapshotHash: previousA.contentHash,
    previousSnapshot: previousA, changeAssessment: "assessed", rubric: { fixed: true }, topics: fixture.gold.topics, gold: {}, sourceHistory: sources,
    pages: currentPages, schema: JSON.parse(fs.readFileSync("experiments/knowledge-maintenance/annotation-schema.json", "utf8")) });
  const revisionPromptB = buildMaintenanceJudgePrompt({ checkpoint: checkpoint.id, snapshotHash: currentSnapshotHash, previousSnapshotHash: previousB.contentHash,
    previousSnapshot: previousB, changeAssessment: "assessed", rubric: { fixed: true }, topics: fixture.gold.topics, gold: {}, sourceHistory: sources,
    pages: currentPages, schema: JSON.parse(fs.readFileSync("experiments/knowledge-maintenance/annotation-schema.json", "utf8")) });
  assert.notEqual(revisionPromptA, revisionPromptB, "same current snapshot with a different frozen predecessor changes maintenance judge inputs");
  assert.match(revisionPromptA, /Prior policy A/);
  assert.match(revisionPromptB, /Prior policy B/);
  assert.doesNotMatch(wikiJudgeInput, /solver model|sources\\+wiki|reasoning trace|2025-02-10|2025-03-10/);
  assert.throws(() => validateWorkdir(path.join(fakeHome, "vault"), { repoRoot: outside, home: fakeHome }), /personal-home/);
  assert.throws(() => validateWorkdir(outside, { repoRoot: outside, home: fakeHome }), /repository/);
  assert.throws(() => validateWorkdir(outside, { repoRoot: path.join(outside, "repo"), home: fakeHome }), /already-exists/);
  const identityDir = path.join(outside, "identity");
  fs.mkdirSync(identityDir);
  fs.writeFileSync(path.join(identityDir, "protocol.txt"), "frozen-v1");
  const frozenCode = codeIdentity({ root: identityDir, files: ["protocol.txt"] });
  fs.writeFileSync(path.join(identityDir, "protocol.txt"), "frozen-v2");
  assert.notEqual(codeIdentity({ root: identityDir, files: ["protocol.txt"] }).hash, frozenCode.hash, "code identity must detect drift");
  const linked = path.join(outside, "linked");
  fs.symlinkSync(path.join(outside, "missing"), linked);
  assert.throws(() => validateWorkdir(linked, { repoRoot: path.join(outside, "repo"), home: fakeHome }), /symlink/);

  let clock = 0;
  const budget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 5 }, now: () => clock });
  const first = budget.beginAttempt({ phase: "answer" });
  budget.finishAttempt(first, { inputTokens: 1, outputTokens: 3, totalTokens: 4, pricing: null });
  const second = budget.beginAttempt({ phase: "judge" });
  assert.throws(() => budget.finishAttempt(second, null), /usage-unavailable/);
  assert.equal(budget.state.calls, 2);
  assert.equal(budget.state.stopped, true);
  assert.equal(budget.state.records[1].status, "usage-unavailable");
  assert.throws(() => budget.beginAttempt(), /usage-unavailable/);
  const timeBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 5 }, now: () => clock });
  clock = 60_001;
  assert.throws(() => timeBudget.beginAttempt(), /time-limit/);
  assert.equal(timeBudget.state.calls, 0, "not-dispatched call does not increment actual attempt count");
  let boundedFetches = 0;
  const callBudget = createBudget({ limits: { calls: 1, minutes: 1, outputTokens: 10 } });
  const callProvider = wrapOpenAIProvider({ streamSimple: (_model, _context, options) => ({ async *[Symbol.asyncIterator]() {
    const body = JSON.stringify({ model: "fixture-model", max_output_tokens: 4096, reasoning: { effort: "medium" }, store: false, stream: true });
    await options.fetch("https://api.openai.com/v1/responses", { method: "POST", body });
    await options.fetch("https://api.openai.com/v1/responses", { method: "POST", body });
  } }) }, callBudget, { fetchImpl: async () => { boundedFetches++; return new Response("", { status: 200 }); } });
  await assert.rejects(async () => { for await (const _event of callProvider.streamSimple({ provider: "openai", api: "openai-responses", baseUrl: "https://api.openai.com/v1", id: "fixture-model" }, { messages: [] }, {})) {} }, /call-limit/);
  assert.equal(callBudget.state.calls, 1);
  assert.equal(boundedFetches, 1, "call-limit blocks, rather than dispatches, the extra provider turn");

  let sentBody;
  const sse = [
    "data: {\"type\":\"response.created\",\"response\":{\"id\":\"resp-test\",\"model\":\"fixture-model\"}}",
    "data: {\"type\":\"response.output_text.delta\",\"delta\":\"ok\"}",
    `data: ${JSON.stringify({ type: "response.completed", response: { id: "resp-test", model: "fixture-model", status: "completed", usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5, input_tokens_details: { cached_tokens: 1 } } } })}`,
    "data: [DONE]", "",
  ].join("\n");
  const providerBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 20 } });
  const provider = wrapOpenAIProvider({ streamSimple: (_model, _context, options) => ({ async *[Symbol.asyncIterator]() {
    sentBody = JSON.parse(JSON.stringify({ model: "fixture-model", max_output_tokens: 4096, reasoning: { effort: "medium" }, store: false, stream: true }));
    const response = await options.fetch("https://api.openai.com/v1/responses", { method: "POST", body: JSON.stringify(sentBody) });
    assert.equal(response.status, 200);
    yield { type: "done" };
  } }) }, providerBudget, { fetchImpl: async (_url, init) => {
    assert.equal(init.redirect, "error");
    return new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } });
  } });
  const wrappedStream = provider.streamSimple({ provider: "openai", api: "openai-responses", baseUrl: "https://api.openai.com/v1", id: "fixture-model" }, { messages: [] }, { maxRetries: 3 });
  for await (const _event of wrappedStream) { /* consume */ }
  assert.equal(sentBody.max_output_tokens, 4096);
  assert.equal(sentBody.reasoning.effort, "medium");
  assert.equal(providerBudget.state.calls, 1);
  assert.equal(providerBudget.state.records[0].usage.outputTokens, 2, "usage comes from raw response.completed SSE payload");

  let tries = 0;
  const failedBudget = createBudget({ limits: { calls: 3, minutes: 1, outputTokens: 20 } });
  const failedProvider = wrapOpenAIProvider({ streamSimple: (_model, _context, options) => ({ async *[Symbol.asyncIterator]() {
    await options.fetch("https://api.openai.com/v1/responses", { method: "POST", body: JSON.stringify({ model: "fixture-model", max_output_tokens: 4096, reasoning: { effort: "medium" }, store: false, stream: true }) });
    throw new Error("private provider error body must never escape");
  } }) }, failedBudget, { fetchImpl: async () => { tries++; throw new Error("secret-looking transport detail"); } });
  await assert.rejects(async () => { for await (const _event of failedProvider.streamSimple({ provider: "openai", api: "openai-responses", baseUrl: "https://api.openai.com/v1", id: "fixture-model" }, { messages: [] }, {})) {} }, /provider-call-failed|transport-error/);
  assert.equal(tries, 1, "provider transport failure is not automatically retried");
  assert.equal(failedBudget.state.calls, 1);
  assert.doesNotMatch(JSON.stringify(failedBudget.state.records), /secret-looking|private provider/);

  const timeoutController = new AbortController();
  const timeoutBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 20 }, onStop: () => timeoutController.abort() });
  const timeoutProvider = wrapOpenAIProvider({ streamSimple: (_model, _context, options) => ({ async *[Symbol.asyncIterator]() {
    await options.fetch("https://api.openai.com/v1/responses", { method: "POST", signal: timeoutController.signal,
      body: JSON.stringify({ model: "fixture-model", max_output_tokens: 4096, reasoning: { effort: "medium" }, store: false, stream: true }) });
  } }) }, timeoutBudget, { fetchImpl: (_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(new Error("secret timeout details")), { once: true })) });
  const timeoutRun = async () => { for await (const _event of timeoutProvider.streamSimple({ provider: "openai", api: "openai-responses", baseUrl: "https://api.openai.com/v1", id: "fixture-model" }, { messages: [] }, {})) {} };
  const timeoutPending = assert.rejects(timeoutRun(), /transport-error/);
  setTimeout(() => timeoutController.abort(), 5);
  await timeoutPending;
  assert.equal(timeoutBudget.state.calls, 1);
  assert.equal(timeoutBudget.state.stopped, true);

  // #41: a request-contract mismatch records only the sorted names of the differing guarded fields, never values.
  const fixtureModel = { provider: "openai", api: "openai-responses", baseUrl: "https://api.openai.com/v1", id: "fixture-model" };
  const mismatchCases = [
    [{ model: "fixture-model", max_output_tokens: 4096, reasoning: { effort: "medium" }, store: false, stream: true }, null],
    [{ model: "fixture-model", reasoning: { effort: "medium" }, store: false, stream: true }, ["max_output_tokens"]],
    [{ model: "secret-model-value", max_output_tokens: 123456, reasoning: { effort: "secret-effort-value" }, store: true, stream: false }, [...REQUEST_CONTRACT_FIELDS]],
  ];
  for (const [body, expectedFields] of mismatchCases) {
    let dispatched = 0;
    const mismatchBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 20 } });
    const mismatchProvider = wrapOpenAIProvider({ streamSimple: (_model, _context, options) => ({ async *[Symbol.asyncIterator]() {
      await options.fetch("https://api.openai.com/v1/responses", { method: "POST", body: JSON.stringify(body) });
      yield { type: "done" };
    } }) }, mismatchBudget, { fetchImpl: async () => { dispatched++; return new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } }); } });
    const consume = async () => { for await (const _event of mismatchProvider.streamSimple(fixtureModel, { messages: [] }, {})) { /* consume */ } };
    if (!expectedFields) {
      await consume();
      assert.equal(dispatched, 1);
      assert.equal(mismatchBudget.state.requestMismatchFields, null, "a matching request records no mismatch fields");
      continue;
    }
    await assert.rejects(consume, /request-contract-mismatch/);
    assert.equal(dispatched, 0, "a mismatched request is never dispatched");
    assert.equal(mismatchBudget.state.calls, 0);
    assert.deepEqual(mismatchBudget.state.requestMismatchFields, expectedFields);
    assert.doesNotMatch(JSON.stringify(mismatchBudget.state), /secret-|123456/, "mismatch diagnostics never retain request values");
  }
  const firstStopBudget = createBudget({ limits: { calls: 1, minutes: 1, outputTokens: 1 } });
  assert.throws(() => firstStopBudget.stop("time-limit"), /time-limit/);
  assert.throws(() => firstStopBudget.stop("request-contract-mismatch", ["model"]), /request-contract-mismatch/);
  assert.equal(firstStopBudget.state.stopCode, "time-limit");
  assert.equal(firstStopBudget.state.requestMismatchFields, null, "mismatch fields are recorded only with the winning stop code");

  const hex = "a".repeat(64);
  const roleRuntimeRecord = { id: "openai/fixture-model", api: "openai-responses", origin: null, effort: "medium" };
  const baseMetadata = (stopCode) => ({ schemaVersion: 1,
    identities: { sourceHash: hex, goldHash: hex, fixtureHash: hex, rubricHash: hex, settingsHash: hex, promptHash: hex, code: { revision: null, revisionStatus: "not-recorded", files: [], hash: hex } },
    limits: { calls: 750, minutes: 90, outputTokens: 500000, maxOutputTokens: 4096 },
    runtime: { node: "v0", python: "Python 3", piCodingAgent: "0.0.0", solver: { ...roleRuntimeRecord }, judge: { ...roleRuntimeRecord } },
    backendRevision: null, backendRevisionStatus: "unknown", stopCode, attempts: [], sessions: [], stages: [],
    timing: { startedAt: "2030-01-01T00:00:00.000Z", elapsedMs: null }, pricing: null });
  validateRunMetadata(baseMetadata("request-contract-mismatch"));
  validateRunMetadata({ ...baseMetadata("request-contract-mismatch"), requestMismatchFields: ["max_output_tokens"] });
  validateRunMetadata({ ...baseMetadata("request-contract-mismatch"), requestMismatchFields: [...REQUEST_CONTRACT_FIELDS] });
  for (const [stopCode, fields] of [["time-limit", ["model"]], [null, ["model"]], ["request-contract-mismatch", []],
    ["request-contract-mismatch", ["model", "model"]], ["request-contract-mismatch", ["model", "max_output_tokens"]],
    ["request-contract-mismatch", ["authorization"]], ["request-contract-mismatch", "model"], ["request-contract-mismatch", [4096]],
    ["request-contract-mismatch", [, "model"]]]) {
    assert.throws(() => validateRunMetadata({ ...baseMetadata(stopCode), requestMismatchFields: fields }), /metadata-request-mismatch-fields-invalid/, JSON.stringify({ stopCode, fields }));
  }

  if (process.env.OSB_KM_TEST_SKIP_MISSING_SDK_CHILD === "1") console.log("NO-SDK-PURE-CHECKS-REACHED");
  const packageRoot = installedPackage();
  if (!packageRoot) {
    const message = "Pi SDK/runtime unavailable; set PI_CODING_AGENT_PACKAGE to run native serializer/coordinator checks.";
    if (process.argv.includes("--require-runtime")) throw new Error(message);
    console.log(`UNVERIFIED: ${message}`);
  } else {
  const sdkUrl = pathToFileURL(path.join(packageRoot, "dist/index.js")).href;
  const authUrl = pathToFileURL(path.join(packageRoot, "dist/core/auth-storage.js")).href;
  const [sdk, authModule] = await Promise.all([import(sdkUrl), import(authUrl)]);
  const [aiUrl] = resolveRuntimeDependencies(packageRoot);
  const piAi = await import(aiUrl);
  const { ModelRuntime } = sdk;
  const { AuthStorage } = authModule;
  const childHost = path.join(outside, "native-subagent-host");
  const childAgentDir = path.join(childHost, "agent");
  const childExtensions = path.join(childHost, ".pi", "extensions");
  fs.mkdirSync(childAgentDir, { recursive: true });
  fs.mkdirSync(childExtensions, { recursive: true });
  fs.writeFileSync(path.join(childExtensions, "km-native.mjs"), `import register from ${JSON.stringify(pathToFileURL(path.resolve("experiments/knowledge-maintenance/native-bridge.mjs")).href)};\nexport default register;\n`);
  const childSettings = sdk.SettingsManager.inMemory({ defaultTools: [], retry: { enabled: false, maxRetries: 0 }, compaction: { enabled: false } });
  const childResources = new sdk.DefaultResourceLoader({ cwd: childHost, agentDir: childAgentDir, settingsManager: childSettings, additionalExtensionPaths: [path.join(childExtensions, "km-native.mjs")], noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true });
  await childResources.reload();
  assert.ok(childResources.getExtensions().extensions.some((entry) => entry.path.endsWith("km-native.mjs")), "disposable child cwd loads only the explicit native bridge extension");
  const childModelRuntime = await ModelRuntime.create({ credentials: AuthStorage.inMemory(), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
  const child = await sdk.createAgentSession({ cwd: childHost, agentDir: childAgentDir, modelRuntime: childModelRuntime, resourceLoader: childResources,
    settingsManager: childSettings, sessionManager: sdk.SessionManager.inMemory(childHost), tools: [] });
  await child.session.bindExtensions({});
  const childMessages = [];
  child.session.subscribe((event) => { if (event.type === "message_end") childMessages.push(event.message); });
  await child.session.prompt("Task: /km-native", { expandPromptTemplates: true });
  assert.equal(childMessages.length, 0, "native subagent command interception handles its exact Task wrapper without an ordinary model turn");
  child.session.dispose();
  childModelRuntime.dispose?.();

  const cancelHost = path.join(outside, "native-cancel-host");
  const cancelAgentDir = path.join(cancelHost, "agent");
  const cancelExtensions = path.join(cancelHost, ".pi", "extensions");
  fs.mkdirSync(cancelAgentDir, { recursive: true });
  fs.mkdirSync(cancelExtensions, { recursive: true });
  fs.writeFileSync(path.join(cancelExtensions, "km-native.mjs"), `import register from ${JSON.stringify(pathToFileURL(path.resolve("experiments/knowledge-maintenance/native-bridge.mjs")).href)};\nexport default register;\n`);
  const cancelRuntime = await ModelRuntime.create({ credentials: AuthStorage.inMemory({ openai: { type: "api_key", key: "sk-offline-cancel-sentinel" } }), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
  await cancelRuntime.refresh({ allowNetwork: false });
  const cancelResources = new sdk.DefaultResourceLoader({ cwd: cancelHost, agentDir: cancelAgentDir, settingsManager: childSettings, additionalExtensionPaths: [path.join(cancelExtensions, "km-native.mjs")], noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true });
  await cancelResources.reload();
  const cancelSession = await sdk.createAgentSession({ cwd: cancelHost, agentDir: cancelAgentDir, modelRuntime: cancelRuntime, resourceLoader: cancelResources,
    settingsManager: childSettings, sessionManager: sdk.SessionManager.inMemory(cancelHost), tools: [] });
  await cancelSession.session.bindExtensions({});
  const cancelVault = path.join(outside, "native-cancel-vault");
  fs.mkdirSync(path.join(cancelVault, "wiki"), { recursive: true });
  fs.writeFileSync(path.join(cancelVault, ".knowledge-maintenance-synthetic"), "obsidian-second-brain synthetic knowledge-maintenance vault v1\n");
  const cancelWorkdir = path.join(outside, "native-cancel-run");
  const cancelConfigPath = path.join(outside, "native-cancel-config.json");
  // The cancellation assertion targets dispatch/shutdown, so pin this synthetic config to a model present in both CI Pi 1.4 and local Pi 1.5 catalogs.
  const cancelConfig = { workdir: cancelWorkdir, vaultRoot: cancelVault, solver: { provider: "openai", model: "gpt-6.1-sol", effort: "medium" }, judge: { provider: "openai", model: "gpt-6.1-sol", effort: "medium" }, limits: { calls: 4, minutes: 1 } };
  assert.ok(cancelRuntime.getModel("openai", "gpt-6.1-sol"), "cancellation fixture uses a model available in the minimum Pi catalog");
  fs.writeFileSync(cancelConfigPath, JSON.stringify(cancelConfig));
  let cancelFetchCount = 0;
  let cancelFetchSignal;
  let markFetchStarted;
  const fetchStarted = new Promise((resolve) => { markFetchStarted = resolve; });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (_url, init) => {
    cancelFetchCount++;
    cancelFetchSignal = init.signal;
    markFetchStarted();
    return new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }));
  };
  try {
    const pendingNativeInput = cancelSession.session.prompt(`Task: /km-native --live --config ${cancelConfigPath}`, { expandPromptTemplates: true });
    const nativeInputSettlement = pendingNativeInput.then(() => null, (error) => error);
    let dispatchTimeout;
    try {
      await Promise.race([fetchStarted, new Promise((_resolve, reject) => { dispatchTimeout = setTimeout(() => reject(new Error("native cancellation test dispatch timed out")), 10_000); })]);
    } finally { clearTimeout(dispatchTimeout); }
    assert.equal(cancelSession.session.isIdle, true, "native input hook runs while the controller session is idle");
    await cancelSession.session.abort();
    assert.equal(cancelFetchSignal.aborted, false, "idle session.abort alone does not cancel the pending native experiment fetch");
    await cancelSession.session.extensionRunner.emit({ type: "session_shutdown", reason: "quit" });
    const inputResult = await nativeInputSettlement;
    assert.equal(inputResult, null, inputResult instanceof Error ? inputResult.message : "native input hook should settle normally after shutdown cancellation");
    assert.equal(cancelFetchSignal.aborted, true, "native session_shutdown cancels the experiment-owned request signal");
    assert.equal(cancelFetchCount, 1, "cancellation prevents any later provider dispatch");
    const cancelMetadata = JSON.parse(fs.readFileSync(path.join(cancelWorkdir, "run-metadata.json"), "utf8"));
    validateRunMetadata(cancelMetadata);
    assert.equal(cancelMetadata.stopCode, "cancelled");
    assert.equal(cancelMetadata.attempts.length, 1);
    assert.equal(cancelMetadata.attempts[0].errorCode, "transport-error", "attempt transport failure is retained separately from the cancellation stop code");
    assert.ok(fs.existsSync(path.join(cancelWorkdir, "score.json")), "cancelled experiment persists a valid partial score");
    assert.deepEqual(fs.readdirSync(outside).filter((name) => name.startsWith(".osb-km-workspace-")), [], "native shutdown waits through experiment workspace cleanup");
  } finally {
    globalThis.fetch = originalFetch;
    cancelSession.session.dispose();
    cancelRuntime.dispose?.();
  }

  const syntheticAuthFile = path.join(outside, "synthetic-auth.json");
  const syntheticAuthBytes = JSON.stringify({ openai: { type: "api_key", key: "sk-offline-synthetic-key" } });
  fs.writeFileSync(syntheticAuthFile, syntheticAuthBytes, { mode: 0o600 });
  const beforeAuth = { bytes: fs.readFileSync(syntheticAuthFile), mtime: fs.statSync(syntheticAuthFile).mtimeMs };
  const runtime = await loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime, readStoredCredential: authModule.readStoredCredential, authFile: syntheticAuthFile });
  assert.deepEqual(fs.readFileSync(syntheticAuthFile), beforeAuth.bytes);
  assert.equal(fs.statSync(syntheticAuthFile).mtimeMs, beforeAuth.mtime);
  assert.equal(fs.existsSync(path.join(outside, "auth.json")), false, "selected credential must not be copied to an auth file");
  const oauthFile = path.join(outside, "oauth-auth.json");
  fs.writeFileSync(oauthFile, JSON.stringify({ openai: { type: "oauth", access: "synthetic-access", refresh: "synthetic-refresh", expires: Date.now() + 60_000 } }));
  await assert.rejects(loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime, readStoredCredential: authModule.readStoredCredential, authFile: oauthFile }), /unsupported-openai-auth-mode-output-cap-unverified/);
  const commandAuth = path.join(outside, "command-auth.json");
  fs.writeFileSync(commandAuth, JSON.stringify({ openai: { type: "api_key", key: "!secret-command" } }));
  await assert.rejects(loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime, readStoredCredential: authModule.readStoredCredential, authFile: commandAuth }), /unsupported-openai-api-key-record/);
  try {
    const model = runtime.getModel("openai", "gpt-6.1-sol");
    const nativeProvider = runtime.getProvider("openai");
    assert.ok(model && nativeProvider?.streamSimple, "installed catalog must expose the selected Responses API model");
    assert.equal(model.api, "openai-responses");
    assert.equal(model.baseUrl, "https://api.openai.com/v1");
    let requestBody;
    const nativeBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 100 } });
    const wrapped = wrapOpenAIProvider(nativeProvider, nativeBudget, { fetchImpl: async (_url, init) => {
      requestBody = JSON.parse(init.body);
      const events = [
        { type: "response.created", response: { id: "resp-native", model: model.id } },
        { type: "response.output_item.added", output_index: 0, item: { id: "msg-native", type: "message", role: "assistant", content: [] } },
        { type: "response.output_text.delta", output_index: 0, item_id: "msg-native", content_index: 0, delta: "synthetic answer" },
        { type: "response.output_item.done", output_index: 0, item: { id: "msg-native", type: "message", role: "assistant", status: "completed", phase: "final_answer", content: [{ type: "output_text", text: "synthetic answer", annotations: [] }] } },
        { type: "response.completed", response: { id: "resp-native", model: model.id, status: "completed", output: [], usage: { input_tokens: 7, output_tokens: 3, total_tokens: 10, input_tokens_details: { cached_tokens: 0 } } } },
      ];
      return new Response(`${events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`, { status: 200, headers: { "content-type": "text/event-stream" } });
    } });
    const nativeStream = wrapped.streamSimple(model, { messages: [{ role: "system", content: "Synthetic system." }, { role: "user", content: "Synthetic question." }] },
      { apiKey: "sk-offline-synthetic-key", reasoning: "medium", reasoningEffort: "medium", maxTokens: 4096 });
    let nativeError = null;
    let streamFailure = null;
    try { for await (const event of nativeStream) if (event.type === "error") nativeError = event; }
    catch (error) { streamFailure = error.message; }
    assert.equal(streamFailure, null, JSON.stringify({ streamFailure, nativeError, requestBody, budget: nativeBudget.state }));
    assert.equal(nativeError, null, JSON.stringify(nativeError));
    assert.equal(requestBody.max_output_tokens, 4096, "the actual installed OpenAI serializer must include the response cap");
    assert.equal(requestBody.reasoning.effort, "medium", "the actual installed serializer must preserve native reasoning mapping");
    assert.equal(nativeBudget.state.calls, 1);
    assert.equal(nativeBudget.state.records[0].usage.outputTokens, 3);
    assert.equal(nativeBudget.state.requestMismatchFields, null);

    // #41: the installed serializer treats a non-sk- OpenAI credential as "Sign in with ChatGPT" and omits
    // max_output_tokens, so the guard must stop before dispatch and name only that field.
    let signInFetches = 0;
    const signInBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 100 } });
    const signInWrapped = wrapOpenAIProvider(nativeProvider, signInBudget, { fetchImpl: async () => { signInFetches++; throw new Error("sign-in request must not be dispatched"); } });
    let signInFailure = null;
    try {
      for await (const _event of signInWrapped.streamSimple(model, { messages: [{ role: "user", content: "Synthetic question." }] },
        { apiKey: "synthetic-chatgpt-sign-in-token", reasoning: "medium", reasoningEffort: "medium", maxTokens: 4096 })) { /* consume */ }
    } catch (error) { signInFailure = error.message; }
    assert.equal(signInFailure, "request-contract-mismatch");
    assert.equal(signInFetches, 0, "the sign-in request is stopped before the instrumented fetch");
    assert.equal(signInBudget.state.calls, 0);
    assert.deepEqual(signInBudget.state.requestMismatchFields, ["max_output_tokens"]);
    assert.doesNotMatch(JSON.stringify(signInBudget.state), /synthetic-chatgpt-sign-in-token/);

    async function runLazyNativeCase({ outputTokens = 5, responseModel = model.id, includeUsage = true, delayedCapture = false, limit = 5 } = {}) {
      const lazyBudget = createBudget({ limits: { calls: 3, minutes: 1, outputTokens: limit } });
      let fetchCount = 0;
      let releaseCapture;
      const rawEvents = [
        { type: "response.created", response: { id: "lazy-native", model: model.id } },
        { type: "response.output_item.added", output_index: 0, item: { id: "lazy-msg", type: "message", role: "assistant", content: [] } },
        { type: "response.output_text.delta", output_index: 0, item_id: "lazy-msg", content_index: 0, delta: "bounded response" },
        { type: "response.output_item.done", output_index: 0, item: { id: "lazy-msg", type: "message", role: "assistant", status: "completed", phase: "final_answer", content: [{ type: "output_text", text: "bounded response", annotations: [] }] } },
        { type: "response.completed", response: { id: "lazy-native", model: responseModel, status: "completed", output: [], ...(includeUsage ? { usage: { input_tokens: 2, output_tokens: outputTokens, total_tokens: outputTokens + 2 } } : {}) } },
      ];
      const payload = `${rawEvents.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`;
      const wrappedNative = wrapOpenAIProvider(nativeProvider, lazyBudget, { maxOutputTokens: 4096, effort: "medium", fetchImpl: async () => {
        fetchCount++;
        const response = new Response(payload, { status: 200, headers: { "content-type": "text/event-stream" } });
        Object.defineProperty(response, "clone", { value: () => new Response(new ReadableStream({ start(controller) {
          const release = () => { controller.enqueue(new TextEncoder().encode(payload)); controller.close(); };
          if (delayedCapture) releaseCapture = release; else release();
        } }), { status: 200, headers: { "content-type": "text/event-stream" } }) });
        return response;
      } });
      const stream = piAi.lazyStream(model, async () => wrappedNative.streamSimple(model,
        { messages: [{ role: "user", content: "synthetic bounded request" }] }, { apiKey: "sk-offline-synthetic-key", reasoning: "medium", maxTokens: 4096 }));
      const observed = [];
      const consuming = (async () => { for await (const event of stream) observed.push(event); })();
      let resultSettled = false;
      const resultPromise = stream.result().then((result) => { resultSettled = true; return result; });
      if (delayedCapture) {
        await new Promise((resolve) => setTimeout(resolve, 10));
        assert.equal(resultSettled, false, "Pi lazyStream must not resolve terminal completion before cloned raw usage is captured");
        assert.equal(lazyBudget.state.records[0].status, "attempted");
        assert.equal(lazyBudget.state.outputTokens, 0);
        assert.throws(() => wrappedNative.streamSimple(model, { messages: [] }, {}), /provider-attempt-in-flight/);
        assert.equal(fetchCount, 1, "an unfinished ledger blocks any subsequent dispatch");
        releaseCapture();
      }
      await consuming;
      const result = await resultPromise;
      return { result, events: observed, budget: lazyBudget.state, fetchCount };
    }
    const delayedLazy = await runLazyNativeCase({ delayedCapture: true, outputTokens: 5, limit: 5 });
    assert.equal(delayedLazy.result.stopReason, "stop", "exact output ceiling is accepted after capture finalizes");
    assert.equal(delayedLazy.budget.outputTokens, 5);
    assert.equal(delayedLazy.fetchCount, 1);
    const overLimitLazy = await runLazyNativeCase({ outputTokens: 6, limit: 5 });
    assert.equal(overLimitLazy.budget.stopCode, "output-token-limit");
    assert.ok(overLimitLazy.events.some((event) => event.type === "error"));
    assert.ok(!overLimitLazy.events.some((event) => event.type === "done"), "over-limit completion never escapes as a successful terminal event");
    assert.equal(overLimitLazy.fetchCount, 1);
    const missingUsageLazy = await runLazyNativeCase({ includeUsage: false });
    assert.equal(missingUsageLazy.budget.stopCode, "usage-unavailable");
    assert.ok(!missingUsageLazy.events.some((event) => event.type === "done"));
    assert.equal(missingUsageLazy.fetchCount, 1);
    const modelDriftLazy = await runLazyNativeCase({ responseModel: "unexpected-backend" });
    assert.equal(modelDriftLazy.budget.stopCode, "response-model-drift");
    assert.ok(!modelDriftLazy.events.some((event) => event.type === "done"));
    assert.equal(modelDriftLazy.fetchCount, 1);

    const missingUsageBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 100 } });
    const missingUsageProvider = wrapOpenAIProvider({ streamSimple: (_model, _context, options) => ({ async *[Symbol.asyncIterator]() {
      await options.fetch("https://api.openai.com/v1/responses", { method: "POST", body: JSON.stringify({ model: "fixture-model", max_output_tokens: 4096, reasoning: { effort: "medium" }, store: false, stream: true }) });
      yield { type: "completed" };
    } }) }, missingUsageBudget, { fetchImpl: async () => new Response(`data: ${JSON.stringify({ type: "response.completed", response: { status: "completed" } })}\n\n`, { status: 200 }) });
    await assert.rejects(async () => { for await (const _event of missingUsageProvider.streamSimple({ provider: "openai", api: "openai-responses", baseUrl: "https://api.openai.com/v1", id: "fixture-model" }, { messages: [] }, {})) {} }, /usage-unavailable/);
    assert.equal(missingUsageBudget.state.calls, 1);
    assert.equal(missingUsageBudget.state.stopped, true);

    const driftModelBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 100 } });
    const durableSnapshots = [];
    const driftModelProvider = wrapOpenAIProvider({ streamSimple: (_model, _context, options) => ({ async *[Symbol.asyncIterator]() {
      await options.fetch("https://api.openai.com/v1/responses", { method: "POST", body: JSON.stringify({ model: "fixture-model", max_output_tokens: 4096, reasoning: { effort: "medium" }, store: false, stream: true }) });
      yield { type: "completed" };
    } }) }, driftModelBudget, { onAttempt: () => durableSnapshots.push("pre-dispatch"), onAttemptComplete: (attempt) => durableSnapshots.push(attempt.status),
      fetchImpl: async () => new Response(`data: ${JSON.stringify({ type: "response.completed", response: { id: "resp-drift", model: "unexpected-backend", status: "completed", usage: { input_tokens: 2, output_tokens: 1, total_tokens: 3 } } })}\n\n`, { status: 200 }) });
    await assert.rejects(async () => { for await (const _event of driftModelProvider.streamSimple({ provider: "openai", api: "openai-responses", baseUrl: "https://api.openai.com/v1", id: "fixture-model" }, { messages: [] }, {})) {} }, /response-model-drift/);
    assert.deepEqual(durableSnapshots, ["pre-dispatch", "completed"]);
    assert.equal(driftModelBudget.state.stopped, true, "response-model drift must latch the whole run after recording actual usage");
    const unknownModelBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 100 } });
    const unknownModelProvider = wrapOpenAIProvider({ streamSimple: (_model, _context, options) => ({ async *[Symbol.asyncIterator]() {
      await options.fetch("https://api.openai.com/v1/responses", { method: "POST", body: JSON.stringify({ model: "fixture-model", max_output_tokens: 4096, reasoning: { effort: "medium" }, store: false, stream: true }) });
      yield { type: "completed" };
    } }) }, unknownModelBudget, { fetchImpl: async () => new Response(`data: ${JSON.stringify({ type: "response.completed", response: { id: "resp-unknown-model", status: "completed", usage: { input_tokens: 2, output_tokens: 1, total_tokens: 3 } } })}\n\n`, { status: 200 }) });
    for await (const _event of unknownModelProvider.streamSimple({ provider: "openai", api: "openai-responses", baseUrl: "https://api.openai.com/v1", id: "fixture-model" }, { messages: [] }, {})) { /* consume */ }
    assert.equal(unknownModelBudget.state.records[0].responseModel, null, "missing backend identity is explicitly unknown");
    assert.equal(unknownModelBudget.state.records[0].usage.responseModel, null);

    const unobservedBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 100 } });
    const unobservedProvider = wrapOpenAIProvider({ streamSimple: () => ({ async *[Symbol.asyncIterator]() { yield { type: "done", reason: "stop", message: {} }; } }) }, unobservedBudget);
    await assert.rejects(async () => { for await (const _event of unobservedProvider.streamSimple(model, { messages: [] }, {})) { /* consume */ } }, /unsupported-openai-route/);
    assert.equal(unobservedBudget.state.calls, 0, "a provider that does not call the injected fetch fails without fabricated attempt evidence");

    for (const status of [401, 429]) {
      let statusAttempts = 0;
      const statusBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 100 } });
      const statusProvider = wrapOpenAIProvider(nativeProvider, statusBudget, { fetchImpl: async () => {
        statusAttempts++;
        return new Response(JSON.stringify({ error: { message: "synthetic denied" } }), { status, headers: { "content-type": "application/json" } });
      } });
      const statusStream = statusProvider.streamSimple(model, { messages: [{ role: "user", content: "Synthetic question." }] }, { apiKey: "sk-offline-synthetic-key", reasoning: "medium", maxTokens: 4096 });
      try { for await (const _event of statusStream) { /* consume */ } } catch { /* stable failure expected */ }
      assert.equal(statusAttempts, 1, `${status} responses must not trigger HTTP/provider retries`);
      assert.equal(statusBudget.state.calls, 1);
      assert.equal(statusBudget.state.stopped, true);
    }

    const driftBudget = createBudget({ limits: { calls: 2, minutes: 1, outputTokens: 10 } });
    let driftFetches = 0;
    const driftProvider = wrapOpenAIProvider({ streamSimple: (_model, _context, options) => ({ async *[Symbol.asyncIterator]() {
      await options.fetch("https://api.openai.com/v1/responses", { method: "POST", body: JSON.stringify({ model: "fixture-model", max_output_tokens: 4096, reasoning: { effort: "medium" }, store: false, stream: true }) });
    } }) }, driftBudget, { beforeDispatch: () => driftBudget.stop("code-identity-drift"), fetchImpl: async () => { driftFetches++; return new Response(); } });
    await assert.rejects(async () => { for await (const _event of driftProvider.streamSimple({ provider: "openai", api: "openai-responses", baseUrl: "https://api.openai.com/v1", id: "fixture-model" }, { messages: [] }, {})) {} }, /code-identity-drift/);
    assert.equal(driftFetches, 0);
    assert.equal(driftBudget.state.calls, 0);

    const vault = path.join(outside, "synthetic-vault");
    const wikiDir = path.join(vault, "wiki");
    fs.mkdirSync(wikiDir, { recursive: true });
    fs.writeFileSync(path.join(vault, ".knowledge-maintenance-synthetic"), "obsidian-second-brain synthetic knowledge-maintenance vault v1\n");
    const pageContent = "---\ntype: note\ntitle: Acorn retention\n---\n# Acorn retention\n\nThe fictional Acorn service keeps requests for 30 days.\n";
    const pagePath = "wiki/retention.md";
    fs.writeFileSync(path.join(wikiDir, "retention.md"), pageContent);
    const pageHash = hash(pageContent);
    const pages = [{ path: pagePath, content: pageContent, sha256: pageHash }];
    const workdirPath = path.join(outside, "coordinator-run");
    // Keep this synthetic SDK-runner fixture on built-in models shared by CI Pi 1.4 and local Pi 1.5.
    const liveConfig = { live: true, authFile: syntheticAuthFile, vaultRoot: vault, workdir: workdirPath,
      solver: { provider: "openai", model: "gpt-6.1-sol", effort: "medium" }, judge: { provider: "openai", model: "gpt-6.1-sol", effort: "medium" },
      limits: { calls: 147, minutes: 4, outputTokens: 10000, maxOutputTokens: 4096 } };
    const offlineManifest = { ...manifest, liveEnabled: true, mode: "live" };
    const cpOne = fixture.checkpoints[0];
    const retention = fixture.visibleTasks.find((task) => task.id === "retention-current");
    const fullRows = fixture.checkpoints.flatMap((checkpoint) => fixture.visibleTasks.flatMap((task) => ["en", "it"].flatMap((language) => ["sources", "sources+wiki", "wiki-only"].map((condition) => ({
      id: `${checkpoint.id}/${task.id}/${language}/${condition}`, checkpoint: checkpoint.id, taskId: task.id, intent: task.intent, topic: task.topic,
      language, condition, question: task.question[language] })))));
    const failedRow = fullRows.at(-1);
    const overlapConfig = { ...liveConfig, workdir: path.join(vault, "nested-workdir") };
    await assert.rejects(runLive(overlapConfig, offlineManifest, fixture, "owner-overlap", { sdk, authModule, runtime }), /workdir-vault-overlap/);
    assert.equal(fs.existsSync(overlapConfig.workdir), false, "coordinator rejects vault/workdir overlap before creating outputs");

    const actualRunConfig = { ...liveConfig, workdir: path.join(outside, "actual-pi-runner"), limits: { ...liveConfig.limits, calls: 1 } };
    const actualRuntime = await loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime: sdk.ModelRuntime, readStoredCredential: authModule.readStoredCredential, authFile: syntheticAuthFile });
    let actualFetchCount = 0;
    const actualOutput = "Maintenance finished without changing the synthetic wiki.";
    const actualFetch = async (_url, init) => {
      actualFetchCount++;
      const request = JSON.parse(init.body);
      const responseId = "actual-runner-fake-response";
      const messageId = "actual-runner-fake-message";
      const events = [
        { type: "response.created", response: { id: responseId, model: request.model } },
        { type: "response.output_item.added", output_index: 0, item: { id: messageId, type: "message", role: "assistant", content: [] } },
        { type: "response.output_text.delta", output_index: 0, item_id: messageId, content_index: 0, delta: actualOutput },
        { type: "response.output_item.done", output_index: 0, item: { id: messageId, type: "message", role: "assistant", status: "completed", phase: "final_answer", content: [{ type: "output_text", text: actualOutput, annotations: [] }] } },
        { type: "response.completed", response: { id: responseId, model: request.model, status: "completed", output: [], usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5 } } },
      ];
      return new Response(`${events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
    const actualRunResult = await runLive(actualRunConfig, { ...offlineManifest, externalWorkdir: actualRunConfig.workdir }, fixture, "owner-actual-runner", {
      sdk, authModule, runtime: actualRuntime, fetchImpl: actualFetch,
    });
    const actualRunMetadata = JSON.parse(fs.readFileSync(path.join(actualRunConfig.workdir, "run-metadata.json"), "utf8"));
    assert.equal(actualRunResult.stopCode, "call-limit", JSON.stringify({ message: "actual runner construction reaches the bounded next judge attempt", actualFetchCount, attempts: actualRunMetadata.attempts, sessions: actualRunMetadata.sessions, stages: actualRunMetadata.stages }));
    assert.equal(actualFetchCount, 1, "actual Pi runner performs exactly one fake provider HTTP call before the shared call ceiling");
    assert.equal(actualRunMetadata.attempts.length, 1);
    assert.equal(actualRunMetadata.sessions[0].phase, "maintenance:2025-01-10");
    assert.ok(fs.existsSync(path.join(actualRunConfig.workdir, "artifact-v2.json")));

    const { ModelRegistry } = await import(pathToFileURL(path.join(packageRoot, "dist/core/model-registry.js")).href);
    const nativeHost = await sdk.ModelRuntime.create({ credentials: AuthStorage.inMemory({ openai: { type: "api_key", key: "sk-offline-host-sentinel" } }), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
    await nativeHost.refresh({ allowNetwork: false });
    const hostRegistryFacade = new ModelRegistry(nativeHost);
    const nativeBaseline = await ModelRuntime.create({ credentials: AuthStorage.inMemory(), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
    const hostRegistry = {
      find: (...args) => hostRegistryFacade.find(...args),
      getAll: () => hostRegistryFacade.getAll(),
      getAvailable: (...args) => hostRegistryFacade.getAvailable(...args),
      getError: (...args) => hostRegistryFacade.getError(...args),
      getProviderAuthStatus: (...args) => hostRegistryFacade.getProviderAuthStatus(...args),
      hasConfiguredAuth: (...args) => hostRegistryFacade.hasConfiguredAuth(...args),
      isUsingOAuth: (...args) => hostRegistryFacade.isUsingOAuth(...args),
      getRegisteredNativeProvider: (...args) => hostRegistryFacade.getRegisteredNativeProvider(...args),
      getRegisteredProviderConfig: (...args) => hostRegistryFacade.getRegisteredProviderConfig(...args),
      getProvider: (...args) => hostRegistryFacade.getProvider(...args),
      stream: (...args) => hostRegistryFacade.stream(...args),
      streamSimple: (model, context, options = {}) => {
        assert.equal(Object.hasOwn(options, "apiKey"), false, "isolated-runtime auth sentinel is not forwarded to host auth");
        assert.equal(Object.hasOwn(options, "headers"), false, "isolated-runtime headers are not forwarded to host auth");
        assert.equal(Object.hasOwn(options, "env"), false, "isolated-runtime environment overrides are not forwarded to host auth");
        assert.equal(Object.hasOwn(options, "transformHeaders"), false, "isolated-runtime hooks cannot inspect or modify host auth headers");
        assert.equal(typeof options.fetch, "function", "dispatch accounting fetch reaches the public host registry stream");
        return hostRegistryFacade.streamSimple(model, context, options);
      },
    };
    const nativeModels = [liveConfig.solver, liveConfig.judge].map(({ provider, model }) => hostRegistry.find(provider, model));
    let rejectedNativeStreamCalls = 0;
    const hostileRegistry = {
      ...hostRegistry,
      getRegisteredNativeProvider: () => ({ id: "openai", streamSimple() { rejectedNativeStreamCalls++; throw new Error("must-not-stream-custom-provider"); } }),
    };
    assert.throws(() => preflightNativeProvider({ hostRegistry: hostileRegistry, baselineRegistry: nativeBaseline, models: nativeModels }), /unsupported-native-provider-route/);
    assert.equal(rejectedNativeStreamCalls, 0, "unsupported native overrides are rejected before any provider delegate can run");
    const overlayRegistry = { ...hostRegistry, getRegisteredProviderConfig: () => ({ baseUrl: "https://opaque-extension.invalid/v1" }) };
    assert.throws(() => preflightNativeProvider({ hostRegistry: overlayRegistry, baselineRegistry: nativeBaseline, models: nativeModels }), /unsupported-native-provider-route/);
    assert.ok(nativeModels.every(Boolean), "offline host registry exposes both selected models");

    let overlayStreamCalls = 0;
    let overlayFetchCalls = 0;
    const originalOverlayFetch = globalThis.fetch;
    globalThis.fetch = async () => { overlayFetchCalls++; throw new Error("command-valued models.json route must not fetch"); };
    try {
      for (const [name, providerConfig] of [
        ["api-key", { apiKey: `!touch ${path.join(outside, "native-api-key-command-ran")}` }],
        ["header", { headers: { "X-Native-Sentinel": `!touch ${path.join(outside, "native-header-command-ran")}` } }],
      ]) {
        const overlayConfigPath = path.join(outside, `native-${name}-models.json`);
        fs.writeFileSync(overlayConfigPath, JSON.stringify({ providers: { openai: providerConfig } }));
        const overlayRuntime = await ModelRuntime.create({ credentials: AuthStorage.inMemory({ openai: { type: "api_key", key: "sk-offline-overlay-sentinel" } }),
          modelsPath: overlayConfigPath, refreshOnCreate: false, allowModelNetwork: false });
        const overlayFacade = new ModelRegistry(overlayRuntime);
        const composedRegistry = {
          getProvider: (...args) => overlayFacade.getProvider(...args),
          find: (...args) => overlayFacade.find(...args),
          getAvailable: (...args) => overlayFacade.getAvailable(...args),
          getError: (...args) => overlayFacade.getError(...args),
          getProviderAuthStatus: (...args) => overlayFacade.getProviderAuthStatus(...args),
          hasConfiguredAuth: (...args) => overlayFacade.hasConfiguredAuth(...args),
          isUsingOAuth: (...args) => overlayFacade.isUsingOAuth(...args),
          getRegisteredNativeProvider: (...args) => overlayFacade.getRegisteredNativeProvider(...args),
          getRegisteredProviderConfig: (...args) => overlayFacade.getRegisteredProviderConfig(...args),
          streamSimple: (...args) => { overlayStreamCalls++; return overlayFacade.streamSimple(...args); },
        };
        const composedModels = nativeModels.map((model) => overlayFacade.find(model.provider, model.id));
        assert.throws(() => preflightNativeProvider({ hostRegistry: composedRegistry, baselineRegistry: nativeBaseline, models: composedModels }), /unsupported-native-provider-route/,
          `models.json ${name} command-valued config fails closed before dispatch`);
        overlayRuntime.dispose?.();
      }
    } finally { globalThis.fetch = originalOverlayFetch; }
    assert.equal(overlayStreamCalls, 0, "command-config rejection precedes host provider stream resolution");
    assert.equal(overlayFetchCalls, 0, "command-config rejection precedes fake fetch dispatch");
    assert.equal(fs.existsSync(path.join(outside, "native-api-key-command-ran")), false, "synthetic API-key command never runs");
    assert.equal(fs.existsSync(path.join(outside, "native-header-command-ran")), false, "synthetic header command never runs");

    const fallbackSentinel = "!km-review-sentinel-must-not-execute";
    const fallbackModelsPath = path.join(outside, "native-semantic-composition-error.json");
    fs.writeFileSync(fallbackModelsPath, JSON.stringify({
      providers: { openai: { oauth: "radius", modelOverrides: { "gpt-6.1-sol": { headers: { "X-KM-Sentinel": fallbackSentinel } } } } },
    }));
    const fallbackRuntime = await ModelRuntime.create({ credentials: AuthStorage.inMemory({ openai: { type: "api_key", key: "sk-offline-fallback-sentinel" } }),
      modelsPath: fallbackModelsPath, refreshOnCreate: false, allowModelNetwork: false });
    const fallbackFacade = new ModelRegistry(fallbackRuntime);
    assert.match(fallbackFacade.getError(), /baseUrl.*required/u, "schema-valid semantic composition failure remains publicly diagnosable");
    const fallbackModels = nativeModels.map((model) => fallbackFacade.find(model.provider, model.id));
    let fallbackCommandResolutions = 0;
    let fallbackStreamCalls = 0;
    let fallbackFetchCalls = 0;
    const originalExecSync = childProcess.execSync;
    const originalSpawnSync = childProcess.spawnSync;
    const originalFallbackFetch = globalThis.fetch;
    childProcess.execSync = () => { fallbackCommandResolutions++; return "intercepted"; };
    childProcess.spawnSync = () => { fallbackCommandResolutions++; return { status: 0, stdout: "intercepted" }; };
    syncBuiltinESMExports();
    globalThis.fetch = async () => { fallbackFetchCalls++; throw new Error("fallback route must not fetch"); };
    try {
      const fallbackRegistry = {
        ...hostRegistry,
        find: (...args) => fallbackFacade.find(...args),
        getAvailable: (...args) => fallbackFacade.getAvailable(...args),
        getError: (...args) => fallbackFacade.getError(...args),
        getProvider: (...args) => fallbackFacade.getProvider(...args),
        getProviderAuthStatus: (...args) => fallbackFacade.getProviderAuthStatus(...args),
        hasConfiguredAuth: (...args) => fallbackFacade.hasConfiguredAuth(...args),
        isUsingOAuth: (...args) => fallbackFacade.isUsingOAuth(...args),
        getRegisteredNativeProvider: (...args) => fallbackFacade.getRegisteredNativeProvider(...args),
        getRegisteredProviderConfig: (...args) => fallbackFacade.getRegisteredProviderConfig(...args),
        streamSimple: (...args) => { fallbackStreamCalls++; return fallbackFacade.streamSimple(...args); },
      };
      assert.throws(() => preflightNativeProvider({ hostRegistry: fallbackRegistry, baselineRegistry: nativeBaseline, models: fallbackModels }), /unsupported-native-provider-route/);
    } finally {
      globalThis.fetch = originalFallbackFetch;
      childProcess.execSync = originalExecSync;
      childProcess.spawnSync = originalSpawnSync;
      syncBuiltinESMExports();
      fallbackRuntime.dispose?.();
    }
    assert.equal(fallbackCommandResolutions, 0, "semantic composition rejection occurs before command-valued header resolution");
    assert.equal(fallbackStreamCalls, 0, "semantic composition rejection precedes host provider streaming");
    assert.equal(fallbackFetchCalls, 0, "semantic composition rejection precedes model fetch");

    const unknownAuthRegistry = { ...hostRegistry, isUsingOAuth: () => false, hasConfiguredAuth: () => false,
      getProviderAuthStatus: () => ({ configured: false }), getAvailable: () => [] };
    assert.throws(() => preflightNativeProvider({ hostRegistry: unknownAuthRegistry, baselineRegistry: nativeBaseline, models: nativeModels }), /unsupported-native-provider-route/,
      "an uninitialized/unknown false OAuth snapshot is not accepted as proof of API-key auth");
    assert.throws(() => preflightNativeProvider({ hostRegistry: { ...hostRegistry, isUsingOAuth: undefined }, baselineRegistry: nativeBaseline, models: nativeModels }), /native-provider-bridge-unavailable/,
      "missing public auth-mode evidence fails closed");
    assert.throws(() => preflightNativeProvider({ hostRegistry: { ...hostRegistry, isUsingOAuth: () => 0 }, baselineRegistry: nativeBaseline, models: nativeModels }), /unsupported-native-provider-route/,
      "OAuth mode evidence must be the strict boolean false");
    assert.throws(() => preflightNativeProvider({ hostRegistry: { ...hostRegistry, hasConfiguredAuth: () => 1 }, baselineRegistry: nativeBaseline, models: nativeModels }), /unsupported-native-provider-route/,
      "configured-auth evidence must be the strict boolean true");
    assert.throws(() => preflightNativeProvider({ hostRegistry: { ...hostRegistry, getError: () => null }, baselineRegistry: nativeBaseline, models: nativeModels }), /unsupported-native-provider-route/,
      "only a clear undefined registry diagnostic is accepted");

    const oauthBaseStore = AuthStorage.inMemory({ openai: { type: "oauth", access: "synthetic-access", refresh: "synthetic-refresh", expires: Date.now() + 10 * 60_000 } });
    const oauthStoreCounts = { read: 0, list: 0, modify: 0 };
    const oauthStore = {
      read: (...args) => { oauthStoreCounts.read++; return oauthBaseStore.read(...args); },
      list: (...args) => { oauthStoreCounts.list++; return oauthBaseStore.list(...args); },
      modify: (...args) => { oauthStoreCounts.modify++; return oauthBaseStore.modify(...args); },
      delete: (...args) => oauthBaseStore.delete(...args),
    };
    const oauthRuntime = await ModelRuntime.create({ credentials: oauthStore, modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
    await oauthRuntime.refresh({ allowNetwork: false });
    const oauthFacade = new ModelRegistry(oauthRuntime);
    const oauthModel = oauthFacade.find("openai", "gpt-6.1-sol");
    assert.ok(oauthModel);
    assert.equal(oauthFacade.isUsingOAuth(oauthModel), true, "synthetic OAuth mode is initialized in the public registry snapshot");
    assert.equal(oauthFacade.hasConfiguredAuth(oauthModel), true);
    assert.deepEqual(oauthFacade.getProviderAuthStatus("openai"), { configured: true, source: "stored" });
    await oauthBaseStore.modify("openai", async (credential) => ({ ...credential, expires: Date.now() + 30_000 }));
    oauthStoreCounts.read = 0;
    oauthStoreCounts.list = 0;
    oauthStoreCounts.modify = 0;
    let oauthRefreshCalls = 0;
    let oauthStreamCalls = 0;
    let oauthFetchCalls = 0;
    const originalOAuthFetch = globalThis.fetch;
    globalThis.fetch = async () => { oauthFetchCalls++; throw new Error("OAuth must be rejected before refresh/model fetch"); };
    try {
      const oauthRegistry = {
        ...hostRegistry,
        find: (...args) => oauthFacade.find(...args),
        getAvailable: (...args) => oauthFacade.getAvailable(...args),
        getError: (...args) => oauthFacade.getError(...args),
        getProviderAuthStatus: (...args) => oauthFacade.getProviderAuthStatus(...args),
        hasConfiguredAuth: (...args) => oauthFacade.hasConfiguredAuth(...args),
        isUsingOAuth: (...args) => oauthFacade.isUsingOAuth(...args),
        getProvider: (...args) => oauthFacade.getProvider(...args),
        streamSimple: (...args) => { oauthStreamCalls++; return oauthFacade.streamSimple(...args); },
        refresh: () => { oauthRefreshCalls++; throw new Error("OAuth admission must not refresh the host registry"); },
      };
      assert.throws(() => preflightNativeProvider({ hostRegistry: oauthRegistry, baselineRegistry: nativeBaseline, models: [oauthModel] }), /unsupported-native-provider-route/,
        "known configured OAuth auth is not eligible for native host delegation");
    } finally { globalThis.fetch = originalOAuthFetch; }
    assert.deepEqual(oauthStoreCounts, { read: 0, list: 0, modify: 0 }, "OAuth admission reads no credential values and performs no refresh/store update");
    assert.equal(oauthRefreshCalls, 0, "OAuth admission does not refresh the host registry");
    assert.equal(oauthStreamCalls, 0, "OAuth rejection precedes host provider stream");
    assert.equal(oauthFetchCalls, 0, "OAuth rejection precedes refresh and model fetch");
    oauthRuntime.dispose?.();

    const delegatedRuntime = await createNativeProviderRuntime({ sdk, AuthStorage, hostRegistry, models: nativeModels });
    const nativeRunConfig = { ...liveConfig, authFile: null, workdir: path.join(outside, "native-delegated-run"), limits: { ...liveConfig.limits, calls: 1 } };
    const nativeManifest = dryRun({ ...nativeRunConfig, live: false }, fixture);
    await assert.rejects(runLive({ ...nativeRunConfig, authFile: path.join(outside, "must-not-be-read.json") }, nativeManifest, fixture, "owner-native-auth-file", { nativeMode: true }), /native-mode-rejects-auth-file/);
    assert.equal(fs.existsSync(path.join(outside, "must-not-be-read.json")), false, "native mode rejects an auth-file path without probing it");
    const partialRunDir = path.join(outside, "native-preflight-partial");
    const partialToken = "owner-native-preflight-partial";
    const partialConfig = { ...nativeRunConfig, workdir: partialRunDir };
    const partialManifest = dryRun({ ...partialConfig, live: false }, fixture);
    await assert.rejects(runLive(partialConfig, { ...partialManifest, liveEnabled: true, mode: "live", externalWorkdir: partialRunDir }, fixture, partialToken, {
      nativeMode: true, runtime: { getModel: () => undefined, dispose() {} },
    }), /configured-openai-model-unavailable/);
    recordPreflightFailure(partialRunDir, partialToken);
    const partialMetadata = JSON.parse(fs.readFileSync(path.join(partialRunDir, "run-metadata.json"), "utf8"));
    validateRunMetadata(partialMetadata);
    assert.equal(partialMetadata.stopCode, "preflight-failed");
    assert.ok(fs.existsSync(path.join(partialRunDir, "artifact-v2.json")));
    assert.ok(fs.existsSync(path.join(partialRunDir, "score.json")), "native preflight failures retain a scored sealed partial artifact");
    let nativeFetchCount = 0;
    let nativeRequestFields;
    const nativeFetch = async (url, init) => {
      nativeFetchCount++;
      assert.equal(new URL(url).origin, "https://api.openai.com");
      assert.equal(new URL(url).pathname, "/v1/responses");
      const request = JSON.parse(init.body);
      nativeRequestFields = { model: request.model, max_output_tokens: request.max_output_tokens ?? null, reasoning_effort: request.reasoning?.effort ?? null, store: request.store ?? null, stream: request.stream ?? null };
      const responseId = "native-delegated-offline-response";
      const messageId = "native-delegated-offline-message";
      const events = [
        { type: "response.created", response: { id: responseId, model: request.model } },
        { type: "response.output_item.added", output_index: 0, item: { id: messageId, type: "message", role: "assistant", content: [] } },
        { type: "response.output_text.delta", output_index: 0, item_id: messageId, content_index: 0, delta: "Offline scoped maintenance result." },
        { type: "response.output_item.done", output_index: 0, item: { id: messageId, type: "message", role: "assistant", status: "completed", phase: "final_answer", content: [{ type: "output_text", text: "Offline scoped maintenance result.", annotations: [] }] } },
        { type: "response.completed", response: { id: responseId, model: request.model, status: "completed", output: [], usage: { input_tokens: 5, output_tokens: 4, total_tokens: 9 } } },
      ];
      return new Response(`${events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
    const nativeRun = await runLive(nativeRunConfig, { ...nativeManifest, liveEnabled: true, mode: "live", externalWorkdir: nativeRunConfig.workdir }, fixture, "owner-native-offline", {
      sdk, authModule, runtime: delegatedRuntime, nativeMode: true,
      nativeBaseUrls: new Map(nativeModels.map((model) => [`${model.provider}/${model.id}`, model.baseUrl])), fetchImpl: nativeFetch,
    });
    const nativeMetadata = JSON.parse(fs.readFileSync(path.join(nativeRunConfig.workdir, "run-metadata.json"), "utf8"));
    assert.equal(nativeRun.stopCode, "call-limit");
    assert.equal(nativeFetchCount, 1, "host registry delegation reaches only the injected fake fetch before the shared call ceiling");
    assert.deepEqual(nativeRequestFields, { model: "gpt-6.1-sol", max_output_tokens: 4096, reasoning_effort: "medium", store: false, stream: true });
    assert.deepEqual({ calls: nativeMetadata.attempts.length, outputTokens: nativeMetadata.attempts[0].usage.outputTokens, inputTokens: nativeMetadata.attempts[0].usage.inputTokens, totalTokens: nativeMetadata.attempts[0].usage.totalTokens }, { calls: 1, outputTokens: 4, inputTokens: 5, totalTokens: 9 });
    assert.equal(nativeMetadata.runtime.solver.origin, "https://api.openai.com");
    assert.equal(nativeMetadata.attempts[0].phase, "maintenance:2025-01-10");
    assert.ok(fs.existsSync(path.join(nativeRunConfig.workdir, "artifact-v2.json")));

    let changingOAuthMode = false;
    let changingHostStreams = 0;
    let changingHostFetches = 0;
    const changingRegistry = { ...hostRegistry,
      isUsingOAuth: () => changingOAuthMode,
      streamSimple: () => { changingHostStreams++; throw new Error("delegation admission must reject before the host stream"); },
      stream: () => { changingHostStreams++; throw new Error("delegation admission must reject before the host stream"); },
    };
    const originalChangingFetch = globalThis.fetch;
    globalThis.fetch = async () => { changingHostFetches++; throw new Error("delegation admission must reject before fetch"); };
    try {
      const changingRuntime = await createNativeProviderRuntime({ sdk, AuthStorage, hostRegistry: changingRegistry, models: [nativeModels[0]] });
      changingOAuthMode = true;
      const changingEvents = [];
      for await (const event of changingRuntime.streamSimple(nativeModels[0], { messages: [] }, { maxTokens: 32, reasoning: "medium" })) changingEvents.push(event);
      assert.ok(changingEvents.some((event) => event.type === "error"), "delegation-time auth-mode change returns a stable provider error");
    } finally { globalThis.fetch = originalChangingFetch; }
    assert.equal(changingHostStreams, 0, "auth-mode admission is rechecked immediately before public host delegation");
    assert.equal(changingHostFetches, 0, "a changed OAuth mode cannot reach provider fetch");
    nativeBaseline.dispose?.();
    nativeHost.dispose?.();

    const exerciseJudgeEffort = async (label, judgeModel) => {
      const roleVault = path.join(outside, `${label}-vault`);
      const roleWiki = path.join(roleVault, "wiki");
      const roleWorkdir = path.join(outside, `${label}-run`);
      fs.mkdirSync(roleWiki, { recursive: true });
      fs.writeFileSync(path.join(roleVault, ".knowledge-maintenance-synthetic"), "obsidian-second-brain synthetic knowledge-maintenance vault v1\n");
      execFileSync("git", ["init", "--quiet", roleVault], { cwd: tmp, env: process.env });
      fs.writeFileSync(path.join(roleVault, ".gitignore"), ".vault-meta/\n");
      const roleConfig = { ...liveConfig, workdir: roleWorkdir, vaultRoot: roleVault, solver: { ...liveConfig.solver, effort: "medium" },
        judge: { provider: "openai", model: judgeModel, effort: "high" }, limits: { calls: 8, minutes: 2, outputTokens: 10000, maxOutputTokens: 256 } };
      const initialRoleManifest = dryRun({ ...roleConfig, live: false }, fixture);
      const roleRuntime = await loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime: sdk.ModelRuntime, readStoredCredential: authModule.readStoredCredential, authFile: syntheticAuthFile });
      const roleRequests = [];
      const roleFetch = async (_url, init) => {
        const request = JSON.parse(init.body);
        roleRequests.push(request);
        const responseId = `role-effort-${roleRequests.length}`;
        const output = "{}";
        const events = [
          { type: "response.created", response: { id: responseId, model: request.model } },
          { type: "response.output_item.added", output_index: 0, item: { id: `msg-${responseId}`, type: "message", role: "assistant", content: [] } },
          { type: "response.output_text.delta", output_index: 0, item_id: `msg-${responseId}`, content_index: 0, delta: output },
          { type: "response.output_item.done", output_index: 0, item: { id: `msg-${responseId}`, type: "message", role: "assistant", status: "completed", phase: "final_answer", content: [{ type: "output_text", text: output, annotations: [] }] } },
          { type: "response.completed", response: { id: responseId, model: request.model, status: "completed", output: [], usage: { input_tokens: 2, output_tokens: 1, total_tokens: 3 } } },
        ];
        return new Response(`${events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`, { status: 200, headers: { "content-type": "text/event-stream" } });
      };
      const result = await runLive(roleConfig, { ...initialRoleManifest, liveEnabled: true, mode: "live", externalWorkdir: roleWorkdir }, fixture, `owner-${label}`, {
        sdk, authModule, runtime: roleRuntime, createPiRunner: async (options) => {
          const runner = await createPiRunner({ ...options, sdkPackage: packageRoot });
          runner.rows = [];
          return runner;
        },
        buildRetrieval: async ({ vaultRoot }) => { buildBm25(vaultRoot); }, fetchImpl: roleFetch,
      });
      const metadata = JSON.parse(fs.readFileSync(path.join(roleWorkdir, "run-metadata.json"), "utf8"));
      validateRunMetadata(metadata);
      return { result, metadata, requests: roleRequests };
    };
    const sameModelEffort = await exerciseJudgeEffort("same-model-effort", "gpt-6.1-sol");
    assert.ok((sameModelEffort.requests.length === 1 && sameModelEffort.result.stopCode === "request-contract-mismatch") ||
      (sameModelEffort.requests.length === fixture.checkpoints.length * 2 && sameModelEffort.result.stopCode === null),
    JSON.stringify({ stopCode: sameModelEffort.result.stopCode, requests: sameModelEffort.requests.map((request) => request.reasoning?.effort), attempts: sameModelEffort.metadata.attempts }));
    assert.equal(sameModelEffort.metadata.runtime.solver.effort, "medium");
    assert.equal(sameModelEffort.metadata.runtime.judge.effort, "high");
    assert.equal(sameModelEffort.requests[0].model, "gpt-6.1-sol");
    assert.equal(sameModelEffort.requests[0].reasoning?.effort, "medium", "same-model solver request uses the solver's configured effort");
    assert.ok(sameModelEffort.requests.slice(1).every((request) => request.reasoning?.effort === "high"), "same-model judge requests use the judge effort or are rejected before fetch");
    assert.ok(sameModelEffort.metadata.attempts.every((attempt) => attempt.model === "gpt-6.1-sol"));
    assert.ok(sameModelEffort.metadata.attempts[0].phase.startsWith("maintenance:"));
    const distinctModelEffort = await exerciseJudgeEffort("distinct-model-effort", "gpt-6-sol");
    assert.equal(distinctModelEffort.result.stopCode, "request-contract-mismatch", "the installed SDK's native medium payload cannot satisfy configured high judge effort");
    assert.equal(distinctModelEffort.requests.length, 1, "distinct-model judge mismatch is rejected before fake fetch after the solver dispatch");
    assert.equal(distinctModelEffort.requests[0].model, "gpt-6.1-sol");
    assert.equal(distinctModelEffort.requests[0].reasoning?.effort, "medium");
    assert.equal(distinctModelEffort.metadata.runtime.solver.effort, "medium");
    assert.equal(distinctModelEffort.metadata.runtime.judge.effort, "high");
    assert.equal(distinctModelEffort.metadata.attempts.length, 1, "rejected judge request is not recorded as dispatched");
    assert.deepEqual(distinctModelEffort.metadata.requestMismatchFields, ["reasoning.effort"], "persisted run metadata names only the mismatched field");
    assert.equal(distinctModelEffort.metadata.attempts[0].phase, "maintenance:2025-01-10");

    const checkUnsafeVaultEntries = (root) => {
      const unsafe = [];
      const walk = (directory) => { for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
        const target = path.join(directory, entry.name); const stat = fs.lstatSync(target);
        if (stat.isSymbolicLink()) unsafe.push(`${path.relative(root, target)}:symlink`);
        else if (stat.isDirectory()) walk(target);
        else if (stat.isFile() && stat.nlink !== 1) unsafe.push(`${path.relative(root, target)}:hardlink:${stat.nlink}`);
      } };
      walk(root); return unsafe;
    };
    assert.deepEqual(checkUnsafeVaultEntries(vault), [], "ordinary coordinator vault retains safe single-link derived state between runs");
    const watchdogConfig = { ...liveConfig, workdir: path.join(outside, "watchdog-run"), limits: { ...liveConfig.limits, minutes: 1 } };
    const watchdogBaseRuntime = await loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime: sdk.ModelRuntime, readStoredCredential: authModule.readStoredCredential, authFile: syntheticAuthFile });
    let watchdogRuntimeDisposed = false;
    const disposeWatchdogRuntime = watchdogBaseRuntime.dispose?.bind(watchdogBaseRuntime) ?? (() => {});
    watchdogBaseRuntime.dispose = () => { watchdogRuntimeDisposed = true; disposeWatchdogRuntime(); };
    const watchdogRuntime = watchdogBaseRuntime;
    let coordinatorWatchdog;
    let watchdogRunner;
    let watchdogFetchCount = 0;
    let fetchSignalAborted = false;
    const watchdogResult = await runLive(watchdogConfig, { ...offlineManifest, externalWorkdir: watchdogConfig.workdir }, fixture, "owner-watchdog", {
      sdk, authModule, runtime: watchdogRuntime, createPiRunner: async (options) => {
        watchdogRunner = await createPiRunner(options);
        watchdogRunner.assertVaultTreeSafe();
        return watchdogRunner;
      },
      setTimeoutImpl: (callback, delay) => { assert.equal(delay, 60_000); coordinatorWatchdog = callback; return null; },
      fetchImpl: (_url, init) => {
        watchdogFetchCount++;
        return new Promise((_resolve, reject) => {
          const signal = init.signal;
          assert.ok(signal, "native provider request carries the coordinator's abort signal");
          const onAbort = () => { fetchSignalAborted = true; reject(new DOMException("synthetic watchdog abort", "AbortError")); };
          signal.addEventListener("abort", onAbort, { once: true });
          assert.equal(typeof coordinatorWatchdog, "function", "coordinator watchdog is installed before dispatch");
          coordinatorWatchdog();
          if (signal.aborted) onAbort();
        });
      },
    });
    const watchdogMetadata = JSON.parse(fs.readFileSync(path.join(watchdogConfig.workdir, "run-metadata.json"), "utf8"));
    const watchdogArtifact = JSON.parse(fs.readFileSync(path.join(watchdogConfig.workdir, "artifact-v2.json"), "utf8"));
    assert.equal(watchdogResult.stopCode, "time-limit", JSON.stringify({ fetchCount: watchdogFetchCount, fetchSignalAborted, attempts: watchdogMetadata.attempts, sessions: watchdogMetadata.sessions, stages: watchdogMetadata.stages, runnerSessions: watchdogRunner?.sessionLog }));
    assert.equal(watchdogFetchCount, 1, "watchdog abort finalizes the in-flight request without another provider dispatch");
    assert.equal(fetchSignalAborted, true, "the coordinator watchdog aborts the native request signal");
    assert.equal(watchdogMetadata.stopCode, "time-limit");
    assert.equal(watchdogMetadata.attempts.length, 1);
    assert.equal(watchdogMetadata.attempts[0].status, "failed");
    assert.equal(watchdogMetadata.attempts[0].errorCode, "transport-error", "attempt cause stays separate from the global deadline stop reason");
    validateRunMetadata(watchdogMetadata);
    assert.ok(watchdogArtifact.trials.length === 144);
    assert.ok(fs.existsSync(path.join(watchdogConfig.workdir, "score.json")), "partial artifact score is retained after watchdog cancellation");
    assert.equal(watchdogRuntimeDisposed, true, "watchdog cancellation disposes the runtime after final persistence");
    for (const stopCode of ["call-limit", "output-token-limit", "usage-unavailable", "response-model-drift"]) {
      const canceledBudget = createBudget({ limits: { calls: 1, minutes: 1, outputTokens: 1 } });
      const canceledAttempt = canceledBudget.beginAttempt({ provider: "openai", api: "openai-responses", model: "synthetic", responseModel: null, phase: "cancellation-test" });
      assert.throws(() => canceledBudget.stop(stopCode), new RegExp(stopCode));
      canceledBudget.failAttempt(canceledAttempt, "transport-error");
      assert.equal(canceledBudget.state.stopCode, stopCode);
      assert.equal(canceledAttempt.errorCode, "transport-error");
      const validOtherLimit = structuredClone(watchdogMetadata);
      validOtherLimit.stopCode = stopCode;
      validOtherLimit.attempts[0].errorCode = canceledAttempt.errorCode;
      validateRunMetadata(validOtherLimit);
    }

    let answerCalls = 0;
    const fakeRunner = {
      rows: fullRows, sessionLog: [],
      async maintain(checkpoint) {
        fs.writeFileSync(path.join(wikiDir, "retention.md"), pageContent);
        return { unauthorizedWriteAttempts: checkpoint === cpOne.id ? ["write", "write"] : [], result: { sessionId: `maint-${checkpoint}`, durationMs: 2, answer: "done" }, wiki: pages };
      },
      snapshotWiki: () => pages,
      async answerCell(row, wikiHash) {
        this.sessionLog.push({ phase: `answer:${row.id}`, sessionId: `answer-${row.condition}`, durationMs: 3, unauthorizedWriteAttempts: row.id === fullRows[0].id ? ["out-of-scope", "out-of-scope"] : [] });
        if (row.id === failedRow.id) { this.sessionLog.at(-1).failure = "private solver details"; this.sessionLog.at(-1).unauthorizedWriteAttempts = ["host-path"]; throw new Error("private solver details"); }
        const answer = row.id === fullRows[0].id ? "The fictional Acorn service keeps requests for 30 days." : "I cannot determine that.";
        const visibleSources = fixture.checkpoints.filter((checkpoint) => checkpoint.id <= row.checkpoint).flatMap((checkpoint) => checkpoint.sources.map((source) => ({ checkpoint: checkpoint.id, path: source.path, sha256: source.sha256 })));
        const sourceHistoryHash = row.condition === "wiki-only" ? null : hash(JSON.stringify(visibleSources));
        return { status: "success", answer, sourceHistoryHash, wikiHash, unauthorizedWriteAttempts: this.sessionLog.at(-1).unauthorizedWriteAttempts };
      },
    };
    const inputs = [];
    const fetchFake = async (_url, init) => {
      const request = JSON.parse(init.body);
      const prompt = JSON.stringify(request.input);
      inputs.push(prompt);
      let output;
      if (prompt.includes("v3 wikiJudgment schema")) {
        const topicRows = fixture.gold.topics.map((topic) => ({ topic, status: "unknown", path: null, pageHash: null, quote: null, start: null, end: null,
          temporal: "unknown", relation: "unknown", priorSupport: "unknown", currentEvidence: [], historicalEvidence: [], history: "unknown", qualifiers: "unknown" }));
        const binding = prompt.match(/Host-bound checkpoint is ([0-9-]+); copy snapshotHash ([0-9a-f]{64}) and previousSnapshotHash (null|\\?"[0-9a-f]{64}\\?")/u);
        assert.ok(binding, "fake maintenance judge must copy exact host-supplied snapshot bindings");
        const checkpoint = binding[1];
        const checkpointIndex = fixture.checkpoints.findIndex((item) => item.id === checkpoint);
        output = JSON.stringify({ schemaVersion: 3, checkpoint, snapshotHash: binding[2], previousSnapshotHash: binding[3] === "null" ? null : binding[3].match(/[0-9a-f]{64}/u)[0],
          changeAssessment: checkpointIndex === 0 ? "initialization" : "assessed", complete: false, topics: topicRows,
          pageTopics: [{ path: pagePath, topics: [] }], unnecessaryChanges: checkpointIndex === 0 ? null : [], unauthorizedWriteAttempts: [] });
      } else {
        answerCalls++;
        if (answerCalls === 1) {
          const phrase = "The fictional Acorn service keeps requests for 30 days.";
          const start = phrase.indexOf("The fictional");
          const source = cpOne.sources[0];
          const sourceStart = source.text.indexOf(phrase);
          const boundHash = prompt.match(/answerHash ([0-9a-f]{64})/u)?.[1];
          output = JSON.stringify({ schemaVersion: 3, answerHash: boundHash, complete: true, outcome: "supported", coveredTopics: [retention.topic], explanation: "supported",
            claims: [{ quote: phrase, start, end: start + phrase.length, unitQuote: phrase, unitStart: start, unitEnd: start + phrase.length, disposition: "supported", temporal: "current", relation: "supported", priorSupport: "unknown", qualifiers: "not-applicable", explanation: "matched", currentEvidence: [{ checkpoint: cpOne.id, path: source.path, sourceHash: source.sha256, start: sourceStart, end: sourceStart + phrase.length, quote: phrase }], historicalEvidence: [] }] });
        } else if (answerCalls === 2) {
          output = JSON.stringify({ schemaVersion: 3, answerHash: prompt.match(/answerHash ([0-9a-f]{64})/u)?.[1], complete: true, outcome: "abstained", coveredTopics: [], explanation: "abstained", claims: [] });
        } else output = "{malformed";
      }
      const responseId = `fake-${inputs.length}`;
      const events = [
        { type: "response.created", response: { id: responseId, model: request.model } },
        { type: "response.output_item.added", output_index: 0, item: { id: `msg-${responseId}`, type: "message", role: "assistant", content: [] } },
        { type: "response.output_text.delta", output_index: 0, item_id: `msg-${responseId}`, content_index: 0, delta: output },
        { type: "response.output_item.done", output_index: 0, item: { id: `msg-${responseId}`, type: "message", role: "assistant", status: "completed", phase: "final_answer", content: [{ type: "output_text", text: output, annotations: [] }] } },
        { type: "response.completed", response: { id: responseId, model: request.model, status: "completed", output: [], usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15, input_tokens_details: { cached_tokens: 0 } } } },
      ];
      return new Response(`${events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
    const testConfig = resolveConfig({ argv: ["--config", configPath], env: {} });
    const initial = dryRun({ ...testConfig, workdir: workdirPath }, fixture);
    const coordinatorResult = await runLive(liveConfig, { ...initial, liveEnabled: true, mode: "live" }, fixture, "owner-test", {
      sdk, authModule, runtime, createPiRunner: async () => fakeRunner, fetchImpl: fetchFake,
    });
    const metadata = JSON.parse(fs.readFileSync(path.join(workdirPath, "run-metadata.json"), "utf8"));
    assert.equal(coordinatorResult.stopCode, "answer-failed", JSON.stringify({ stopCode: coordinatorResult.stopCode, inputs: inputs.length, lastPrompt: inputs.at(-1)?.slice(0, 600), attempts: metadata.attempts, stages: metadata.stages }));
    const artifact = JSON.parse(fs.readFileSync(path.join(workdirPath, "artifact-v2.json"), "utf8"));
    const report = JSON.parse(fs.readFileSync(path.join(workdirPath, "score.json"), "utf8"));
    assert.equal(artifact.trials.length, 144);
    const completedTrials = artifact.trials.filter((trial) => trial.status === "success");
    assert.deepEqual(completedTrials.slice(0, 3).map((trial) => trial.answerJudgment.outcome), ["supported", "abstained", "unknown"]);
    assert.equal(completedTrials.length, 143);
    assert.ok(completedTrials.every((trial) => trial.answerJudgment.schemaVersion === 3));
    assert.equal(artifact.trials.filter((trial) => trial.status === "missing").length, 0, "all 144 cells are durably terminal after the final solver failure");
    const failedTrial = artifact.trials.find((trial) => trial.id === failedRow.id);
    assert.equal(failedTrial.status, "failed");
    assert.deepEqual(failedTrial.unauthorizedWriteAttempts, ["host-path"]);
    assert.equal(metadata.sessions.find((item) => item.phase === `answer:${failedRow.id}`).errorCode, "session-failed");
    assert.deepEqual(artifact.wikiJudgments[0].unauthorizedWriteAttempts, ["write", "write"]);
    assert.deepEqual(artifact.trials[0].unauthorizedWriteAttempts, ["out-of-scope", "out-of-scope"]);
    assert.equal(report.answers.find((item) => item.id === fullRows[0].id).unauthorizedWriteAttempts, 2);
    assert.equal(report.efficiency.byCondition.sources.allTrials.modelCompletions, 48);
    assert.equal(report.efficiency.byCondition.sources.allTrials.answerJudge.attempts, 48);
    assert.equal(report.efficiency.byCondition.sources.allTrials.answerSolver.attempts, 0);
    assert.ok(report.efficiency.byCondition.sources.correctOnly.correctAnswers > 0);
    assert.equal(report.answers.find((item) => item.id === fullRows[1].id).correct, false);
    assert.equal(report.efficiency.preparation.maintenanceJudge.attempts, 4, JSON.stringify({ stopCode: coordinatorResult.stopCode, attempts: metadata.attempts.map((item) => item.phase), stages: metadata.stages }));
    assert.equal(report.efficiency.preparation.maintenanceJudge.inputTokens, 40);
    assert.equal(report.efficiency.preparation.maintenanceJudge.cachedInputTokens, 0);
    assert.equal(report.efficiency.preparation.maintenanceJudge.outputTokens, 20);
    assert.equal(report.efficiency.preparation.maintenanceSolver.inputTokens, 0);
    assert.equal(report.efficiency.preparation.costUSD, null);
    assert.equal(metadata.sessions.filter((item) => item.phase.startsWith("judge-")).length, 147);
    assert.equal(metadata.sessions.length, 295, "fresh solver and judge session results/timings are recorded");
    assert.doesNotMatch(JSON.stringify(metadata), /sk-offline-synthetic-key|private solver details|Candidate answer:/);
    validateRunMetadata(metadata);
    assert.throws(() => validateRunMetadata({ ...metadata, unexpectedPrivateField: "x" }), /metadata-schema-invalid/);
    assert.equal(metadata.attempts.length, 147);
    const maintenanceJudgePrompts = inputs.filter((prompt) => prompt.includes("Required topic IDs:"));
    assert.equal(maintenanceJudgePrompts.length, 4);
    assert.ok(maintenanceJudgePrompts.every((prompt, index) => prompt.includes(`copy snapshotHash ${artifact.wikiSnapshots[index].contentHash} and previousSnapshotHash`)));
    const answerJudgePrompts = inputs.filter((prompt) => prompt.includes("Required topic ID:"));
    const evidenceByTask = new Map();
    for (const prompt of answerJudgePrompts) {
      const checkpoint = fixture.checkpoints.find((item) => prompt.includes(`Active checkpoint: ${item.id}`));
      const task = fixture.visibleTasks.find((item) => prompt.includes(`Required topic ID: ${item.topic}\\nExpected outcome`));
      const language = task && ["en", "it"].find((value) => prompt.includes(`Question: ${task.question[value]}\\nRequired topic ID:`));
      assert.ok(checkpoint && task && language, JSON.stringify({checkpoint: checkpoint?.id, task: task?.id, text: prompt.slice(prompt.indexOf("Active checkpoint"), prompt.indexOf("Candidate answer:"))}));
      const evidence = ["Expected outcome/temporal gold:", "Full source evidence through active checkpoint:"].map((label, index, labels) => {
        const start = prompt.indexOf(label);
        const end = index + 1 < labels.length ? prompt.indexOf(labels[index + 1], start) : prompt.indexOf("Candidate answer:", start);
        return prompt.slice(start, end);
      }).join("\\n");
      const group = `${checkpoint.id}/${task.id}/${language}`;
      evidenceByTask.set(group, [...(evidenceByTask.get(group) ?? []), evidence]);
      assert.ok(!/sources\\+wiki|wiki-only|model identity|solver trace/u.test(prompt), "judge input has no treatment label/model/trace fields");
    }
    assert.equal(answerJudgePrompts.length, 143);
    assert.equal(evidenceByTask.size, 48);
    assert.ok([...evidenceByTask.values()].every((records) => new Set(records).size === 1), "answer judges receive identical source/gold context across arms");

    const bindingRuntime = await loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime, readStoredCredential: authModule.readStoredCredential, authFile: syntheticAuthFile });
    const bindingWorkdir = path.join(outside, "strict-binding-run");
    const bindingRow = fullRows.find((row) => row.checkpoint === cpOne.id && row.condition === "sources" && row.language === "en");
    const bindingRunner = { rows: [bindingRow], sessionLog: [],
      async maintain(checkpoint) { return { unauthorizedWriteAttempts: [], result: { sessionId: `binding-maint-${checkpoint}`, durationMs: 1, answer: "done" }, wiki: pages }; },
      async answerCell(row) {
        const visible = fixture.checkpoints.filter((item) => item.id <= row.checkpoint).flatMap((item) => item.sources.map((source) => ({ checkpoint: item.id, path: source.path, sha256: source.sha256 })));
        return { status: "success", answer: "I cannot determine that.", sourceHistoryHash: hash(JSON.stringify(visible)), unauthorizedWriteAttempts: [],
          result: { sessionId: `binding-answer-${row.id}`, durationMs: 1, answer: "I cannot determine that." } };
      }, snapshotWiki: () => pages };
    const bindingFetch = async (_url, init) => {
      const request = JSON.parse(init.body);
      const prompt = JSON.stringify(request.input);
      let output;
      if (prompt.includes("v3 wikiJudgment schema")) {
        const binding = prompt.match(/Host-bound checkpoint is ([0-9-]+); copy snapshotHash ([0-9a-f]{64}) and previousSnapshotHash (null|\\?"[0-9a-f]{64}\\?")/u);
        assert.ok(binding, "strict-binding judge fixture reads host-supplied bindings");
        const checkpoint = binding[1];
        const index = fixture.checkpoints.findIndex((item) => item.id === checkpoint);
        const topicRows = fixture.gold.topics.map((topic) => ({ topic, status: "unknown", path: null, pageHash: null, quote: null, start: null, end: null,
          temporal: "unknown", relation: "unknown", priorSupport: "unknown", currentEvidence: [], historicalEvidence: [], history: "unknown", qualifiers: "unknown" }));
        const pageRows = [{ path: pagePath, topics: [] }];
        if (index === 1) topicRows[1] = { ...topicRows[0] };
        if (index === 2) pageRows.push({ ...pageRows[0] });
        const previousHash = binding[3] === "null" ? null : binding[3].match(/[0-9a-f]{64}/u)[0];
        output = JSON.stringify({ schemaVersion: 3, checkpoint: index === 3 ? fixture.checkpoints[2].id : checkpoint,
          snapshotHash: index === 0 ? hash("wrong snapshot") : binding[2], previousSnapshotHash: previousHash,
          changeAssessment: index === 0 ? "initialization" : "assessed", complete: true, topics: topicRows,
          pageTopics: pageRows, unnecessaryChanges: index === 0 ? null : [], unauthorizedWriteAttempts: [] });
      } else output = JSON.stringify({ schemaVersion: 3, complete: true, outcome: "abstained", coveredTopics: [], explanation: "no binding", claims: [] });
      const responseId = `binding-${bindingFetch.calls++}`;
      const events = [
        { type: "response.created", response: { id: responseId, model: request.model } },
        { type: "response.output_item.added", output_index: 0, item: { id: `msg-${responseId}`, type: "message", role: "assistant", content: [] } },
        { type: "response.output_text.delta", output_index: 0, item_id: `msg-${responseId}`, content_index: 0, delta: output },
        { type: "response.output_item.done", output_index: 0, item: { id: `msg-${responseId}`, type: "message", status: "completed", phase: "final_answer", content: [{ type: "output_text", text: output, annotations: [] }] } },
        { type: "response.completed", response: { id: responseId, model: request.model, status: "completed", output: [], usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } } },
      ];
      return new Response(`${events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
    bindingFetch.calls = 1;
    const bindingInitial = dryRun({ ...liveConfig, live: false, workdir: bindingWorkdir }, fixture);
    await runLive({ ...liveConfig, workdir: bindingWorkdir }, { ...bindingInitial, liveEnabled: true, mode: "live", externalWorkdir: bindingWorkdir }, fixture, "owner-binding", {
      sdk, authModule, runtime: bindingRuntime, createPiRunner: async () => bindingRunner, fetchImpl: bindingFetch,
    });
    const bindingArtifact = JSON.parse(fs.readFileSync(path.join(bindingWorkdir, "artifact-v2.json"), "utf8"));
    const bindingMetadata = JSON.parse(fs.readFileSync(path.join(bindingWorkdir, "run-metadata.json"), "utf8"));
    assert.equal(bindingMetadata.attempts.length, 5, JSON.stringify({ stopCode: bindingMetadata.stopCode, attempts: bindingMetadata.attempts, stages: bindingMetadata.stages }));
    assert.ok(bindingArtifact.wikiJudgments.every((judgment) => !judgment.complete && judgment.topics.every((topic) => topic.status === "unknown")),
      "wrong snapshot/checkpoint and duplicate topic/page identities fall back to unknown annotations");
    const boundAnswer = bindingArtifact.trials.find((trial) => trial.id === bindingRow.id);
    assert.equal(boundAnswer.answerJudgment.outcome, "unknown", "an answer judge missing its required answer hash is not repaired by the host");
    assert.equal(boundAnswer.answerJudgment.complete, false);

    const setupRuntime = await loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime, readStoredCredential: authModule.readStoredCredential, authFile: syntheticAuthFile });
    let setupDisposed = false;
    const instrumentedRuntime = { getModel: (...args) => setupRuntime.getModel(...args), getProvider: (...args) => setupRuntime.getProvider(...args),
      registerNativeProvider: () => {}, dispose: () => { setupDisposed = true; setupRuntime.dispose?.(); } };
    const setupConfig = { ...liveConfig, workdir: path.join(outside, "setup-failure-run") };
    await assert.rejects(runLive(setupConfig, { ...initial, liveEnabled: true, mode: "live", externalWorkdir: setupConfig.workdir }, fixture, "owner-failure", {
      sdk, authModule, runtime: instrumentedRuntime, createPiRunner: async () => { throw new Error("synthetic setup failure"); }, fetchImpl: fetchFake,
    }), /synthetic setup failure/);
    assert.equal(setupDisposed, true, "all runner-construction failures dispose the configured runtime");

    const failingAuthRuntime = await loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime, readStoredCredential: authModule.readStoredCredential, authFile: syntheticAuthFile });
    let failingRuntimeDisposed = false;
    const disposeFailingRuntime = failingAuthRuntime.dispose?.bind(failingAuthRuntime) ?? (() => {});
    failingAuthRuntime.dispose = () => { failingRuntimeDisposed = true; disposeFailingRuntime(); };
    const fetchFailureConfig = { ...liveConfig, workdir: path.join(outside, "provider-throw-run") };
    let fetchFailureCalls = 0;
    const fetchFailureRunner = { rows: [], sessionLog: [],
      async maintain(checkpoint) { return { unauthorizedWriteAttempts: checkpoint === cpOne.id ? ["delete", "delete"] : [],
        result: { sessionId: `failure-maint-${checkpoint}`, durationMs: 1, answer: "done" }, wiki: pages }; }, snapshotWiki: () => pages };
    const fetchFailureResult = await runLive(fetchFailureConfig, { ...initial, liveEnabled: true, mode: "live", externalWorkdir: fetchFailureConfig.workdir }, fixture, "owner-fetch-failure", {
      sdk, authModule, runtime: failingAuthRuntime, createPiRunner: async () => fetchFailureRunner,
      fetchImpl: async () => { fetchFailureCalls++; throw new Error("secret provider failure details"); },
    });
    const fetchFailureMetadata = JSON.parse(fs.readFileSync(path.join(fetchFailureConfig.workdir, "run-metadata.json"), "utf8"));
    const fetchFailureArtifact = JSON.parse(fs.readFileSync(path.join(fetchFailureConfig.workdir, "artifact-v2.json"), "utf8"));
    assert.equal(fetchFailureResult.stopCode, "transport-error", JSON.stringify({ fetchFailureCalls, attempts: fetchFailureMetadata.attempts, sessions: fetchFailureMetadata.sessions }));
    assert.equal(fetchFailureMetadata.stopCode, "transport-error");
    assert.equal(fetchFailureMetadata.attempts.length, 1);
    assert.equal(fetchFailureMetadata.attempts[0].status, "failed");
    assert.equal(fetchFailureMetadata.attempts[0].errorCode, "transport-error");
    assert.doesNotMatch(JSON.stringify(fetchFailureMetadata), /secret provider failure details/);
    assert.equal(fetchFailureArtifact.trials.length, 144);
    assert.deepEqual(fetchFailureArtifact.wikiJudgments[0].unauthorizedWriteAttempts, ["delete", "delete"]);
    assert.ok(fs.existsSync(path.join(fetchFailureConfig.workdir, "score.json")), "validated artifact score survives a provider transport failure");
    validateRunMetadata(fetchFailureMetadata);
    const sdkErrorMetadata = structuredClone(fetchFailureMetadata);
    sdkErrorMetadata.stopCode = "provider-error";
    sdkErrorMetadata.attempts[0].errorCode = "provider-error";
    validateRunMetadata(sdkErrorMetadata);
    assert.equal(failingRuntimeDisposed, true, "provider failure cleanup disposes the runtime after metadata/artifact persistence");
  } finally { runtime.dispose?.(); }
  }
  if (process.env.OSB_KM_TEST_SKIP_MISSING_SDK_CHILD !== "1") {
    const bin = path.join(outside, "no-pi-bin");
    fs.mkdirSync(bin);
    const whichShim = path.join(bin, "which");
    fs.writeFileSync(whichShim, "#!/bin/sh\\nexit 1\\n", { mode: 0o700 });
    const env = { ...process.env, PATH: bin, OSB_KM_TEST_SKIP_MISSING_SDK_CHILD: "1" };
    delete env.PI_CODING_AGENT_PACKAGE;
    delete env.PI_MANAGED_INSTALL_ROOT;
    const testPath = fileURLToPath(import.meta.url);
    const optionalRun = spawnSync(process.execPath, [testPath], { encoding: "utf8", env, timeout: 30_000 });
    assert.equal(optionalRun.status, 0, optionalRun.stderr);
    assert.match(optionalRun.stdout, /NO-SDK-PURE-CHECKS-REACHED/u);
    assert.match(optionalRun.stdout, /UNVERIFIED: Pi SDK\/runtime unavailable/u);
    const requiredRun = spawnSync(process.execPath, [testPath, "--require-runtime"], { encoding: "utf8", env, timeout: 30_000 });
    assert.notEqual(requiredRun.status, 0, "--require-runtime must fail when the installed SDK is unavailable");
    assert.match(requiredRun.stdout, /NO-SDK-PURE-CHECKS-REACHED/u);
    assert.match(requiredRun.stderr, /Pi SDK\/runtime unavailable/u);
  }
  console.log("Knowledge-maintenance live-support offline contract PASS");
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
