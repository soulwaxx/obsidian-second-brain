import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { pathToFileURL } from "node:url";

const [source, hook, middleware] = process.argv.slice(2);
if (!source || !hook || !middleware) throw new Error("extension, hook, and middleware paths required");

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-extension-"));
try {
  const vault = path.join(tmp, "vault");
  fs.mkdirSync(path.join(vault, "wiki", "topic"), { recursive: true });
  fs.mkdirSync(path.join(vault, "wiki", ".raw"));
  fs.writeFileSync(path.join(vault, "wiki", "index.md"), "# Test TOC\\n");
  fs.mkdirSync(path.join(tmp, "extensions"), { recursive: true });
  fs.mkdirSync(path.join(tmp, "hooks"), { recursive: true });
  fs.mkdirSync(path.join(tmp, "scripts"), { recursive: true });
  fs.copyFileSync(hook, path.join(tmp, "hooks", "obsidian-session.sh"));
  for (const script of ["config_contract.py", "wiki_lifecycle.py", "bm25-index.py"]) {
    fs.copyFileSync(path.resolve(path.dirname(source), `../scripts/${script}`), path.join(tmp, "scripts", script));
  }
  process.env.OBSIDIAN_AGENT_CONFIG = path.join(tmp, "properties.json");
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: path.join(tmp, "other-vault") }));
  process.env.OBSIDIAN_VAULT_PATH = vault;
  const importLine = 'import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";';
  const text = fs.readFileSync(source, "utf8");
  assert.ok(text.includes(importLine));
  fs.writeFileSync(path.join(tmp, "extensions", "obsidian.ts"), text.replace(importLine,
    'type ExtensionAPI = unknown;'));
  process.env.WIKI_MIDDLEWARE_DIR = path.resolve(middleware);
  const handlers = new Map();
  const { default: extension } = await import(pathToFileURL(path.join(tmp, "extensions", "obsidian.ts")));
  extension({ on: (event, handler) => handlers.set(event, handler) });
  const ctx = { cwd: vault, sessionManager: { getSessionId: () => "pi-writer-session" }, ui: { notify: () => {} } };
  const call = (p, toolName = "write") => handlers.get("tool_call")({ toolName, input: { path: p } }, ctx);
  assert.equal(await call("wiki/topic/attachment.bin"), undefined);
  assert.equal(await call("outside.md"), undefined);
  for (const p of ["wiki/index.md", "wiki/topic/index.md", "wiki/log.md", "wiki/.raw/secret.env"]) {
    assert.equal((await call(p))?.block, true, `expected block: ${p}`);
  }
  fs.symlinkSync(path.join(vault, "wiki", "topic"), path.join(vault, "wiki", "alias"));
  assert.equal((await call("wiki/alias/page.md", "edit"))?.block, true);
  assert.equal((await call("wiki/topic/page.md", "bash")), undefined);
  let injected;
  await handlers.get("session_start")({}, { ...ctx, ui: { notify: () => {} } });
  injected = await handlers.get("before_agent_start")({}, ctx);
  assert.match(injected.message.content, /wiki locator/);
  assert.doesNotMatch(injected.message.content, /# Test TOC/);
  const resumedLocator = await handlers.get("before_agent_start")({}, { ...ctx, sessionManager: { getSessionId: () => "pi-resumed-session" } });
  assert.match(resumedLocator.message.content, /wiki locator/, "new/resumed session identity receives its own locator");
  await handlers.get("session_compact")({}, ctx);
  assert.match((await handlers.get("before_agent_start")({}, ctx)).message.content, /wiki locator/,
    "compaction re-injects the locator after its prior context may have been discarded");
  const locatorRetargetVault = path.join(tmp, "locator-retarget-vault");
  fs.mkdirSync(path.join(locatorRetargetVault, "wiki"), { recursive: true });
  fs.writeFileSync(path.join(locatorRetargetVault, "wiki", "Unique Locator.md"), "---\\ntype: note\\ntitle: Unique Retarget Locator\\n---\\n# Unique Retarget Locator\\n");
  process.env.OBSIDIAN_VAULT_PATH = locatorRetargetVault;
  const retargetedLocator = await handlers.get("before_agent_start")({}, ctx);
  assert.match(retargetedLocator.message.content, /locator-retarget-vault/,
    "same session reloads and injects the locator for a newly selected vault");
  process.env.OBSIDIAN_VAULT_PATH = vault;
  const returnedLocator = await handlers.get("before_agent_start")({}, ctx);
  assert.match(returnedLocator.message.content, /Configured Obsidian wiki: .*\/vault\/wiki/,
    "same session reinjects the current locator when configuration returns from B to previously visited A");
  assert.equal(await handlers.get("before_agent_start")({}, ctx), undefined,
    "same selected session/vault pair remains deduplicated after A-B-A reselection");

  // Broken-config repair is limited to the exact selected regular config file.
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: vault, features: { guard: "bad" }, custom: { preserve: true } }));
  const repairCopy = path.join(tmp, "extensions", "obsidian-repair.ts");
  fs.writeFileSync(repairCopy, text.replace(importLine, "type ExtensionAPI = unknown;"));
  const repairHandlers = new Map();
  const { default: repairExtension } = await import(pathToFileURL(repairCopy));
  repairExtension({ on: (event, handler) => repairHandlers.set(event, handler) });
  const otherCwd = path.join(tmp, "outside");
  fs.mkdirSync(otherCwd);
  const outsideDiagnostic = await repairHandlers.get("before_agent_start")({}, { cwd: otherCwd });
  assert.match(outsideDiagnostic.message.content, /integration is fail-closed/);
  assert.equal(await repairHandlers.get("tool_call")({ toolName: "write", input: { path: path.join(otherCwd, "ordinary.ts") } }, { cwd: otherCwd }), undefined,
    "invalid vault configuration must not block unrelated repository writes");
  // One diagnostic is sufficient across cwd changes; repeat prompts are suppressed.
  assert.equal(await repairHandlers.get("before_agent_start")({}, ctx), undefined,
    "unchanged config diagnostics must not renew an already proposed repair");
  const changedInvalidConfig = JSON.parse(fs.readFileSync(process.env.OBSIDIAN_AGENT_CONFIG, "utf8"));
  changedInvalidConfig.custom.repairRevision = 2;
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify(changedInvalidConfig));
  const changedRepairDiagnostic = await repairHandlers.get("before_agent_start")({}, ctx);
  assert.match(changedRepairDiagnostic.message.content, /exact minimal changes/,
    "a changed invalid config must present a new exact repair proposal");
  const alternateVault = path.join(tmp, "alternate-vault");
  fs.mkdirSync(alternateVault);
  process.env.OBSIDIAN_VAULT_PATH = alternateVault;
  const changedVaultDiagnostic = await repairHandlers.get("before_agent_start")({}, { cwd: alternateVault });
  assert.match(changedVaultDiagnostic.message.content, /exact minimal changes/,
    "a changed configured vault must present a new repair proposal");
  process.env.OBSIDIAN_VAULT_PATH = vault;
  assert.match((await repairHandlers.get("before_agent_start")({}, ctx)).message.content, /exact minimal changes/,
    "returning to the original vault must not reuse another vault's proposal");
  assert.equal((await repairHandlers.get("tool_call")({ toolName: "write", input: { path: "wiki/topic/page.md" } }, ctx))?.block, true,
    "wiki writes remain blocked while config is invalid");
  assert.equal(await repairHandlers.get("tool_call")({ toolName: "write", input: { path: path.join(tmp, "other-settings.json") } }, ctx), undefined,
    "unrelated files remain writable while the selected vault config is invalid");
  assert.equal(await repairHandlers.get("tool_call")({ toolName: "write", input: { path: process.env.OBSIDIAN_AGENT_CONFIG } }, ctx), undefined,
    "the selected fixture config can be repaired");
  const configAlias = path.join(tmp, "config-alias.json");
  fs.symlinkSync(process.env.OBSIDIAN_AGENT_CONFIG, configAlias);
  assert.equal(await repairHandlers.get("tool_call")({ toolName: "write", input: { path: configAlias } }, ctx), undefined,
    "unrelated settings remain writable; config repair authority is not granted through aliases");
  const selectedConfig = JSON.parse(fs.readFileSync(process.env.OBSIDIAN_AGENT_CONFIG, "utf8"));
  selectedConfig.features.guard = true;
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify(selectedConfig));
  assert.deepEqual(JSON.parse(fs.readFileSync(process.env.OBSIDIAN_AGENT_CONFIG, "utf8")).custom, { preserve: true, repairRevision: 2 });
  const revalidatedPage = path.join(vault, "wiki/topic/revalidated.md");
  assert.equal(await repairHandlers.get("tool_call")({ toolCallId: "revalidated-write", toolName: "write", input: { path: revalidatedPage } }, ctx), undefined,
    "the same loaded session accepts page writes after config revalidation");
  fs.writeFileSync(revalidatedPage, "---\ntype: note\ntitle: Revalidated\n---\n# Revalidated\n");
  await repairHandlers.get("tool_result")({ toolCallId: "revalidated-write", toolName: "write", input: { path: revalidatedPage }, isError: false }, ctx);
  await repairHandlers.get("turn_end")({}, ctx);

  // Invalid configs must not crash loading or silently disable the integration.
  delete process.env.OBSIDIAN_VAULT_PATH;
  for (const config of ["{invalid", "null", "[]", JSON.stringify({ vaultPath: 42 })]) {
    fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, config);
    const copy = path.join(tmp, "extensions", `obsidian-${Math.random()}.ts`);
    fs.writeFileSync(copy, text.replace(importLine, "type ExtensionAPI = unknown;"));
    const { default: invalidExtension } = await import(pathToFileURL(copy));
    const invalidHandlers = new Map();
    invalidExtension({ on: (event, handler) => invalidHandlers.set(event, handler) });
    const warnings = [];
    await invalidHandlers.get("session_start")({}, { ...ctx, ui: { notify: (message) => warnings.push(message) } });
    assert.match(warnings.join(" "), /(?:cannot load|invalid) .*properties\.json/);
    const diagnostic = await invalidHandlers.get("before_agent_start")({}, ctx);
    assert.equal(diagnostic, undefined, "do not direct outside/ambiguous work to repair another vault config");
  }
  process.env.OBSIDIAN_VAULT_PATH = vault;

  // Same-session config changes are reloaded before guarded operations; an empty
  // invocation override is consistently treated as unset.
  process.env.OBSIDIAN_VAULT_PATH = "";
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: vault, features: { guard: false } }));
  const reloadCopy = path.join(tmp, "extensions", "obsidian-reload.ts");
  fs.writeFileSync(reloadCopy, text.replace(importLine, "type ExtensionAPI = unknown;"));
  const reloadHandlers = new Map();
  const { default: reloadExtension } = await import(pathToFileURL(reloadCopy));
  reloadExtension({ on: (event, handler) => reloadHandlers.set(event, handler) });
  assert.equal(await reloadHandlers.get("tool_call")({ toolName: "write", input: { path: "wiki/index.md" } }, ctx), undefined);
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: vault, features: { guard: true } }));
  assert.equal((await reloadHandlers.get("tool_call")({ toolName: "write", input: { path: "wiki/index.md" } }, ctx))?.block, true);

  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: vault, features: { guard: false, autoCommit: false } }));
  const featureCopy = path.join(tmp, "extensions", "obsidian-features.ts");
  fs.writeFileSync(featureCopy, text.replace(importLine, "type ExtensionAPI = unknown;"));
  const featureHandlers = new Map();
  const { default: featureExtension } = await import(pathToFileURL(featureCopy));
  featureExtension({ on: (event, handler) => featureHandlers.set(event, handler) });
  assert.equal(await featureHandlers.get("tool_call")({ toolName: "write", input: { path: "wiki/index.md" } }, ctx), undefined);
  const simulateWrite = async (event, context) => {
    const result = await featureHandlers.get("tool_call")(event, context);
    if (!result?.block && typeof event.input.path === "string") {
      const target = path.resolve(context.cwd, event.input.path);
      if (fs.existsSync(target)) fs.appendFileSync(target, "\n");
    }
    return result;
  };
  const notifications = [];
  fs.unlinkSync(path.join(vault, "wiki/index.md")); // Replace the unmarked TOC fixture with middleware-owned output.
  const disabledPage = path.join(vault, "wiki/topic/disabled.md");
  fs.writeFileSync(disabledPage, "---\ntype: note\ntitle: Disabled Page\nlast_updated: 2026-01-09\n---\n# Disabled Page\n");
  await simulateWrite({ toolCallId: "write-1", toolName: "write", input: { path: disabledPage } }, ctx);
  let siblingReadFinished = false;
  let readDelayFiredResolve;
  const readDelayFired = new Promise((resolve) => { readDelayFiredResolve = resolve; });
  const originalSetTimeout = globalThis.setTimeout;
  let observedReadDelay = false;
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay === 0 && !observedReadDelay) {
      observedReadDelay = true;
      return originalSetTimeout((...timerArgs) => {
        callback(...timerArgs);
        readDelayFiredResolve();
      }, delay, ...args);
    }
    return originalSetTimeout(callback, delay, ...args);
  };
  let siblingReadResult;
  const siblingRead = featureHandlers.get("tool_call")(
    { toolCallId: "read-1", toolName: "read", input: { path: "wiki/topic/index.md" } }, ctx,
  ).then((result) => { siblingReadFinished = true; siblingReadResult = result; });
  await readDelayFired;
  globalThis.setTimeout = originalSetTimeout;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(siblingReadFinished, true, "in-flight wiki read must return a bounded block instead of waiting forever for a missing callback");
  assert.equal(siblingReadResult?.block, true, "sibling navigation must be blocked while the writer tool is still active");
  await featureHandlers.get("tool_result")({ toolCallId: "write-1", toolName: "write", input: { path: disabledPage }, isError: false }, ctx);
  await siblingRead;
  assert.equal(await featureHandlers.get("tool_call")({ toolName: "read", input: { path: "wiki/topic/index.md" } }, ctx), undefined,
    "navigation becomes available after the writer result is settled and synchronized");
  const canonicalVaultForAlias = fs.realpathSync(vault);
  if (process.platform === "darwin" && canonicalVaultForAlias.startsWith("/private/var/")) {
    const aliasVault = canonicalVaultForAlias.replace(/^\/private\/var\//, "/var/");
    assert.equal(fs.realpathSync(aliasVault), canonicalVaultForAlias);
    const aliasPage = path.join(canonicalVaultForAlias, "wiki/topic/alias/nested/page.md");
    fs.mkdirSync(path.dirname(aliasPage), { recursive: true });
    fs.writeFileSync(aliasPage, "---\ntype: note\ntitle: Alias Page\nlast_updated: 2026-01-09\n---\n# Alias Page\n");
    const aliasCtx = { ...ctx, cwd: aliasVault };
    await simulateWrite({ toolCallId: "alias-write", toolName: "write", input: { path: "wiki/topic/alias/nested/page.md" } }, aliasCtx);
    let aliasReadFinished = false;
    let aliasDelayFiredResolve;
    const aliasDelayFired = new Promise((resolve) => { aliasDelayFiredResolve = resolve; });
    const aliasSetTimeout = globalThis.setTimeout;
    let sawAliasDelay = false;
    globalThis.setTimeout = (callback, delay, ...args) => {
      if (delay === 0 && !sawAliasDelay) {
        sawAliasDelay = true;
        return aliasSetTimeout((...timerArgs) => {
          callback(...timerArgs);
          aliasDelayFiredResolve();
        }, delay, ...args);
      }
      return aliasSetTimeout(callback, delay, ...args);
    };
    let aliasReadResult;
    const aliasRead = featureHandlers.get("tool_call")(
      { toolCallId: "alias-read", toolName: "read", input: { path: "wiki/topic/index.md" } }, aliasCtx,
    ).then((result) => { aliasReadFinished = true; aliasReadResult = result; });
    await aliasDelayFired;
    globalThis.setTimeout = aliasSetTimeout;
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(aliasReadFinished, true, "canonical alias navigation returns a bounded block while the writer tool remains active");
    assert.equal(aliasReadResult?.block, true);
    await featureHandlers.get("tool_result")({ toolCallId: "alias-write", toolName: "write", input: { path: "wiki/topic/alias/nested/page.md" }, isError: false }, aliasCtx);
    await aliasRead;
    assert.equal(await featureHandlers.get("tool_call")({ toolName: "read", input: { path: "wiki/topic/index.md" } }, aliasCtx), undefined,
      "canonical alias navigation resumes after the writer has settled");
    assert.match(fs.readFileSync(path.join(canonicalVaultForAlias, "wiki/topic/alias/nested/index.md"), "utf8"), /\[Alias Page\]/);
  }
  // A later tool/read in the same assistant turn must observe fresh navigation.
  assert.match(fs.readFileSync(path.join(vault, "wiki/topic/index.md"), "utf8"), /\[Disabled Page\]\(\.\/disabled.md\)/);
  await featureHandlers.get("turn_end")({}, { ...ctx, ui: { notify: (message) => notifications.push(message) } });
  assert.deepEqual(notifications, []);
  assert.match(fs.readFileSync(path.join(vault, "wiki/topic/index.md"), "utf8"), /\[Disabled Page\]\(\.\/disabled.md\)/);

  // A failed post-write validation must leave ordinary pages available for repair,
  // while generated navigation stays hidden until a successful synchronization.
  const repairPage = path.join(vault, "wiki/topic/repair-me.md");
  const repairWarnings = [];
  const repairCtx = { ...ctx, ui: { notify: (message) => repairWarnings.push(message) } };
  fs.writeFileSync(repairPage, "# Missing required frontmatter\n");
  await simulateWrite({ toolCallId: "repair-failed-write", toolName: "write", input: { path: repairPage } }, repairCtx);
  const failedWriteResult = await featureHandlers.get("tool_result")({ toolCallId: "repair-failed-write", toolName: "write", input: { path: repairPage }, isError: false, content: [{ type: "text", text: "write succeeded" }] }, repairCtx);
  assert.match(repairWarnings.join(" "), /post-write validation\/sync failed/);
  assert.match(failedWriteResult.content.map((part) => part.text ?? "").join(" "), /bytes were retained and were not successfully finalized/,
    "validation failure must be visible in the tool result presented to the model");
  const settleDiagnostic = await featureHandlers.get("agent_before_settle")({ entries: [], continue: false }, repairCtx);
  assert.equal(settleDiagnostic.continue, true, "the final actionable boundary allows at most one repair turn");
  assert.match(settleDiagnostic.entries[0].content, /Inspect\/repair the retained page/);
  const repeatedSettleDiagnostic = await featureHandlers.get("agent_before_settle")({ entries: [], continue: false }, repairCtx);
  assert.equal(repeatedSettleDiagnostic.continue, undefined, "persistent failures cannot cause unbounded continuation");
  const topicIndex = path.join(vault, "wiki/topic/index.md");
  const validPage = path.join(vault, "wiki/topic/valid-after-failure.md");
  fs.writeFileSync(validPage, "---\ntype: note\ntitle: Valid After Failure\nlast_updated: 2026-01-09\n---\n# Valid After Failure\n");
  await simulateWrite({ toolCallId: "valid-after-failure", toolName: "write", input: { path: validPage } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "valid-after-failure", toolName: "write", input: { path: validPage }, isError: false }, repairCtx);
  assert.equal(fs.readFileSync(topicIndex, "utf8").includes("valid-after-failure.md"), false,
    "valid page must not refresh navigation while a prior changed page remains invalid");
  assert.equal((await featureHandlers.get("tool_call")({ toolName: "read", input: { path: topicIndex } }, repairCtx))?.block, true,
    "valid page must not clear another changed page's validation barrier");
  assert.equal((await featureHandlers.get("tool_call")({ toolName: "read", input: { path: fs.realpathSync(repairPage) } }, repairCtx))?.block, undefined,
    "ordinary failed page must remain readable for diagnosis");
  assert.equal((await featureHandlers.get("tool_call")({ toolCallId: "repair-edit-probe", toolName: "edit", input: { path: path.relative(vault, repairPage) } }, repairCtx))?.block, undefined,
    "failed page must remain editable for repair");
  await featureHandlers.get("tool_result")({ toolCallId: "repair-edit-probe", toolName: "edit", input: { path: path.relative(vault, repairPage) }, isError: false }, repairCtx);
  assert.equal((await featureHandlers.get("tool_call")({ toolCallId: "repair-write-probe", toolName: "write", input: { path: path.relative(vault, repairPage) } }, repairCtx))?.block, undefined,
    "failed page must remain writable for repair");
  await featureHandlers.get("tool_result")({ toolCallId: "repair-write-probe", toolName: "write", input: { path: path.relative(vault, repairPage) }, isError: false }, repairCtx);
  const blockedNavigation = await simulateWrite({ toolName: "read", input: { path: fs.realpathSync(topicIndex) } }, repairCtx);
  assert.equal(blockedNavigation?.block, true,
    "generated navigation must remain blocked until recovery");
  fs.writeFileSync(repairPage, "---\ntype: note\ntitle: Repaired Page\nlast_updated: 2026-01-09\n---\n# Repaired Page\n");
  await simulateWrite({ toolCallId: "repair-write", toolName: "write", input: { path: path.relative(vault, repairPage) } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "repair-write", toolName: "write", input: { path: path.relative(vault, repairPage) }, isError: false }, repairCtx);
  assert.equal(await featureHandlers.get("tool_call")({ toolName: "read", input: { path: fs.realpathSync(topicIndex) } }, repairCtx), undefined,
    "generated navigation is readable again after recovery");
  assert.match(fs.readFileSync(topicIndex, "utf8"), /\[Repaired Page\]\(\.\/repair-me.md\)/,
    "navigation read finalizes the successful repair batch");
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(vault, ".vault-meta/lifecycle/state.json"), "utf8")).pending, {},
    "successful navigation-read finalization empties durable lifecycle pending state");
  assert.equal(await featureHandlers.get("agent_before_settle")({ entries: [], continue: false }, repairCtx), undefined,
    "successful navigation-read finalization clears this owner's obsolete model diagnostic and continuation budget");

  // Shutdown must not publish a valid later page while an earlier ordinary
  // page remains invalid, even after its immediate postwrite failure.
  const shutdownInvalid = path.join(vault, "wiki/topic/shutdown-invalid.md");
  const shutdownLater = path.join(vault, "wiki/topic/shutdown-later.md");
  fs.writeFileSync(shutdownInvalid, "# Missing frontmatter\n");
  await simulateWrite({ toolCallId: "shutdown-invalid", toolName: "write", input: { path: shutdownInvalid } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "shutdown-invalid", toolName: "write", input: { path: shutdownInvalid }, isError: false }, repairCtx);
  fs.writeFileSync(shutdownLater, "---\ntype: note\ntitle: Shutdown Later\n---\n# Shutdown Later\n");
  await simulateWrite({ toolCallId: "shutdown-later", toolName: "write", input: { path: shutdownLater } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "shutdown-later", toolName: "write", input: { path: shutdownLater }, isError: false }, repairCtx);
  assert.equal(fs.readFileSync(topicIndex, "utf8").includes("shutdown-later.md"), false,
    "a valid later write must not refresh navigation while an ordinary page remains invalid");
  const indexBeforeShutdown = fs.readFileSync(topicIndex, "utf8");
  await featureHandlers.get("session_shutdown")({}, repairCtx);
  assert.equal(fs.readFileSync(topicIndex, "utf8"), indexBeforeShutdown,
    "shutdown must preserve generated indexes byte-for-byte while validation is unresolved");
  assert.equal(fs.readFileSync(topicIndex, "utf8").includes("shutdown-later.md"), false,
    "shutdown must preserve the navigation barrier for unresolved invalid pages");
  fs.writeFileSync(shutdownInvalid, "---\ntype: note\ntitle: Shutdown Repaired\n---\n# Shutdown Repaired\n");
  await simulateWrite({ toolCallId: "shutdown-repair", toolName: "write", input: { path: shutdownInvalid } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "shutdown-repair", toolName: "write", input: { path: shutdownInvalid }, isError: false }, repairCtx);
  await featureHandlers.get("session_shutdown")({}, repairCtx);
  assert.match(fs.readFileSync(topicIndex, "utf8"), /\[Shutdown Later\]/,
    "repair must recover the navigation refresh");
  const deletedInvalid = path.join(vault, "wiki/topic/deleted-invalid.md");
  fs.writeFileSync(deletedInvalid, "# Invalid then deleted\n");
  await simulateWrite({ toolCallId: "deleted-invalid", toolName: "write", input: { path: deletedInvalid } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "deleted-invalid", toolName: "write", input: { path: deletedInvalid }, isError: false }, repairCtx);
  fs.unlinkSync(deletedInvalid);
  await featureHandlers.get("session_shutdown")({}, repairCtx);
  assert.equal(fs.readFileSync(topicIndex, "utf8").includes("deleted-invalid.md"), false,
    "deleting an unresolved invalid page must allow safe shutdown recovery");

  const retargetVault = path.join(tmp, "retarget-vault");
  fs.mkdirSync(path.join(retargetVault, "wiki"), { recursive: true });
  const retargetPage = path.join(vault, "wiki/topic/retarget.md");
  const retargetWarnings = [];
  const retargetCtx = { ...ctx, ui: { notify: (message) => retargetWarnings.push(message) } };
  await featureHandlers.get("tool_call")({ toolCallId: "retarget-write", toolName: "write", input: { path: retargetPage } }, retargetCtx);
  fs.writeFileSync(retargetPage, "---\ntype: note\ntitle: Retarget\n---\n# Retarget\n");
  process.env.OBSIDIAN_VAULT_PATH = retargetVault;
  await featureHandlers.get("tool_result")({ toolCallId: "retarget-write", toolName: "write", input: { path: retargetPage }, isError: false }, retargetCtx);
  process.env.OBSIDIAN_VAULT_PATH = vault;
  const beforeRetargetFinalize = JSON.parse(fs.readFileSync(path.join(vault, ".vault-meta/lifecycle/state.json"), "utf8"));
  assert.ok(Object.keys(beforeRetargetFinalize.pending).some((key) => key.endsWith("/retarget.md")),
    `captured retarget write missing from original vault state: ${JSON.stringify(beforeRetargetFinalize)}`);
  assert.deepEqual(retargetWarnings, [], "completed retargeted results are recorded against their original vault owner");
  await featureHandlers.get("turn_end")({}, retargetCtx);
  const stateAfterRetarget = JSON.parse(fs.readFileSync(path.join(vault, ".vault-meta/lifecycle/state.json"), "utf8"));
  assert.match(fs.readFileSync(path.join(vault, "wiki/topic", "index.md"), "utf8"), /\[Retarget\]/,
    `reselecting the captured vault reconciles a write that missed tool_result tracking: ${retargetWarnings.join(" | ")}; ${JSON.stringify(stateAfterRetarget)}`);
  assert.match(fs.readFileSync(path.join(vault, "wiki", "log.md"), "utf8"), /\*\*Creation\*\*.*retarget\.md/s);
  assert.equal(fs.existsSync(path.join(retargetVault, "wiki", "index.md")), false,
    "a changed config boundary must not redirect pending page work");
  assert.deepEqual(stateAfterRetarget.pending, {},
    "the original vault reconciles and clears the captured write after it is reselected");

  // An approved config edit must reselect before the very next tool is classified.
  const savedConfig = fs.readFileSync(process.env.OBSIDIAN_AGENT_CONFIG, "utf8");
  delete process.env.OBSIDIAN_VAULT_PATH;
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: vault, features: { retrievalRefresh: false } }));
  const reselectHandlers = new Map();
  featureExtension({ on: (event, handler) => reselectHandlers.set(event, handler) });
  const reselectWarnings = [];
  const reselectCtx = { ...ctx, cwd: otherCwd, ui: { notify: (message) => reselectWarnings.push(message) } };
  await reselectHandlers.get("session_start")({}, reselectCtx);
  const configEdit = { toolCallId: "approved-config-retarget", toolName: "edit", input: { path: process.env.OBSIDIAN_AGENT_CONFIG } };
  assert.equal(await reselectHandlers.get("tool_call")(configEdit, reselectCtx), undefined);
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: retargetVault, features: { retrievalRefresh: false } }));
  await reselectHandlers.get("tool_result")({ ...configEdit, isError: false }, reselectCtx);
  const newlySelectedPage = path.join(retargetVault, "wiki/new-selection.md");
  const nextWrite = { toolCallId: "new-selection-write", toolName: "write", input: { path: newlySelectedPage } };
  assert.equal(await reselectHandlers.get("tool_call")(nextWrite, reselectCtx), undefined);
  const selectedStatePath = path.join(retargetVault, ".vault-meta/lifecycle/state.json");
  assert.ok(fs.existsSync(selectedStatePath), "newly selected vault must capture the next write before it executes");
  const selectedKey = fs.realpathSync(path.dirname(newlySelectedPage)) + path.sep + path.basename(newlySelectedPage);
  const selectedCapture = JSON.parse(fs.readFileSync(selectedStatePath, "utf8")).pending[selectedKey];
  assert.equal(selectedCapture.existed, false);
  assert.equal(selectedCapture.captures["pi:pi-writer-session"]["new-selection-write"], "active");
  const oldVaultLog = fs.readFileSync(path.join(vault, "wiki/log.md"), "utf8");
  fs.writeFileSync(newlySelectedPage, "---\ntype: note\ntitle: New Selection\n---\n# New Selection\n");
  await reselectHandlers.get("tool_result")({ ...nextWrite, isError: false }, reselectCtx);
  await reselectHandlers.get("turn_end")({}, reselectCtx);
  assert.match(fs.readFileSync(path.join(retargetVault, "wiki/index.md"), "utf8"), /New Selection/);
  assert.match(fs.readFileSync(path.join(retargetVault, "wiki/log.md"), "utf8"), /\*\*Creation\*\*.*new-selection\.md/s);
  assert.deepEqual(JSON.parse(fs.readFileSync(selectedStatePath, "utf8")).pending, {});
  assert.deepEqual(reselectWarnings, [], "new selection must not lose its prewrite capture");
  assert.equal(fs.readFileSync(path.join(vault, "wiki/log.md"), "utf8"), oldVaultLog);
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, savedConfig);
  process.env.OBSIDIAN_VAULT_PATH = vault;

  const readerHandlers = new Map();
  featureExtension({ on: (event, handler) => readerHandlers.set(event, handler) });
  const readerNotifications = [];
  const readerCtx = { ...ctx, sessionManager: { getSessionId: () => "pi-reader-session" }, ui: { notify: (message) => readerNotifications.push(message) } };
  const lifecycleStatePath = path.join(vault, ".vault-meta/lifecycle/state.json");
  const sharedIndex = path.join(vault, "wiki/topic/index.md");
  const sharedLog = path.join(vault, "wiki/log.md");

  // Two-session pause before the file write: reader shutdown and navigation may
  // neither consume the active preimage nor publish another session's capture.
  const beforeWrite = path.join(vault, "wiki/topic/paused-before.md");
  fs.writeFileSync(beforeWrite, "---\ntype: note\ntitle: Before Pause\n---\n# Before Pause\n");
  const beforeIndex = fs.readFileSync(sharedIndex, "utf8");
  const beforeLog = fs.readFileSync(sharedLog, "utf8");
  await featureHandlers.get("tool_call")({ toolCallId: "paused-before", toolName: "write", input: { path: beforeWrite } }, ctx);
  const stateDuringPause = JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8"));
  const beforeWriteKey = fs.realpathSync(beforeWrite);
  const capturedBefore = stateDuringPause.pending[beforeWriteKey];
  assert.equal(capturedBefore.existed, true);
  const capturedDigest = capturedBefore.before;
  await readerHandlers.get("session_shutdown")({}, readerCtx);
  assert.equal((await readerHandlers.get("tool_call")({ toolName: "read", input: { path: sharedIndex } }, readerCtx))?.block, true,
    "reader navigation must defer while another session owns an active prewrite capture");
  assert.deepEqual(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")), stateDuringPause,
    "reader shutdown/navigation must leave the live capture and immutable preimage unchanged");
  assert.equal(fs.readFileSync(sharedIndex, "utf8"), beforeIndex);
  assert.equal(fs.readFileSync(sharedLog, "utf8"), beforeLog);
  fs.writeFileSync(beforeWrite, "---\ntype: note\ntitle: Before Pause Revised\n---\n# Before Pause Revised\n");
  await featureHandlers.get("tool_result")({ toolCallId: "paused-before", toolName: "write", input: { path: beforeWrite }, isError: false }, ctx);
  assert.equal(fs.readFileSync(sharedIndex, "utf8").includes("paused-before.md"), false,
    "postwrite settlement alone does not publish navigation before writer finalization");
  assert.equal(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending[beforeWriteKey].before, capturedDigest);
  await featureHandlers.get("turn_end")({}, ctx);
  assert.match(fs.readFileSync(sharedLog, "utf8"), /\*\*Update\*\*.*paused-before\.md/s,
    "prewrite existence must classify the settled write as Update");
  assert.doesNotMatch(fs.readFileSync(sharedLog, "utf8"), /\*\*Creation\*\*.*paused-before\.md/s);

  // Two-session pause after a partial write: readers still cannot reconcile or
  // publish bytes until the owning writer returns a settled valid result.
  const midWrite = path.join(vault, "wiki/topic/paused-mid-write.md");
  fs.writeFileSync(midWrite, "---\ntype: note\ntitle: Mid Write\n---\n# Mid Write\n");
  const midIndex = fs.readFileSync(sharedIndex, "utf8");
  const midLog = fs.readFileSync(sharedLog, "utf8");
  await featureHandlers.get("tool_call")({ toolCallId: "paused-mid", toolName: "write", input: { path: midWrite } }, ctx);
  const midState = JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8"));
  const midWriteKey = fs.realpathSync(midWrite);
  const midDigest = midState.pending[midWriteKey].before;
  fs.writeFileSync(midWrite, "---\ntype: note\ntitle: Half Written\n");
  await readerHandlers.get("session_shutdown")({}, readerCtx);
  assert.equal((await readerHandlers.get("tool_call")({ toolName: "read", input: { path: sharedIndex } }, readerCtx))?.block, true);
  assert.deepEqual(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")), midState,
    "reader recovery must not reconcile partly written bytes or replace the preimage");
  assert.equal(fs.readFileSync(sharedIndex, "utf8"), midIndex);
  assert.equal(fs.readFileSync(sharedLog, "utf8"), midLog);
  fs.writeFileSync(midWrite, "---\ntype: note\ntitle: Mid Write Revised\n---\n# Mid Write Revised\n");
  await featureHandlers.get("tool_result")({ toolCallId: "paused-mid", toolName: "write", input: { path: midWrite }, isError: false }, ctx);
  assert.equal(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending[midWriteKey].before, midDigest);
  assert.equal(fs.readFileSync(sharedIndex, "utf8").includes("paused-mid-write.md"), false);
  await featureHandlers.get("turn_end")({}, ctx);
  assert.match(fs.readFileSync(sharedLog, "utf8"), /\*\*Update\*\*.*paused-mid-write\.md/s);
  assert.doesNotMatch(fs.readFileSync(sharedLog, "utf8"), /\*\*Creation\*\*.*paused-mid-write\.md/s);

  // A real lost callback is recovered only at the writer's own settled shutdown.
  const missingCallback = path.join(vault, "wiki/topic/missing-callback.md");
  fs.writeFileSync(missingCallback, "---\ntype: note\ntitle: Existing Callback\n---\n# Existing Callback\n");
  await featureHandlers.get("tool_call")({ toolCallId: "missing-callback", toolName: "write", input: { path: missingCallback } }, ctx);
  const missingCallbackKey = fs.realpathSync(missingCallback);
  const missingBefore = JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending[missingCallbackKey].before;
  fs.writeFileSync(missingCallback, "---\ntype: note\ntitle: Recovered Callback\n---\n# Recovered Callback\n");
  const beforeRecoveryIndex = fs.readFileSync(sharedIndex, "utf8");
  await featureHandlers.get("session_shutdown")({}, ctx);
  assert.equal(fs.readFileSync(sharedIndex, "utf8").includes("missing-callback.md"), true);
  assert.equal(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending[missingCallbackKey], undefined);
  assert.match(fs.readFileSync(sharedLog, "utf8"), /\*\*Update\*\*.*missing-callback\.md/s);
  assert.ok(missingBefore && fs.readFileSync(sharedIndex, "utf8") !== beforeRecoveryIndex);

  // Result callbacks use the captured owner/path even after context replacement.
  const replacementPage = path.join(vault, "wiki/topic/replaced-context.md");
  await featureHandlers.get("tool_call")({ toolCallId: "replaced-context", toolName: "write", input: { path: replacementPage } }, ctx);
  const replacementKey = fs.realpathSync(path.dirname(replacementPage)) + path.sep + path.basename(replacementPage);
  assert.equal(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending[replacementKey].captures["pi:pi-writer-session"]["replaced-context"], "active");
  fs.writeFileSync(replacementPage, "---\ntype: note\ntitle: Replaced Context\n---\n# Replaced Context\n");
  await featureHandlers.get("tool_result")({ toolCallId: "replaced-context", toolName: "write", input: { path: replacementPage }, isError: false }, readerCtx);
  await featureHandlers.get("turn_end")({}, ctx);
  assert.equal(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending[replacementKey], undefined);
  assert.match(fs.readFileSync(sharedLog, "utf8"), /replaced-context\.md/);

  const bounded = async (work, label) => {
    let timer;
    try {
      return await Promise.race([
        work,
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out`)), 20000); }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  };
  const prepare = (event) => bounded(featureHandlers.get("tool_call")(event, ctx), `prepare ${event.toolCallId}`);
  const pageContent = (title) => `---\ntype: note\ntitle: ${title}\n---\n# ${title}\n`;
  const featureConfig = fs.readFileSync(process.env.OBSIDIAN_AGENT_CONFIG, "utf8");

  // Pi prepares parallel batches sequentially, before any execution/result.
  // Exercise both guarded writes and the guard-disabled capture path.
  for (const guard of [true, false]) {
    fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: vault, features: { guard, retrievalRefresh: false } }));
    const batch = ["edit", "write"].map((toolName, i) => ({
      toolCallId: `parallel-${guard}-${i}`, toolName,
      input: { path: path.join(vault, `wiki/topic/parallel-${guard}-${i}.md`) },
    }));
    for (const event of batch) fs.writeFileSync(event.input.path, pageContent("Before Parallel"));
    for (const event of batch) assert.equal(await prepare(event), undefined);
    const prepared = JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8"));
    for (const event of batch) {
      const entry = prepared.pending[fs.realpathSync(event.input.path)];
      assert.equal(entry.existed, true);
      assert.equal(entry.captures["pi:pi-writer-session"][event.toolCallId], "active");
      assert.equal(fs.readFileSync(event.input.path, "utf8"), pageContent("Before Parallel"));
    }
    assert.equal((await prepare({ toolCallId: "parallel-nav", toolName: "read", input: { path: sharedIndex } }))?.block, true);
    const indexBeforeResults = fs.readFileSync(sharedIndex, "utf8");
    for (const event of batch) fs.writeFileSync(event.input.path, pageContent(event.toolCallId));
    await bounded(Promise.all(batch.map((event) => featureHandlers.get("tool_result")({ ...event, isError: false }, ctx))), "parallel results");
    const recorded = JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8"));
    for (const event of batch) {
      assert.equal(recorded.pending[fs.realpathSync(event.input.path)].captures["pi:pi-writer-session"][event.toolCallId], "settled");
    }
    assert.equal(fs.readFileSync(sharedIndex, "utf8"), indexBeforeResults, "results do not publish before finalization");
    await featureHandlers.get("turn_end")({}, ctx);
    assert.deepEqual(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending, {});
    for (const event of batch) {
      assert.ok(fs.readFileSync(sharedIndex, "utf8").includes(path.basename(event.input.path)));
      assert.ok(fs.readFileSync(sharedLog, "utf8").includes(path.basename(event.input.path)));
    }
    assert.equal(await prepare({ toolCallId: "parallel-nav-settled", toolName: "read", input: { path: sharedIndex } }), undefined);
  }

  // Aborts may omit tool_result (and an exceptional exit may omit turn_end).
  // Recover only after agent_end, including the original vault after retargeting.
  for (const emitTurnEnd of [true, false]) {
    const earlier = { toolCallId: "completed-before-abort", toolName: "write", input: { path: path.join(vault, "wiki/topic/completed-before-abort.md") } };
    if (!emitTurnEnd) {
      assert.equal(await prepare(earlier), undefined);
      fs.writeFileSync(earlier.input.path, pageContent("Completed Before Abort"));
      await featureHandlers.get("tool_result")({ ...earlier, isError: false }, ctx);
      assert.ok(!fs.readFileSync(sharedIndex, "utf8").includes(path.basename(earlier.input.path)));
    }
    const aborted = [0, 1].map((i) => ({
      toolCallId: `aborted-${emitTurnEnd}-${i}`, toolName: "edit",
      input: { path: path.join(vault, `wiki/topic/aborted-${emitTurnEnd}-${i}.md`) },
    }));
    for (const event of aborted) {
      fs.writeFileSync(event.input.path, pageContent("Before Abort"));
      assert.equal(await prepare(event), undefined);
    }
    const pausedState = JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8"));
    await featureHandlers.get("agent_end")({}, readerCtx);
    assert.deepEqual(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")), pausedState,
      "another session's agent_end must not recover live writer captures");
    if (!emitTurnEnd) fs.writeFileSync(aborted[1].input.path, pageContent("Written Without Callback"));
    const indexBeforeAbort = fs.readFileSync(sharedIndex, "utf8");
    if (emitTurnEnd) {
      await featureHandlers.get("turn_end")({}, ctx);
      assert.deepEqual(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")), pausedState,
        "turn_end must not recover captures before the agent settles");
    }
    process.env.OBSIDIAN_VAULT_PATH = retargetVault;
    await featureHandlers.get("agent_end")({}, ctx);
    process.env.OBSIDIAN_VAULT_PATH = vault;
    assert.deepEqual(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending, {});
    if (emitTurnEnd) {
      assert.equal(fs.readFileSync(sharedIndex, "utf8"), indexBeforeAbort, "unchanged aborted writes do not publish navigation");
      for (const event of aborted) assert.ok(!fs.readFileSync(sharedLog, "utf8").includes(path.basename(event.input.path)));
    } else {
      assert.ok(fs.readFileSync(sharedIndex, "utf8").includes(path.basename(earlier.input.path)),
        "abort recovery also finalizes earlier completed writes in this session");
      assert.ok(fs.readFileSync(sharedIndex, "utf8").includes(path.basename(aborted[1].input.path)));
      assert.match(fs.readFileSync(sharedLog, "utf8"), /\*\*Update\*\*.*aborted-false-1\.md/s);
    }
    const retry = { toolCallId: `retry-${emitTurnEnd}`, toolName: "write", input: { path: aborted[0].input.path } };
    assert.equal(await prepare(retry), undefined, "the same session can write again after abort recovery");
    fs.writeFileSync(retry.input.path, pageContent(retry.toolCallId));
    await featureHandlers.get("tool_result")({ ...retry, isError: false }, ctx);
    await featureHandlers.get("turn_end")({}, ctx);
    assert.equal(await prepare({ toolCallId: "retry-nav", toolName: "read", input: { path: sharedIndex } }), undefined);
    assert.deepEqual(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending, {});
  }

  // Error results can leave partial bytes; recovery must validate, not publish
  // an invalid page or leave its active capture poisoning later repair writes.
  const failed = { toolCallId: "failed-partial", toolName: "write", input: { path: path.join(vault, "wiki/topic/failed-partial.md") } };
  assert.equal(await prepare(failed), undefined);
  fs.writeFileSync(failed.input.path, "---\ntype: note\ntitle: Partial\n");
  await featureHandlers.get("tool_result")({ ...failed, isError: true }, ctx);
  const indexBeforeFailure = fs.readFileSync(sharedIndex, "utf8");
  await featureHandlers.get("agent_end")({}, ctx);
  assert.equal(fs.readFileSync(sharedIndex, "utf8"), indexBeforeFailure);
  assert.equal((await prepare({ toolCallId: "failed-nav", toolName: "read", input: { path: sharedIndex } }))?.block, true);
  const failedEntry = JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending[fs.realpathSync(failed.input.path)];
  assert.equal(failedEntry.captures["pi:pi-writer-session"][failed.toolCallId], "settled");
  const repaired = { ...failed, toolCallId: "failed-partial-repair" };
  assert.equal(await prepare(repaired), undefined);
  fs.writeFileSync(repaired.input.path, pageContent("Partial Repaired"));
  await featureHandlers.get("tool_result")({ ...repaired, isError: false }, ctx);
  await featureHandlers.get("turn_end")({}, ctx);
  assert.equal(await prepare({ toolCallId: "repaired-nav", toolName: "read", input: { path: sharedIndex } }), undefined);
  assert.deepEqual(JSON.parse(fs.readFileSync(lifecycleStatePath, "utf8")).pending, {});
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, featureConfig);

  const binary = path.join(vault, "wiki/topic/attachment.bin");
  await featureHandlers.get("tool_call")({ toolCallId: "binary-write", toolName: "write", input: { path: binary } }, ctx);
  fs.writeFileSync(binary, "fixture attachment");
  await featureHandlers.get("tool_result")({ toolCallId: "binary-write", toolName: "write", input: { path: binary }, isError: false }, ctx);
  assert.equal(await featureHandlers.get("tool_call")({ toolName: "read", input: { path: topicIndex } }, ctx), undefined,
    "non-Markdown wiki tool results do not create a navigation failure");
  const settledState = JSON.parse(fs.readFileSync(path.join(vault, ".vault-meta/lifecycle/state.json"), "utf8"));
  assert.deepEqual(settledState.pending, {}, "non-Markdown tool results do not enter lifecycle pending state");
  assert.equal(fs.readFileSync(topicIndex, "utf8").includes("attachment.bin"), false,
    "non-Markdown tool results do not alter wiki navigation");
  console.log("pi Obsidian lifecycle checks PASS");
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
