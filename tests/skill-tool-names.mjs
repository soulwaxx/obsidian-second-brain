// Every wiki_* tool name the portable skills mention must exist in the Pi registration or in the MCP
// tools/list of the matching profile (#64). Static: it proves the names are real, not that a model follows the skills.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { defaultLauncher as launcher, startMcp } from "./mcp-client.mjs";

const root = path.resolve(path.dirname(launcher), "..");
const work = fs.mkdtempSync(path.join(os.tmpdir(), "skill-tool-names-"));
const env = { OBSIDIAN_AGENT_CONFIG: path.join(work, "missing-config.json"), OBSIDIAN_VAULT_PATH: "" };

async function profileTools(profile) {
  const client = startMcp({ cwd: work, args: ["mcp", "--tools", profile], env });
  try {
    await client.initialize();
    return (await client.request("tools/list")).result.tools.map((tool) => tool.name);
  } finally {
    await client.close();
  }
}

try {
  const piTools = [...fs.readFileSync(path.join(root, "extensions/obsidian.ts"), "utf8").matchAll(/registerWikiTool\("(wiki_[a-z_]+)"/g)].map((m) => m[1]);
  const read = await profileTools("read");
  const all = await profileTools("all");
  assert.deepEqual(piTools, read, "Pi registers exactly the MCP read profile");
  const batch = all.filter((name) => !read.includes(name));
  assert.deepEqual(batch, ["wiki_batch_inspect", "wiki_batch_apply", "wiki_batch_recover"]);

  const skills = ["wiki", "wiki-query", "wiki-save", "wiki-ingest", "wiki-research", "wiki-health"];
  const refs = fs.readdirSync(path.join(root, "skills/wiki/references")).filter((name) => name.endsWith(".md"));
  const files = [...skills.map((name) => `skills/${name}/SKILL.md`), ...refs.map((name) => `skills/wiki/references/${name}`)];
  const mentioned = new Map();
  for (const file of files) {
    const text = fs.readFileSync(path.join(root, file), "utf8");
    for (const [name] of text.matchAll(/\bwiki_[a-z]+(?:_[a-z]+)*\b(?!\.py)/g)) {
      mentioned.set(name, [...(mentioned.get(name) ?? []), file]);
    }
  }
  for (const [name, where] of mentioned) assert.ok(all.includes(name), `${name} is neither registered in Pi nor listed by the MCP server (${where.join(", ")})`);
  for (const name of [...read, ...batch]) assert.ok(mentioned.has(name), `the portable skills never mention ${name}`);
  // Read-only skills may only name tools every profile has (Pi, Claude, and read-profile MCP agents).
  for (const skill of ["wiki-query", "wiki-health"]) {
    const text = fs.readFileSync(path.join(root, `skills/${skill}/SKILL.md`), "utf8");
    for (const name of batch) assert.ok(!text.includes(name), `${skill} must not name ${name}`);
  }
  // Pi and the Claude plugin never get the batch tools, so only the write-path guidance may name them.
  const writeGuidance = new Set(["skills/wiki/SKILL.md", "skills/wiki-save/SKILL.md", "skills/wiki-ingest/SKILL.md", "skills/wiki-research/SKILL.md",
    "skills/wiki/references/tool-surface.md", "skills/wiki/references/focused-workflows.md"]);
  for (const name of batch) for (const file of mentioned.get(name)) assert.ok(writeGuidance.has(file), `${name} named outside write guidance: ${file}`);
  console.log("Skill tool names match the Pi registration and MCP profiles");
} finally {
  fs.rmSync(work, { recursive: true, force: true });
}
