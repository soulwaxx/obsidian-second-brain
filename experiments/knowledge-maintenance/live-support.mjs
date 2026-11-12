import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../..");
export const DEFAULTS = Object.freeze({
  solver: Object.freeze({ provider: "openai", model: "gpt-6.1-sol", effort: "medium" }),
  judge: Object.freeze({ provider: "openai", model: "gpt-6.1-terra", effort: "medium" }),
  limits: Object.freeze({ calls: 750, minutes: 90, outputTokens: 500000, maxOutputTokens: 4096 }),
});
const KEYS = new Set(["solver", "judge", "limits", "authFile", "workdir", "vaultRoot"]);
const PREFIX = "OSB_KM_";
const CODE_PATHS = [
  "experiments/knowledge-maintenance/evaluation.py",
  "experiments/knowledge-maintenance/fixtures.json",
  "experiments/knowledge-maintenance/pi-runner.mjs",
  "experiments/knowledge-maintenance/live-support.mjs",
  "experiments/knowledge-maintenance/native-bridge.mjs",
  "experiments/knowledge-maintenance/run.mjs",
  "experiments/knowledge-maintenance/rubric.json",
  "experiments/knowledge-maintenance/annotation-schema.json",
  "tests/knowledge-maintenance-live.test.mjs",
  "tests/knowledge-maintenance-runtime.test.mjs",
  "tests/test_knowledge_maintenance.py",
  "tests/pi-runtime-paths.mjs",
  "extensions/obsidian.ts",
  "hooks/obsidian-session.sh",
  "hooks/hooks.json",
  "package.json",
];
// Sorted names of the guarded OpenAI Responses request fields; only these names (never values) may be recorded.
export const REQUEST_CONTRACT_FIELDS = Object.freeze(["max_output_tokens", "model", "reasoning.effort", "store", "stream"]);
const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const canonicalJson = (value) => Array.isArray(value) ? `[${value.map(canonicalJson).join(",")}]` : value && typeof value === "object" ? `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}` : JSON.stringify(value);
const canonicalHash = (value) => sha256(canonicalJson(value));
const fail = (code) => { throw new Error(code); };

function exactObject(value, keys, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${name}-must-be-object`);
  for (const key of Object.keys(value)) if (!keys.has(key)) fail(`${name}-unknown-key`);
}

function positiveInt(value, key, max) {
  if (!Number.isSafeInteger(value) || value < 1 || value > max) fail(`invalid-${key}`);
  return value;
}

function loadConfigFile(file) {
  if (!file) return {};
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) fail("unsafe-config-file");
  let parsed;
  try { parsed = JSON.parse(fs.readFileSync(file, "utf8")); } catch { fail("invalid-config-json"); }
  exactObject(parsed, KEYS, "config");
  return parsed;
}

function parsePair(value, key) {
  if (value === undefined) return undefined;
  exactObject(value, new Set(["provider", "model", "effort"]), key);
  if (typeof value.provider !== "string" || typeof value.model !== "string" || typeof value.effort !== "string") fail(`invalid-${key}`);
  if (!/^[a-z0-9-]+$/u.test(value.provider) || !/^[a-zA-Z0-9._-]+$/u.test(value.model) || !["low", "medium", "high"].includes(value.effort)) fail(`invalid-${key}`);
  return { provider: value.provider, model: value.model, effort: value.effort };
}

function mergeConfig(config, env, cli) {
  const merged = { ...config };
  for (const [key, raw] of Object.entries(env)) {
    if (!key.startsWith(PREFIX)) continue;
    const field = key.slice(PREFIX.length).toLowerCase();
    if (field === "config") continue;
    if (!["solver_provider", "solver_model", "solver_effort", "judge_provider", "judge_model", "judge_effort", "workdir", "auth_file", "vault_root", "calls", "minutes", "output_tokens", "max_output_tokens", "response_tokens"].includes(field)) fail("unknown-prefixed-environment-key");
    if (field === "workdir") merged.workdir = raw;
    else if (field === "auth_file") merged.authFile = raw;
    else if (field === "vault_root") merged.vaultRoot = raw;
    else if (field.startsWith("solver_") || field.startsWith("judge_")) {
      const role = field.startsWith("solver_") ? "solver" : "judge";
      merged[role] = { ...merged[role], [field.slice(role.length + 1)]: raw };
    } else {
      const limit = field === "calls" ? "calls" : field === "minutes" ? "minutes" : field === "output_tokens" ? "outputTokens" : "maxOutputTokens";
      merged.limits = { ...merged.limits, [limit]: Number(raw) };
    }
  }
  for (const [key, value] of Object.entries(cli)) {
    if (["live", "dryRun", "validateConfig", "configFile"].includes(key)) continue;
    if (["solverProvider", "solverModel", "solverEffort", "judgeProvider", "judgeModel", "judgeEffort"].includes(key)) {
      const role = key.startsWith("solver") ? "solver" : "judge";
      const field = key.endsWith("Provider") ? "provider" : key.endsWith("Model") ? "model" : "effort";
      merged[role] = { ...merged[role], [field]: value };
    } else if (["calls", "minutes", "outputTokens", "maxOutputTokens"].includes(key)) merged.limits = { ...merged.limits, [key]: value };
    else merged[key] = value;
  }
  return merged;
}

export function resolveConfig({ argv = [], env = process.env, cwd = process.cwd(), configPath } = {}) {
  const args = { live: false, dryRun: false, validateConfig: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--live") { args.live = true; continue; }
    if (arg === "--dry-run") { args.dryRun = true; continue; }
    if (arg === "--validate-config") { args.validateConfig = true; continue; }
    if (arg === "--help" || arg === "-h") { args.help = true; continue; }
    const flags = new Map([["--config", "configFile"], ["--solver-provider", "solverProvider"], ["--solver-model", "solverModel"], ["--solver-effort", "solverEffort"], ["--judge-provider", "judgeProvider"], ["--judge-model", "judgeModel"], ["--judge-effort", "judgeEffort"], ["--workdir", "workdir"], ["--auth-file", "authFile"], ["--vault-root", "vaultRoot"], ["--max-calls", "calls"], ["--max-minutes", "minutes"], ["--max-output-tokens-total", "outputTokens"], ["--response-tokens", "maxOutputTokens"]]);
    const field = flags.get(arg);
    if (!field || i + 1 >= argv.length || argv[i + 1].startsWith("--")) fail("invalid-command-line");
    args[field] = ["calls", "minutes", "outputTokens", "maxOutputTokens"].includes(field) ? Number(argv[++i]) : argv[++i];
  }
  if (args.live && args.dryRun) fail("live-and-dry-run-conflict");
  const dedicated = args.configFile ?? env.OSB_KM_CONFIG ?? configPath;
  const fileConfig = loadConfigFile(dedicated);
  const withRoleDefaults = { ...fileConfig };
  for (const role of ["solver", "judge"]) {
    if (fileConfig[role] !== undefined) exactObject(fileConfig[role], new Set(["provider", "model", "effort"]), role);
    withRoleDefaults[role] = { ...DEFAULTS[role], ...fileConfig[role] };
  }
  const resolved = mergeConfig(withRoleDefaults, env, args);
  exactObject(resolved, KEYS, "resolved-config");
  const solver = parsePair(resolved.solver, "solver") ?? DEFAULTS.solver;
  const judge = parsePair(resolved.judge, "judge") ?? DEFAULTS.judge;
  const limitsInput = resolved.limits ?? {};
  exactObject(limitsInput, new Set(["calls", "minutes", "outputTokens", "maxOutputTokens"]), "limits");
  const limits = {
    calls: positiveInt(limitsInput.calls ?? DEFAULTS.limits.calls, "calls", 750),
    minutes: positiveInt(limitsInput.minutes ?? DEFAULTS.limits.minutes, "minutes", 90),
    outputTokens: positiveInt(limitsInput.outputTokens ?? DEFAULTS.limits.outputTokens, "output-tokens", 500000),
    maxOutputTokens: positiveInt(limitsInput.maxOutputTokens ?? DEFAULTS.limits.maxOutputTokens, "max-output-tokens", 4096),
  };
  if (limits.maxOutputTokens < 16) fail("max-output-tokens-below-provider-minimum");
  if (typeof resolved.workdir !== "string" || !path.isAbsolute(resolved.workdir)) fail("absolute-workdir-required");
  if (resolved.authFile !== undefined && (typeof resolved.authFile !== "string" || !path.isAbsolute(resolved.authFile))) fail("absolute-auth-file-required");
  if (resolved.vaultRoot !== undefined && (typeof resolved.vaultRoot !== "string" || !path.isAbsolute(resolved.vaultRoot))) fail("absolute-vault-root-required");
  return { live: args.live === true, dryRun: args.dryRun === true || args.live !== true, validateConfig: args.validateConfig === true,
    help: args.help === true, cwd: path.resolve(cwd), solver, judge, limits, workdir: resolved.workdir,
    authFile: resolved.authFile ?? null, vaultRoot: resolved.vaultRoot ?? null };
}

function isWithin(parent, target) {
  const rel = path.relative(parent, target);
  return rel === "" || (!rel.startsWith(`..${path.sep}`) && rel !== ".." && !path.isAbsolute(rel));
}

export function validateWorkdir(workdir, { repoRoot = ROOT, home = process.env.HOME } = {}) {
  if (typeof workdir !== "string" || !path.isAbsolute(workdir)) fail("absolute-workdir-required");
  const target = path.resolve(workdir);
  if (isWithin(path.resolve(repoRoot), target)) fail("workdir-inside-repository");
  if (home && isWithin(path.resolve(home), target)) fail("workdir-inside-personal-home");
  let cursor = path.parse(target).root;
  for (const part of target.slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    try { if (fs.lstatSync(cursor).isSymbolicLink()) fail("workdir-symlink"); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  if (fs.existsSync(target)) fail("workdir-already-exists");
  return target;
}

function codePathManifest(root) {
  const names = new Set(CODE_PATHS);
  const collect = (directory, extensions) => {
    const absolute = path.join(root, directory);
    const walk = (current) => {
      for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
        if (entry.name === "__pycache__" || entry.name === "node_modules") continue;
        const file = path.join(current, entry.name);
        const stat = fs.lstatSync(file);
        if (stat.isSymbolicLink()) fail("unsafe-code-input");
        if (entry.isDirectory()) walk(file);
        else if (stat.isFile() && extensions.has(path.extname(entry.name))) names.add(path.relative(root, file).split(path.sep).join("/"));
      }
    };
    walk(absolute);
  };
  collect("skills", new Set([".md", ".py", ".sh"]));
  collect("scripts", new Set([".py", ".mjs"]));
  collect("extensions", new Set([".ts", ".js"]));
  collect("hooks", new Set([".sh", ".json"]));
  return [...names].sort();
}

export function codeIdentity({ root = ROOT, files } = {}) {
  const selectedFiles = files ?? codePathManifest(root);
  const records = selectedFiles.map((name) => {
    const file = path.resolve(root, name);
    const stat = fs.lstatSync(file);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) fail("unsafe-code-input");
    return { path: name, sha256: sha256(fs.readFileSync(file)) };
  });
  return { revision: null, revisionStatus: "not-recorded", files: records, hash: sha256(JSON.stringify(records)) };
}

export function createBudget({ limits = DEFAULTS.limits, now = () => performance.now(), onStop = () => {} } = {}) {
  const started = now();
  const state = { calls: 0, outputTokens: 0, stopped: false, stopCode: null, requestMismatchFields: null, records: [] };
  const stop = (code, mismatchedFields) => {
    if (!state.stopped) {
      state.stopped = true;
      state.stopCode = code;
      if (code === "request-contract-mismatch" && mismatchedFields?.length) state.requestMismatchFields = [...mismatchedFields];
      onStop(code);
    }
    fail(code);
  };
  return {
    state,
    beginAttempt(metadata = {}) {
      if (state.stopped) fail(state.stopCode ?? "budget-stopped");
      if (state.calls >= limits.calls) stop("call-limit");
      if (now() - started >= limits.minutes * 60_000) stop("time-limit");
      const attempt = { id: state.calls + 1, ...metadata, status: "attempted", usage: null };
      state.calls += 1;
      state.records.push(attempt);
      return attempt;
    },
    cancelPendingAttempt(attempt) {
      if (attempt?.status !== "attempted" || state.records.at(-1) !== attempt || state.calls !== attempt.id) fail("attempt-ledger-invariant");
      state.records.pop();
      state.calls--;
    },
    finishAttempt(attempt, usage) {
      if (!usage || !Number.isSafeInteger(usage.outputTokens) || usage.outputTokens < 0 ||
        !Number.isSafeInteger(usage.inputTokens) || usage.inputTokens < 0 ||
        !Number.isSafeInteger(usage.totalTokens) || usage.totalTokens < usage.outputTokens || usage.totalTokens < usage.inputTokens) {
        attempt.status = "usage-unavailable";
        stop("usage-unavailable");
      }
      attempt.status = "completed";
      attempt.usage = usage;
      state.outputTokens += usage.outputTokens;
      if (state.outputTokens > limits.outputTokens) stop("output-token-limit");
    },
    failAttempt(attempt, code = "provider-error") {
      attempt.status = "failed";
      if (!state.stopped) { state.stopped = true; state.stopCode = code; onStop(code); }
      attempt.errorCode = code === "transport-error" ? "transport-error" : "provider-error";
    },
    stop,
  };
}

function parseUsage(data) {
  if (data?.type !== "response.completed" || !data.response || data.response.status !== "completed") return null;
  const usage = data.response.usage;
  if (!usage || !Number.isSafeInteger(usage.input_tokens) || !Number.isSafeInteger(usage.output_tokens) || !Number.isSafeInteger(usage.total_tokens)) return null;
  const details = usage.input_tokens_details ?? {};
  return {
    inputTokens: usage.input_tokens,
    outputTokens: usage.output_tokens,
    totalTokens: usage.total_tokens,
    cachedInputTokens: Number.isSafeInteger(details.cached_tokens) ? details.cached_tokens : null,
    pricing: null,
    responseId: typeof data.response.id === "string" ? data.response.id : null,
    responseModel: typeof data.response.model === "string" ? data.response.model : null,
  };
}

function inspectResponsesBody(raw) {
  const trimmed = typeof raw === "string" ? raw.trim() : "";
  if (!trimmed.startsWith("{")) return null;
  try { return JSON.parse(trimmed); } catch { return null; }
}

export function wrapOpenAIProvider(provider, budget, { maxOutputTokens = 4096, effort = "medium", fetchImpl = globalThis.fetch, expectedRequestUrl, beforeDispatch = () => {}, onAttempt = () => {}, onAttemptComplete = () => {} } = {}) {
  if (!provider || typeof provider.streamSimple !== "function" || typeof fetchImpl !== "function") fail("openai-provider-unavailable");
  const original = provider.streamSimple;
  let inFlight = false;
  const safeCodes = new Set(["request-body-unavailable", "request-contract-mismatch", "response-model-drift", "usage-unavailable", "output-token-limit", "time-limit", "call-limit", "code-identity-drift", "unsupported-openai-route", "transport-error", "provider-error"]);
  return {
    ...provider,
    streamSimple(model, context, options = {}) {
      if (inFlight) fail("provider-attempt-in-flight");
      const expectedUrl = typeof expectedRequestUrl === "function" ? expectedRequestUrl(model) : expectedRequestUrl ?? "https://api.openai.com/v1/responses";
      if (model.provider !== "openai" || model.api !== "openai-responses" || typeof expectedUrl !== "string") fail("unsupported-openai-route");
      if (model.compat?.supportsMaxOutputTokens === false) fail("output-cap-unsupported");
      const responseLimit = typeof maxOutputTokens === "function" ? maxOutputTokens(model) : maxOutputTokens;
      const requestEffort = typeof effort === "function" ? effort(model) : effort;
      const reports = [];
      let attempt;
      let captureReader;
      let captureJob;
      let requestViolation;
      inFlight = true;
      const fetch = async (input, init = {}) => {
        let rawBody = init.body;
        if (rawBody === undefined && typeof Request !== "undefined" && input instanceof Request) rawBody = await input.clone().text();
        const body = inspectResponsesBody(rawBody);
        if (!body) { requestViolation = "request-body-unavailable"; budget.stop(requestViolation); }
        const expected = { max_output_tokens: responseLimit, model: model.id, "reasoning.effort": requestEffort, store: false, stream: true };
        const actual = { max_output_tokens: body.max_output_tokens, model: body.model, "reasoning.effort": body.reasoning?.effort, store: body.store, stream: body.stream };
        const mismatchedFields = REQUEST_CONTRACT_FIELDS.filter((field) => actual[field] !== expected[field]);
        if (mismatchedFields.length) {
          requestViolation = "request-contract-mismatch";
          budget.stop(requestViolation, mismatchedFields);
        }
        const request = input instanceof Request ? input : null;
        const requestUrl = new URL(request ? request.url : String(input));
        const method = init.method ?? request?.method;
        if (requestUrl.href !== expectedUrl || requestUrl.search || requestUrl.hash || method?.toUpperCase() !== "POST") { requestViolation = "unsupported-openai-route"; fail(requestViolation); }
        if (["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"].some((key) => process.env[key])) { requestViolation = "ambient-proxy-disabled"; fail(requestViolation); }
        beforeDispatch();
        attempt = budget.beginAttempt({ provider: "openai", api: model.api, model: model.id, responseModel: null });
        try { onAttempt(attempt); } catch {
          budget.cancelPendingAttempt(attempt);
          attempt = null;
          throw new Error("attempt-ledger-failed");
        }
        try {
          const response = await fetchImpl(input, { ...init, redirect: "error" });
          const capture = response.clone();
          captureReader = capture.body?.getReader();
          captureJob = (async () => {
            if (!captureReader) return;
            const decoder = new TextDecoder();
            let raw = "";
            for (;;) {
              const { done, value } = await captureReader.read();
              if (done) break;
              raw += decoder.decode(value, { stream: true });
            }
            raw += decoder.decode();
            for (const line of raw.split(/\r?\n/u)) {
              if (!line.startsWith("data: ") || line === "data: [DONE]") continue;
              try {
                const usage = parseUsage(JSON.parse(line.slice(6)));
                if (usage) reports.push(usage);
              } catch { /* malformed provider events are rejected after stream completion */ }
            }
          })().catch(() => {});
          return response;
        } catch {
          budget.failAttempt(attempt, "transport-error");
          onAttemptComplete(attempt);
          throw new Error("provider-transport-error");
        }
      };
      const onProviderStreamEvent = async (event, eventModel) => {
        if (event?.type === "response.completed") {
          const usage = parseUsage(event);
          if (usage) reports.push(usage);
        }
        return options.onProviderStreamEvent?.(event, eventModel);
      };
      let stream;
      try { stream = original.call(provider, model, context, { ...options, maxTokens: responseLimit, maxRetries: 0, fetch, onProviderStreamEvent }); }
      catch (error) { inFlight = false; throw error; }
      return (async function* () {
        const terminalEvents = [];
        const cancelCapture = async () => {
          try { await captureReader?.cancel(); } catch { /* Capture may already be settled. */ }
          if (captureJob) await captureJob;
        };
        try {
          for await (const event of stream) {
            if (event.type === "done" || event.type === "error") terminalEvents.push(event);
            else yield event;
          }
          if (captureJob) await captureJob;
          if (!attempt) fail(requestViolation ?? "unsupported-openai-route");
          if (attempt.status === "failed") fail(attempt.errorCode ?? budget.state.stopCode ?? "provider-call-failed");
          if (terminalEvents.some((event) => event.type === "error")) {
            if (attempt.status === "attempted") {
              budget.failAttempt(attempt, "provider-error");
              onAttemptComplete(attempt);
            }
            fail(budget.state.stopCode ?? "provider-error");
          }
          const usage = reports.at(-1);
          try {
            if (!usage) budget.finishAttempt(attempt, null);
            attempt.responseModel = usage.responseModel;
            budget.finishAttempt(attempt, usage);
            if (usage.responseModel && usage.responseModel !== model.id) budget.stop("response-model-drift");
          } finally { onAttemptComplete(attempt); }
          for (const event of terminalEvents) yield event;
        } catch (error) {
          await cancelCapture();
          if (!budget.state.stopped && attempt?.status === "attempted") { budget.failAttempt(attempt, "provider-error"); onAttemptComplete(attempt); }
          const code = budget.state.stopCode ?? (safeCodes.has(error?.message) ? error.message : "provider-call-failed");
          throw new Error(code);
        } finally {
          if (captureReader && captureJob) await cancelCapture();
          if (attempt?.status === "attempted") {
            budget.failAttempt(attempt, budget.state.stopCode ?? "provider-error");
            try { onAttemptComplete(attempt); } catch { /* Preserve the stable failed-attempt ledger code. */ }
          }
          inFlight = false;
        }
      })();
    },
  };
}

export async function loadSelectedOpenAIAuth({ AuthStorage, ModelRuntime, readStoredCredential, authFile }) {
  if (!authFile || !path.isAbsolute(authFile)) fail("explicit-auth-file-required");
  let cursor = path.parse(path.resolve(authFile)).root;
  for (const part of path.resolve(authFile).slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    if (fs.lstatSync(cursor).isSymbolicLink()) fail("unsafe-auth-file");
  }
  const stat = fs.lstatSync(authFile);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) fail("unsafe-auth-file");
  const credential = readStoredCredential("openai", authFile);
  if (!credential || credential.type !== "api_key") fail("unsupported-openai-auth-mode-output-cap-unverified");
  if (typeof credential.key !== "string" || !credential.key.startsWith("sk-") || credential.key.startsWith("!") || credential.key.startsWith("$") || credential.env !== undefined) fail("unsupported-openai-api-key-record");
  const credentials = AuthStorage.inMemory({ openai: credential });
  return ModelRuntime.create({ credentials, modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
}
