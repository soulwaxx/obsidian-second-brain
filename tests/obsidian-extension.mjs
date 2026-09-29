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
  fs.copyFileSync(hook, path.join(tmp, "hooks", "obsidian-session.sh"));
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
    assert.match(warnings.join(" "), /cannot load .*properties\.json/);
  }
  process.env.OBSIDIAN_VAULT_PATH = vault;

  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: vault, features: { guard: false, autoCommit: false } }));
  const featureCopy = path.join(tmp, "extensions", "obsidian-features.ts");
  fs.writeFileSync(featureCopy, text.replace(importLine, "type ExtensionAPI = unknown;"));
  const featureHandlers = new Map();
  const { default: featureExtension } = await import(pathToFileURL(featureCopy));
  featureExtension({ on: (event, handler) => featureHandlers.set(event, handler) });
  assert.equal(await featureHandlers.get("tool_call")({ toolName: "write", input: { path: "wiki/index.md" } }, ctx), undefined);
  const notifications = [];
  await featureHandlers.get("turn_end")({}, { ...ctx, ui: { notify: (message) => notifications.push(message) } });
  assert.deepEqual(notifications, []);
  console.log("pi Obsidian lifecycle checks PASS");
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
