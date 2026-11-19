import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { defaultLauncher as launcher, startMcp } from "./mcp-client.mjs";

// Reviewed batch write tools of `mcp --tools all` (#63), against synthetic disposable vaults and temporary configs.
const root = path.resolve(path.dirname(launcher), "..");
const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "mcp-batch-")));
const python = process.env.PYTHON || "python3";
const clients = new Set();
const sha = (text) => createHash("sha256").update(text).digest("hex");
const page = (title, body = "") => `---\ntype: Note\n---\n# ${title}\n${body}`;
const BATCH_TOOLS = ["wiki_batch_inspect", "wiki_batch_apply", "wiki_batch_recover"];

function snapshot(...directories) {
  const entries = [];
  const walk = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true }).filter((item) => item.name !== ".git").sort((a, b) => a.name.localeCompare(b.name))) {
      const target = path.join(directory, entry.name);
      const info = fs.lstatSync(target, { bigint: true });
      entries.push([target, info.mode.toString(), info.isFile() ? fs.readFileSync(target).toString("base64") : info.isSymbolicLink() ? fs.readlinkSync(target) : null]);
      if (info.isDirectory()) walk(target);
    }
  };
  directories.forEach(walk);
  return JSON.stringify(entries);
}

try {
  const home = path.join(tmp, "home");
  const cwd = path.join(tmp, "client cwd");
  const vault = path.join(tmp, "vault ü");
  const outside = path.join(tmp, "outside");
  for (const directory of [home, cwd, outside, path.join(vault, "wiki/topic")]) fs.mkdirSync(directory, { recursive: true });
  const wikiDir = path.join(vault, "wiki");
  const existingRel = "wiki/topic/coffee.md";
  const existing = page("Coffee", "Espresso evidence.\n");
  fs.writeFileSync(path.join(vault, existingRel), existing);
  fs.writeFileSync(path.join(outside, "secret.md"), "outside bytes\n");
  fs.writeFileSync(path.join(vault, ".gitignore"), ".vault-meta/\n");
  const config = path.join(tmp, "properties.json");
  fs.writeFileSync(config, JSON.stringify({ vaultPath: vault }));

  // A `git` wrapper records every subcommand the batch flow runs; runtime may only query Git, never write it.
  const realGit = spawnSync("sh", ["-c", "command -v git"], { encoding: "utf8" }).stdout.trim();
  const gitLog = path.join(tmp, "git.log");
  const shims = path.join(tmp, "shims");
  fs.mkdirSync(shims);
  fs.writeFileSync(path.join(shims, "git"), `#!/bin/sh\nset -- "$@"; args="$*"\nwhile [ $# -gt 0 ]; do case "$1" in -C|-c) shift 2 ;; -*) shift ;; *) echo "$1" >> "${gitLog}"; break ;; esac; done\nexec "${realGit}" $args\n`, { mode: 0o755 });
  const git = (...args) => execFileSync(realGit, ["-C", vault, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  git("init", "-q");

  // Stalls the lifecycle finalize step (after the journal is saved and pages are published) while a flag file exists.
  const stallDir = path.join(tmp, "stall");
  const stallFlag = path.join(tmp, "stall.flag");
  const stalled = path.join(tmp, "stalled");
  fs.mkdirSync(stallDir);
  fs.writeFileSync(path.join(stallDir, "sitecustomize.py"), `import os, subprocess, time
_run = subprocess.run
def run(*args, **kwargs):
    command = args[0] if args else kwargs.get("args")
    if isinstance(command, list) and "finalize" in command and os.path.exists(os.environ["OSB_STALL_FLAG"]):
        open(os.environ["OSB_STALLED"], "w").close()
        time.sleep(float(open(os.environ["OSB_STALL_FLAG"]).read()))
    return _run(*args, **kwargs)
subprocess.run = run
`);
  const baseEnv = { HOME: home, OBSIDIAN_AGENT_CONFIG: config, OBSIDIAN_VAULT_PATH: "", OSB_PI_TOOL_DEADLINE_MS: "30000", PYTHONWARNINGS: "error::DeprecationWarning",
    PATH: `${shims}${path.delimiter}${process.env.PATH}`, PYTHONPATH: stallDir, OSB_STALL_FLAG: stallFlag, OSB_STALLED: stalled };
  const open = (options = {}) => {
    const client = startMcp({ cwd, args: ["mcp", "--tools", "all"], ...options, env: { ...baseEnv, ...options.env } });
    clients.add(client);
    return client;
  };
  const bridge = (operation, input, env = {}) => {
    const result = spawnSync(process.execPath, [launcher, "--pi-tool", operation], { cwd, encoding: "utf8", input: JSON.stringify(input), env: { ...process.env, ...baseEnv, ...env } });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
  };
  const cli = (command, bundle, ...extra) => {
    const file = path.join(tmp, "cli-bundle.json");
    fs.writeFileSync(file, JSON.stringify({ version: 1, operations: bundle }));
    const result = spawnSync(process.execPath, [launcher, command, "--bundle", file, "--config", config, ...extra], { cwd, encoding: "utf8", env: { ...process.env, ...baseEnv } });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
  };
  const structured = (response) => response.result.structuredContent;

  const client = open();
  const init = await client.initialize();

  // Profile surface: `all` lists seven tools with the right hints; `read` never lists or accepts the batch tools.
  const listed = (await client.request("tools/list")).result.tools;
  assert.deepEqual(listed.map((tool) => tool.name), ["wiki_search", "wiki_outline", "wiki_read", "wiki_doctor", ...BATCH_TOOLS]);
  const byName = Object.fromEntries(listed.map((tool) => [tool.name, tool]));
  assert.deepEqual(byName.wiki_batch_inspect.annotations, { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false });
  assert.deepEqual(byName.wiki_batch_apply.annotations, { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false });
  assert.equal(byName.wiki_batch_recover.annotations.readOnlyHint, false);
  assert.equal(byName.wiki_batch_recover.annotations.destructiveHint, true);
  for (const tool of listed) {
    assert.equal(tool.inputSchema.additionalProperties, false);
    assert.ok(tool.outputSchema.properties.operation.enum.includes("batch-apply"), "output schema covers the batch operations");
  }
  assert.ok(!Object.hasOwn(byName.wiki_batch_apply.inputSchema.properties, "vault"), "writes target only the configured vault");
  assert.match(init.result.instructions, /only way to change the wiki/);
  assert.doesNotMatch(init.result.instructions, /writes are guarded/, "the MCP locator never claims the hook's write guard");
  assert.match(init.result.instructions, /never edit, create, move or delete vault files directly with file or shell tools/);
  assert.match(init.result.instructions, /show the user its review summary/);
  assert.match(init.result.instructions, /wait for the user's explicit approval/);
  assert.match(init.result.instructions, /wiki_batch_recover with the same planHash/);
  assert.match(init.result.instructions, /never write index\.md or log\.md/);
  for (const flags of [["--tools", "read"], []]) {
    const readOnly = open({ args: ["mcp", ...flags] });
    const { instructions } = (await readOnly.initialize()).result;
    assert.deepEqual((await readOnly.request("tools/list")).result.tools.map((tool) => tool.name), ["wiki_search", "wiki_outline", "wiki_read", "wiki_doctor"]);
    assert.doesNotMatch(instructions, /wiki_batch/);
    for (const name of BATCH_TOOLS) assert.equal((await readOnly.callTool(name, {})).error.code, -32602, `${name} is refused under --tools read`);
    await readOnly.close();
  }
  const extension = fs.readFileSync(path.join(root, "extensions/obsidian.ts"), "utf8");
  assert.doesNotMatch(extension, /wiki_batch|batch-inspect|batch-apply|batch-recover/, "the batch tools are never Pi tools");

  // Inspect: bounded review summary, shared plan hash with the CLI, bridge parity, and no write.
  const created = { path: "wiki/topic/tea.md", expectedHash: null, content: page("Tea", "Green tea.\n") };
  const updated = { path: existingRel, expectedHash: sha(existing), content: page("Coffee", "Espresso evidence.\nDouble shot.\n") };
  const operations = [created, updated];
  const beforeInspect = snapshot(vault, home, outside);
  const inspected = await client.callTool("wiki_batch_inspect", { operations });
  assert.equal(inspected.result.isError, false, JSON.stringify(inspected.result));
  assert.deepEqual(structured(inspected), bridge("batch-inspect", { operations }), "inspect parity with the stdin bridge");
  const review = structured(inspected).payload;
  assert.equal(review.planHash, cli("batch-inspect", operations).planHash, "same plan hash as the CLI batch-inspect");
  assert.deepEqual(review.targets, [created.path, existingRel]);
  assert.deepEqual(review.operations.map((item) => [item.operation, item.beforeBytes, item.afterBytes, item.validation]),
    [["create", null, Buffer.byteLength(created.content), "passed"], ["update", Buffer.byteLength(existing), Buffer.byteLength(updated.content), "passed"]]);
  assert.match(review.operations[1].diff, /\+Double shot\./);
  assert.ok(!JSON.stringify(review).includes("Green tea.\\n---") && Buffer.byteLength(inspected.result.content[0].text) < 64 * 1024);
  assert.ok(snapshot(vault, home, outside) === beforeInspect, "inspect writes nothing");
  assert.equal(fs.existsSync(path.join(vault, ".vault-meta")), false, "inspect creates no lifecycle state");

  // Staleness: a changed bundle, file, or config between inspect and apply is refused; a wrong hash is refused.
  const refused = async (input, pattern, label, name = "wiki_batch_apply") => {
    const before = snapshot(wikiDir, outside); // refusal inside the writer lock may create empty journal state, never pages
    const response = await client.callTool(name, input);
    assert.equal(response.result.isError, true, `${label}: ${JSON.stringify(response.result)}`);
    assert.equal(structured(response).ok, false);
    assert.match(structured(response).message, pattern, label);
    assert.ok(snapshot(wikiDir, outside) === before, `${label}: no page was written`);
    return structured(response);
  };
  await refused({ operations, planHash: "0".repeat(64) }, /approval hash does not match/, "wrong plan hash");
  await refused({ operations: [{ ...created, content: page("Tea", "Black tea.\n") }, updated], planHash: review.planHash }, /approval hash does not match/, "changed bundle");
  fs.writeFileSync(path.join(vault, existingRel), `${existing}Edited elsewhere.\n`);
  const driftBefore = fs.readFileSync(path.join(vault, existingRel), "utf8");
  await refused({ operations, planHash: review.planHash }, /precondition mismatch/, "changed file");
  assert.equal(fs.readFileSync(path.join(vault, existingRel), "utf8"), driftBefore, "the concurrent edit is preserved");
  fs.writeFileSync(path.join(vault, existingRel), existing);
  fs.writeFileSync(config, JSON.stringify({ vaultPath: vault, features: { retrievalRefresh: false } }));
  await refused({ operations, planHash: review.planHash }, /approval hash does not match/, "changed config");
  fs.writeFileSync(config, JSON.stringify({ vaultPath: vault }));

  // Validation: new secrets (assembled at runtime) and invalid frontmatter are tool errors without partial writes.
  const fakeToken = ["gh", "p_", "x9Kq2Lm8Zr4Tv7Bn3Wc6Hd1Fs5Jg0Ya8Pu2Q"].join("");
  const leaky = { path: "wiki/topic/leak.md", expectedHash: null, content: page("Leak", `token ${fakeToken}\n`) };
  for (const [name, input] of [["wiki_batch_inspect", { operations: [created, leaky] }], ["wiki_batch_apply", { operations: [created, leaky], planHash: review.planHash }]]) {
    const result = await refused(input, /secret/i, `${name} secret refusal`, name);
    assert.ok(!JSON.stringify(result).includes(fakeToken), "the matched value is never echoed");
  }
  await refused({ operations: [{ path: "wiki/topic/bad.md", expectedHash: null, content: "no frontmatter\n" }] }, /OKF validation/, "invalid frontmatter", "wiki_batch_inspect");
  const legacy = `legacy ${fakeToken}\n`;
  fs.writeFileSync(path.join(vault, "wiki/topic/legacy.md"), page("Legacy", legacy));
  const legacyEdit = { path: "wiki/topic/legacy.md", expectedHash: sha(page("Legacy", legacy)), content: page("Legacy", `${legacy}More prose.\n`) };
  assert.equal(structured(await client.callTool("wiki_batch_inspect", { operations: [legacyEdit] })).ok, true, "only new findings are refused (#70 baseline rule)");
  fs.rmSync(path.join(vault, "wiki/topic/legacy.md"));

  // Protected destinations: reserved, hidden, out-of-wiki and symlinked paths are refused and nothing leaves the vault.
  fs.symlinkSync(path.join(outside, "secret.md"), path.join(vault, "wiki/topic/linked.md"));
  fs.symlinkSync(outside, path.join(vault, "wiki/outward"));
  const protectedPaths = [
    ["wiki/index.md", /lifecycle\/middleware-owned/], ["wiki/topic/Log.md", /lifecycle\/middleware-owned/], ["wiki/_plan.md", /lifecycle\/middleware-owned/],
    ["notes/x.md", /limited to Markdown under wiki\//], ["../outside/x.md", /canonical|unsafe|limited/], [".raw/agent-captures/x.md", /limited to Markdown under wiki\//],
    ["wiki/.hidden/x.md", /hidden wiki paths/], ["wiki/topic/x.txt", /limited to Markdown under wiki\//], [path.join(outside, "x.md"), /canonical/],
    ["wiki\\topic\\x.md", /canonical/], ["wiki/meta/evidence/x.json", /limited to Markdown under wiki\//],
    ["wiki/topic/linked.md", /not a regular file/], ["wiki/outward/x.md", /unsafe destination parent|not a regular file/],
  ];
  for (const [target, pattern] of protectedPaths) {
    const op = { path: target, expectedHash: null, content: page("Nope") };
    await refused({ operations: [op] }, pattern, `protected path ${target}`, "wiki_batch_inspect");
    // Symlink targets are checked after the plan-hash comparison, so a forged hash may be refused first; either way nothing is written.
    await refused({ operations: [op], planHash: "1".repeat(64) }, new RegExp(`${pattern.source}|approval hash does not match`), `protected path ${target} (apply)`);
  }
  assert.equal(fs.readFileSync(path.join(outside, "secret.md"), "utf8"), "outside bytes\n");
  assert.deepEqual(fs.readdirSync(outside).sort(), ["secret.md"], "nothing was written outside the vault");
  fs.rmSync(path.join(vault, "wiki/topic/linked.md"));
  fs.rmSync(path.join(vault, "wiki/outward"));

  // Argument validation shared with the schema: unknown keys, no `vault`, malformed entries, too many pages.
  for (const [input, label] of [[{ operations: [created], vault }, "vault input"], [{ operations: [{ path: "wiki/a.md", content: "x" }] }, "missing expectedHash"],
    [{ operations: [{ ...created, extra: 1 }] }, "extra operation key"], [{ operations: [] }, "empty operations"],
    [{ operations: Array.from({ length: 21 }, (_, index) => ({ path: `wiki/n${index}.md`, expectedHash: null, content: page("N") })) }, "21 pages"]]) {
    const response = await client.callTool("wiki_batch_inspect", input);
    assert.equal(structured(response).ok, false, label);
    assert.equal(structured(response).code, "invalid-request", label);
  }
  for (const input of [{}, { planHash: "a".repeat(64), batchId: "11111111-1111-4111-8111-111111111111" }, { planHash: "short" }]) {
    assert.equal(structured(await client.callTool("wiki_batch_recover", input)).code, "invalid-request", JSON.stringify(input));
  }
  assert.equal(structured(await client.callTool("wiki_batch_apply", { operations })).code, "invalid-request", "apply requires planHash");
  assert.match((await refused({ planHash: "a".repeat(64) }, /no recoverable batch journal/, "unknown plan hash", "wiki_batch_recover")).code, /batch-refused/);

  // Apply: the reviewed flow publishes pages, navigation, log and index through the CLI's own batch code.
  const applied = await client.callTool("wiki_batch_apply", { operations, planHash: review.planHash });
  assert.equal(applied.result.isError, false, JSON.stringify(applied.result));
  assert.deepEqual(structured(applied).payload, { batchId: structured(applied).payload.batchId, noop: false, phase: "complete", planHash: review.planHash });
  assert.equal(fs.readFileSync(path.join(vault, created.path), "utf8"), created.content);
  assert.equal(fs.readFileSync(path.join(vault, existingRel), "utf8"), updated.content);
  assert.match(fs.readFileSync(path.join(vault, "wiki/topic/index.md"), "utf8"), /tea/i, "lifecycle finalization generated the index (never written by the tool)");
  assert.ok(fs.existsSync(path.join(vault, "wiki/log.md")), "the lifecycle owns log.md");
  assert.ok(fs.existsSync(path.join(vault, ".vault-meta/retrieval/bm25.json")), "post-apply retrieval refresh ran");
  const replay = cli("batch-apply", operations, "--plan-hash", review.planHash);
  assert.deepEqual(replay, { batchId: structured(applied).payload.batchId, noop: true, phase: "complete", planHash: review.planHash }, "the CLI sees the MCP journal");
  assert.deepEqual(structured(await client.callTool("wiki_batch_apply", { operations, planHash: review.planHash })).payload, replay, "repeat apply is a verified no-op");
  assert.deepEqual(structured(await client.callTool("wiki_batch_recover", { planHash: review.planHash })).payload,
    { applied: true, batchId: replay.batchId, noop: true, phase: "complete" }, "recover of a completed batch is a no-op");
  assert.equal(git("diff", "--cached", "--name-only"), "", "nothing was staged");

  // Size cap: a bundle exactly at 48 KiB round-trips, one byte more is refused before any write, summaries stay bounded.
  const CAP = 48 * 1024;
  const sized = (bytes) => {
    const op = { path: "wiki/topic/big.md", expectedHash: null, content: page("Big") };
    const overhead = JSON.stringify([op]).length;
    return { ...op, content: op.content + "x".repeat(bytes - overhead) };
  };
  const atCap = sized(CAP);
  assert.equal(Buffer.byteLength(JSON.stringify([atCap])), CAP);
  const capInspect = structured(await client.callTool("wiki_batch_inspect", { operations: [atCap] }));
  assert.equal(capInspect.ok, true, JSON.stringify(capInspect).slice(0, 500));
  assert.equal(capInspect.payload.operations[0].diffTruncated, true);
  assert.ok(Buffer.byteLength(JSON.stringify(capInspect)) < 64 * 1024, "inspect output stays within the result limit");
  const capApply = structured(await client.callTool("wiki_batch_apply", { operations: [atCap], planHash: capInspect.payload.planHash }));
  assert.equal(capApply.ok, true, JSON.stringify(capApply));
  assert.equal(fs.readFileSync(path.join(vault, atCap.path), "utf8"), atCap.content, "a bundle at the cap round-trips");
  const overCap = { ...atCap, path: "wiki/topic/bigger.md", content: `${atCap.content}x` };
  const bytesBefore = snapshot(wikiDir, outside);
  const over = structured(await client.callTool("wiki_batch_inspect", { operations: [overCap] }));
  assert.equal(over.code, "invalid-request");
  assert.match(over.message, /48 KiB bundle limit/);
  assert.equal(structured(await client.callTool("wiki_batch_apply", { operations: [overCap], planHash: "2".repeat(64) })).code, "invalid-request");
  assert.ok(snapshot(wikiDir, outside) === bytesBefore, "an oversized bundle is refused before any write");
  const stdinOver = spawnSync(process.execPath, [launcher, "--pi-tool", "batch-inspect"], { cwd, encoding: "utf8", input: JSON.stringify({ operations: [{ ...overCap, content: "y".repeat(70_000) }] }), env: { ...process.env, ...baseEnv } });
  assert.match(JSON.parse(stdinOver.stdout).message, /64 KiB transport limit/, "the bridge bounds stdin");
  // Twenty pages of escape-heavy text: the bounded summary still fits the result limit (tests/test_pi_wiki_tools.py covers the diff-dropping fallback).
  const dense = Array.from({ length: 20 }, (_, index) => ({ path: `wiki/dense/p${index}.md`, expectedHash: null, content: page(`P${index}`, "\u0001".repeat(380) + "\n") }));
  const denseResult = await client.callTool("wiki_batch_inspect", { operations: dense });
  assert.equal(denseResult.result.isError, false, JSON.stringify(denseResult.result).slice(0, 400));
  assert.ok(Buffer.byteLength(denseResult.result.content[0].text) < 64 * 1024, "escape-heavy summaries fit the result limit");
  assert.ok(structured(denseResult).payload.operations.every((item) => /^[0-9a-f]{64}$/.test(item.afterHash)), "hashes and sizes always stay");

  // Recovery after a deadline: once the journal is saved the 30 s deadline no longer kills the publication.
  // `prepare` arms the finalize stall (seconds) and returns the reviewed plan hash.
  const prepare = async (operations, seconds) => {
    fs.writeFileSync(stallFlag, String(seconds));
    fs.rmSync(stalled, { force: true });
    return structured(await client.callTool("wiki_batch_inspect", { operations })).payload.planHash;
  };
  const waitUntil = async (predicate) => { const limit = Date.now() + 15_000; while (!predicate() && Date.now() < limit) await new Promise((resolve) => setTimeout(resolve, 20)); assert.ok(predicate()); };
  // Starts an apply, cancels it while the stalled finalize runs, and checks that the cancelled call is not answered.
  let nextRequest = 70;
  const cancelDuringPublish = async (operations, planHash) => {
    const canceller = open();
    await canceller.initialize();
    const id = nextRequest++;
    canceller.send({ jsonrpc: "2.0", id, method: "tools/call", params: { name: "wiki_batch_apply", arguments: { operations, planHash } } });
    await waitUntil(() => fs.existsSync(stalled));
    canceller.notify("notifications/cancelled", { requestId: id });
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.ok(!canceller.messages.some((message) => message.id === id), "a cancelled call is not answered");
    await canceller.close();
    fs.rmSync(stallFlag);
  };
  const late = { path: "wiki/topic/late.md", expectedHash: null, content: page("Late", "Published after the deadline.\n") };
  const lateHash = await prepare([late], 3);
  const slow = open({ env: { OSB_PI_TOOL_DEADLINE_MS: "1000" } });
  await slow.initialize();
  const started = Date.now();
  const lateResult = structured(await slow.callTool("wiki_batch_apply", { operations: [late], planHash: lateHash }));
  assert.ok(Date.now() - started > 2500, "the stalled finalize outlived the 1 s deadline");
  assert.equal(lateResult.ok, true, JSON.stringify(lateResult));
  assert.equal(lateResult.payload.phase, "complete", "the deadline is lifted once the journal is saved");
  await slow.close();
  fs.rmSync(stallFlag);

  // Cancellation during publication, then recovery through MCP tools only (also from a fresh server process).
  const interrupted = { path: "wiki/topic/interrupted.md", expectedHash: null, content: page("Interrupted", "Needs recovery.\n") };
  const interruptedHash = await prepare([interrupted], 60);
  await cancelDuringPublish([interrupted], interruptedHash);
  assert.equal(fs.readFileSync(path.join(vault, interrupted.path), "utf8"), interrupted.content, "the page was published before the stall");
  // Recover waits for the writer lock inside the 30 s deadline (the marker fires only once the lock is held), so a
  // held lock ends in a clean timeout error, never a hang; a later recover succeeds once the lock is free.
  const lockFile = path.join(vault, ".vault-meta/lifecycle/batches/writer.lock");
  const holder = spawn("python3", ["-c", "import fcntl,sys,time\nfd=open(sys.argv[1],'a')\nfcntl.flock(fd,fcntl.LOCK_EX)\nprint('locked',flush=True)\ntime.sleep(60)", lockFile], { stdio: ["ignore", "pipe", "ignore"] });
  try {
    await new Promise((resolve, reject) => { holder.stdout.once("data", resolve); holder.once("exit", () => reject(new Error("lock holder exited"))); });
    const blockedClient = open({ env: { OSB_PI_TOOL_DEADLINE_MS: "1000" } });
    await blockedClient.initialize();
    const waitStart = Date.now();
    const waited = structured(await blockedClient.callTool("wiki_batch_recover", { planHash: interruptedHash }));
    assert.ok(Date.now() - waitStart < 10_000, "recover did not hang on the held lock");
    assert.equal(waited.ok, false);
    assert.equal(waited.code, "timeout");
    await blockedClient.close();
  } finally {
    holder.kill("SIGKILL");
  }
  assert.equal(fs.readFileSync(path.join(vault, interrupted.path), "utf8"), interrupted.content, "the timed-out recover changed nothing");
  const recovering = open();
  await recovering.initialize();
  const doctor = structured(await recovering.callTool("wiki_doctor", {})).payload;
  assert.equal(doctor.batches.unfinishedCount, 1, "wiki_doctor reports the unfinished batch");
  assert.equal(doctor.batches.unfinished[0].planHash, interruptedHash);
  assert.deepEqual(doctor.batches.unfinished[0].paths, [interrupted.path]);
  assert.notEqual(doctor.batches.unfinished[0].phase, "complete");
  const recovered = structured(await recovering.callTool("wiki_batch_recover", { planHash: interruptedHash }));
  assert.equal(recovered.ok, true, JSON.stringify(recovered));
  assert.equal(recovered.payload.phase, "complete");
  assert.equal(recovered.payload.batchId, doctor.batches.unfinished[0].batchId);
  assert.match(fs.readFileSync(path.join(vault, "wiki/topic/index.md"), "utf8"), /interrupted/i, "recovery finished lifecycle finalization");
  assert.equal(structured(await recovering.callTool("wiki_doctor", {})).payload.batches.unfinishedCount, 0, "no unfinished batch remains");
  assert.deepEqual(structured(await recovering.callTool("wiki_batch_recover", { batchId: recovered.payload.batchId })).payload.noop, true, "recover by batchId matches the CLI semantics");
  assert.equal(cli("batch-apply", [interrupted], "--plan-hash", interruptedHash).noop, true);
  await recovering.close();

  // A conflicting edit made after an interruption blocks recovery and is preserved.
  const conflicted = { path: "wiki/topic/conflicted.md", expectedHash: null, content: page("Conflicted", "Approved text.\n") };
  const conflictedHash = await prepare([conflicted], 60);
  await cancelDuringPublish([conflicted], conflictedHash);
  fs.writeFileSync(path.join(vault, conflicted.path), page("Conflicted", "Someone else edited this.\n"));
  const again = open();
  await again.initialize();
  const blocked = await again.callTool("wiki_batch_recover", { planHash: conflictedHash });
  await again.close();
  assert.equal(blocked.result.isError, true);
  assert.match(structured(blocked).message, /recovery preflight conflict|preserving/);
  assert.equal(fs.readFileSync(path.join(vault, conflicted.path), "utf8"), page("Conflicted", "Someone else edited this.\n"), "the conflicting edit is preserved");

  // No Git writes: the workflow only queried Git; nothing was staged or committed.
  const gitCommands = fs.existsSync(gitLog) ? new Set(fs.readFileSync(gitLog, "utf8").split("\n").filter(Boolean)) : new Set();
  for (const forbidden of ["add", "commit", "pull", "push", "stash", "reset", "checkout", "rm", "merge", "rebase", "fetch", "config", "init"]) {
    assert.ok(!gitCommands.has(forbidden), `git ${forbidden} was never run (saw ${[...gitCommands]})`);
  }
  assert.equal(git("diff", "--cached", "--name-only"), "");
  assert.throws(() => git("rev-parse", "--verify", "HEAD"), "no commit was created");
  console.log("MCP batch PASS: all-profile surface, inspect summary and CLI plan-hash parity, stale/secret/protected-path refusals, size cap, deadline lift, cancellation and recovery, no Git writes.");
} finally {
  for (const client of clients) { try { client.child.kill("SIGKILL"); } catch { /* already exited */ } }
  fs.rmSync(tmp, { recursive: true, force: true });
}
