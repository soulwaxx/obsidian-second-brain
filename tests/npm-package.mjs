import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const work = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-npm-package-")));
// Publish dry runs must still create and install a real temporary test archive.
const env = { ...process.env, npm_config_dry_run: "false", npm_config_cache: path.join(work, "cache"), npm_config_update_notifier: "false", PYTHONWARNINGS: "error::DeprecationWarning", OBSIDIAN_AGENT_CONFIG: path.join(work, "inactive-config.json"), OBSIDIAN_VAULT_PATH: "", WIKI_MIDDLEWARE_DIR: "" };
const run = (command, args, options = {}) => execFileSync(command, args, { cwd: root, env, ...options });

try {
  const [archive] = JSON.parse(run("npm", ["pack", "--json", "--pack-destination", work], { encoding: "utf8" }));
  const files = new Set(archive.files.map((file) => file.path));
  for (const resource of [
    "package.json", "README.md", "LICENSE", "NOTICE.md", "properties.example.json",
    ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
    "extensions/obsidian.ts", "hooks/obsidian-session.sh", "hooks/hooks.json",
    "agents/pi/wiki-vault.md", "agents/claude/wiki-vault.md",
    "scripts/module_loading.py", "scripts/config_contract.py", "scripts/wiki_lifecycle.py", "scripts/bootstrap-vault.py", "scripts/provision-retrieval.py",
    "scripts/bm25-index.py", "scripts/retrieve.py", "scripts/contextual-prefix.py",
    "scripts/obsidian-second-brain.py", "scripts/obsidian-second-brain.mjs",
    "skills/wiki/SKILL.md", "skills/wiki-query/SKILL.md", "skills/wiki-save/SKILL.md",
    "skills/wiki-ingest/SKILL.md", "skills/wiki-research/SKILL.md", "skills/wiki-health/SKILL.md",
    "skills/wiki/scripts/okf_mw/LICENSE-claude-obsidian",
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
    assert.doesNotMatch(file, /(^|\/)(tests|\.github|node_modules|__pycache__|\.vault-meta)(\/|$)|\.pyc$|\.tgz$|(^|\/)AGENTS\.md$|^(?!scripts\/obsidian-second-brain\.mjs$)scripts\/.*\.mjs$/);
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
  const entry = marketplace.plugins.find((item) => item.name === plugin.name);
  assert.ok(!Object.hasOwn(entry, "version"));
  assert.deepEqual(entry.source, { source: "npm", package: manifest.name });
  assert.equal(manifest.publishConfig.access, "public");
  assert.ok(manifest.keywords.includes("pi-package"));
  assert.equal(manifest.peerDependencies["@earendil-works/pi-coding-agent"], "*");
  assert.equal(manifest.peerDependenciesMeta["@earendil-works/pi-coding-agent"].optional, true);
  const expectedSkills = ["wiki", "wiki-query", "wiki-save", "wiki-ingest", "wiki-research", "wiki-health"];
  assert.deepEqual(manifest.pi.skills, expectedSkills.map((name) => `./skills/${name}`));
  for (const resource of [...manifest.pi.extensions, ...manifest.pi.skills, ...manifest.pi.subagents.agents]) {
    assert.ok(fs.existsSync(path.join(installed, resource)), `missing declared Pi resource: ${resource}`);
  }
  for (const name of expectedSkills) {
    assert.ok(fs.existsSync(path.join(installed, `skills/${name}/SKILL.md`)), `missing installed skill: ${name}`);
  }
  const claudePlugin = JSON.parse(fs.readFileSync(path.join(installed, ".claude-plugin/plugin.json"), "utf8"));
  assert.equal(claudePlugin.name, "obsidian-second-brain");
  for (const name of expectedSkills) {
    const skill = fs.readFileSync(path.join(installed, `skills/${name}/SKILL.md`), "utf8");
    assert.match(skill, new RegExp(`^name: ${name}$`, "m"), `Claude skill metadata: ${name}`);
  }
  for (const file of files) assert.ok(fs.existsSync(path.join(installed, file)), `missing installed file: ${file}`);
  assert.ok(!fs.existsSync(path.join(prefix, "node_modules/@earendil-works/pi-coding-agent")), "package must not bundle the Pi host");
  const cliVault = path.join(work, "cli-vault");
  fs.mkdirSync(path.join(cliVault, "wiki"), { recursive: true });
  const cli = path.join(prefix, "node_modules/.bin/obsidian-second-brain");
  const doctor = JSON.parse(run(cli, ["doctor", "--json", "--config", path.join(work, "missing-config.json"), "--vault", cliVault], { cwd: work, encoding: "utf8" }));
  assert.equal(doctor.vault, fs.realpathSync(cliVault));
  assert.equal(doctor.retrieval.indexExists, false, "installed doctor must not build a retrieval index");
  const loadedSkill = path.join(installed, "skills/wiki-query/SKILL.md");
  const packageRootFromSkill = path.resolve(path.dirname(loadedSkill), "../..");
  assert.equal(packageRootFromSkill, installed, "loaded skill path must derive the active package root");
  const packageRelativeDoctor = JSON.parse(run(process.execPath, [
    path.join(packageRootFromSkill, "scripts/obsidian-second-brain.mjs"),
    "doctor", "--json", "--config", path.join(work, "missing-config.json"), "--vault", cliVault,
  ], { cwd: work, encoding: "utf8" }));
  assert.equal(packageRelativeDoctor.vault, fs.realpathSync(cliVault));
  assert.equal(packageRelativeDoctor.retrieval.indexExists, false, "package-relative doctor must remain read-only");

  const captureVault = path.join(work, "capture-vault");
  fs.mkdirSync(path.join(captureVault, "wiki"), { recursive: true });
  const captureConfig = path.join(work, "capture-config.json");
  fs.writeFileSync(captureConfig, JSON.stringify({ vaultPath: captureVault }));
  const captureSources = ["capture-a.txt", "capture-b.txt", "capture-c.txt"].map((name) => path.join(work, name));
  for (const source of captureSources) fs.writeFileSync(source, "offline packed shared payload\\n");
  const captureCli = (...args) => JSON.parse(run(cli, [
    ...args, "--config", captureConfig, "--json",
  ], { cwd: work, encoding: "utf8" }));
  const capture = (source) => captureCli("capture-inspect", "--source", source);
  const publish = (source, reviewed) => captureCli("capture-apply", "--source", source,
    "--plan-hash", reviewed.planHash);
  const reviewA = capture(captureSources[0]);
  publish(captureSources[0], reviewA);
  const recordA = path.join(captureVault, reviewA.targets.find((target) => target.endsWith(".md")));
  const payload = path.join(captureVault, reviewA.targets.find((target) => target.endsWith(".txt")));
  const payloadBefore = { bytes: fs.readFileSync(payload), stat: fs.statSync(payload) };
  const reviewB = capture(captureSources[1]);
  publish(captureSources[1], reviewB);
  const recordB = path.join(captureVault, reviewB.targets.find((target) => target.endsWith(".md")));
  const recordBBefore = { bytes: fs.readFileSync(recordB), stat: fs.statSync(recordB) };
  fs.writeFileSync(recordA, "damaged older reference\\n");
  const damagedA = { bytes: fs.readFileSync(recordA), stat: fs.statSync(recordA) };
  const reviewC = capture(captureSources[2]);
  assert.equal(reviewC.noop, false, "packed capture must compose B record with separate payload evidence");
  publish(captureSources[2], reviewC);
  assert.deepEqual(fs.readFileSync(payload), payloadBefore.bytes, "packed capture must preserve shared payload bytes");
  assert.equal(fs.statSync(payload).ino, payloadBefore.stat.ino, "packed capture must not replace shared payload inode");
  assert.equal(fs.statSync(payload).mtimeMs, payloadBefore.stat.mtimeMs, "packed capture must not rewrite shared payload");
  assert.deepEqual(fs.readFileSync(recordB), recordBBefore.bytes, "packed capture must preserve B metadata");
  assert.equal(fs.statSync(recordB).ino, recordBBefore.stat.ino);
  assert.equal(fs.statSync(recordB).mtimeMs, recordBBefore.stat.mtimeMs);
  assert.deepEqual(fs.readFileSync(recordA), damagedA.bytes, "packed capture must not repair damaged A metadata");
  assert.equal(fs.statSync(recordA).ino, damagedA.stat.ino);
  assert.equal(fs.statSync(recordA).mtimeMs, damagedA.stat.mtimeMs);

  const evidenceVault = path.join(work, "evidence-vault");
  fs.mkdirSync(path.join(evidenceVault, "wiki"), { recursive: true });
  const evidenceConfig = path.join(work, "evidence-config.json");
  fs.writeFileSync(evidenceConfig, JSON.stringify({ vaultPath: evidenceVault }));
  const evidenceCli = (...args) => JSON.parse(run(cli, [...args, "--config", evidenceConfig, "--json"], {
    cwd: work, encoding: "utf8",
  }));
  const evidenceBundle = (name, operations) => {
    const bundle = path.join(work, `${name}.json`);
    fs.writeFileSync(bundle, JSON.stringify({ version: 1, operations }));
    return bundle;
  };
  const evidenceText = (record) => `${JSON.stringify(record, null, 2)}\n`;
  const evidenceHash = (bytes) => createHash("sha256").update(bytes).digest("hex");
  const supportId = `source-${"a".repeat(24)}`;
  const contradictionId = `source-${"b".repeat(24)}`;
  const supportRecord = {
    schema: "okf-evidence-v1", kind: "source", id: supportId,
    locator: "https://example.test/support", contentHash: `sha256:${"1".repeat(64)}`,
    freshness: "current", review: { status: "unreviewed" }, futureExtension: { preserve: true },
  };
  const contradictionRecord = {
    schema: "okf-evidence-v1", kind: "source", id: contradictionId,
    locator: "https://example.test/contradiction", contentHash: `sha256:${"2".repeat(64)}`,
    freshness: "unknown", review: { status: "unreviewed" },
  };
  const claimRecord = {
    schema: "okf-evidence-v1", kind: "claim", id: "claim-packed",
    statement: "A selected claim", support: [supportId], contradictions: [contradictionId],
    uncertainty: "Needs review", evidenceState: "evidence-linked", review: { status: "unreviewed" },
  };
  const flatOps = [supportRecord, contradictionRecord, claimRecord].map((record) => ({
    path: `wiki/meta/evidence/${record.id}.json`, expectedHash: null, content: evidenceText(record),
  }));
  const flatBundle = evidenceBundle("evidence-flat", flatOps);
  const flatPreview = evidenceCli("batch-inspect", "--vault", evidenceVault, "--bundle", flatBundle,
    "--authority", "wiki-ledger");
  assert.equal(flatPreview.targets.length, 3);
  const flatApply = evidenceCli("batch-apply", "--vault", evidenceVault, "--bundle", flatBundle,
    "--authority", "wiki-ledger", "--plan-hash", flatPreview.planHash);
  assert.equal(flatApply.phase, "complete");
  const supportPath = path.join(evidenceVault, "wiki/meta/evidence", `${supportId}.json`);
  const supportBytes = fs.readFileSync(supportPath);
  const pagePath = path.join(evidenceVault, "wiki/claim.md");
  fs.writeFileSync(pagePath, `---\ntype: Note\nsources:\n  - id: ${supportId}\nclaims: [claim-packed]\n---\nClaim page\n`);
  const readReport = () => evidenceCli("evidence-report", "--vault", evidenceVault, "--as-of", "2026-06-01");
  const flatReport = readReport();
  assert.equal(flatReport.ledgerSummary.sources, 2);
  assert.equal(flatReport.ledgerSummary.claims, 1);
  const claimPage = flatReport.pages.find((item) => item.path === "wiki/claim.md");
  assert.equal(claimPage.references[0].state, "ledger-source-current");
  assert.equal(claimPage.claims[0].state, "contradictory-evidence-recorded");
  assert.deepEqual(claimPage.claims[0].support, [supportId]);
  assert.deepEqual(claimPage.claims[0].contradictions, [contradictionId]);
  assert.equal(claimPage.claims[0].uncertainty, "Needs review");

  const updatedSupport = {
    ...supportRecord, freshness: "stale", review: { status: "reviewed", reviewedBy: "fixture" },
  };
  const updateBundle = evidenceBundle("evidence-update", [{
    path: `wiki/meta/evidence/${supportId}.json`, expectedHash: evidenceHash(supportBytes),
    content: evidenceText(updatedSupport),
  }]);
  const updatePreview = evidenceCli("batch-inspect", "--vault", evidenceVault, "--bundle", updateBundle,
    "--authority", "wiki-ledger");
  fs.writeFileSync(supportPath, "concurrent edit\n");
  const drift = spawnSync(cli, ["batch-apply", "--vault", evidenceVault, "--bundle", updateBundle,
    "--authority", "wiki-ledger", "--plan-hash", updatePreview.planHash, "--config", evidenceConfig, "--json"],
  { cwd: work, env, encoding: "utf8" });
  assert.notEqual(drift.status, 0);
  assert.match(drift.stderr, /target precondition mismatch/);
  assert.equal(fs.readFileSync(supportPath, "utf8"), "concurrent edit\n");
  fs.writeFileSync(supportPath, supportBytes);
  const acceptedUpdate = evidenceCli("batch-apply", "--vault", evidenceVault, "--bundle", updateBundle,
    "--authority", "wiki-ledger", "--plan-hash", updatePreview.planHash);
  assert.equal(acceptedUpdate.phase, "complete");
  assert.deepEqual(JSON.parse(fs.readFileSync(supportPath, "utf8")).futureExtension, { preserve: true });
  const updateReport = readReport();
  assert.equal(updateReport.sources.find((item) => item.id === supportId).freshness, "stale");
  assert.equal(updateReport.sources.find((item) => item.id === supportId).reviewStatus, "reviewed");
  assert.equal(evidenceCli("batch-apply", "--vault", evidenceVault, "--bundle", updateBundle,
    "--authority", "wiki-ledger", "--plan-hash", updatePreview.planHash).noop, true);

  const nestedVault = path.join(work, "evidence-nested-vault");
  fs.mkdirSync(path.join(nestedVault, "wiki"), { recursive: true });
  const nestedConfig = path.join(work, "evidence-nested-config.json");
  fs.writeFileSync(nestedConfig, JSON.stringify({ vaultPath: nestedVault }));
  const nestedSnapshot = () => {
    const entries = [];
    const walk = (directory) => {
      for (const entry of fs.readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
        const target = path.join(directory, entry.name);
        const info = fs.lstatSync(target);
        entries.push([path.relative(nestedVault, target), info.mode, info.mtimeMs,
          info.isFile() ? fs.readFileSync(target).toString("base64") : info.isSymbolicLink() ? fs.readlinkSync(target) : null]);
        if (info.isDirectory()) walk(target);
      }
    };
    walk(nestedVault);
    return JSON.stringify(entries);
  };
  const nestedCli = (command, bundlePath, extra = []) => spawnSync(cli, [command, "--vault", nestedVault,
    "--bundle", bundlePath, "--authority", "wiki-ledger", "--config", nestedConfig, "--json", ...extra],
  { cwd: work, env, encoding: "utf8", timeout: 3_000 });
  const nestedSource = { ...supportRecord, id: `source-${"c".repeat(24)}` };
  const nestedClaim = { ...claimRecord, id: "claim-nested", support: [supportId] };
  const nestedRecords = [nestedSource, nestedClaim];
  for (const record of nestedRecords) {
    const bundle = evidenceBundle(`nested-${record.kind}`, [{
      path: `wiki/meta/evidence/selected/${record.id}.json`, expectedHash: null, content: evidenceText(record),
    }]);
    const before = nestedSnapshot();
    const inspected = nestedCli("batch-inspect", bundle);
    assert.notEqual(inspected.status, 0);
    assert.match(inspected.stderr, /canonical evidence ledger path/);
    assert.equal(nestedSnapshot(), before);
    const applied = nestedCli("batch-apply", bundle, ["--plan-hash", "a".repeat(64)]);
    assert.notEqual(applied.status, 0);
    assert.match(applied.stderr, /canonical evidence ledger path/);
    assert.equal(nestedSnapshot(), before);
  }
  const mixedNested = evidenceBundle("nested-later-target", [
    { path: `wiki/meta/evidence/${nestedSource.id}.json`, expectedHash: null, content: evidenceText(nestedSource) },
    { path: `wiki/meta/evidence/selected/${nestedClaim.id}.json`, expectedHash: null, content: evidenceText(nestedClaim) },
  ]);
  const beforeMixed = nestedSnapshot();
  for (const [command, args] of [["batch-inspect", []], ["batch-apply", ["--plan-hash", "b".repeat(64)]]]) {
    const result = nestedCli(command, mixedNested, args);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /canonical evidence ledger path/);
    assert.equal(nestedSnapshot(), beforeMixed);
  }
  assert.ok(!fs.existsSync(path.join(nestedVault, "wiki/meta")), "rejected nested ledger bundles must not create any ledger directories");
  assert.ok(!fs.existsSync(path.join(nestedVault, ".vault-meta")), "rejected nested ledger bundles must not create batch state");

  const hook = path.join(installed, "hooks/obsidian-session.sh");
  const middleware = path.join(installed, "skills/wiki/scripts/okf_mw");
  run("bash", ["tests/obsidian-lifecycle.sh", hook, middleware], { stdio: "inherit" });
  run("python3", ["tests/obsidian-middleware.py", middleware], { stdio: "inherit" });
  run(process.execPath, ["tests/obsidian-extension.mjs", path.join(installed, "extensions/obsidian.ts"), hook, middleware], { stdio: "inherit" });
  console.log(`npm archive, isolated installation, and packed runtime PASS (${files.size} files)`);
} finally {
  fs.rmSync(work, { recursive: true, force: true });
}
