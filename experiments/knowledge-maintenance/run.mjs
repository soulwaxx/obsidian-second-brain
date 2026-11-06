#!/usr/bin/env node
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { installedPackage } from "../../tests/pi-runtime-paths.mjs";
import { assertSupportedGitMetadataLayout, createPiRunner } from "./pi-runner.mjs";
import { codeIdentity, createBudget, loadSelectedOpenAIAuth, resolveConfig, validateWorkdir, wrapOpenAIProvider } from "./live-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const EXP = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = path.join(EXP, "fixtures.json");
const EVALUATOR = path.join(EXP, "evaluation.py");
const RUBRIC_PATH = path.join(EXP, "rubric.json");
const ANNOTATION_SCHEMA_PATH = path.join(EXP, "annotation-schema.json");
const OUTPUT_HELP = `Knowledge maintenance experiment (repository-only; never run during normal install/test/release).\n\nUsage: node experiments/knowledge-maintenance/run.mjs --config /absolute/config.json [--dry-run | --live]\n  --live is a literal CLI-only opt-in. Dry-run is default and never reads credentials or contacts Pi/provider APIs.\n  --validate-config prints the same offline safe manifest. Config requires absolute workdir; live also requires vaultRoot and authFile.\n  CLI overrides: --solver-provider --solver-model --solver-effort --judge-provider --judge-model --judge-effort --workdir --auth-file --vault-root\n  --max-calls --max-minutes --max-output-tokens-total --response-tokens.\n`;
const TASK_CONTRACT = "Answer only the assigned question from the explicitly provided knowledge; preserve temporal and stated qualifications; state uncertainty or abstain rather than inventing; cite only evidence you received. Do not write files.";
const hash = (value) => createHash("sha256").update(value).digest("hex");
const canonicalJson = (value) => Array.isArray(value) ? `[${value.map(canonicalJson).join(",")}]` : value && typeof value === "object" ? `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}` : JSON.stringify(value);
const canonicalHash = (value) => hash(canonicalJson(value));
function compareCodePoints(left, right) {
  const a = Array.from(left, (char) => char.codePointAt(0));
  const b = Array.from(right, (char) => char.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index++) if (a[index] !== b[index]) return a[index] - b[index];
  return a.length - b.length;
}
const safeError = () => "stage-failed";
const WRITE_CODES = new Set(["write", "delete", "shell-write", "out-of-scope", "host-path"]);
const normalizeWriteAttempts = (values) => (Array.isArray(values) ? values : []).map((value) => WRITE_CODES.has(value) ? value : "out-of-scope");

function expectedResponsesUrl(baseUrl) {
  let base;
  try { base = new URL(baseUrl); } catch { throw new Error("unsupported-openai-route"); }
  if (base.protocol !== "https:" || base.username || base.password || base.search || base.hash || !base.hostname) throw new Error("unsupported-openai-route");
  const path = `${base.pathname.replace(/\/+$/u, "")}/responses`;
  return new URL(path, base.origin).href;
}

function withPrivateInput(value, callback) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "osb-km-input-"));
  const file = path.join(directory, "input.json");
  try {
    const fd = fs.openSync(file, "wx", 0o600);
    try { fs.writeFileSync(fd, value, "utf8"); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    return callback(file);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
}

function python(code, input = "") {
  return withPrivateInput(input, (file) => {
    const result = spawnSync("python3", ["-c", "import sys; sys.stdin=open(sys.argv[1], encoding='utf-8'); exec(sys.argv[2])", file, code],
      { cwd: ROOT, encoding: "utf8", timeout: 15_000, maxBuffer: 16 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, PYTHONWARNINGS: "error::DeprecationWarning", PYTHONDONTWRITEBYTECODE: "1" } });
    if (result.status !== 0) throw new Error("offline-evaluator-rejected-artifact");
    return result.stdout;
  });
}

function runEvaluator(artifact) {
  return withPrivateInput(JSON.stringify(artifact), (file) => spawnSync("python3", [EVALUATOR, file],
    { cwd: ROOT, encoding: "utf8", timeout: 30_000, maxBuffer: 16 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } }));
}

function fixtureLoad() {
  const stat = fs.lstatSync(FIXTURE_PATH);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) throw new Error("unsafe-fixture");
  return JSON.parse(fs.readFileSync(FIXTURE_PATH, "utf8"));
}

function manifest(config, fixture) {
  const identity = codeIdentity();
  const rubric = JSON.parse(fs.readFileSync(RUBRIC_PATH, "utf8"));
  const taskIds = fixture.checkpoints.flatMap((cp) => fixture.visibleTasks.flatMap((task) => ["en", "it"].flatMap((lang) => ["sources", "sources+wiki", "wiki-only"].map((condition) => `${cp.id}/${task.id}/${lang}/${condition}`))));
  return {
    schemaVersion: 1,
    mode: config.live ? "live" : "dry-run",
    liveEnabled: config.live,
    settings: { solver: config.solver, judge: config.judge, limits: config.limits },
    identities: {
      sourceHash: canonicalHash(fixture.checkpoints), goldHash: canonicalHash(fixture.gold),
      fixtureHash: canonicalHash(Object.fromEntries(Object.entries(fixture).filter(([key]) => key !== "gold"))),
      rubricHash: canonicalHash(rubric), settingsHash: canonicalHash({ solver: config.solver, judge: config.judge, limits: config.limits }),
      promptHash: canonicalHash({ annotationSchema: JSON.parse(fs.readFileSync(ANNOTATION_SCHEMA_PATH, "utf8")), taskContract: TASK_CONTRACT,
        maintenance: fixture.visibleTasks.map((item) => item.maintenance),
        maintenanceJudge: "v3 maintenance judgment: six topics, host-bound current/previous snapshot quotes/evidence, temporal relations, unique page inventory and assessed/unknown transitions",
        answerJudge: "v3 answer judgment: all claims, strict answer hash binding, bound candidate/unit/source quotes, temporal relation, prior support and qualifications" }),
      code: identity,
    },
    externalWorkdir: config.workdir,
    syntheticVault: config.vaultRoot ?? null,
    taskIds,
  };
}

function safeDirectory(target) {
  const resolvedTarget = path.resolve(target);
  const home = process.env.HOME ? path.resolve(process.env.HOME) : null;
  const relativeTo = (base) => path.relative(base, resolvedTarget);
  if (relativeTo(ROOT) === "" || (!relativeTo(ROOT).startsWith(`..${path.sep}`) && relativeTo(ROOT) !== ".." && !path.isAbsolute(relativeTo(ROOT)))) throw new Error("synthetic-vault-inside-repository");
  if (home && (relativeTo(home) === "" || (!relativeTo(home).startsWith(`..${path.sep}`) && relativeTo(home) !== ".." && !path.isAbsolute(relativeTo(home))))) throw new Error("synthetic-vault-inside-personal-home");
  let cursor = path.parse(resolvedTarget).root;
  for (const part of resolvedTarget.slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const entry = fs.lstatSync(cursor);
    if (entry.isSymbolicLink()) throw new Error("synthetic-vault-symlink");
  }
  const stat = fs.lstatSync(target);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("synthetic-vault-required");
  const marker = path.join(target, ".knowledge-maintenance-synthetic");
  const markerStat = fs.lstatSync(marker);
  if (!markerStat.isFile() || markerStat.isSymbolicLink() || markerStat.nlink !== 1 || fs.readFileSync(marker, "utf8") !== "obsidian-second-brain synthetic knowledge-maintenance vault v1\n") throw new Error("synthetic-vault-marker-required");
  const wiki = path.join(target, "wiki");
  const wikiStat = fs.lstatSync(wiki);
  if (!wikiStat.isDirectory() || wikiStat.isSymbolicLink()) throw new Error("synthetic-wiki-directory-required");
  return fs.realpathSync(target);
}

function persistFile(directory, name, value) {
  const target = path.join(directory, name);
  const temporary = path.join(directory, `.${name}.${randomUUID()}.tmp`);
  const bytes = `${JSON.stringify(value, null, 2)}\n`;
  const fd = fs.openSync(temporary, "wx", 0o600);
  try { fs.writeFileSync(fd, bytes, "utf8"); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  try {
    const stat = fs.lstatSync(target);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) throw new Error("unsafe-output-collision");
    fs.renameSync(temporary, target);
  } catch (error) {
    if (error.code === "ENOENT") fs.renameSync(temporary, target);
    else { fs.rmSync(temporary, { force: true }); throw error; }
  }
}

export function validateRunMetadata(metadata) {
  const exact = (value, keys, label) => { if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).some((key) => !keys.includes(key)) || keys.some((key) => !Object.hasOwn(value, key))) throw new Error(`${label}-schema-invalid`); };
  exact(metadata, ["schemaVersion", "identities", "limits", "runtime", "backendRevision", "backendRevisionStatus", "stopCode", "attempts", "sessions", "stages", "timing", "pricing"], "metadata");
  if (metadata.schemaVersion !== 1 || !Array.isArray(metadata.attempts) || !Array.isArray(metadata.sessions) || !Array.isArray(metadata.stages) || metadata.backendRevision !== null || metadata.backendRevisionStatus !== "unknown" || metadata.pricing !== null || !(metadata.stopCode === null || ["preflight-incomplete", "preflight-failed", "run-stopped", "request-body-unavailable", "request-contract-mismatch", "code-identity-drift", "time-limit", "call-limit", "output-token-limit", "usage-unavailable", "transport-error", "provider-error", "response-model-drift", "unsupported-openai-route", "judge-session-failed", "maintenance-failed", "answer-failed", "budget-stopped", "cancelled", "artifact-validation-failed", "score-generation-failed"].includes(metadata.stopCode))) throw new Error("metadata-schema-invalid");
  exact(metadata.limits, ["calls", "minutes", "outputTokens", "maxOutputTokens"], "limits");
  if (!Object.values(metadata.limits).every((value) => Number.isSafeInteger(value) && value > 0)) throw new Error("metadata-limits-invalid");
  exact(metadata.identities, ["sourceHash", "goldHash", "fixtureHash", "rubricHash", "settingsHash", "promptHash", "code"], "identities");
  for (const key of ["sourceHash", "goldHash", "fixtureHash", "rubricHash", "settingsHash", "promptHash"]) if (!/^[0-9a-f]{64}$/u.test(metadata.identities[key])) throw new Error("metadata-identity-invalid");
  exact(metadata.identities.code, ["revision", "revisionStatus", "files", "hash"], "codeIdentity");
  if (metadata.identities.code.revision !== null || metadata.identities.code.revisionStatus !== "not-recorded" || !/^[0-9a-f]{64}$/u.test(metadata.identities.code.hash) || !Array.isArray(metadata.identities.code.files)) throw new Error("metadata-code-identity-invalid");
  exact(metadata.runtime, ["node", "python", "piCodingAgent", "solver", "judge"], "runtime");
  if (![metadata.runtime.node, metadata.runtime.python, metadata.runtime.piCodingAgent].every((value) => typeof value === "string" && value.length > 0)) throw new Error("metadata-runtime-invalid");
  for (const role of ["solver", "judge"]) {
    exact(metadata.runtime[role], ["id", "api", "origin", "effort"], `${role}-runtime`);
    const origin = metadata.runtime[role].origin;
    if (metadata.runtime[role].api !== "openai-responses" || !(origin === null || (typeof origin === "string" && (() => {
      try { const parsed = new URL(origin); return parsed.protocol === "https:" && parsed.origin === origin; } catch { return false; }
    })()))) throw new Error("metadata-runtime-invalid");
  }
  for (const attempt of metadata.attempts) {
    exact(attempt, ["id", "provider", "api", "model", "responseModel", "phase", "status", "usage", ...(Object.hasOwn(attempt, "errorCode") ? ["errorCode"] : [])], "attempt");
    if (!Number.isSafeInteger(attempt.id) || attempt.id < 1 || attempt.provider !== "openai" || attempt.api !== "openai-responses" || typeof attempt.model !== "string" || typeof attempt.phase !== "string") throw new Error("metadata-attempt-invalid");
    if (!attempt.phase || !["attempted", "completed", "failed", "usage-unavailable"].includes(attempt.status) || !(attempt.responseModel === null || typeof attempt.responseModel === "string")) throw new Error("metadata-attempt-invalid");
    if ((attempt.status === "failed" && !Object.hasOwn(attempt, "errorCode")) ||
      (Object.hasOwn(attempt, "errorCode") && !["transport-error", "provider-error"].includes(attempt.errorCode))) throw new Error("metadata-attempt-invalid");
    if (attempt.usage !== null) {
      exact(attempt.usage, ["inputTokens", "outputTokens", "totalTokens", "cachedInputTokens", "pricing", "responseId", "responseModel"], "usage");
      if (![attempt.usage.inputTokens, attempt.usage.outputTokens, attempt.usage.totalTokens].every(Number.isSafeInteger) || !(attempt.usage.cachedInputTokens === null || Number.isSafeInteger(attempt.usage.cachedInputTokens)) || attempt.usage.pricing !== null || !(attempt.usage.responseId === null || typeof attempt.usage.responseId === "string") || !(attempt.usage.responseModel === null || typeof attempt.usage.responseModel === "string")) throw new Error("metadata-usage-invalid");
    }
  }
  for (const session of metadata.sessions) {
    exact(session, ["phase", "sessionId", "durationMs", "result", "errorCode", "unauthorizedWriteAttempts"], "session");
    if (typeof session.phase !== "string" || !["completed", "failed", "received", "empty"].includes(session.result) || !(session.errorCode === null || session.errorCode === "session-failed") || !(session.sessionId === null || typeof session.sessionId === "string") || !(session.durationMs === null || Number.isFinite(session.durationMs)) || !Array.isArray(session.unauthorizedWriteAttempts) || session.unauthorizedWriteAttempts.some((value) => !WRITE_CODES.has(value))) throw new Error("metadata-session-invalid");
  }
  for (const stage of metadata.stages) {
    if (!stage || typeof stage !== "object" || typeof stage.phase !== "string" || !["complete", "incomplete", "failed", "stopped"].includes(stage.status)) throw new Error("metadata-stage-invalid");
    for (const key of Object.keys(stage)) if (!["phase", "status", "durationMs", "retrievalBuildMs", "code"].includes(key)) throw new Error("metadata-stage-invalid");
    for (const value of [stage.durationMs, stage.retrievalBuildMs]) if (value !== undefined && value !== null && !Number.isFinite(value)) throw new Error("metadata-stage-invalid");
  }
  if (typeof metadata.timing?.startedAt !== "string" || !(metadata.timing.elapsedMs === null || Number.isFinite(metadata.timing.elapsedMs))) throw new Error("metadata-timing-invalid");
  return metadata;
}

function validateArtifact(artifact) {
  const sealed = JSON.parse(python("import json,sys; sys.path.insert(0,'experiments/knowledge-maintenance'); import evaluation as e; a=e.seal_artifact(json.load(sys.stdin)); e.validate_artifact(a); e.score(a); print(json.dumps(a,ensure_ascii=False,separators=(',',':')))", JSON.stringify(artifact)));
  Object.assign(artifact, sealed);
  return artifact;
}

function efficiencyReport(artifact, metadata, scoreReport) {
  const conditions = ["sources", "sources+wiki", "wiki-only"];
  const correctness = new Map(scoreReport.answers.map((item) => [item.id, item.correct]));
  const summarize = (trials) => {
    const ids = new Set(trials.map((trial) => trial.id));
    const forKind = (kind) => {
      const attempts = metadata.attempts.filter((item) => kind === "solver" ? item.phase?.startsWith("answer:") && ids.has(item.phase.slice("answer:".length)) : item.phase?.startsWith("judge-answer:") && ids.has(item.phase.slice("judge-answer:".length)));
      const known = attempts.filter((item) => item.usage);
      const complete = known.length === attempts.length;
      return { attempts: attempts.length, usageUnavailableAttempts: attempts.length - known.length,
        inputTokens: complete ? known.reduce((sum, item) => sum + item.usage.inputTokens, 0) : null,
        cachedInputTokens: complete && known.every((item) => Number.isSafeInteger(item.usage.cachedInputTokens)) ? known.reduce((sum, item) => sum + item.usage.cachedInputTokens, 0) : null,
        outputTokens: complete ? known.reduce((sum, item) => sum + item.usage.outputTokens, 0) : null,
        totalTokens: complete ? known.reduce((sum, item) => sum + item.usage.totalTokens, 0) : null,
        elapsedMs: metadata.sessions.filter((item) => kind === "solver" ? item.phase.startsWith("answer:") && ids.has(item.phase.slice(7)) : item.phase.startsWith("judge-answer:") && ids.has(item.phase.slice(13))).reduce((sum, item) => sum + (item.durationMs ?? 0), 0), costUSD: null };
    };
    return { expectedTrials: trials.length, modelCompletions: trials.filter((trial) => trial.status === "success").length,
      correctAnswers: trials.filter((trial) => correctness.get(trial.id) === true).length,
      answerSolver: forKind("solver"), answerJudge: forKind("judge") };
  };
  const prep = (prefix) => {
    const attempts = metadata.attempts.filter((item) => item.phase?.startsWith(`${prefix}:`));
    const known = attempts.filter((item) => item.usage);
    const complete = known.length === attempts.length;
    const records = metadata.sessions.filter((item) => item.phase?.startsWith(`${prefix}:`));
    return { attempts: attempts.length, usageUnavailableAttempts: attempts.length - known.length,
      inputTokens: complete ? known.reduce((sum, item) => sum + item.usage.inputTokens, 0) : null,
      cachedInputTokens: complete && known.every((item) => Number.isSafeInteger(item.usage.cachedInputTokens)) ? known.reduce((sum, item) => sum + item.usage.cachedInputTokens, 0) : null,
      outputTokens: complete ? known.reduce((sum, item) => sum + item.usage.outputTokens, 0) : null,
      totalTokens: complete ? known.reduce((sum, item) => sum + item.usage.totalTokens, 0) : null,
      elapsedMs: records.reduce((sum, item) => sum + (item.durationMs ?? 0), 0), costUSD: null };
  };
  const maintenanceStages = metadata.stages.filter((item) => item.phase?.startsWith("maintenance:"));
  return { note: "Actual provider-reported usage only; missing usage makes category totals null. Correct-only uses evaluator correct===true; completions and judging remain separate. Pricing is unavailable. Shared preparation is not treated as free for sources-only.",
    byCondition: Object.fromEntries(conditions.map((condition) => {
      const trials = artifact.trials.filter((trial) => trial.condition === condition);
      const correct = trials.filter((trial) => correctness.get(trial.id) === true);
      return [condition, { allTrials: summarize(trials), correctOnly: summarize(correct) }];
    })),
    preparation: { checkpointCount: 4, maintenanceSolver: prep("maintenance"), maintenanceJudge: prep("judge-maintenance"),
      indexingMs: maintenanceStages.reduce((sum, item) => sum + (item.retrievalBuildMs ?? 0), 0), costUSD: null } };
}

function emptyJudgment(topicList, checkpoint, snapshotHash, pages, previousSnapshotHash = null, changeAssessment = "unknown") {
  return {
    schemaVersion: 3, checkpoint, snapshotHash, previousSnapshotHash, changeAssessment, complete: false,
    topics: topicList.map((topic) => ({ topic, status: "unknown", path: null, pageHash: null, quote: null, start: null, end: null, temporal: "unknown", relation: "unknown", priorSupport: "unknown", currentEvidence: [], historicalEvidence: [], history: "unknown", qualifiers: "unknown" })),
    pageTopics: pages.map((page) => ({ path: page.path, topics: [] })), unnecessaryChanges: changeAssessment === "assessed" ? [] : null, unauthorizedWriteAttempts: [],
  };
}

function unknownAnswer(answer, answerHash) {
  return { schemaVersion: 3, answerHash, complete: false, outcome: "unknown", coveredTopics: [], explanation: "Judge output unavailable or invalid; outcome left unknown.", claims: [] };
}

export function buildAnswerJudgePrompt({ candidateId, answerHash, rubric, schema, checkpoint, question, topic, gold, sourceHistory, answer }) {
  return `Return exactly one strict JSON object conforming to the attached v3 answerJudgment schema. The opaque candidate ID is ${candidateId}; copy the host-supplied answerHash ${answerHash} verbatim. Annotate every factual claim, exact candidate quote and containing-unit quote with Unicode code-point offsets, temporal scope, relation, prior support, qualifiers, and exact current/historical source evidence quote/hash/offsets. Use unknown/incomplete instead of repairing malformed or incomplete output. The host validates all citations mechanically. Do not infer a treatment, solver identity, model, cost, or trace. The host supplies the same question, gold, and full active/historical source evidence to every arm; no wiki or arm-specific fields are provided.\nRUBRIC: ${JSON.stringify(rubric)}\nSTRICT JSON SCHEMA: ${JSON.stringify(schema)}\nActive checkpoint: ${checkpoint}\nQuestion: ${question}\nRequired topic ID: ${topic}\nExpected outcome/temporal gold: ${JSON.stringify(gold)}\nFull source evidence through active checkpoint: ${JSON.stringify(sourceHistory)}\nCandidate answer: ${JSON.stringify(answer)}`;
}

export function buildMaintenanceJudgePrompt({ checkpoint, snapshotHash, previousSnapshotHash, previousSnapshot, changeAssessment, rubric, schema, topics, gold, sourceHistory, pages }) {
  return `Return exactly one strict JSON object conforming to the attached v3 wikiJudgment schema. Host-bound checkpoint is ${checkpoint}; copy snapshotHash ${snapshotHash} and previousSnapshotHash ${JSON.stringify(previousSnapshotHash)} verbatim (do not compute or alter them). Annotate every required topic ID exactly once and every candidate page exactly once. Cite exact candidate-page quotes and Unicode code-point offsets; cite exact dated source evidence quotes, hashes, and offsets. The host validates all snapshot/page bindings and binds unauthorizedWriteAttempts; do not repair or invent binding values. Transition assessment is ${changeAssessment}: initialization has no before-state and must use changeAssessment=initialization with unnecessaryChanges=null; a revision includes its preceding frozen wiki and must assess unnecessaryChanges as a list; if transition evidence is unavailable or insufficient use unknown with unnecessaryChanges=null. Compare the complete previous and current snapshots, not source history as a proxy for prior authored content. You are an independent read-only judge; use unknown when uncertain.\nRUBRIC: ${JSON.stringify(rubric)}\nSTRICT JSON SCHEMA: ${JSON.stringify(schema)}\nRequired topic IDs: ${JSON.stringify(topics)}\nCheckpoint topic gold: ${JSON.stringify(gold)}\nActive/historical source evidence through checkpoint: ${JSON.stringify(sourceHistory)}\nPrevious frozen candidate snapshot: ${JSON.stringify(previousSnapshot)}\nCurrent candidate pages (host-bound path/hash/content): ${JSON.stringify(pages)}`;
}

function parseJudgment(text) {
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    return value;
  } catch { return null; }
}

function blankLoader(createExtensionRuntime) {
  return {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }), getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }), getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => "You are a read-only evaluator. Return only the requested JSON object.",
    getSystemPromptSource: () => undefined, getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
    extendResources: () => {}, reload: async () => {},
  };
}

async function makeJudge({ sdk, runtime, model, effort, vaultRoot, agentDir, budget, controller, identityCheck, sessionRecords, prompt, phase }) {
  const settingsManager = sdk.SettingsManager.inMemory({ retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, compaction: { enabled: false }, cacheWarming: "off", defaultTools: [] });
  const loader = blankLoader(sdk.createExtensionRuntime);
  const start = performance.now();
  let session;
  let abortJudge;
  try {
    ({ session } = await sdk.createAgentSession({ cwd: vaultRoot, agentDir, model, thinkingLevel: effort, modelRuntime: runtime,
      resourceLoader: loader, sessionManager: sdk.SessionManager.inMemory(vaultRoot), settingsManager, tools: [] }));
    await session.bindExtensions({});
    abortJudge = () => { void session.abort().catch(() => undefined); };
    controller.signal.addEventListener("abort", abortJudge, { once: true });
    if (controller.signal.aborted) abortJudge();
    await identityCheck();
    await session.prompt(prompt);
    const text = session.getLastAssistantText() ?? "";
    sessionRecords.push({ phase, sessionId: session.sessionId ?? null, durationMs: performance.now() - start, result: text.trim() ? "received" : "empty", errorCode: null, unauthorizedWriteAttempts: [] });
    return text;
  } catch {
    sessionRecords.push({ phase, sessionId: session?.sessionId ?? null, durationMs: performance.now() - start, result: "failed", errorCode: "session-failed", unauthorizedWriteAttempts: [] });
    budget.state.stopped = true;
    budget.state.stopCode ??= "judge-session-failed";
    controller.abort();
    return "";
  } finally {
    if (abortJudge) controller.signal.removeEventListener("abort", abortJudge);
    session?.dispose();
  }
}

export async function runLive(config, initialManifest, fixture, ownerToken, deps = {}) {
  const packageResolver = deps.installedPackage ?? installedPackage;
  const runnerFactory = deps.createPiRunner ?? createPiRunner;
  const nativeMode = deps.nativeMode === true;
  if (nativeMode ? config.authFile !== null : !config.authFile) throw new Error(nativeMode ? "native-mode-rejects-auth-file" : "explicit-auth-file-required");
  if (!config.vaultRoot) throw new Error("explicit-synthetic-vault-required");
  if (config.solver.provider !== "openai" || config.judge.provider !== "openai") throw new Error("unsupported-provider-route");
  const vaultRoot = safeDirectory(config.vaultRoot);
  assertSupportedGitMetadataLayout(vaultRoot);
  const workdir = validateWorkdir(config.workdir);
  const isWithin = (parent, target) => { const relative = path.relative(parent, target); return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative)); };
  if (isWithin(vaultRoot, workdir) || isWithin(workdir, vaultRoot)) throw new Error("workdir-vault-overlap");
  fs.mkdirSync(workdir, { mode: 0o700 });
  const ownerFd = fs.openSync(path.join(workdir, ".run-owner"), "wx", 0o600);
  try { fs.writeFileSync(ownerFd, `${ownerToken}\\n`, "utf8"); fs.fsyncSync(ownerFd); } finally { fs.closeSync(ownerFd); }
  const settings = { model: `${config.solver.provider}/${config.solver.model}`, effort: config.solver.effort, attemptCount: 1,
    taskContract: TASK_CONTRACT, taskContractHash: canonicalHash(TASK_CONTRACT), readOnly: true, limits: { maxOutputTokens: config.limits.maxOutputTokens } };
  const rubric = JSON.parse(fs.readFileSync(RUBRIC_PATH, "utf8"));
  const annotationSchema = JSON.parse(fs.readFileSync(ANNOTATION_SCHEMA_PATH, "utf8"));
  const judgeSettings = { model: `${config.judge.provider}/${config.judge.model}`, effort: config.judge.effort, rubric, rubricHash: canonicalHash(rubric), maxOutputTokens: config.limits.maxOutputTokens };
  const emptyCode = "import json,sys; sys.path.insert(0,'experiments/knowledge-maintenance'); import evaluation as e; v=json.load(sys.stdin); print(json.dumps(e.empty_artifact(v['settings'],v['judgeSettings'],v['candidateHash'])))";
  const artifact = JSON.parse(python(emptyCode, JSON.stringify({ settings, judgeSettings, candidateHash: initialManifest.identities.code.hash })));
  validateArtifact(artifact);
  persistFile(workdir, "artifact-v2.json", artifact);
  const pyVersion = spawnSync("python3", ["--version"], { encoding: "utf8", timeout: 5_000 }).stdout.trim();
  const piPackage = JSON.parse(fs.readFileSync(path.join(packageResolver(), "package.json"), "utf8"));
  const sidecar = { schemaVersion: 1, identities: initialManifest.identities, limits: config.limits,
    runtime: { node: process.version, python: pyVersion, piCodingAgent: piPackage.version,
      solver: { id: `${config.solver.provider}/${config.solver.model}`, api: "openai-responses", origin: nativeMode ? null : "https://api.openai.com", effort: config.solver.effort },
      judge: { id: `${config.judge.provider}/${config.judge.model}`, api: "openai-responses", origin: nativeMode ? null : "https://api.openai.com", effort: config.judge.effort } },
    backendRevision: null, backendRevisionStatus: "unknown", stopCode: "preflight-incomplete", attempts: [], sessions: [], stages: [], timing: { startedAt: new Date().toISOString(), elapsedMs: null }, pricing: null };
  const controller = new AbortController();
  let timer;
  const began = performance.now();
  const budget = createBudget({ limits: config.limits, onStop: () => controller.abort() });
  const externalSignal = deps.signal;
  const cancelRun = () => {
    if (!budget.state.stopped) { budget.state.stopped = true; budget.state.stopCode = "cancelled"; }
    controller.abort();
  };
  if (externalSignal?.aborted) cancelRun();
  else externalSignal?.addEventListener("abort", cancelRun, { once: true });
  const persistMetadata = () => {
    sidecar.attempts = budget.state.records.map(({ id, ...record }) => ({ id, ...record }));
    sidecar.stopCode = budget.state.stopCode ?? sidecar.stopCode;
    sidecar.timing.elapsedMs = performance.now() - began;
    validateRunMetadata(sidecar);
    persistFile(workdir, "run-metadata.json", sidecar);
  };
  persistMetadata();
  const sdkRoot = packageResolver();
  if (!sdkRoot) throw new Error("installed-pi-runtime-unavailable");
  const sdkUrl = pathToFileURL(path.join(sdkRoot, "dist/index.js")).href;
  const authModule = pathToFileURL(path.join(sdkRoot, "dist/core/auth-storage.js")).href;
  const [sdk, auth] = deps.sdk && deps.authModule ? [deps.sdk, deps.authModule] : await Promise.all([import(sdkUrl), import(authModule)]);
  const catalogRuntime = await sdk.ModelRuntime.create({ credentials: auth.AuthStorage.inMemory(), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
  let solverCatalogModel;
  let judgeCatalogModel;
  try {
    solverCatalogModel = catalogRuntime.getModel(config.solver.provider, config.solver.model);
    judgeCatalogModel = catalogRuntime.getModel(config.judge.provider, config.judge.model);
  } finally { catalogRuntime.dispose?.(); }
  if (!solverCatalogModel || !judgeCatalogModel) throw new Error("configured-openai-model-unavailable");
  const runtime = deps.runtime ?? await (deps.loadAuth ?? loadSelectedOpenAIAuth)({ AuthStorage: auth.AuthStorage, ModelRuntime: sdk.ModelRuntime, readStoredCredential: auth.readStoredCredential, authFile: config.authFile });
  const solverModel = runtime.getModel(config.solver.provider, config.solver.model);
  const judgeModel = runtime.getModel(config.judge.provider, config.judge.model);
  if (!solverModel || !judgeModel) { runtime.dispose?.(); throw new Error("configured-openai-model-unavailable"); }
  const expectedRoutes = new Map();
  for (const [role, model] of [["solver", solverModel], ["judge", judgeModel]]) {
    if (model.provider !== "openai" || model.api !== "openai-responses") { runtime.dispose?.(); throw new Error("unsupported-openai-route"); }
    const modelKey = `${model.provider}/${model.id}`;
    const pinnedBaseUrl = nativeMode ? deps.nativeBaseUrls?.get(modelKey) : undefined;
    if (nativeMode && (typeof pinnedBaseUrl !== "string" || pinnedBaseUrl !== model.baseUrl)) { runtime.dispose?.(); throw new Error("native-model-route-drift"); }
    const requestUrl = nativeMode ? expectedResponsesUrl(pinnedBaseUrl) : "https://api.openai.com/v1/responses";
    expectedRoutes.set(`${model.provider}/${model.id}`, requestUrl);
    sidecar.runtime[role].origin = new URL(requestUrl).origin;
  }
  persistMetadata();
  sidecar.stopCode = null;
  const persist = (artifact) => {
    validateArtifact(artifact);
    persistFile(workdir, "artifact-v2.json", artifact);
    persistMetadata();
  };
  const externalWorkspace = fs.mkdtempSync(path.join(path.dirname(workdir), ".osb-km-workspace-"));
  const agentDir = path.join(externalWorkspace, "empty-agent");
  let currentPhase = "preflight";
  const identityCheck = () => {
    const fresh = codeIdentity();
    if (fresh.hash !== initialManifest.identities.code.hash) budget.stop("code-identity-drift");
  };
  let runner;
  try {
    fs.mkdirSync(agentDir, { mode: 0o700 });
    const provider = runtime.getProvider("openai");
    if (!provider) throw new Error("openai-provider-unavailable");
    runtime.registerNativeProvider(wrapOpenAIProvider(provider, budget, { maxOutputTokens: config.limits.maxOutputTokens, fetchImpl: deps.fetchImpl,
      effort: () => currentPhase.startsWith("judge-") ? config.judge.effort : config.solver.effort,
      expectedRequestUrl: nativeMode ? (model) => expectedRoutes.get(`${model.provider}/${model.id}`) : undefined,
      beforeDispatch: identityCheck,
      onAttempt: (attempt) => { attempt.phase = currentPhase; persistMetadata(); }, onAttemptComplete: persistMetadata }));
    runner = await runnerFactory({ vaultRoot, workspaceRoot: externalWorkspace, modelRuntime: runtime,
      model: { ...solverModel, maxTokens: config.limits.maxOutputTokens }, effort: config.solver.effort, maxOutputTokens: config.limits.maxOutputTokens,
      sdkPackage: sdkRoot, signal: controller.signal, onProviderEvent: () => {} });
  } catch (error) {
    clearTimeout(timer);
    try { runtime.dispose?.(); } finally { fs.rmSync(externalWorkspace, { recursive: true, force: true }); }
    throw error;
  }
  timer = (deps.setTimeoutImpl ?? setTimeout)(() => { budget.state.stopped = true; budget.state.stopCode = "time-limit"; controller.abort(); }, config.limits.minutes * 60_000);
  timer?.unref?.();
  const judge = async (prompt, phase) => {
    currentPhase = phase;
    await identityCheck();
    return makeJudge({ sdk, runtime, model: { ...judgeModel, maxTokens: config.limits.maxOutputTokens }, effort: config.judge.effort,
      vaultRoot, agentDir, budget, controller, identityCheck, sessionRecords: sidecar.sessions, prompt, phase });
  };
  const failArtifact = (phase, code) => sidecar.stages.push({ phase, status: "stopped", code });
  const recordSolverSession = (phase, result, writeAttempts = []) => sidecar.sessions.push({ phase, sessionId: result?.sessionId ?? null,
    durationMs: result?.durationMs ?? runner.sessionLog.at(-1)?.durationMs ?? null, result: result ? (result.status === "failed" ? "failed" : "completed") : "failed",
    errorCode: result?.status === "failed" || !result ? "session-failed" : null,
    unauthorizedWriteAttempts: normalizeWriteAttempts(writeAttempts.length ? writeAttempts : runner.sessionLog.at(-1)?.unauthorizedWriteAttempts) });
  try {
    persist(artifact);
    if (controller.signal.aborted) throw new Error(budget.state.stopCode ?? "cancelled");
    for (const cp of fixture.checkpoints) {
      const stageStart = performance.now();
      currentPhase = `maintenance:${cp.id}`;
      await identityCheck();
      let maintenanceOk = false;
      let maintenanceWriteAttempts = [];
      let retrievalBuildMs = null;
      try {
        const maintained = await runner.maintain(cp.id, {});
        maintenanceWriteAttempts = normalizeWriteAttempts(maintained.unauthorizedWriteAttempts);
        recordSolverSession(`maintenance:${cp.id}`, maintained.result, maintenanceWriteAttempts);
        runner.assertVaultTreeSafe?.();
        const retrievalStart = performance.now();
        const build = spawnSync("python3", [path.join(ROOT, "scripts/bm25-index.py"), "build", "--vault", vaultRoot], { cwd: workdir, encoding: "utf8", timeout: 120000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
        retrievalBuildMs = performance.now() - retrievalStart;
        if (build.status !== 0) throw new Error("retrieval-build-failed");
        const pages = maintained.wiki.map((page) => ({ path: page.path, content: page.content, sha256: page.sha256 }));
        const snapshotHash = canonicalHash(pages.map(({ path: pagePath, sha256: digest }) => ({ path: pagePath, sha256: digest })).sort((a, b) => compareCodePoints(a.path, b.path)));
        const snapshot = { checkpoint: cp.id, status: "complete", pages, contentHash: snapshotHash };
        const judgmentIndex = fixture.checkpoints.findIndex((x) => x.id === cp.id);
        const priorRecord = judgmentIndex > 0 ? artifact.wikiSnapshots[judgmentIndex - 1] : null;
        const previousSnapshot = priorRecord?.status === "complete" ? priorRecord : null;
        const previousSnapshotHash = previousSnapshot?.contentHash ?? null;
        const expectedChangeAssessment = judgmentIndex === 0 ? "initialization" : previousSnapshot ? "assessed" : "unknown";
        const fallbackAssessment = judgmentIndex === 0 ? "initialization" : "unknown";
        artifact.wikiSnapshots[judgmentIndex] = snapshot;
        if (judgmentIndex + 1 < artifact.wikiJudgments.length) artifact.wikiJudgments[judgmentIndex + 1].previousSnapshotHash = snapshotHash;
        artifact.wikiJudgments[judgmentIndex] = { ...emptyJudgment(fixture.gold.topics, cp.id, snapshotHash, pages, previousSnapshotHash, fallbackAssessment), unauthorizedWriteAttempts: maintenanceWriteAttempts };
        for (const trial of artifact.trials) if (trial.checkpoint === cp.id && trial.condition !== "sources") trial.wikiHash = snapshotHash;
        persist(artifact);
        const sourceHistory = fixture.checkpoints.filter((item) => item.id <= cp.id).flatMap((item) => item.sources.map((source) => ({ checkpoint: item.id, path: source.path, text: source.text, sha256: source.sha256 })));
        const gold = Object.fromEntries(fixture.visibleTasks.map((task) => [task.topic, fixture.gold.expectedAnswers[task.id][cp.id]]));
        const maintenancePrompt = buildMaintenanceJudgePrompt({ checkpoint: cp.id, snapshotHash, previousSnapshotHash, previousSnapshot, changeAssessment: expectedChangeAssessment,
          rubric, schema: annotationSchema, topics: fixture.gold.topics, gold, sourceHistory, pages });
        const text = await judge(maintenancePrompt, `judge-maintenance:${cp.id}`);
        const parsed = parseJudgment(text);
        const fallback = { ...emptyJudgment(fixture.gold.topics, cp.id, snapshotHash, pages, previousSnapshotHash, fallbackAssessment), unauthorizedWriteAttempts: maintenanceWriteAttempts };
        const topicRows = Array.isArray(parsed?.topics) ? parsed.topics : [];
        const topicIds = topicRows.map((topic) => topic?.topic);
        const pageRows = Array.isArray(parsed?.pageTopics) ? parsed.pageTopics : [];
        const pageIds = pageRows.map((page) => page?.path);
        const annotationIdsMatch = topicRows.length === fixture.gold.topics.length && new Set(topicIds).size === topicIds.length &&
          fixture.gold.topics.every((topic) => topicIds.includes(topic)) && pageRows.length === pages.length && new Set(pageIds).size === pageIds.length &&
          pages.every((page) => pageIds.includes(page.path));
        const transitionBindingValid = parsed?.previousSnapshotHash === previousSnapshotHash &&
          (expectedChangeAssessment === "initialization" ? parsed?.changeAssessment === "initialization" :
            expectedChangeAssessment === "unknown" ? parsed?.changeAssessment === "unknown" :
            ["assessed", "unknown"].includes(parsed?.changeAssessment) && (parsed?.changeAssessment !== "assessed" || parsed?.complete === true));
        if (parsed && parsed.schemaVersion === 3 && parsed.checkpoint === cp.id && parsed.snapshotHash === snapshotHash && transitionBindingValid && annotationIdsMatch) {
          artifact.wikiJudgments[judgmentIndex] = { ...parsed,
            unauthorizedWriteAttempts: maintenanceWriteAttempts };
          try { validateArtifact(artifact); } catch { artifact.wikiJudgments[judgmentIndex] = { ...fallback, unauthorizedWriteAttempts: maintenanceWriteAttempts }; }
        } else artifact.wikiJudgments[judgmentIndex] = fallback;
        maintenanceOk = true;
      } catch {
        if (!sidecar.sessions.some((entry) => entry.phase === `maintenance:${cp.id}`)) recordSolverSession(`maintenance:${cp.id}`, null, maintenanceWriteAttempts);
        const idx = fixture.checkpoints.findIndex((x) => x.id === cp.id);
        let pages = [];
        try { pages = runner.snapshotWiki().map((page) => ({ path: page.path, content: page.content, sha256: page.sha256 })); } catch { pages = []; }
        const contentHash = canonicalHash(pages.map(({ path: pagePath, sha256: digest }) => ({ path: pagePath, sha256: digest })).sort((a, b) => compareCodePoints(a.path, b.path)));
        const snapshot = { checkpoint: cp.id, status: "failed", pages, contentHash };
        const priorRecord = idx > 0 ? artifact.wikiSnapshots[idx - 1] : null;
        const previousSnapshotHash = priorRecord?.status === "complete" ? priorRecord.contentHash : null;
        const fallbackAssessment = idx === 0 ? "initialization" : "unknown";
        artifact.wikiSnapshots[idx] = snapshot;
        artifact.wikiJudgments[idx] = { ...emptyJudgment(fixture.gold.topics, cp.id, snapshot.contentHash, pages, previousSnapshotHash, fallbackAssessment), unauthorizedWriteAttempts: maintenanceWriteAttempts.length ? maintenanceWriteAttempts : normalizeWriteAttempts(runner.sessionLog.at(-1)?.unauthorizedWriteAttempts) };
        for (const trial of artifact.trials) if (trial.checkpoint === cp.id && trial.condition !== "sources") trial.wikiHash = contentHash;
        failArtifact(`maintenance:${cp.id}`, safeError());
      }
      const maintenanceIndex = fixture.checkpoints.findIndex((x) => x.id === cp.id);
      const maintenanceStatus = !maintenanceOk ? "failed" : artifact.wikiJudgments[maintenanceIndex].complete ? "complete" : "incomplete";
      sidecar.stages.push({ phase: `maintenance:${cp.id}`, status: maintenanceStatus, durationMs: performance.now() - stageStart,
        ...(maintenanceOk ? { retrievalBuildMs } : {}) });
      persist(artifact);
      if (!maintenanceOk || budget.state.stopped) throw new Error(budget.state.stopCode ?? "maintenance-failed");
      const cpSnapshot = artifact.wikiSnapshots[fixture.checkpoints.findIndex((x) => x.id === cp.id)];
      for (const row of runner.rows.filter((item) => item.checkpoint === cp.id)) {
        const cellStart = performance.now();
        const idx = artifact.trials.findIndex((item) => item.id === row.id);
        currentPhase = `answer:${row.id}`;
        await identityCheck();
        let answerResult;
        try {
          answerResult = await runner.answerCell(row, row.condition === "sources" ? null : cpSnapshot.contentHash);
        } catch {
          recordSolverSession(`answer:${row.id}`, null);
          artifact.trials[idx].status = "failed";
          artifact.trials[idx].unauthorizedWriteAttempts = normalizeWriteAttempts(runner.sessionLog.at(-1)?.unauthorizedWriteAttempts);
          failArtifact(`answer:${row.id}`, safeError());
          persist(artifact);
          throw new Error(budget.state.stopCode ?? "answer-failed");
        }
        recordSolverSession(`answer:${row.id}`, answerResult, answerResult.unauthorizedWriteAttempts);
        if (answerResult.status !== "success" || !answerResult.answer.trim()) {
          artifact.trials[idx].status = "failed";
          artifact.trials[idx].unauthorizedWriteAttempts = normalizeWriteAttempts(answerResult.unauthorizedWriteAttempts);
          failArtifact(`answer:${row.id}`, "empty-or-failed-answer");
          persist(artifact);
          throw new Error("answer-failed");
        }
        const answerHash = hash(answerResult.answer);
        const candidateId = randomUUID();
        const sourceHistory = fixture.checkpoints.filter((item) => item.id <= cp.id).flatMap((item) => item.sources.map((source) => ({ checkpoint: item.id, path: source.path, text: source.text, sha256: source.sha256 })));
        const taskGold = fixture.gold.expectedAnswers[row.taskId][cp.id];
        artifact.trials[idx] = { id: row.id, checkpoint: row.checkpoint, taskId: row.taskId, language: row.language, condition: row.condition,
          status: "success", answer: answerResult.answer, answerHash, answerJudgment: unknownAnswer(answerResult.answer, answerHash),
          sourceHistoryHash: answerResult.sourceHistoryHash, wikiHash: row.condition === "sources" ? null : cpSnapshot.contentHash,
          settings, unauthorizedWriteAttempts: normalizeWriteAttempts(answerResult.unauthorizedWriteAttempts) };
        persist(artifact);
        const judgePrompt = buildAnswerJudgePrompt({ candidateId, answerHash, rubric, schema: annotationSchema, checkpoint: cp.id, question: row.question,
          topic: row.topic, gold: taskGold, sourceHistory, answer: answerResult.answer });
        const judgeText = await judge(judgePrompt, `judge-answer:${row.id}`);
        const parsed = parseJudgment(judgeText);
        artifact.trials[idx].answerJudgment = parsed?.schemaVersion === 3 && parsed.answerHash === answerHash ? parsed : unknownAnswer(answerResult.answer, answerHash);
        try { validateArtifact(artifact); } catch { artifact.trials[idx].answerJudgment = unknownAnswer(answerResult.answer, answerHash); }
        const validatedJudgment = artifact.trials[idx].answerJudgment;
        sidecar.stages.push({ phase: `answer:${row.id}`, status: validatedJudgment.complete ? "complete" : "incomplete", durationMs: performance.now() - cellStart });
        persist(artifact);
        if (budget.state.stopped) throw new Error(budget.state.stopCode ?? "budget-stopped");
      }
    }
  } catch (error) {
    const safeCodes = new Set(["request-body-unavailable", "request-contract-mismatch", "code-identity-drift", "time-limit", "call-limit", "output-token-limit", "usage-unavailable", "response-model-drift", "unsupported-openai-route", "judge-session-failed", "maintenance-failed", "answer-failed", "budget-stopped", "cancelled"]);
    sidecar.stopCode ??= budget.state.stopCode ?? (safeCodes.has(error?.message) ? error.message : "run-stopped");
    sidecar.stages.push({ phase: "coordinator", status: "stopped", code: sidecar.stopCode });
    for (const trial of artifact.trials) if (trial.status === "missing") trial.status = "missing";
    for (let i = 0; i < artifact.wikiSnapshots.length; i++) {
      if (artifact.wikiSnapshots[i].status === "missing") {
        const cp = fixture.checkpoints[i];
        const missing = { checkpoint: cp.id, status: "missing", pages: [], contentHash: canonicalHash([]) };
        const priorRecord = i > 0 ? artifact.wikiSnapshots[i - 1] : null;
        const previousSnapshotHash = priorRecord?.status === "complete" ? priorRecord.contentHash : null;
        const fallbackAssessment = i === 0 ? "initialization" : "unknown";
        artifact.wikiSnapshots[i] = missing;
        artifact.wikiJudgments[i] = emptyJudgment(fixture.gold.topics, cp.id, missing.contentHash, [], previousSnapshotHash, fallbackAssessment);
      }
    }
  } finally {
    clearTimeout(timer);
    externalSignal?.removeEventListener("abort", cancelRun);
    try {
      sidecar.stopCode ??= budget.state.stopCode;
      try { persistMetadata(); } catch { sidecar.stopCode ??= "artifact-validation-failed"; }
      try {
        validateArtifact(artifact);
        persistFile(workdir, "artifact-v2.json", artifact);
        const scored = runEvaluator(artifact);
        if (scored.status === 0) {
          const report = JSON.parse(scored.stdout);
          report.efficiency = efficiencyReport(artifact, sidecar, report);
          persistFile(workdir, "score.json", report);
        } else sidecar.stopCode ??= "score-generation-failed";
      } catch { sidecar.stopCode ??= "artifact-validation-failed"; }
      try { persistMetadata(); } catch { /* Preserve the last successfully persisted sidecar and artifact. */ }
    } finally {
      try { runtime.dispose?.(); } finally { fs.rmSync(externalWorkspace, { recursive: true, force: true }); }
    }
  }
  return { workdir, stopCode: sidecar.stopCode };
}

export function dryRun(config, fixture) {
  if (config.live) throw new Error("dry-run-cannot-be-live");
  validateWorkdir(config.workdir);
  return manifest(config, fixture);
}

export function recordPreflightFailure(workdir, ownerToken) {
  try {
    const ownerPath = path.join(workdir, ".run-owner");
    const owner = fs.lstatSync(ownerPath);
    if (!owner.isFile() || owner.isSymbolicLink() || owner.nlink !== 1 || fs.readFileSync(ownerPath, "utf8") !== `${ownerToken}\\n`) return;
    const file = path.join(workdir, "run-metadata.json");
    const stat = fs.lstatSync(file);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) return;
    const metadata = JSON.parse(fs.readFileSync(file, "utf8"));
    metadata.stopCode = "preflight-failed";
    persistFile(workdir, "run-metadata.json", validateRunMetadata(metadata));
    const artifactPath = path.join(workdir, "artifact-v2.json");
    const artifactStat = fs.lstatSync(artifactPath);
    if (!artifactStat.isFile() || artifactStat.isSymbolicLink() || artifactStat.nlink !== 1) return;
    const artifact = JSON.parse(fs.readFileSync(artifactPath, "utf8"));
    const report = runEvaluator(artifact);
    if (report.status === 0) persistFile(workdir, "score.json", JSON.parse(report.stdout));
  } catch { /* No safe preflight report path was created. */ }
}

async function main(argv = process.argv.slice(2)) {
  if (argv.includes("--help") || argv.includes("-h")) { process.stdout.write(OUTPUT_HELP); return; }
  const config = resolveConfig({ argv });
  const fixture = fixtureLoad();
  const resolved = manifest(config, fixture);
  if (!config.live) { process.stdout.write(`${JSON.stringify(resolved, null, 2)}\n`); return; }
  if (!config.authFile || !config.vaultRoot) throw new Error("live-requires-auth-file-and-synthetic-vault-config");
  const ownerToken = randomUUID();
  try {
    const result = await runLive(config, resolved, fixture, ownerToken);
    process.stdout.write(`${JSON.stringify({ workdir: result.workdir, stopCode: result.stopCode ?? null })}\n`);
  } catch {
    recordPreflightFailure(config.workdir, ownerToken);
    throw new Error("live-run-stopped; inspect nonsecret run-metadata.json");
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => { process.stderr.write(`${error instanceof Error ? error.message : "run-failed"}\n`); process.exitCode = 1; });
}
