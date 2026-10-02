import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

const configPath = path.resolve(process.env.OBSIDIAN_AGENT_CONFIG?.trim() || path.join(os.homedir(), ".config/obsidian-second-brain/properties.json"));
type Config = { vaultPath?: string | null; features?: { guard?: boolean | null; autoCommit?: boolean | null; toc?: boolean | null; retrievalRefresh?: boolean | null } };
let config: Config = {};
let configError: string | null = null;
let knownVault: string | null = null;
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CONFIG_HELPER = path.join(REPO, "scripts/config_contract.py");
const execFileAsync = promisify(execFile);
async function selectedConfigRepairTarget(rawPath: unknown, cwd: string): Promise<boolean> {
	if (typeof rawPath !== "string" || !configError) return false;
	try {
		const { stdout } = await execFileAsync("python3", [CONFIG_HELPER, configPath, "--repair-target", rawPath, "--cwd", cwd], { encoding: "utf8" });
		return (JSON.parse(stdout) as { repairTarget?: boolean }).repairTarget === true;
	} catch {
		return false;
	}
}

function configDiagnosticKey(): string {
	let configState = "unavailable";
	try {
		const stat = fs.lstatSync(configPath);
		configState = stat.isFile()
			? createHash("sha256").update(fs.readFileSync(configPath)).digest("hex")
			: `${stat.mode}:${stat.size}:${stat.mtimeMs}`;
	} catch {
		// The diagnostic itself still identifies load failures; missing/unreadable
		// selected configs share a key until their state changes.
	}
	return createHash("sha256").update(JSON.stringify([configError, knownVault, configState])).digest("hex");
}

async function refreshConfig(): Promise<void> {
	const invocationOverride = process.env.OBSIDIAN_VAULT_PATH?.trim() || null;
	try {
		const { stdout } = await execFileAsync("python3", [CONFIG_HELPER, configPath], { encoding: "utf8" });
		const result = JSON.parse(stdout) as { config: Config | null; error: string | null };
		if (!result.error) {
			config = result.config ?? {};
			const selected = invocationOverride ?? resolveHome(config.vaultPath);
			knownVault = canonicalDirectory(selected);
			configError = null;
		} else {
			configError = result.error;
			const possible = result.config?.vaultPath;
			knownVault = canonicalDirectory(invocationOverride ?? resolveHome(possible) ?? knownVault);
		}
	} catch (err) {
		configError = `cannot load ${configPath}: ${(err as Error).message}`;
		knownVault = canonicalDirectory(invocationOverride ?? knownVault);
	}
}

function resolveHome(p: string | undefined | null): string | null {
	if (!p) return null;
	if (p.startsWith("~/")) return path.join(os.homedir(), p.slice(2));
	return p;
}

function canonicalDirectory(p: string | null): string | null {
	if (!p) return null;
	try {
		return fs.realpathSync(p);
	} catch {
		return null;
	}
}

const HOOK = path.join(REPO, "hooks/obsidian-session.sh");

function canonicalPath(p: string, cwd: string): string | null {
	try {
		return fs.realpathSync(path.resolve(cwd, p));
	} catch {
		return null;
	}
}

function canonicalReadPath(p: string, cwd: string): string {
	const target = path.resolve(cwd, p);
	return canonicalPath(p, cwd) ?? path.join(canonicalDirectory(path.dirname(target)) ?? path.dirname(target), path.basename(target));
}

function canonicalPotentialPath(target: string): string {
	let existing = target;
	const missing: string[] = [];
	while (!fs.existsSync(existing)) {
		const parent = path.dirname(existing);
		if (parent === existing) return target;
		missing.unshift(path.basename(existing));
		existing = parent;
	}
	const canonical = canonicalDirectory(existing);
	return canonical ? path.join(canonical, ...missing) : target;
}

function isInsideVault(p: string): boolean {
	if (!knownVault) return false;
	const canonical = canonicalDirectory(p);
	return canonical === knownVault || canonical?.startsWith(knownVault + path.sep) === true;
}

function wikiWriteCandidate(rawPath: unknown, cwd: string): string | null {
	if (typeof rawPath !== "string" || !knownVault) return null;
	const target = canonicalPotentialPath(path.resolve(cwd, rawPath));
	const wiki = path.join(knownVault, "wiki") + path.sep;
	if (!target.startsWith(wiki) || !target.toLowerCase().endsWith(".md")) return null;
	const name = path.basename(target).toLowerCase();
	if (name === "index.md" || name === "log.md" || name === "_plan.md") return null;
	return target;
}

function touchedWikiPath(rawPath: unknown, cwd: string): string | null {
	if (typeof rawPath !== "string" || !knownVault) return null;
	const target = canonicalPath(rawPath, cwd);
	const wiki = path.join(knownVault, "wiki") + path.sep;
	if (!target || !target.startsWith(wiki)) return null;
	try {
		if (!fs.statSync(target).isFile()) return null;
	} catch {
		return null;
	}
	const name = path.basename(target).toLowerCase();
	if (name === "index.md" || name === "log.md" || name === "_plan.md") return null;
	return target;
}

async function runHook(sub: string, cwd: string, args: string[] = []): Promise<{ output: string; error?: string }> {
	if (configError) return { output: "", error: configError };
	if (!HOOK) return { output: "", error: "hook is not configured" };
	if (!fs.existsSync(HOOK)) return { output: "", error: `hook is missing at ${HOOK}` };
	try {
		const { stdout } = await execFileAsync("bash", [HOOK, sub, ...args], {
			cwd,
			env: { ...process.env, OBSIDIAN_VAULT_PATH: knownVault ?? "" },
			encoding: "utf8",
			timeout: 15000,
		});
		return { output: stdout.trim() };
	} catch (err: unknown) {
		const result = err as { stdout?: unknown; stderr?: unknown; message?: unknown };
		const detail = [result.stdout, result.stderr].filter((v): v is string => typeof v === "string" && v.trim() !== "").join(" ").trim();
		return {
			output: detail,
			error: detail || (typeof result.message === "string" ? result.message : "hook failed"),
		};
	}
}

export default function (pi: ExtensionAPI) {
	let toc = "";
	let injected = false;
	const touchedByVault = new Map<string, Set<string>>();
	const pendingWikiWrites = new Map<string, { vault: string; done: Promise<boolean>; release: (synced: boolean) => void }>();
	const postwriteQueues = new Map<string, Promise<void>>();
	const pendingSyncFailures = new Map<string, string>();
	let lastConfigDiagnostic: string | null = null;

	pi.on("session_start", async (_event, ctx) => {
		await refreshConfig();
		if (configError) ctx.ui.notify(`obsidian configuration needs repair: ${configError}`, "warning");
		if (!knownVault || !isInsideVault(ctx.cwd) || configError) return;
		const result = await runHook("start", knownVault);
		toc = result.output.trim();
		if (result.error) ctx.ui.notify(`obsidian start hook failed: ${result.error}`, "warning");
	});

	pi.on("before_agent_start", async (_event, ctx) => {
		await refreshConfig();
		if (configError && knownVault && isInsideVault(ctx.cwd)) {
			const diagnostic = configDiagnosticKey();
			if (diagnostic === lastConfigDiagnostic) return;
			lastConfigDiagnostic = diagnostic;
			return { message: { customType: "obsidian-config-diagnostic", content: `Obsidian wiki integration is fail-closed: ${configError}. Do not resume wiki writes. Propose the exact minimal changes to ${configPath}, preserving unrelated fields. Ask the user to approve those exact changes before editing; after approval edit only this selected config, then revalidate it in this session. Do not reset settings or enable autoCommit.`, display: false } };
		}
		if (!configError) lastConfigDiagnostic = null;
		if (injected || toc.length === 0 || !isInsideVault(ctx.cwd)) return;
		injected = true;
		return { message: { customType: "obsidian-toc", content: "Obsidian wiki table of contents (auto-loaded):\n\n" + toc, display: false } };
	});

	pi.on("tool_call", async (event, ctx) => {
		const isWrite = event.toolName === "write" || event.toolName === "edit";
		const isRead = event.toolName === "read";
		const eventVault = knownVault && isInsideVault(ctx.cwd) ? knownVault : null;
		const readPath = typeof event.input.path === "string" && eventVault
			? canonicalReadPath(event.input.path, ctx.cwd)
			: null;
		const isNavigationRead = Boolean(isRead && readPath && eventVault
			&& readPath.startsWith(path.join(eventVault, "wiki") + path.sep)
			&& path.basename(readPath).toLowerCase() === "index.md");
		let pendingBefore = [...pendingWikiWrites.values()].filter((entry) => entry.vault === eventVault).map((entry) => entry.done);
		let pendingId: string | undefined;
		if (isNavigationRead && eventVault) {
			// Let sibling tool_call hooks in this parallel batch register pending writes.
			await new Promise<void>((resolve) => setTimeout(resolve, 0));
			pendingBefore = [...pendingWikiWrites.values()].filter((entry) => entry.vault === eventVault).map((entry) => entry.done);
			const outcomes = await Promise.all(pendingBefore);
			const syncFailure = pendingSyncFailures.get(eventVault);
			if (outcomes.some((synced) => !synced) || syncFailure) {
				return { block: true, reason: syncFailure ?? "wiki page changed but its generated indexes did not sync; retry lifecycle before reading" };
			}
		}
		if (isWrite && eventVault) {
			const candidate = wikiWriteCandidate(event.input.path, ctx.cwd);
			if (candidate && typeof event.toolCallId === "string" && event.toolCallId) {
				let release!: (synced: boolean) => void;
				const done = new Promise<boolean>((resolve) => { release = resolve; });
				pendingId = event.toolCallId;
				pendingWikiWrites.set(pendingId, { vault: eventVault, done, release });
				await Promise.all(pendingBefore);
			}
		}
		await refreshConfig();
		if (configError && isWrite && eventVault && await selectedConfigRepairTarget(event.input.path, ctx.cwd)) return;
		if (pendingId && eventVault !== knownVault) {
			pendingWikiWrites.get(pendingId)?.release(false);
			pendingWikiWrites.delete(pendingId);
			return { block: true, reason: "Obsidian vault boundary changed during write; retry after revalidation" };
		}
		if ((!configError && config.features?.guard === false) || !isWrite || !isInsideVault(ctx.cwd)) return;
		const rawPath = event.input.path;
		if (typeof rawPath !== "string" || !rawPath) {
			if (pendingId) { pendingWikiWrites.get(pendingId)?.release(true); pendingWikiWrites.delete(pendingId); }
			return { block: true, reason: "obsidian write path is missing" };
		}
		const result = await runHook("guard", ctx.cwd, [rawPath]);
		if (result.error) {
			if (pendingId) { pendingWikiWrites.get(pendingId)?.release(true); pendingWikiWrites.delete(pendingId); }
			return { block: true, reason: result.error };
		}
	});

	pi.on("tool_result", async (event, ctx) => {
		const pending = pendingWikiWrites.get(event.toolCallId);
		const eventVault = pending?.vault ?? (knownVault && isInsideVault(ctx.cwd) ? knownVault : null);
		if (event.isError || (event.toolName !== "write" && event.toolName !== "edit") || !eventVault) {
			if (pending) { pending.release(true); pendingWikiWrites.delete(event.toolCallId); }
			return;
		}
		const touchedPath = touchedWikiPath(event.input.path, ctx.cwd);
		if (!touchedPath) {
			if (pending) { pending.release(true); pendingWikiWrites.delete(event.toolCallId); }
			return;
		}
		const touched = touchedByVault.get(eventVault) ?? new Set<string>();
		touched.add(touchedPath);
		touchedByVault.set(eventVault, touched);
		await refreshConfig();
		if (configError || knownVault !== eventVault || !isInsideVault(ctx.cwd)) {
			const reason = configError ?? "vault boundary changed";
			pendingSyncFailures.set(eventVault, reason);
			ctx.ui.notify(`obsidian post-write sync failed; path retained for retry: ${reason}`, "warning");
			if (pending) { pending.release(false); pendingWikiWrites.delete(event.toolCallId); }
			return;
		}
		try {
			const queue = postwriteQueues.get(eventVault) ?? Promise.resolve();
			const queued = queue.then(() => {
				for (const changedPath of touched) {
					try { fs.statSync(changedPath); } catch { touched.delete(changedPath); }
				}
				return touched.size > 0
					? runHook("postwrite", eventVault, [...touched])
					: { output: "synced" };
			});
			postwriteQueues.set(eventVault, queued.then(() => undefined, () => undefined));
			const result = await queued;
			if (result.error || result.output !== "synced") {
				const reason = result.error ?? `Unexpected post-write result: ${result.output}`;
				pendingSyncFailures.set(eventVault, reason);
				ctx.ui.notify(`obsidian post-write validation/sync failed; path retained for retry: ${reason}`, "warning");
			} else {
				pendingSyncFailures.delete(eventVault);
			}
		} finally {
			if (pending) {
				pending.release(!pendingSyncFailures.has(eventVault));
				pendingWikiWrites.delete(event.toolCallId);
			}
		}
	});

	pi.on("turn_end", async (_event, ctx) => {
		await refreshConfig();
		if (configError || !isInsideVault(ctx.cwd) || !knownVault) return;
		const touched = touchedByVault.get(knownVault);
		if (!touched || touched.size === 0) return;
		// Prune paths deleted since they were tracked (e.g. _plan.md removed mid-turn).
		for (const p of touched) {
			try { fs.statSync(p); } catch { touched.delete(p); }
		}
		if (touched.size === 0) return;
		const pending = [...touched];
		const result = await runHook("autocommit", knownVault, pending);
		if (result.error) {
			ctx.ui.notify(`obsidian autocommit failed; touched paths retained: ${result.error}`, "warning");
			return;
		}
		if (result.output !== "committed" && result.output !== "clean" && result.output !== "disabled") {
			ctx.ui.notify("obsidian did not settle touched paths; changes retained", "warning");
			return;
		}
		pendingSyncFailures.delete(knownVault);
		for (const touchedPath of pending) touched.delete(touchedPath);
		if (touched.size === 0) touchedByVault.delete(knownVault);
	});

	pi.on("session_shutdown", async (_event, ctx) => {
		await refreshConfig();
		if (!isInsideVault(ctx.cwd) || !knownVault) return;
		const unsettled = touchedByVault.get(knownVault)?.size ?? 0;
		if (unsettled > 0) {
			ctx.ui.notify(`obsidian shutdown with ${unsettled} unsettled touched path(s)`, "warning");
		}
		const result = await runHook("stop", knownVault);
		if (result.error) ctx.ui.notify(`obsidian convergence check failed: ${result.error}`, "warning");
	});
}
