import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

const configPath = process.env.OBSIDIAN_AGENT_CONFIG ?? path.join(os.homedir(), ".config/obsidian-second-brain/properties.json");
let config: { vaultPath?: string | null; features?: { guard?: boolean; autoCommit?: boolean } } = {};
let configError: string | null = null;
try {
	config = JSON.parse(fs.readFileSync(configPath, "utf8"));
} catch (err) {
	if ((err as NodeJS.ErrnoException).code !== "ENOENT") configError = `cannot load ${configPath}: ${(err as Error).message}`;
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

const HOOK = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../hooks/obsidian-session.sh");
const VAULT_PATH = canonicalDirectory(resolveHome(config.vaultPath));
const execFileAsync = promisify(execFile);

function canonicalPath(p: string, cwd: string): string | null {
	try {
		return fs.realpathSync(path.resolve(cwd, p));
	} catch {
		return null;
	}
}

function isInsideVault(p: string): boolean {
	if (!VAULT_PATH) return false;
	const canonical = canonicalDirectory(p);
	return canonical === VAULT_PATH || canonical?.startsWith(VAULT_PATH + path.sep) === true;
}

function touchedWikiPath(rawPath: unknown, cwd: string): string | null {
	if (typeof rawPath !== "string" || !VAULT_PATH) return null;
	const target = canonicalPath(rawPath, cwd);
	const wiki = path.join(VAULT_PATH, "wiki") + path.sep;
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
			env: { ...process.env, OBSIDIAN_VAULT_PATH: VAULT_PATH ?? "" },
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
	const touched = new Set<string>();

	pi.on("session_start", async (_event, ctx) => {
		if (!VAULT_PATH || !isInsideVault(ctx.cwd)) return;
		const result = await runHook("start", VAULT_PATH);
		toc = result.output.trim();
		if (result.error) ctx.ui.notify(`obsidian start hook failed: ${result.error}`, "warning");
	});

	pi.on("before_agent_start", async (_event, ctx) => {
		if (injected || toc.length === 0 || !isInsideVault(ctx.cwd)) return;
		injected = true;
		return {
			message: {
				customType: "obsidian-toc",
				content: "Obsidian wiki table of contents (auto-loaded):\n\n" + toc,
				display: false,
			},
		};
	});

	pi.on("tool_call", async (event, ctx) => {
		if (config.features?.guard === false || (event.toolName !== "write" && event.toolName !== "edit") || !isInsideVault(ctx.cwd)) return;
		const rawPath = event.input.path;
		if (typeof rawPath !== "string" || !rawPath) return { block: true, reason: "obsidian write path is missing" };
		const result = await runHook("guard", ctx.cwd, [rawPath]);
		if (result.error) return { block: true, reason: result.error };
	});

	pi.on("tool_result", async (event, ctx) => {
		if (event.isError || (event.toolName !== "write" && event.toolName !== "edit") || !isInsideVault(ctx.cwd)) return;
		const touchedPath = touchedWikiPath(event.input.path, ctx.cwd);
		if (touchedPath) touched.add(touchedPath);
	});

	pi.on("turn_end", async (_event, ctx) => {
		if (config.features?.autoCommit === false || !isInsideVault(ctx.cwd) || touched.size === 0 || !VAULT_PATH) return;
		// Prune paths deleted since they were tracked (e.g. _plan.md removed mid-turn).
		for (const p of touched) {
			try { fs.statSync(p); } catch { touched.delete(p); }
		}
		if (touched.size === 0) return;
		const pending = [...touched];
		const result = await runHook("autocommit", VAULT_PATH, pending);
		if (result.error) {
			ctx.ui.notify(`obsidian autocommit failed; touched paths retained: ${result.error}`, "warning");
			return;
		}
		if (result.output !== "committed" && result.output !== "clean" && result.output !== "disabled") {
			ctx.ui.notify("obsidian did not settle touched paths; changes retained", "warning");
			return;
		}
		for (const touchedPath of pending) touched.delete(touchedPath);
	});

	pi.on("session_shutdown", async (_event, ctx) => {
		if (!isInsideVault(ctx.cwd) || !VAULT_PATH) return;
		if (touched.size > 0) {
			ctx.ui.notify(`obsidian shutdown with ${touched.size} unsettled touched path(s)`, "warning");
		}
		const result = await runHook("stop", VAULT_PATH);
		if (result.error) ctx.ui.notify(`obsidian convergence check failed: ${result.error}`, "warning");
	});
}
