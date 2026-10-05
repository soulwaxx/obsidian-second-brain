import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const work = fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-npm-package-"));
// Publish dry runs must still create and install a real temporary test archive.
const env = { ...process.env, npm_config_dry_run: "false", npm_config_cache: path.join(work, "cache"), npm_config_update_notifier: "false", OBSIDIAN_AGENT_CONFIG: path.join(work, "inactive-config.json"), OBSIDIAN_VAULT_PATH: "", WIKI_MIDDLEWARE_DIR: "" };
const run = (command, args, options = {}) => execFileSync(command, args, { cwd: root, env, ...options });

try {
  const [archive] = JSON.parse(run("npm", ["pack", "--json", "--pack-destination", work], { encoding: "utf8" }));
  const files = new Set(archive.files.map((file) => file.path));
  for (const resource of [
    "package.json", "README.md", "LICENSE", "NOTICE.md", "properties.example.json",
    ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
    "extensions/obsidian.ts", "hooks/obsidian-session.sh", "hooks/hooks.json",
    "agents/pi/wiki-vault.md", "agents/claude/wiki-vault.md",
    "scripts/config_contract.py", "scripts/wiki_lifecycle.py", "scripts/bootstrap-vault.py", "scripts/provision-retrieval.py",
    "scripts/bm25-index.py", "scripts/retrieve.py", "scripts/contextual-prefix.py",
    "skills/wiki/SKILL.md", "skills/wiki/scripts/okf_mw/LICENSE-claude-obsidian",
    "skills/wiki/scripts/okf_mw/guard.py", "skills/wiki/scripts/okf_mw/validate.py",
    "skills/wiki/scripts/okf_mw/sync.py", "skills/wiki/scripts/okf_mw/lint.py",
    "skills/wiki/scripts/okf_mw/okf_paths.py", "skills/wiki/scripts/okf_mw/ownership.py",
    "docs/setup.md",
  ]) {
    assert.ok(files.has(resource), `missing npm resource: ${resource}`);
  }
  for (const name of fs.readdirSync(path.join(root, "skills/wiki/references"))) {
    if (name.endsWith(".md")) assert.ok(files.has(`skills/wiki/references/${name}`), `missing skill reference: ${name}`);
  }
  for (const file of files) {
    assert.doesNotMatch(file, /(^|\/)(tests|\.github|node_modules|__pycache__|\.vault-meta)(\/|$)|\.pyc$|\.tgz$|(^|\/)AGENTS\.md$|^scripts\/.*\.mjs$/);
    assert.ok(
      /^(package\.json|README\.md|LICENSE|NOTICE\.md|properties\.example\.json)$/.test(file)
      || /^(\.claude-plugin|agents|docs|extensions|hooks|scripts|skills)\//.test(file),
      `unexpected npm resource: ${file}`,
    );
  }

  const prefix = path.join(work, "install");
  run("npm", [
    "install", "--prefix", prefix, "--offline", "--ignore-scripts",
    "--legacy-peer-deps", "--no-audit", "--no-fund", "--package-lock=false",
    path.join(work, archive.filename),
  ], { stdio: "inherit" });
  const installed = path.join(prefix, "node_modules/@soulwaxx/obsidian-second-brain");
  const manifest = JSON.parse(fs.readFileSync(path.join(installed, "package.json"), "utf8"));
  assert.equal(manifest.name, "@soulwaxx/obsidian-second-brain");
  assert.notEqual(manifest.private, true);
  assert.equal(manifest.license, "MIT");
  const plugin = JSON.parse(fs.readFileSync(path.join(installed, ".claude-plugin/plugin.json"), "utf8"));
  const marketplace = JSON.parse(fs.readFileSync(path.join(installed, ".claude-plugin/marketplace.json"), "utf8"));
  assert.equal(plugin.version, manifest.version);
  assert.equal(marketplace.plugins.find((entry) => entry.name === plugin.name).version, manifest.version);
  assert.equal(manifest.publishConfig.access, "public");
  assert.ok(manifest.keywords.includes("pi-package"));
  assert.equal(manifest.peerDependencies["@earendil-works/pi-coding-agent"], "*");
  assert.equal(manifest.peerDependenciesMeta["@earendil-works/pi-coding-agent"].optional, true);
  for (const resource of [...manifest.pi.extensions, ...manifest.pi.skills, ...manifest.pi.subagents.agents]) {
    assert.ok(fs.existsSync(path.join(installed, resource)), `missing declared Pi resource: ${resource}`);
  }
  for (const file of files) assert.ok(fs.existsSync(path.join(installed, file)), `missing installed file: ${file}`);
  assert.ok(!fs.existsSync(path.join(prefix, "node_modules/@earendil-works/pi-coding-agent")), "package must not bundle the Pi host");

  const hook = path.join(installed, "hooks/obsidian-session.sh");
  const middleware = path.join(installed, "skills/wiki/scripts/okf_mw");
  run("bash", ["tests/obsidian-lifecycle.sh", hook, middleware], { stdio: "inherit" });
  run("python3", ["tests/obsidian-middleware.py", middleware], { stdio: "inherit" });
  run(process.execPath, ["tests/obsidian-extension.mjs", path.join(installed, "extensions/obsidian.ts"), hook, middleware], { stdio: "inherit" });
  console.log(`npm archive, isolated installation, and packed runtime PASS (${files.size} files)`);
} finally {
  fs.rmSync(work, { recursive: true, force: true });
}
