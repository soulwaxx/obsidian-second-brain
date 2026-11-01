import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseNpmArchive } from "./npm-pack.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const work = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-npm-package-")));
// Publish dry runs must still create and install a real temporary test archive.
const env = { ...process.env, npm_config_dry_run: "false", npm_config_cache: path.join(work, "cache"), npm_config_update_notifier: "false", PYTHONWARNINGS: "error::DeprecationWarning", OBSIDIAN_AGENT_CONFIG: path.join(work, "inactive-config.json"), OBSIDIAN_VAULT_PATH: "", WIKI_MIDDLEWARE_DIR: "" };
const run = (command, args, options = {}) => execFileSync(command, args, { cwd: root, env, ...options });

try {
  const archive = parseNpmArchive(run("npm", ["pack", "--json", "--pack-destination", work], { encoding: "utf8" }));
  const files = new Set(archive.files.map((file) => file.path));
  for (const resource of [
    "package.json", "requirements.txt", "README.md", "LICENSE", "NOTICE.md", "properties.example.json",
    ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
    "extensions/obsidian.ts", "hooks/obsidian-session.sh", "hooks/hooks.json",
    "agents/pi/wiki-vault.md", "agents/claude/wiki-vault.md",
    "scripts/module_loading.py", "scripts/config_contract.py", "scripts/wiki_lifecycle.py", "scripts/bootstrap-vault.py",
    "scripts/bm25-index.py", "scripts/retrieve.py", "scripts/contextual-prefix.py", "scripts/evaluate-retrieval.py", "scripts/retrieval_projection.py",
    "scripts/obsidian-second-brain.py", "scripts/obsidian-second-brain.mjs", "scripts/pi_wiki_tools.py", "scripts/evidence_reports.py", "scripts/evidence_ledger.py", "scripts/wiki_batch.py", "scripts/source_baselines.py", "scripts/retrieval_reads.py",
    "scripts/obsidian_compat.py", "scripts/wiki_migrate.py", "scripts/wiki_history.py",
    "skills/wiki/SKILL.md", "skills/wiki-query/SKILL.md", "skills/wiki-save/SKILL.md",
    "skills/wiki-ingest/SKILL.md", "skills/wiki-research/SKILL.md", "skills/wiki-health/SKILL.md",
    "skills/wiki-agent/SKILL.md",
    "skills/wiki/scripts/okf_mw/LICENSE-claude-obsidian",
    "skills/wiki/scripts/okf_mw/guard.py", "skills/wiki/scripts/okf_mw/validate.py",
    "skills/wiki/scripts/okf_mw/sync.py", "skills/wiki/scripts/okf_mw/lint.py",
    "skills/wiki/scripts/okf_mw/okf_paths.py", "skills/wiki/scripts/okf_mw/ownership.py",
    "docs/setup.md", "docs/link-migration.md", "docs/pi-tools.md", "docs/source-baselines.md",
  ]) {
    assert.ok(files.has(resource), `missing npm resource: ${resource}`);
  }
  assert.ok(!files.has("scripts/provision-retrieval.py"), "retired helper-copy provisioner must not ship");
  for (const name of fs.readdirSync(path.join(root, "skills/wiki/references"))) {
    if (name.endsWith(".md")) assert.ok(files.has(`skills/wiki/references/${name}`), `missing skill reference: ${name}`);
  }
  for (const file of files) {
    assert.doesNotMatch(file, /(^|\/)(tests|\.github|node_modules|__pycache__|\.vault-meta)(\/|$)|\.pyc$|\.tgz$|(^|\/)AGENTS\.md$|^(?!scripts\/obsidian-second-brain\.mjs$)scripts\/.*\.mjs$/);
    assert.ok(
      /^(package\.json|requirements\.txt|README\.md|LICENSE|NOTICE\.md|properties\.example\.json)$/.test(file)
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
  assert.deepEqual(manifest.engines, { node: ">=18.0.0" });
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
  assert.equal(claudePlugin.userConfig.vault_agent_effort.default, "inherit");
  assert.deepEqual(claudePlugin.userConfig.vault_agent_effort.options, ["inherit", "low", "medium", "high", "xhigh", "max"]);
  for (const name of [...expectedSkills, "wiki-agent"]) {
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
  const nativeDoctor = JSON.parse(run(process.execPath, [path.join(installed, "scripts/obsidian-second-brain.mjs"), "--pi-tool", "doctor", JSON.stringify({ vault: cliVault })], { cwd: work, encoding: "utf8" }));
  assert.equal(nativeDoctor.operation, "doctor");
  assert.equal(nativeDoctor.payload.vault, fs.realpathSync(cliVault), "packed native tool uses explicit vault without npm bin lookup");
  const binSearchPath = process.env.PATH.split(path.delimiter).filter((entry) => path.resolve(entry) !== path.resolve(path.join(prefix, "node_modules/.bin"))).join(path.delimiter);
  assert.ok(!binSearchPath.split(path.delimiter).some((entry) => path.resolve(entry) === path.resolve(path.join(prefix, "node_modules/.bin"))));
  const unrelatedCwd = path.join(work, "caller cwd ü space");
  const relativeVault = path.join(unrelatedCwd, "relative vault ü");
  fs.mkdirSync(path.join(relativeVault, "wiki"), { recursive: true });
  fs.mkdirSync(unrelatedCwd, { recursive: true });
  const packageFixture = path.join(work, "packed package fixture ü");
  fs.cpSync(installed, packageFixture, { recursive: true });
  const extensionPath = path.join(packageFixture, "extensions/pi-tools-package-test.ts");
  const extensionSource = fs.readFileSync(path.join(packageFixture, "extensions/obsidian.ts"), "utf8")
    .replace('import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";', "type ExtensionAPI = unknown;")
    .replace('import { Type } from "@earendil-works/pi-ai";', 'const Type = { Object: (properties, options) => ({ properties, options }), String: (options) => ({ options }), Boolean: () => ({}), Record: () => ({}), Unknown: () => ({}), Optional: (schema) => ({ schema }), Integer: (options) => ({ options }), Number: (options) => ({ options }), Union: (schemas) => ({ schemas }), Null: () => ({}), Literal: (value) => ({ value }) };');
  fs.writeFileSync(extensionPath, extensionSource);
  const savedConfig = process.env.OBSIDIAN_AGENT_CONFIG;
  const savedVaultOverride = process.env.OBSIDIAN_VAULT_PATH;
  const savedPath = process.env.PATH;
  try {
    process.env.PATH = binSearchPath;
    process.env.OBSIDIAN_VAULT_PATH = "";
    process.env.OBSIDIAN_AGENT_CONFIG = path.join(work, "blocked-config-directory");
    fs.mkdirSync(process.env.OBSIDIAN_AGENT_CONFIG);
    const { default: packedExtension } = await import(`${pathToFileURL(extensionPath).href}?package-native-tools`);
    const packedTools = new Map();
    packedExtension({ on: () => {}, registerTool: (tool) => packedTools.set(tool.name, tool) });
    const packedPage = path.join(cliVault, "wiki/packed ü.md");
    const packedSource = "# Packed native page\nBody.\n";
    fs.writeFileSync(packedPage, packedSource);
    const packedHash = createHash("sha256").update(packedSource).digest("hex");
    const packedRead = await packedTools.get("wiki_read").execute("packed-read", { vault: cliVault, page: "wiki/packed ü.md", expectedHash: packedHash, start: 0, end: packedSource.length }, undefined, undefined, { cwd: unrelatedCwd });
    assert.equal(packedRead.structuredContent.ok, true, JSON.stringify(packedRead.structuredContent));
    process.env.OBSIDIAN_AGENT_CONFIG = path.join(work, "relative-selection.json");
    fs.writeFileSync(process.env.OBSIDIAN_AGENT_CONFIG, JSON.stringify({ vaultPath: path.basename(relativeVault) }));
    const packedDoctor = await packedTools.get("wiki_doctor").execute("packed-doctor", {}, undefined, undefined, { cwd: unrelatedCwd });
    assert.equal(packedDoctor.structuredContent.payload.vault, fs.realpathSync(relativeVault), "packed extension uses caller cwd for a relative config vault");
  } finally {
    if (savedConfig === undefined) delete process.env.OBSIDIAN_AGENT_CONFIG; else process.env.OBSIDIAN_AGENT_CONFIG = savedConfig;
    if (savedVaultOverride === undefined) delete process.env.OBSIDIAN_VAULT_PATH; else process.env.OBSIDIAN_VAULT_PATH = savedVaultOverride;
    if (savedPath === undefined) delete process.env.PATH; else process.env.PATH = savedPath;
    fs.rmSync(packageFixture, { recursive: true, force: true });
  }

  const readPage = path.join(cliVault, "wiki/read-page.md");
  const readSource = "---\ntype: Note\n---\n# Evidence 😀\nSelected text.\n";
  fs.writeFileSync(readPage, readSource);
  const sourceHash = createHash("sha256").update(fs.readFileSync(readPage)).digest("hex");
  const readOutlineArgs = ["outline", "--json", "--vault", cliVault, "--page", "wiki/read-page.md"];
  const binOutline = JSON.parse(run(cli, readOutlineArgs, { cwd: work, encoding: "utf8" }));
  assert.equal(binOutline.sourceHash, sourceHash);
  const headingId = `heading:${readSource.indexOf("#")}`;
  assert.equal(binOutline.entries.find((item) => item.kind === "heading").id, headingId);
  const binRead = JSON.parse(run(cli, ["read", "--json", "--vault", cliVault, "--page", "wiki/read-page.md",
    "--expected-hash", sourceHash, "--section", headingId], { cwd: work, encoding: "utf8" }));
  assert.match(binRead.text, /Selected text/);
  const packageOutline = JSON.parse(run(process.execPath, [path.join(packageRootFromSkill, "scripts/obsidian-second-brain.mjs"),
    ...readOutlineArgs], { cwd: work, encoding: "utf8" }));
  assert.equal(packageOutline.sourceHash, sourceHash);
  const packageRead = JSON.parse(run(process.execPath, [path.join(packageRootFromSkill, "scripts/obsidian-second-brain.mjs"),
    "read", "--json", "--vault", cliVault, "--page", "wiki/read-page.md", "--expected-hash", sourceHash,
    "--start", "0", "--end", String(Array.from(readSource).length)], { cwd: work, encoding: "utf8" }));
  assert.equal(packageRead.text, readSource);
  run("python3", [path.join(installed, "scripts/bm25-index.py"), "build", "--vault", cliVault], { cwd: work });
  const indexPath = path.join(cliVault, ".vault-meta/retrieval/bm25.json");
  const readSnapshot = (file) => ({ bytes: fs.readFileSync(file), mtimeNs: fs.statSync(file).mtimeNs });
  const sourceBeforeSearch = readSnapshot(readPage);
  const indexBeforeSearch = readSnapshot(indexPath);
  for (const searchCommand of [
    [cli, ["search", "Selected text", "--json", "--vault", cliVault]],
    [process.execPath, [path.join(packageRootFromSkill, "scripts/obsidian-second-brain.mjs"),
      "search", "Selected text", "--json", "--vault", cliVault]],
  ]) {
    const searchResult = JSON.parse(run(searchCommand[0], searchCommand[1], { cwd: work, encoding: "utf8" }));
    const match = searchResult.results.find((item) => item.path === "wiki/read-page.md");
    assert.ok(match?.readRef, "packed search must include a usable source reference");
    assert.equal(match.sourceHash, sourceHash);
    const roundtrip = JSON.parse(run(searchCommand[0], [
      ...(searchCommand[0] === cli ? [] : [searchCommand[1][0]]),
      "read", "--json", "--vault", cliVault, "--page", match.readRef.page,
      "--expected-hash", match.readRef.expectedHash, "--start", String(match.readRef.start), "--end", String(match.readRef.end),
    ], { cwd: work, encoding: "utf8" }));
    assert.match(roundtrip.text, /Selected text/);
    assert.deepEqual(readSnapshot(readPage), sourceBeforeSearch, "search/read must not rewrite source page");
    assert.deepEqual(readSnapshot(indexPath), indexBeforeSearch, "search/read must not rewrite index/cache");
  }

  const evaluatorHelp = run("python3", [path.join(installed, "scripts/evaluate-retrieval.py"), "--help"], { cwd: work, encoding: "utf8" });
  assert.match(evaluatorHelp, /--vault/);
  assert.match(evaluatorHelp, /--gold/);
  run("python3", ["tests/test_retrieval_evaluation.py"], { env: { ...env, OSB_EVALUATOR_PATH: path.join(installed, "scripts/evaluate-retrieval.py") }, stdio: "inherit" });
  for (const suite of ["test_wiki_lifecycle.py", "test_obsidian_compat.py", "test_wiki_migrate.py", "test_wiki_history.py"]) {
    run("python3", [`tests/${suite}`], {
      env: { ...env, OSB_TEST_PACKAGE_ROOT: installed }, stdio: "inherit",
    });
  }

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
  const invalidCapture = spawnSync(cli, ["capture-inspect", "--source", "relative.txt", "--config", captureConfig, "--json"],
    { cwd: work, env, encoding: "utf8" });
  assert.notEqual(invalidCapture.status, 0);
  assert.equal(invalidCapture.stderr, "");
  const captureError = JSON.parse(invalidCapture.stdout);
  assert.equal(captureError.code, "command-error");
  assert.match(captureError.error, /absolute paths/);
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
  assert.equal(drift.stderr, "");
  const driftError = JSON.parse(drift.stdout);
  assert.equal(driftError.code, "command-error");
  assert.match(driftError.error, /target precondition mismatch|invalid ledger record/);
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
    assert.equal(inspected.stderr, "");
    const inspectError = JSON.parse(inspected.stdout);
    assert.equal(inspectError.code, "command-error");
    assert.match(inspectError.error, /canonical evidence ledger path/);
    assert.equal(nestedSnapshot(), before);
    const applied = nestedCli("batch-apply", bundle, ["--plan-hash", "a".repeat(64)]);
    assert.notEqual(applied.status, 0);
    assert.equal(applied.stderr, "");
    const applyError = JSON.parse(applied.stdout);
    assert.equal(applyError.code, "command-error");
    assert.match(applyError.error, /canonical evidence ledger path/);
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
    assert.equal(result.stderr, "");
    const batchError = JSON.parse(result.stdout);
    assert.equal(batchError.code, "command-error");
    assert.match(batchError.error, /canonical evidence ledger path/);
    assert.equal(nestedSnapshot(), beforeMixed);
  }
  assert.ok(!fs.existsSync(path.join(nestedVault, "wiki/meta")), "rejected nested ledger bundles must not create any ledger directories");
  assert.ok(!fs.existsSync(path.join(nestedVault, ".vault-meta")), "rejected nested ledger bundles must not create batch state");

  const baselineVault = path.join(work, "baseline-vault");
  const baselineSourceRoot = path.join(work, "baseline-source-root");
  fs.mkdirSync(path.join(baselineVault, "wiki"), { recursive: true });
  fs.mkdirSync(baselineSourceRoot);
  const baselineConfig = path.join(work, "baseline-config.json");
  fs.writeFileSync(baselineConfig, JSON.stringify({ vaultPath: baselineVault }));
  const baselineSource = path.join(baselineSourceRoot, "selected.bin");
  fs.writeFileSync(baselineSource, Buffer.from([0, 13, 10, 255]));
  const baselinePage = path.join(baselineVault, "wiki/selected.md");
  const baselinePageBytes = Buffer.from("---\ntype: Note\n---\nSelected page.\r\n");
  fs.writeFileSync(baselinePage, baselinePageBytes);
  const baselineCli = (...args) => JSON.parse(run(cli, [
    ...args, "--config", baselineConfig, "--json",
  ], { cwd: unrelatedCwd, encoding: "utf8" }));
  const baselineSnapshot = () => {
    const entries = [];
    const walk = (directory) => {
      for (const entry of fs.readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
        const target = path.join(directory, entry.name);
        const info = fs.lstatSync(target);
        entries.push([path.relative(work, target), info.mtimeMs,
          info.isFile() ? fs.readFileSync(target).toString("base64") : info.isSymbolicLink() ? fs.readlinkSync(target) : null]);
        if (info.isDirectory()) walk(target);
      }
    };
    walk(baselineVault);
    walk(baselineSourceRoot);
    return JSON.stringify(entries);
  };
  const observationSelection = path.join(work, "observation-selection.json");
  fs.writeFileSync(observationSelection, JSON.stringify({ version: 1, rootId: "packed-root",
    assetId: "asset-packed", locator: "selected.bin", observedAt: "2026-06-01T12:00:00Z" }));
  const beforeObservationPreview = baselineSnapshot();
  const observationReview = baselineCli("observe-inspect", "--selection", observationSelection,
    "--source-root", baselineSourceRoot);
  assert.equal(baselineSnapshot(), beforeObservationPreview, "observe preview must not mutate the source or vault");
  assert.equal(baselineCli("observe-apply", "--selection", observationSelection,
    "--source-root", baselineSourceRoot, "--plan-hash", observationReview.planHash).phase, "complete");
  const repeatedObservation = baselineCli("observe-inspect", "--selection", observationSelection,
    "--source-root", baselineSourceRoot);
  assert.equal(repeatedObservation.noop, true, "exact observation repeat is a no-op");
  assert.equal(baselineCli("observe-apply", "--selection", observationSelection,
    "--source-root", baselineSourceRoot, "--plan-hash", repeatedObservation.planHash).noop, true);

  const pageBundle = evidenceBundle("baseline-page-batch", [{
    path: "wiki/selected.md", expectedHash: evidenceHash(baselinePageBytes), content: baselinePageBytes.toString("utf8"),
  }]);
  const beforePagePreview = baselineSnapshot();
  const pageReview = baselineCli("batch-inspect", "--vault", baselineVault, "--bundle", pageBundle,
    "--authority", "wiki-page");
  assert.equal(baselineSnapshot(), beforePagePreview, "page batch preview must not mutate source or vault");
  const pageReceipt = baselineCli("batch-apply", "--vault", baselineVault, "--bundle", pageBundle,
    "--authority", "wiki-page", "--plan-hash", pageReview.planHash);
  assert.equal(pageReceipt.phase, "complete");

  const reconciliationSelection = path.join(work, "reconciliation-selection.json");
  const observationRecord = fs.readdirSync(path.join(baselineVault, "wiki/meta/evidence"))
    .map((name) => JSON.parse(fs.readFileSync(path.join(baselineVault, "wiki/meta/evidence", name), "utf8")))
    .find((record) => record.kind === "observation");
  fs.writeFileSync(reconciliationSelection, JSON.stringify({ version: 1, reconciliations: [{
    page: "wiki/selected.md", observationId: observationRecord.id, pageBatchId: pageReceipt.batchId,
    reconciledAt: "2026-06-01T13:00:00Z", outcome: "no-change",
  }] }));
  const beforeBaselinePreview = baselineSnapshot();
  const baselineReview = baselineCli("baseline-inspect", "--selection", reconciliationSelection);
  assert.equal(baselineSnapshot(), beforeBaselinePreview, "baseline preview must not mutate the source or vault");
  const baselineReceipt = baselineCli("baseline-apply", "--selection", reconciliationSelection,
    "--plan-hash", baselineReview.planHash);
  assert.equal(baselineReceipt.phase, "complete");
  const repeatedBaseline = baselineCli("baseline-inspect", "--selection", reconciliationSelection);
  assert.equal(repeatedBaseline.noop, true, "exact baseline repeat is a no-op");
  assert.equal(baselineCli("baseline-apply", "--selection", reconciliationSelection,
    "--plan-hash", repeatedBaseline.planHash).noop, true);
  const baselineRecordPath = path.join(baselineVault, "wiki/meta/evidence",
    fs.readdirSync(path.join(baselineVault, "wiki/meta/evidence")).find((name) => {
      const record = JSON.parse(fs.readFileSync(path.join(baselineVault, "wiki/meta/evidence", name), "utf8"));
      return record.kind === "baseline";
    }));
  const firstBaseline = JSON.parse(fs.readFileSync(baselineRecordPath, "utf8"));
  const firstHistory = firstBaseline.history.slice();

  fs.writeFileSync(baselineSource, Buffer.from("Second selected source version.\n"));
  fs.writeFileSync(observationSelection, JSON.stringify({ version: 1, rootId: "packed-root",
    assetId: "asset-packed", locator: "selected.bin", observedAt: "2026-06-03T12:00:00Z" }));
  const secondObservationReview = baselineCli("observe-inspect", "--selection", observationSelection,
    "--source-root", baselineSourceRoot);
  assert.equal(baselineCli("observe-apply", "--selection", observationSelection,
    "--source-root", baselineSourceRoot, "--plan-hash", secondObservationReview.planHash).phase, "complete");
  const secondObservationRecord = fs.readdirSync(path.join(baselineVault, "wiki/meta/evidence"))
    .map((name) => JSON.parse(fs.readFileSync(path.join(baselineVault, "wiki/meta/evidence", name), "utf8")))
    .find((record) => record.kind === "observation" && record.observedAt === "2026-06-03T12:00:00Z");
  const revisedPageBytes = Buffer.from("---\ntype: Note\n---\nSelected page revised.\r\n");
  const revisedPageBundle = evidenceBundle("baseline-page-update", [{
    path: "wiki/selected.md", expectedHash: evidenceHash(baselinePageBytes), content: revisedPageBytes.toString("utf8"),
  }]);
  const beforeRevisedPagePreview = baselineSnapshot();
  const revisedPageReview = baselineCli("batch-inspect", "--vault", baselineVault, "--bundle", revisedPageBundle,
    "--authority", "wiki-page");
  assert.equal(baselineSnapshot(), beforeRevisedPagePreview, "updated page preview must remain read-only");
  const revisedPageReceipt = baselineCli("batch-apply", "--vault", baselineVault, "--bundle", revisedPageBundle,
    "--authority", "wiki-page", "--plan-hash", revisedPageReview.planHash);
  assert.equal(revisedPageReceipt.phase, "complete");

  fs.writeFileSync(reconciliationSelection, JSON.stringify({ version: 1, reconciliations: [{
    page: "wiki/selected.md", observationId: secondObservationRecord.id, pageBatchId: revisedPageReceipt.batchId,
    reconciledAt: "2026-06-04T13:00:00Z", outcome: "updated",
  }] }));
  const beforeSecondBaselinePreview = baselineSnapshot();
  const secondBaselineReview = baselineCli("baseline-inspect", "--selection", reconciliationSelection);
  assert.equal(baselineSnapshot(), beforeSecondBaselinePreview, "updated baseline preview must remain read-only");
  assert.equal(baselineCli("baseline-apply", "--selection", reconciliationSelection,
    "--plan-hash", secondBaselineReview.planHash).phase, "complete");
  const secondBaseline = JSON.parse(fs.readFileSync(baselineRecordPath, "utf8"));
  assert.equal(secondBaseline.id, firstBaseline.id, "page/asset baseline identity remains stable across updates");
  assert.deepEqual(secondBaseline.history.slice(0, firstHistory.length), firstHistory, "old history is retained exactly");
  assert.equal(secondBaseline.history.length, 2);
  assert.equal(secondBaseline.history[1].outcome, "updated");
  const repeatedUpdate = baselineCli("baseline-apply", "--selection", reconciliationSelection,
    "--plan-hash", secondBaselineReview.planHash);
  assert.equal(repeatedUpdate.noop, true, "approved updated reconciliation retry is idempotent");
  const updatedNoop = baselineCli("baseline-inspect", "--selection", reconciliationSelection);
  assert.equal(updatedNoop.noop, true, "updated reconciliation repeat is a verified no-op");
  assert.equal(baselineCli("baseline-apply", "--selection", reconciliationSelection,
    "--plan-hash", updatedNoop.planHash).noop, true);

  const historyResult = baselineCli("source-history", "--observation-id", secondObservationRecord.id,
    "--source-root", baselineSourceRoot);
  assert.equal(historyResult.observationId, secondObservationRecord.id);
  assert.equal(historyResult.status, "no-commit-recorded");
  const baselineReport = baselineCli("evidence-report", "--vault", baselineVault, "--as-of", "2026-06-05");
  assert.equal(baselineReport.ledgerSummary.observations, 2);
  assert.equal(baselineReport.pages.find((item) => item.path === "wiki/selected.md").baselines[0].latest.outcome, "updated");

  const hook = path.join(installed, "hooks/obsidian-session.sh");
  const middleware = path.join(installed, "skills/wiki/scripts/okf_mw");
  run("bash", ["tests/obsidian-lifecycle.sh", hook, middleware], { stdio: "inherit" });
  run("python3", ["tests/obsidian-middleware.py", middleware], { stdio: "inherit" });
  run(process.execPath, ["tests/obsidian-extension.mjs", path.join(installed, "extensions/obsidian.ts"), hook, middleware], { stdio: "inherit" });
  console.log(`npm archive, isolated installation, and packed runtime PASS (${files.size} files)`);
} finally {
  fs.rmSync(work, { recursive: true, force: true });
}
