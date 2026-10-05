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
	if (typeof rawPath !== "string" || !knownVault || !rawPath.toLowerCase().endsWith(".md")) return null;
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

async function runHook(sub: string, cwd: string, args: string[] = [], vaultOverride = knownVault): Promise<{ output: string; error?: string }> {
	if (!HOOK) return { output: "", error: "hook is not configured" };
	if (!fs.existsSync(HOOK)) return { output: "", error: `hook is missing at ${HOOK}` };
	try {
		const { stdout } = await execFileAsync("bash", [HOOK, sub, ...args], {
			cwd,
			env: { ...process.env, OBSIDIAN_VAULT_PATH: vaultOverride ?? "" },
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
	const pendingWikiWrites = new Map<string, { owner: string; toolId: string; vault: string; path: string }>();
	const ownedVaultsBySession = new Map<string, Set<string>>();
	const pendingKey = (owner: string, toolId: string) => JSON.stringify([owner, toolId]);
	const sessionQueuePrefix = (owner: string) => JSON.stringify([owner]).slice(0, -1) + ",";
	const sessionIdentity = (ctx: { sessionManager?: { getSessionId?: () => string } }) => {
		const id = ctx.sessionManager?.getSessionId?.() ?? "";
		return id ? `pi:${id}` : "";
	};
	const rememberVault = (owner: string, vault: string) => {
		if (!owner) return;
		const vaults = ownedVaultsBySession.get(owner) ?? new Set<string>();
		vaults.add(vault);
		ownedVaultsBySession.set(owner, vaults);
	};
	const postwriteQueues = new Map<string, Promise<void>>();
	const pendingSyncFailures = new Map<string, string>();
	let lastConfigDiagnostic: string | null = null;

	pi.on("session_start", async (_event, ctx) => {
		await refreshConfig();
		if (configError) ctx.ui.notify(`obsidian configuration needs repair: ${configError}`, "warning");
		if (!knownVault) return;
		rememberVault(sessionIdentity(ctx), knownVault);
		const result = await runHook("start", ctx.cwd);
		toc = result.output.trim();
		if (result.error) ctx.ui.notify(`obsidian start hook failed: ${result.error}`, "warning");
	});

	pi.on("before_agent_start", async (_event, ctx) => {
		await refreshConfig();
		if (configError && knownVault) {
			const diagnostic = configDiagnosticKey();
			if (diagnostic === lastConfigDiagnostic) return;
			lastConfigDiagnostic = diagnostic;
			return { message: { customType: "obsidian-config-diagnostic", content: `Obsidian wiki integration is fail-closed: ${configError}. Do not resume wiki writes. Propose the exact minimal changes to ${configPath}, preserving unrelated fields. Ask the user to approve those exact changes before editing; after approval edit only this selected config, then revalidate it in this session. Do not reset settings or enable autoCommit.`, display: false } };
		}
		if (!configError) lastConfigDiagnostic = null;
		if (injected || toc.length === 0) return;
		injected = true;
		return { message: { customType: "obsidian-toc", content: "Obsidian wiki locator (load wiki/WIKI.md and relevant wiki/index.md only when this task needs the wiki):\n\n" + toc, display: false } };
	});

	pi.on("tool_call", async (event, ctx) => {
		const owner = sessionIdentity(ctx);
		await refreshConfig();
		const isWrite = event.toolName === "write" || event.toolName === "edit";
		const isRead = event.toolName === "read";
		const eventVault = knownVault;
		if (eventVault) rememberVault(owner, eventVault);
		const readPath = typeof event.input.path === "string" && eventVault
			? canonicalReadPath(event.input.path, ctx.cwd)
			: null;
		const isNavigationRead = Boolean(isRead && readPath && eventVault
			&& readPath.startsWith(path.join(eventVault, "wiki") + path.sep)
			&& path.basename(readPath).toLowerCase() === "index.md");
		let pendingId: string | undefined;
		let candidate: string | null = null;
		if (isNavigationRead && eventVault) {
			if (!owner) return { block: true, reason: "Obsidian reader session identity is unavailable; refusing to reconcile writer captures" };
			// Let concurrently dispatched nested tool hooks register pending writes.
			await new Promise<void>((resolve) => setTimeout(resolve, 0));
			if ([...pendingWikiWrites.values()].some((entry) => entry.vault === eventVault)) {
				return { block: true, reason: "wiki tool write is still in flight; retry navigation after its writer settles" };
			}
			const syncFailure = pendingSyncFailures.get(eventVault);
			if (syncFailure) {
				return { block: true, reason: syncFailure };
			}
			const finalized = await runHook("finalize", ctx.cwd, ["--owner", owner], eventVault);
			if (finalized.error || (finalized.output !== "synced" && finalized.output !== "clean")) {
				const reason = finalized.error ?? `Unexpected finalization result: ${finalized.output}`;
				pendingSyncFailures.set(eventVault, reason);
				return { block: true, reason };
			}
			pendingSyncFailures.delete(eventVault);
			touchedByVault.delete(eventVault);
		}
		if (isWrite && eventVault) {
			candidate = wikiWriteCandidate(event.input.path, ctx.cwd);
			if (candidate) {
				if (!owner || typeof event.toolCallId !== "string" || !event.toolCallId) {
					return { block: true, reason: "Obsidian writer session/tool identity is unavailable; refusing an untracked wiki write" };
				}
				pendingId = pendingKey(owner, event.toolCallId);
				pendingWikiWrites.set(pendingId, { owner, toolId: event.toolCallId, vault: eventVault, path: candidate });
				// Pi prepares all calls before executing a parallel batch. Never wait
				// here for another call's result; lifecycle hooks lock their own state.
			}
		}
		await refreshConfig();
		if (configError && isWrite && eventVault && await selectedConfigRepairTarget(event.input.path, ctx.cwd)) return;
		if (pendingId && eventVault !== knownVault) {
			pendingWikiWrites.delete(pendingId);
			return { block: true, reason: "Obsidian vault boundary changed during write; retry after revalidation" };
		}
		if (!isWrite || !knownVault) return;
		const rawPath = event.input.path;
		if (typeof rawPath !== "string" || !rawPath) {
			if (pendingId) pendingWikiWrites.delete(pendingId);
			return { block: true, reason: "obsidian write path is missing" };
		}
		if (configError) {
			const target = canonicalPotentialPath(path.resolve(ctx.cwd, rawPath));
			if (target.startsWith(path.join(knownVault, "wiki") + path.sep)
				&& !(await selectedConfigRepairTarget(rawPath, ctx.cwd))) {
				if (pendingId) pendingWikiWrites.delete(pendingId);
				return { block: true, reason: `Obsidian configuration needs repair: ${configError}` };
			}
		}
		if (!configError && config.features?.guard === false) {
			if (candidate) {
				const captured = await runHook("prewrite-capture", ctx.cwd, [rawPath, "--owner", owner, "--tool", event.toolCallId], eventVault);
				if (captured.error) {
					pendingWikiWrites.delete(pendingId!);
					return { block: true, reason: captured.error };
				}
			}
			return;
		}
		const result = await runHook("guard", ctx.cwd, [rawPath]);
		if (result.error) {
			if (pendingId) pendingWikiWrites.delete(pendingId);
			return { block: true, reason: result.error };
		}
		if (candidate) {
			const captured = await runHook("prewrite-capture", ctx.cwd, [rawPath, "--owner", owner, "--tool", event.toolCallId], eventVault);
			if (captured.error) {
				if (pendingId) pendingWikiWrites.delete(pendingId);
				return { block: true, reason: captured.error };
			}
		}
	});

	pi.on("tool_result", async (event, ctx) => {
		const currentOwner = sessionIdentity(ctx);
		const captured = [...pendingWikiWrites.entries()].filter(([, entry]) => entry.toolId === event.toolCallId);
		const pendingId = captured.length === 1 ? captured[0][0] : pendingKey(currentOwner, event.toolCallId);
		const pending = pendingWikiWrites.get(pendingId);
		const eventVault = pending?.vault ?? knownVault;
		// A failed tool may have written partial bytes. Retain its capture until
		// agent_end can safely recover and validate it after all tools settle.
		if (event.isError && pending) return;
		if (event.isError || (event.toolName !== "write" && event.toolName !== "edit") || !eventVault) {
			if (pending) pendingWikiWrites.delete(pendingId);
			return;
		}
		const touchedPath = pending?.path ?? touchedWikiPath(event.input.path, ctx.cwd) ?? null;
		if (!touchedPath) {
			if (pending) pendingWikiWrites.delete(pendingId);
			return;
		}
		const touched = touchedByVault.get(eventVault) ?? new Set<string>();
		touched.add(touchedPath);
		touchedByVault.set(eventVault, touched);
		await refreshConfig();
		if (configError) {
			const reason = configError ?? "vault boundary changed";
			pendingSyncFailures.set(eventVault, reason);
			ctx.ui.notify(`obsidian post-write sync failed; path retained for retry: ${reason}`, "warning");
			return;
		}
		try {
			const queueKey = JSON.stringify([pending?.owner ?? currentOwner, eventVault]);
			const queue = postwriteQueues.get(queueKey) ?? Promise.resolve();
			const queued = queue.then(() => {
				return touched.size > 0
					? runHook("postwrite", ctx.cwd, [touchedPath, "--owner", pending?.owner ?? currentOwner, "--tool", pending?.toolId ?? event.toolCallId], eventVault)
					: { output: "clean" };
			});
			postwriteQueues.set(queueKey, queued.then(() => undefined, () => undefined));
			const result = await queued;
			if (result.error || (result.output !== "pending" && result.output !== "clean")) {
				const reason = result.error ?? `Unexpected post-write result: ${result.output}`;
				pendingSyncFailures.set(eventVault, reason);
				ctx.ui.notify(`obsidian post-write validation/sync failed; path retained for retry: ${reason}`, "warning");
			} else {
				pendingSyncFailures.delete(eventVault);
			}
		} finally {
			if (pending) pendingWikiWrites.delete(pendingId);
		}
	});

	pi.on("turn_end", async (_event, ctx) => {
		const owner = sessionIdentity(ctx);
		if (!owner) {
			ctx.ui.notify("obsidian wiki finalization skipped because the writer session identity is unavailable", "warning");
			return;
		}
		await Promise.all([...postwriteQueues.entries()]
			.filter(([key]) => key.startsWith(sessionQueuePrefix(owner)))
			.map(([, queue]) => queue));
		await refreshConfig();
		if (configError || !knownVault) return;
		if ([...pendingWikiWrites.values()].some((entry) => entry.owner === owner && entry.vault === knownVault)) {
			ctx.ui.notify("obsidian turn ended with an unsettled wiki tool capture; dependent navigation remains blocked until agent-end recovery", "warning");
			return;
		}
		const touched = touchedByVault.get(knownVault);
		if (!touched || touched.size === 0) return;
		const pending = [...touched];
		const result = await runHook("finalize", ctx.cwd, ["--owner", owner], knownVault);
		if (result.error) {
			ctx.ui.notify(`obsidian wiki finalization failed; touched paths retained: ${result.error}`, "warning");
			return;
		}
		if (result.output !== "synced" && result.output !== "clean") {
			ctx.ui.notify("obsidian did not settle touched paths; changes retained", "warning");
			return;
		}
		pendingSyncFailures.delete(knownVault);
		for (const touchedPath of pending) touched.delete(touchedPath);
		if (touched.size === 0) touchedByVault.delete(knownVault);
	});

	pi.on("agent_end", async (_event, ctx) => {
		const owner = sessionIdentity(ctx);
		if (!owner) return;
		// Unlike tool_call or an abort signal, agent_end runs after all tool
		// executions/results have settled, so missing callbacks are recoverable.
		await Promise.all([...postwriteQueues.entries()]
			.filter(([key]) => key.startsWith(sessionQueuePrefix(owner)))
			.map(([, queue]) => queue));
		const vaults = new Set<string>();
		for (const [key, entry] of pendingWikiWrites) {
			if (entry.owner === owner) {
				vaults.add(entry.vault);
				pendingWikiWrites.delete(key);
			}
		}
		for (const vault of vaults) {
			const result = await runHook("finalize", ctx.cwd, ["--recover-owner", owner], vault);
			if (result.error || (result.output !== "synced" && result.output !== "clean")) {
				const reason = result.error ?? `Unexpected recovery result: ${result.output}`;
				pendingSyncFailures.set(vault, reason);
				ctx.ui.notify(`obsidian agent-end recovery failed; changes retained: ${reason}`, "warning");
			} else {
				pendingSyncFailures.delete(vault);
				touchedByVault.delete(vault);
			}
		}
	});

	pi.on("session_shutdown", async (_event, ctx) => {
		const owner = sessionIdentity(ctx);
		if (!owner) {
			ctx.ui.notify("obsidian shutdown recovery skipped because the writer session identity is unavailable", "warning");
			return;
		}
		// Pi emits shutdown after abort/idle settlement; wait for postwrite
		// callback queues, then recover only this session's captures that lack a result.
		await Promise.all([...postwriteQueues.entries()]
			.filter(([key]) => key.startsWith(sessionQueuePrefix(owner)))
			.map(([, queue]) => queue));
		for (const [key, entry] of pendingWikiWrites) {
			if (entry.owner === owner) {
				pendingWikiWrites.delete(key);
			}
		}
		await refreshConfig();
		const vaults = ownedVaultsBySession.get(owner) ?? new Set(knownVault ? [knownVault] : []);
		for (const vault of vaults) {
			const unsettled = touchedByVault.get(vault)?.size ?? 0;
			if (unsettled > 0) {
				ctx.ui.notify(`obsidian shutdown with ${unsettled} unsettled touched path(s)`, "warning");
			}
			const result = await runHook("stop", ctx.cwd, ["--recover-owner", owner], vault);
			if (result.error) ctx.ui.notify(`obsidian convergence check failed: ${result.error}`, "warning");
		}
		ownedVaultsBySession.delete(owner);
	});
}
