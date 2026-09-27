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
  fs.mkdirSync(path.join(tmp, "extensions"), { recursive: true });
  fs.mkdirSync(path.join(tmp, "hooks"), { recursive: true });
  fs.copyFileSync(hook, path.join(tmp, "hooks", "obsidian-session.sh"));
  process.env.OBSIDIAN_AGENT_CONFIG = path.join(tmp, "properties.json");
  fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: vault }));
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
  console.log("pi Obsidian pre-write checks PASS");
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
