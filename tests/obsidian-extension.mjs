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
  fs.copyFileSync(path.resolve(path.dirname(source), "../scripts/config_contract.py"), path.join(tmp, "scripts/config_contract.py"));
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
  const ctx = { cwd: vault };
  const call = (p, toolName = "write") => handlers.get("tool_call")({ toolName, input: { path: p } }, ctx);
  assert.equal(await call("wiki/topic/page.md"), undefined);
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
  assert.match(injected.message.content, /# Test TOC/);

  // Broken-config repair is limited to the exact selected regular config file.
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: vault, features: { guard: "bad" }, custom: { preserve: true } }));
  const repairCopy = path.join(tmp, "extensions", "obsidian-repair.ts");
  fs.writeFileSync(repairCopy, text.replace(importLine, "type ExtensionAPI = unknown;"));
  const repairHandlers = new Map();
  const { default: repairExtension } = await import(pathToFileURL(repairCopy));
  repairExtension({ on: (event, handler) => repairHandlers.set(event, handler) });
  const otherCwd = path.join(tmp, "outside");
  fs.mkdirSync(otherCwd);
  assert.equal(await repairHandlers.get("before_agent_start")({}, { cwd: otherCwd }), undefined,
    "outside-vault work must not receive another vault's repair directive");
  const repairDiagnostic = await repairHandlers.get("before_agent_start")({}, ctx);
  assert.match(repairDiagnostic.message.content, /exact minimal changes/);
  // A repeated identical diagnostic after an explicit approval must not restart
  // the pending repair conversation or ask the user to approve the same change.
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
  assert.equal((await repairHandlers.get("tool_call")({ toolName: "write", input: { path: path.join(tmp, "other-settings.json") } }, ctx))?.block, true,
    "unrelated settings do not inherit repair authority");
  assert.equal(await repairHandlers.get("tool_call")({ toolName: "write", input: { path: process.env.OBSIDIAN_AGENT_CONFIG } }, ctx), undefined,
    "the selected fixture config can be repaired");
  const configAlias = path.join(tmp, "config-alias.json");
  fs.symlinkSync(process.env.OBSIDIAN_AGENT_CONFIG, configAlias);
  assert.equal((await repairHandlers.get("tool_call")({ toolName: "write", input: { path: configAlias } }, ctx))?.block, true,
    "a symlink alias is not an approved repair target");
  const selectedConfig = JSON.parse(fs.readFileSync(process.env.OBSIDIAN_AGENT_CONFIG, "utf8"));
  selectedConfig.features.guard = true;
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify(selectedConfig));
  assert.deepEqual(JSON.parse(fs.readFileSync(process.env.OBSIDIAN_AGENT_CONFIG, "utf8")).custom, { preserve: true, repairRevision: 2 });
  assert.equal(await repairHandlers.get("tool_call")({ toolName: "write", input: { path: "wiki/topic/page.md" } }, ctx), undefined,
    "the same loaded session accepts page writes after config revalidation");

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
  const notifications = [];
  fs.unlinkSync(path.join(vault, "wiki/index.md")); // Replace the unmarked TOC fixture with middleware-owned output.
  const disabledPage = path.join(vault, "wiki/topic/disabled.md");
  fs.writeFileSync(disabledPage, "---\ntype: note\ntitle: Disabled Page\nlast_updated: 2026-01-09\n---\n# Disabled Page\n");
  await featureHandlers.get("tool_call")({ toolCallId: "write-1", toolName: "write", input: { path: disabledPage } }, ctx);
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
  const siblingRead = featureHandlers.get("tool_call")(
    { toolCallId: "read-1", toolName: "read", input: { path: "wiki/topic/index.md" } }, ctx,
  ).then(() => { siblingReadFinished = true; });
  await readDelayFired;
  globalThis.setTimeout = originalSetTimeout;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(siblingReadFinished, false, "sibling wiki read must wait for post-write sync");
  await featureHandlers.get("tool_result")({ toolCallId: "write-1", toolName: "write", input: { path: disabledPage }, isError: false }, ctx);
  await siblingRead;
  if (process.platform === "darwin" && vault.startsWith("/private/var/")) {
    const aliasVault = vault.replace(/^\/private\/var\//, "/var/");
    assert.equal(fs.realpathSync(aliasVault), fs.realpathSync(vault));
    const aliasPage = path.join(vault, "wiki/topic/alias/nested/page.md");
    fs.mkdirSync(path.dirname(aliasPage), { recursive: true });
    fs.writeFileSync(aliasPage, "---\ntype: note\ntitle: Alias Page\nlast_updated: 2026-01-09\n---\n# Alias Page\n");
    const aliasCtx = { ...ctx, cwd: aliasVault };
    await featureHandlers.get("tool_call")({ toolCallId: "alias-write", toolName: "write", input: { path: "wiki/topic/alias/nested/page.md" } }, aliasCtx);
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
    const aliasRead = featureHandlers.get("tool_call")(
      { toolCallId: "alias-read", toolName: "read", input: { path: "wiki/topic/index.md" } }, aliasCtx,
    ).then(() => { aliasReadFinished = true; });
    await aliasDelayFired;
    globalThis.setTimeout = aliasSetTimeout;
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(aliasReadFinished, false, "canonical vault writes through /var alias must register before navigation reads");
    await featureHandlers.get("tool_result")({ toolCallId: "alias-write", toolName: "write", input: { path: "wiki/topic/alias/nested/page.md" }, isError: false }, aliasCtx);
    await aliasRead;
    assert.match(fs.readFileSync(path.join(vault, "wiki/topic/index.md"), "utf8"), /\[Alias Page\]/);
  }
  // A later tool/read in the same assistant turn must observe fresh navigation.
  assert.match(fs.readFileSync(path.join(vault, "wiki/topic/index.md"), "utf8"), /\[Disabled Page\]\(disabled.md\)/);
  await featureHandlers.get("turn_end")({}, { ...ctx, ui: { notify: (message) => notifications.push(message) } });
  assert.deepEqual(notifications, []);
  assert.match(fs.readFileSync(path.join(vault, "wiki/topic/index.md"), "utf8"), /\[Disabled Page\]\(disabled.md\)/);

  // A failed post-write validation must leave ordinary pages available for repair,
  // while generated navigation stays hidden until a successful synchronization.
  const repairPage = path.join(vault, "wiki/topic/repair-me.md");
  const repairWarnings = [];
  const repairCtx = { ...ctx, ui: { notify: (message) => repairWarnings.push(message) } };
  fs.writeFileSync(repairPage, "# Missing required frontmatter\n");
  await featureHandlers.get("tool_call")({ toolCallId: "repair-failed-write", toolName: "write", input: { path: repairPage } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "repair-failed-write", toolName: "write", input: { path: repairPage }, isError: false }, repairCtx);
  assert.match(repairWarnings.join(" "), /post-write validation\/sync failed/);
  const topicIndex = path.join(vault, "wiki/topic/index.md");
  const validPage = path.join(vault, "wiki/topic/valid-after-failure.md");
  fs.writeFileSync(validPage, "---\ntype: note\ntitle: Valid After Failure\nlast_updated: 2026-01-09\n---\n# Valid After Failure\n");
  await featureHandlers.get("tool_call")({ toolCallId: "valid-after-failure", toolName: "write", input: { path: validPage } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "valid-after-failure", toolName: "write", input: { path: validPage }, isError: false }, repairCtx);
  assert.equal(fs.readFileSync(topicIndex, "utf8").includes("valid-after-failure.md"), false,
    "valid page must not refresh navigation while a prior changed page remains invalid");
  assert.equal((await featureHandlers.get("tool_call")({ toolName: "read", input: { path: topicIndex } }, repairCtx))?.block, true,
    "valid page must not clear another changed page's validation barrier");
  assert.equal((await featureHandlers.get("tool_call")({ toolName: "read", input: { path: fs.realpathSync(repairPage) } }, repairCtx))?.block, undefined,
    "ordinary failed page must remain readable for diagnosis");
  assert.equal((await featureHandlers.get("tool_call")({ toolName: "edit", input: { path: path.relative(vault, repairPage) } }, repairCtx))?.block, undefined,
    "failed page must remain editable for repair");
  assert.equal((await featureHandlers.get("tool_call")({ toolName: "write", input: { path: path.relative(vault, repairPage) } }, repairCtx))?.block, undefined,
    "failed page must remain writable for repair");
  const blockedNavigation = await featureHandlers.get("tool_call")({ toolName: "read", input: { path: fs.realpathSync(topicIndex) } }, repairCtx);
  assert.equal(blockedNavigation?.block, true,
    "generated navigation must remain blocked until recovery");
  fs.writeFileSync(repairPage, "---\ntype: note\ntitle: Repaired Page\nlast_updated: 2026-01-09\n---\n# Repaired Page\n");
  await featureHandlers.get("tool_call")({ toolCallId: "repair-write", toolName: "write", input: { path: path.relative(vault, repairPage) } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "repair-write", toolName: "write", input: { path: path.relative(vault, repairPage) }, isError: false }, repairCtx);
  assert.match(fs.readFileSync(topicIndex, "utf8"), /\[Repaired Page\]\(repair-me.md\)/,
    "successful repair must refresh generated navigation");
  assert.equal(await featureHandlers.get("tool_call")({ toolName: "read", input: { path: fs.realpathSync(topicIndex) } }, repairCtx), undefined,
    "generated navigation is readable again after recovery");

  // Shutdown must not publish a valid later page while an earlier ordinary
  // page remains invalid, even after its immediate postwrite failure.
  const shutdownInvalid = path.join(vault, "wiki/topic/shutdown-invalid.md");
  const shutdownLater = path.join(vault, "wiki/topic/shutdown-later.md");
  fs.writeFileSync(shutdownInvalid, "# Missing frontmatter\n");
  await featureHandlers.get("tool_call")({ toolCallId: "shutdown-invalid", toolName: "write", input: { path: shutdownInvalid } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "shutdown-invalid", toolName: "write", input: { path: shutdownInvalid }, isError: false }, repairCtx);
  fs.writeFileSync(shutdownLater, "---\ntype: note\ntitle: Shutdown Later\n---\n# Shutdown Later\n");
  await featureHandlers.get("tool_call")({ toolCallId: "shutdown-later", toolName: "write", input: { path: shutdownLater } }, repairCtx);
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
  await featureHandlers.get("tool_call")({ toolCallId: "shutdown-repair", toolName: "write", input: { path: shutdownInvalid } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "shutdown-repair", toolName: "write", input: { path: shutdownInvalid }, isError: false }, repairCtx);
  assert.match(fs.readFileSync(topicIndex, "utf8"), /\[Shutdown Later\]/,
    "repair must recover the navigation refresh");
  const deletedInvalid = path.join(vault, "wiki/topic/deleted-invalid.md");
  fs.writeFileSync(deletedInvalid, "# Invalid then deleted\n");
  await featureHandlers.get("tool_call")({ toolCallId: "deleted-invalid", toolName: "write", input: { path: deletedInvalid } }, repairCtx);
  await featureHandlers.get("tool_result")({ toolCallId: "deleted-invalid", toolName: "write", input: { path: deletedInvalid }, isError: false }, repairCtx);
  fs.unlinkSync(deletedInvalid);
  await featureHandlers.get("session_shutdown")({}, repairCtx);
  assert.equal(fs.readFileSync(topicIndex, "utf8").includes("deleted-invalid.md"), false,
    "deleting an unresolved invalid page must allow safe shutdown recovery");
  console.log("pi Obsidian lifecycle checks PASS");
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
