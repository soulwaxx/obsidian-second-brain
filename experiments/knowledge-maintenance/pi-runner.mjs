import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { installedPackage, resolveRuntimeDependencies } from "../../tests/pi-runtime-paths.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const CONDITIONS = ["sources", "sources+wiki", "wiki-only"];
const sha256 = (value) => createHash("sha256").update(value).digest("hex");
function compareCodePoints(left, right) {
  const a = Array.from(left, (char) => char.codePointAt(0));
  const b = Array.from(right, (char) => char.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index++) if (a[index] !== b[index]) return a[index] - b[index];
  return a.length - b.length;
}
function canonicalJson(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("canonical JSON requires finite numbers");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  fail("unsupported canonical JSON value");
}
export const canonicalHash = (value) => sha256(canonicalJson(value));
const fail = (message) => { throw new Error(message); };

function inside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function safePath(root, relative, { existing = false, markdown = true } = {}) {
  if (typeof relative !== "string" || !relative || relative.includes("\\") || relative.startsWith("/") || relative.split("/").some((part) => !part || part === "." || part === "..")) fail("out-of-scope");
  if (markdown && !relative.toLowerCase().endsWith(".md")) fail("out-of-scope");
  const target = path.resolve(root, relative);
  if (!inside(root, target)) fail("out-of-scope");
  let walk = root;
  for (const part of relative.split("/")) {
    walk = path.join(walk, part);
    try {
      const stat = fs.lstatSync(walk);
      if (stat.isSymbolicLink()) fail("symlink");
      if (stat.isFile() && stat.nlink !== 1) fail("hardlink");
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
  }
  if (existing && !fs.existsSync(target)) fail("not-found");
  return target;
}

function snapshot(root) {
  const records = {};
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const target = path.join(directory, entry.name);
      const rel = path.relative(root, target);
      const stat = fs.lstatSync(target);
      const kind = entry.isSymbolicLink() ? "symlink" : entry.isDirectory() ? "directory" : stat.isFile() ? "file" : "other";
      records[rel] = { kind, mode: stat.mode, links: stat.nlink, size: stat.size, mtimeNs: stat.mtimeNs?.toString() ?? String(stat.mtimeMs), bytes: kind === "file" && stat.nlink === 1 ? fs.readFileSync(target).toString("base64") : null };
      if (entry.isDirectory()) visit(target);
    }
  };
  try {
    const stat = fs.lstatSync(root);
    if (stat.isDirectory()) visit(root);
    else records[path.basename(root)] = { kind: stat.isSymbolicLink() ? "symlink" : stat.isFile() ? "file" : "other", mode: stat.mode, links: stat.nlink, size: stat.size, mtimeNs: stat.mtimeNs?.toString() ?? String(stat.mtimeMs), bytes: stat.isFile() && stat.nlink === 1 ? fs.readFileSync(root).toString("base64") : null };
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  return records;
}

function safeAbsoluteFile(root, target, { directory = false } = {}) {
  const relative = path.relative(root, target).split(path.sep).join("/");
  if (!inside(root, target) || !relative || relative.split("/").some((part) => !part || part === "." || part === "..")) fail("out-of-scope");
  let current = root;
  for (const part of relative.split("/")) {
    current = path.join(current, part);
    const stat = fs.lstatSync(current);
    if (stat.isSymbolicLink()) fail("symlink");
    if (stat.isFile() && stat.nlink !== 1) fail("hardlink");
  }
  const stat = fs.lstatSync(target);
  if (directory ? !stat.isDirectory() : !stat.isFile()) fail(directory ? "not-directory" : "not-file");
  return target;
}

function loadFixture(fixture) {
  if (fixture) return fixture;
  const fixturePath = safeAbsoluteFile(ROOT, path.join(ROOT, "experiments/knowledge-maintenance/fixtures.json"));
  return JSON.parse(fs.readFileSync(fixturePath, "utf8"));
}

function taskRows(fixture) {
  const rows = [];
  for (const cp of fixture.checkpoints) for (const task of fixture.visibleTasks) for (const language of ["en", "it"]) for (const condition of CONDITIONS) {
    rows.push({ id: `${cp.id}/${task.id}/${language}/${condition}`, checkpoint: cp.id, taskId: task.id, topic: task.topic, intent: task.intent, language, condition, question: task.question[language] });
  }
  return rows;
}

async function loadSdk(packageRoot) {
  if (!packageRoot) fail("UNVERIFIED: installed Pi SDK unavailable; set PI_CODING_AGENT_PACKAGE");
  const sdkUrl = pathToFileURL(path.join(packageRoot, "dist/index.js")).href;
  const [ai] = await Promise.all(resolveRuntimeDependencies(packageRoot).slice(0, 1).map((url) => import(url)));
  return [await import(sdkUrl), ai];
}

function skillAllowlist() {
  const dirs = ["wiki", "wiki-query", "wiki-save", "wiki-ingest", "wiki-research", "wiki-health"];
  const files = new Set();
  for (const dir of dirs) {
    const base = safeAbsoluteFile(ROOT, path.join(ROOT, "skills", dir), { directory: true });
    files.add(safeAbsoluteFile(ROOT, path.join(base, "SKILL.md")));
    if (dir === "wiki") for (const entry of fs.readdirSync(safeAbsoluteFile(ROOT, path.join(base, "references"), { directory: true }))) if (entry.endsWith(".md")) files.add(safeAbsoluteFile(ROOT, path.join(base, "references", entry)));
  }
  return files;
}

function makeScopedExtension({ vaultRoot, sourceRecords, sourceEnabled, wikiEnabled, writable, skills, writeAttempts, accessDenials, providerEvents, onProviderEvent }) {
  return (pi) => {
    const sourceMap = new Map(sourceRecords.map((item) => [item.path, item]));
    const allowedTools = new Set(["experiment_source_read", "experiment_skill_read", ...(wikiEnabled ? ["wiki_search", "wiki_outline", "wiki_read"] : []), ...(writable ? ["write", "edit"] : [])]);
    pi.on("before_provider_request", (event, ctx) => {
      const record = { event: "before_provider_request", provider: ctx.model?.provider ?? null, model: ctx.model?.id ?? null };
      providerEvents.push(record); onProviderEvent?.(record);
    });
    pi.on("after_provider_response", (event, ctx) => {
      const record = { event: "after_provider_response", provider: ctx.model?.provider ?? null, model: ctx.model?.id ?? null, status: event.status };
      providerEvents.push(record); onProviderEvent?.(record);
    });
    pi.registerTool({
      name: "experiment_source_read", label: "Read supplied source", description: "Read one explicitly supplied synthetic source version by its checkpoint-qualified path.",
      parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"], additionalProperties: false },
      execute: async (_id, params) => {
        const item = sourceMap.get(params.path);
        if (!sourceEnabled || !item) { accessDenials.push("source-unavailable"); return { content: [{ type: "text", text: "Denied: source is unavailable in this arm." }], details: undefined, isError: true }; }
        return { content: [{ type: "text", text: `${item.path}\nsha256=${item.sha256}\n${item.text}` }], details: undefined };
      },
    });
    pi.registerTool({
      name: "experiment_skill_read", label: "Read selected wiki skill", description: "Read an explicitly allowlisted shipped wiki skill or reference Markdown file.",
      parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"], additionalProperties: false },
      execute: async (_id, params) => {
        let requested;
        try {
          if (typeof params.path !== "string" || path.isAbsolute(params.path) || params.path.split(/[\\/]/).some((part) => part === ".." || part === "")) fail("out-of-scope");
          requested = safePath(ROOT, params.path, { existing: true });
        } catch (error) { accessDenials.push(error.message || "out-of-scope"); return { content: [{ type: "text", text: "Denied: resource path is unsafe." }], details: undefined, isError: true }; }
        if (!skills.has(requested) || !inside(ROOT, requested)) { accessDenials.push("out-of-scope"); return { content: [{ type: "text", text: "Denied: resource is not allowlisted." }], details: undefined, isError: true }; }
        return { content: [{ type: "text", text: fs.readFileSync(requested, "utf8") }], details: undefined };
      },
    });
    pi.on("tool_call", (event) => {
      if (!allowedTools.has(event.toolName)) {
        if (event.toolName === "write" || event.toolName === "edit") writeAttempts.push("write");
        else accessDenials.push("tool-out-of-scope");
        return { block: true, reason: "Tool is outside the experiment allowlist." };
      }
      if (event.toolName === "write" || event.toolName === "edit") {
        try {
          if (typeof event.input.path !== "string" || path.isAbsolute(event.input.path) || event.input.path.startsWith("@") || event.input.path.includes("\\\\")) fail("host-path");
          const target = safePath(vaultRoot, event.input.path);
          if (!inside(path.join(vaultRoot, "wiki"), target)) fail("out-of-scope");
          const name = path.basename(target).toLowerCase();
          if (["index.md", "log.md", "_plan.md"].includes(name)) fail("reserved");
        } catch (error) {
          const code = error.message === "reserved" ? "write" : error.message === "host-path" ? "host-path" : "out-of-scope";
          writeAttempts.push(code);
          return { block: true, reason: "Denied by synthetic wiki write scope." };
        }
      }
      if (["wiki_search", "wiki_outline", "wiki_read", "wiki_doctor"].includes(event.toolName)) {
        if (!wikiEnabled) { accessDenials.push("wiki-unavailable"); return { block: true, reason: "Wiki access is not available in this condition." }; }
        const input = event.input;
        if (input.vault !== undefined && input.vault !== vaultRoot) { accessDenials.push("vault-override"); return { block: true, reason: "Vault override denied." }; }
        if (input.hybrid || input.ollamaUrl || input.ollamaModel) { accessDenials.push("network-option"); return { block: true, reason: "Network and hybrid options are disabled." }; }
        if (input.page !== undefined) {
          try {
            if (typeof input.page !== "string" || !input.page.startsWith("wiki/") || input.page.startsWith("@")) fail("out-of-scope");
            const target = safePath(vaultRoot, input.page, { existing: true });
            if (!inside(path.join(vaultRoot, "wiki"), target)) fail("out-of-scope");
          }
          catch (error) { accessDenials.push(error.message || "out-of-scope"); return { block: true, reason: "Wiki path denied." }; }
        }
        input.vault = vaultRoot;
        input.hybrid = false;
        delete input.ollamaUrl;
        delete input.ollamaModel;
      }
    });
  };
}

/**
 * Build an offline, injectable Pi SDK experiment adapter. The host owns vault
 * preparation/BM25 builds and supplies the already configured ModelRuntime.
 */
export async function createPiRunner(options) {
  const { vaultRoot, workspaceRoot, modelRuntime, model, effort = "medium", maxOutputTokens = 4096, fixture: suppliedFixture, sdkPackage, signal, onSession, onProviderEvent } = options ?? {};
  if (!vaultRoot || !workspaceRoot || !modelRuntime || !model) fail("vaultRoot, workspaceRoot, modelRuntime, and model are required");
  for (const root of [vaultRoot, workspaceRoot]) {
    let current = path.parse(path.resolve(root)).root;
    for (const part of path.resolve(root).slice(current.length).split(path.sep).filter(Boolean)) {
      current = path.join(current, part);
      const stat = fs.lstatSync(current);
      if (stat.isSymbolicLink()) fail("experiment roots must not traverse symlinks");
    }
    if (!fs.lstatSync(root).isDirectory()) fail("experiment roots must be real directories");
  }
  const vault = fs.realpathSync(vaultRoot);
  const workspace = fs.realpathSync(workspaceRoot);
  if (inside(vault, workspace)) fail("workspace-overlaps-vault");
  const marker = path.join(vault, ".knowledge-maintenance-synthetic");
  const markerStat = fs.lstatSync(marker);
  if (!markerStat.isFile() || markerStat.isSymbolicLink() || markerStat.nlink !== 1 || fs.readFileSync(marker, "utf8") !== "obsidian-second-brain synthetic knowledge-maintenance vault v1\n") fail("synthetic-vault-marker-required");
  const fixture = loadFixture(suppliedFixture);
  const sdkRoot = sdkPackage ?? installedPackage();
  if (!sdkRoot) fail("UNVERIFIED: installed Pi SDK unavailable; runner requires the actual installed SDK");
  if (!Number.isInteger(maxOutputTokens) || maxOutputTokens < 1 || maxOutputTokens > 4096) fail("maxOutputTokens must be between 1 and 4096");
  const [sdk] = await loadSdk(sdkRoot);
  const { createAgentSession, SessionManager, SettingsManager } = sdk;
  const extensionLoaderUrl = pathToFileURL(path.join(sdkRoot, "dist/core/extensions/loader.js")).href;
  const skillsLoaderUrl = pathToFileURL(path.join(sdkRoot, "dist/core/skills.js")).href;
  const eventBusUrl = pathToFileURL(path.join(sdkRoot, "dist/core/event-bus.js")).href;
  const [{ loadExtensions, loadExtensionFromFactory, createExtensionRuntime }, { loadSkills }, { createEventBus }] = await Promise.all([import(extensionLoaderUrl), import(skillsLoaderUrl), import(eventBusUrl)]);
  const skills = skillAllowlist();
  const configDir = path.join(workspace, ".knowledge-maintenance");
  if (fs.existsSync(configDir)) fail("experiment config directory already exists; refusing to overwrite host files");
  fs.mkdirSync(configDir, { recursive: true });
  const configPath = path.join(configDir, "properties.json");
  fs.writeFileSync(configPath, JSON.stringify({ vaultPath: vault, features: { guard: true, autoCommit: false, toc: true, retrievalRefresh: false } }));
  const sessionCwd = path.join(configDir, "cwd");
  const agentDir = path.join(configDir, "empty-agent");
  fs.mkdirSync(sessionCwd);
  fs.mkdirSync(agentDir);
  const rows = taskRows(fixture);
  const inputPaths = [safeAbsoluteFile(ROOT, path.join(ROOT, "experiments/knowledge-maintenance/fixtures.json")), ...skills];
  const inputSnapshot = () => Object.fromEntries(inputPaths.map((input) => [path.relative(ROOT, input), snapshot(input)]));
  const sessionLog = [];
  let sessionActive = false;

  async function runSession({ phase, sourceRecords, sourceEnabled, wikiEnabled, prompt, writable }) {
    if (wikiEnabled) assertWikiTreeSafe(vault);
    if (signal?.aborted) fail("cancelled-before-session");
    if (sessionActive) fail("concurrent sessions are disabled for deterministic bounded orchestration");
    sessionActive = true;
    const writeAttempts = [];
    const accessDenials = [];
    const providerEvents = [];
    const start = performance.now();
    const before = snapshot(vault);
    const configBefore = snapshot(configPath);
    const sourceBefore = inputSnapshot();
    const previousEnv = { config: process.env.OBSIDIAN_AGENT_CONFIG, vault: process.env.OBSIDIAN_VAULT_PATH, middleware: process.env.WIKI_MIDDLEWARE_DIR };
    process.env.OBSIDIAN_AGENT_CONFIG = configPath;
    process.env.OBSIDIAN_VAULT_PATH = vault;
    process.env.WIKI_MIDDLEWARE_DIR = path.join(ROOT, "skills/wiki/scripts/okf_mw");
    const restoreEnv = () => {
      for (const [key, value] of Object.entries({ OBSIDIAN_AGENT_CONFIG: previousEnv.config, OBSIDIAN_VAULT_PATH: previousEnv.vault, WIKI_MIDDLEWARE_DIR: previousEnv.middleware })) {
        if (value === undefined) delete process.env[key]; else process.env[key] = value;
      }
      sessionActive = false;
    };
    let loader;
    let session;
    try {
      const scoped = makeScopedExtension({ vaultRoot: vault, sourceRecords, sourceEnabled, wikiEnabled, writable, skills, writeAttempts, accessDenials, providerEvents, onProviderEvent });
      const settingsManager = SettingsManager.inMemory({ retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, compaction: { enabled: false }, cacheWarming: "off", defaultTools: [] });
      const eventBus = createEventBus();
      const extensionRuntime = createExtensionRuntime();
      const loadedExtensions = await loadExtensions([path.join(ROOT, "extensions/obsidian.ts")], sessionCwd, eventBus, extensionRuntime);
      const scopedExtension = await loadExtensionFromFactory(scoped, sessionCwd, eventBus, extensionRuntime, "<knowledge-maintenance-scope>");
      const loadedSkills = loadSkills({ cwd: sessionCwd, agentDir, skillPaths: [...new Set([...skills].map((file) => file.endsWith("SKILL.md") ? path.dirname(file) : path.dirname(path.dirname(file))))], includeDefaults: false });
      loader = {
        getExtensions: () => ({ ...loadedExtensions, extensions: [scopedExtension, ...loadedExtensions.extensions] }),
        getSkills: () => loadedSkills,
        getPrompts: () => ({ prompts: [], diagnostics: [] }), getThemes: () => ({ themes: [], diagnostics: [] }), getAgentsFiles: () => ({ agentsFiles: [] }),
        getSystemPrompt: () => "You are completing a bounded synthetic knowledge-maintenance evaluation. Do not access any other files or tools.",
        getSystemPromptSource: () => undefined, getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
        extendResources: () => fail("dynamic-resource-discovery-disabled"), reload: async () => {},
      };
      if (loadedExtensions.errors.length) fail(`extension-load:${loadedExtensions.errors.map((entry) => entry.error).join(";")}`);
      if (loader.getExtensions().errors.length) fail(`extension-load:${loader.getExtensions().errors.map((entry) => entry.error).join(";")}`);
      const wikiTools = wikiEnabled ? ["wiki_search", "wiki_outline", "wiki_read"] : [];
      const tools = [...(writable ? ["write", "edit"] : []), ...wikiTools, "experiment_source_read", "experiment_skill_read"];
      const sessionModel = { ...model, maxTokens: Math.min(model.maxTokens ?? maxOutputTokens, maxOutputTokens) };
      ({ session } = await createAgentSession({ cwd: vault, agentDir, model: sessionModel, thinkingLevel: effort, modelRuntime, resourceLoader: loader, sessionManager: SessionManager.inMemory(vault), settingsManager, tools }));
    } catch (error) {
      restoreEnv();
      let readonlyUnchanged = false;
      let sourceInputsUnchanged = false;
      try {
        readonlyUnchanged = !writable && JSON.stringify(before) === JSON.stringify(snapshot(vault)) &&
          JSON.stringify(configBefore) === JSON.stringify(snapshot(configPath)) && JSON.stringify(sourceBefore) === JSON.stringify(inputSnapshot());
        sourceInputsUnchanged = JSON.stringify(sourceBefore) === JSON.stringify(inputSnapshot());
      } catch { /* Keep the conservative false values when snapshots cannot be verified. */ }
      sessionLog.push({ phase, sessionId: null, readonlyUnchanged, sourceInputsUnchanged,
        unauthorizedWriteAttempts: [...writeAttempts], accessDenials: [...accessDenials], durationMs: performance.now() - start, failure: "session-init-failed" });
      throw error;
    }
    const abortSession = () => { void session.abort().catch(() => undefined); };
    signal?.addEventListener("abort", abortSession, { once: true });
    const unsub = session.subscribe((event) => {
      if (event.type === "message_end" && event.message.role === "assistant") {
        const record = { phase, model: event.message.model, provider: event.message.provider, usage: event.message.usage ?? null, stopReason: event.message.stopReason };
        providerEvents.push(record); onProviderEvent?.(record);
      }
    });
    let failure;
    try {
      onSession?.({ phase, session });
      await session.bindExtensions({});
      await session.prompt(prompt);
      const answer = session.getLastAssistantText() ?? "";
      return { answer, messages: session.messages, sessionId: session.sessionId, toolNames: session.getActiveToolNames(), cancelled: signal?.aborted ?? false, durationMs: performance.now() - start, providerEvents, unauthorizedWriteAttempts: writeAttempts, accessDenials, failure: null };
    } catch (error) {
      failure = "session-failed";
      throw error;
    } finally {
      unsub();
      signal?.removeEventListener("abort", abortSession);
      session.dispose();
      restoreEnv();
      const after = snapshot(vault);
      const configAfter = snapshot(configPath);
      const sourceAfter = inputSnapshot();
      const readonlyUnchanged = !writable && JSON.stringify(before) === JSON.stringify(after) && JSON.stringify(configBefore) === JSON.stringify(configAfter) && JSON.stringify(sourceBefore) === JSON.stringify(sourceAfter);
      sessionLog.push({ phase, sessionId: session.sessionId, readonlyUnchanged, sourceInputsUnchanged: JSON.stringify(sourceBefore) === JSON.stringify(sourceAfter), unauthorizedWriteAttempts: [...writeAttempts], accessDenials: [...accessDenials], durationMs: performance.now() - start, failure });
      if (!writable && !readonlyUnchanged) fail(`read-only-mutation:${phase}`);
    }
  }

  function visibleSources(checkpoint) {
    return fixture.checkpoints.filter((cp) => cp.id <= checkpoint).flatMap((cp) => cp.sources.map((source) => ({ checkpoint: cp.id, path: `${cp.id}/${source.path}`, originalPath: source.path, text: source.text, sha256: source.sha256 })));
  }

  async function maintain(checkpoint, priorWiki = {}) {
    const cp = fixture.checkpoints.find((entry) => entry.id === checkpoint);
    if (!cp) fail("unknown checkpoint");
    const descriptions = fixture.visibleTasks.map((task) => `- ${task.maintenance}`).join("\n");
    const sources = visibleSources(checkpoint);
    const sourceManifest = sources.map((source) => `${source.path} sha256=${source.sha256}`).join("\n");
    const priorPaths = Array.isArray(priorWiki) ? priorWiki.map((page) => page.path) : Object.keys(priorWiki);
    const prompt = `Chronological checkpoint ${checkpoint}. Maintain the synthetic wiki as the sole writer. Use the supplied source-reading tool for evidence and native wiki tools for existing content. Revise or retract changed facts, preserve sourced dates and qualifications, reuse canonical pages, retain useful dated history, and do not invent unspecified facts. Do not answer evaluation questions. Do not inspect future checkpoints, gold, or answer outputs.\n\nMaintenance descriptions (not questions):\n${descriptions}\n\nAvailable source manifest (content must be read with experiment_source_read):\n${sourceManifest}\n\nThe earlier wiki pages are already present in the selected synthetic vault. Prior wiki memory inventory: ${priorPaths.sort().join(", ") || "empty"}. Read source versions only through experiment_source_read. Read shipped skill/reference files only through experiment_skill_read. Author valid OKF pages with YAML type frontmatter.`;
    const result = await runSession({ phase: `maintenance:${checkpoint}`, sourceRecords: sources, sourceEnabled: true, wikiEnabled: true, prompt, writable: true });
    return { checkpoint, result, wiki: snapshotWiki(vault), durationMs: result.durationMs, unauthorizedWriteAttempts: result.unauthorizedWriteAttempts, accessDenials: result.accessDenials };
  }

  function unsafeWikiEntries(root) {
    const wiki = path.join(root, "wiki");
    const unsafe = [];
    const walk = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const target = path.join(dir, entry.name);
        const stat = fs.lstatSync(target);
        if (stat.isSymbolicLink()) { unsafe.push({ path: target, kind: "symlink" }); continue; }
        if (stat.isDirectory()) walk(target);
        else if (stat.isFile() && stat.nlink !== 1) unsafe.push({ path: target, kind: "hardlink" });
      }
    };
    const stat = fs.lstatSync(wiki);
    if (stat.isSymbolicLink() || !stat.isDirectory()) fail("unsafe-wiki-root");
    walk(wiki);
    return unsafe;
  }

  function assertWikiTreeSafe(root) {
    const unsafe = unsafeWikiEntries(root);
    if (unsafe.length) fail(`unsafe-wiki-entry:${unsafe.map((item) => item.kind).join(",")}`);
  }

  function snapshotWiki(root, accessDenials = []) {
    const wiki = path.join(root, "wiki");
    const pages = [];
    const walk = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const target = path.join(dir, entry.name);
        const stat = fs.lstatSync(target);
        if (stat.isSymbolicLink()) { accessDenials.push("wiki-symlink-skipped"); continue; }
        if (stat.isDirectory()) { walk(target); continue; }
        if (!stat.isFile() || stat.nlink !== 1) { if (stat.isFile()) accessDenials.push("wiki-hardlink-skipped"); continue; }
        if (entry.name.toLowerCase().endsWith(".md") && !["index.md", "log.md"].includes(entry.name.toLowerCase())) {
          const content = fs.readFileSync(target, "utf8");
          pages.push({ path: `wiki/${path.relative(wiki, target).split(path.sep).join("/")}`, content, sha256: sha256(content) });
        }
      }
    };
    walk(wiki);
    return pages.sort((a, b) => compareCodePoints(a.path, b.path));
  }

  function wikiContentHash(pages = snapshotWiki(vault)) {
    return canonicalHash(pages.map(({ path: pagePath, sha256: digest }) => ({ path: pagePath, sha256: digest })));
  }

  async function answer(row, wikiHash) {
    const expected = rows.find((item) => item.id === row?.id);
    if (!expected || Object.keys(row).length !== Object.keys(expected).length || Object.keys(expected).some((key) => row[key] !== expected[key])) fail("answer row is not an exact frozen matrix cell");
    const sources = visibleSources(row.checkpoint);
    const allowSources = row.condition !== "wiki-only";
    const sourceManifest = allowSources ? sources.map((source) => `${source.path} sha256=${source.sha256}`).join("\n") : "none";
    const wikiPages = row.condition === "sources" ? [] : snapshotWiki(vault);
    const actualWikiHash = row.condition === "sources" ? null : wikiContentHash(wikiPages);
    if (row.condition === "sources" ? wikiHash !== null : wikiHash !== actualWikiHash) fail("wiki hash does not match the current frozen checkpoint snapshot");
    const wikiManifest = row.condition === "sources" ? "none" : wikiPages.map((page) => `${page.path} sha256=${page.sha256}`).join("\n");
    const prompt = `Answer only the assigned question. Preserve temporal and stated qualifications; state uncertainty or abstain rather than inventing; cite only evidence received. Do not write files.\n\nDeclared knowledge availability: ${row.condition}. ${allowSources ? "Use only source versions through the active checkpoint via experiment_source_read." : "No source history is available; use only the selected wiki via native wiki tools."} ${row.condition === "sources+wiki" ? "The selected wiki is the frozen checkpoint snapshot." : ""}\n\nAvailable source manifest (content must be read with experiment_source_read):\n${sourceManifest}\n\nAvailable wiki manifest (content must be read with native wiki tools):\n${wikiManifest}\n\nCheckpoint: ${row.checkpoint}\nLanguage: ${row.language}\nQuestion: ${row.question}`;
    const sourceRecords = allowSources ? sources : [];
    const result = await runSession({ phase: `answer:${row.id}`, sourceRecords, sourceEnabled: allowSources, wikiEnabled: row.condition !== "sources", prompt, writable: false });
    return { ...row, status: result.answer.trim() ? "success" : "failed", readOnly: true, attemptCount: 1, answer: result.answer, answerHash: result.answer.trim() ? sha256(result.answer) : null, sourceHistoryHash: allowSources ? canonicalHash(sources.map(({ checkpoint, originalPath, sha256: hash }) => ({ checkpoint, path: originalPath, sha256: hash }))) : null, wikiHash: row.condition === "sources" ? null : wikiHash, model: `${model.provider}/${model.id}`, effort, sessionId: result.sessionId, durationMs: result.durationMs, cancelled: result.cancelled, providerEvents: result.providerEvents, toolNames: result.toolNames, usageRecords: result.providerEvents.filter((event) => Object.hasOwn(event, "usage")).map((event) => event.usage), unauthorizedWriteAttempts: result.unauthorizedWriteAttempts, accessDenials: result.accessDenials, messages: result.messages };
  }

  async function runExperiment({ buildRetrieval }) {
    if (typeof buildRetrieval !== "function") fail("buildRetrieval callback required: BM25 preparation is host-owned and timed separately");
    const checkpoints = [];
    const answers = [];
    for (const cp of fixture.checkpoints) {
      const checkpoint = await maintain(cp.id, checkpoints.at(-1)?.wiki ?? {});
      checkpoints.push(checkpoint);
      assertWikiTreeSafe(vault);
      const prepStart = performance.now();
      await buildRetrieval({ checkpoint: cp.id, vaultRoot: vault });
      checkpoint.retrievalBuildMs = performance.now() - prepStart;
      const wikiHash = wikiContentHash(checkpoint.wiki);
      const frozen = snapshot(vault);
      for (const row of rows.filter((item) => item.checkpoint === cp.id)) answers.push(await answer(row, row.condition === "sources" ? null : wikiHash));
      if (JSON.stringify(frozen) !== JSON.stringify(snapshot(vault))) fail(`frozen-wiki-mutated:${cp.id}`);
    }
    return { schemaVersion: 1, checkpoints, answers, timings: sessionLog, toolScopeViolations: sessionLog.flatMap((item) => item.accessDenials), unauthorizedWriteAttempts: sessionLog.flatMap((item) => item.unauthorizedWriteAttempts), matrixSize: answers.length };
  }

  return { fixture, rows, maintain, answerCell: answer, runExperiment, snapshotWiki: () => snapshotWiki(vault), wikiHash: () => wikiContentHash(), sessionLog, vaultRoot: vault, model: `${model.provider}/${model.id}`, effort, maxOutputTokens };
}
