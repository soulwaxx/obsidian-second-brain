import { randomUUID } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import { installedPackage } from "../../tests/pi-runtime-paths.mjs";

const INTERNAL_AUTH_SENTINEL = "sk-osb-native-dispatch-sentinel-not-a-credential";

/** Build an isolated SDK runtime whose public streaming calls delegate to Pi's active registry. */
export function preflightNativeProvider({ hostRegistry, baselineRegistry, models }) {
  if (!hostRegistry?.getProvider || !hostRegistry?.getRegisteredNativeProvider || !hostRegistry?.getRegisteredProviderConfig || !baselineRegistry?.getProvider || !baselineRegistry?.getModel || !Array.isArray(models) || models.length === 0) {
    throw new Error("native-provider-bridge-unavailable");
  }
  const providerIds = new Set(models.map((model) => model?.provider));
  if (providerIds.size !== 1 || providerIds.has(undefined)) throw new Error("native-provider-bridge-requires-one-provider");
  const providerId = [...providerIds][0];
  const hostProvider = hostRegistry.getProvider(providerId);
  const baselineProvider = baselineRegistry.getProvider(providerId);
  if (hostRegistry.getRegisteredNativeProvider(providerId) || hostRegistry.getRegisteredProviderConfig(providerId) ||
    !hostProvider?.streamSimple || !baselineProvider?.streamSimple ||
    hostProvider.auth?.apiKey?.check !== baselineProvider.auth?.apiKey?.check ||
    models.some((model) => model.provider !== providerId || model.api !== "openai-responses" ||
      JSON.stringify(model) !== JSON.stringify(baselineRegistry.getModel(model.provider, model.id)))) {
    throw new Error("unsupported-native-provider-route");
  }
  return { providerId, hostProvider };
}

export async function createNativeProviderRuntime({ sdk, AuthStorage, hostRegistry, models }) {
  if (!sdk?.ModelRuntime || !AuthStorage?.inMemory) throw new Error("native-provider-bridge-unavailable");
  const baselineRuntime = await sdk.ModelRuntime.create({ credentials: AuthStorage.inMemory(), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
  let providerId;
  let hostProvider;
  try { ({ providerId, hostProvider } = preflightNativeProvider({ hostRegistry, baselineRegistry: baselineRuntime, models })); }
  finally { baselineRuntime.dispose?.(); }
  const runtime = await sdk.ModelRuntime.create({
    credentials: AuthStorage.inMemory({ [providerId]: { type: "api_key", key: INTERNAL_AUTH_SENTINEL } }),
    modelsPath: null,
    refreshOnCreate: false,
    allowModelNetwork: false,
  });
  const selected = [...new Map(models.map((model) => [`${model.provider}/${model.id}`, model])).values()];
  const delegateOptions = (options = {}) => {
    const { apiKey, headers, env, baseUrl, transformHeaders, ...publicOptions } = options;
    if (apiKey !== INTERNAL_AUTH_SENTINEL) throw new Error("native-provider-auth-boundary-violation");
    return publicOptions;
  };
  const delegate = {
    ...hostProvider,
    getModels: () => selected,
    streamSimple(model, context, options) {
      return hostRegistry.streamSimple(model, context, delegateOptions(options));
    },
    stream(model, context, options) {
      if (typeof hostRegistry.stream !== "function") throw new Error("native-provider-stream-unavailable");
      return hostRegistry.stream(model, context, delegateOptions(options));
    },
  };
  try {
    runtime.registerNativeProvider(delegate);
    for (const model of selected) {
      if (!runtime.getModel(model.provider, model.id)) throw new Error("native-provider-model-registration-failed");
    }
    return runtime;
  } catch (error) {
    runtime.dispose?.();
    throw error;
  }
}

function commandArgs(raw) {
  return (raw.match(/"[^"]*"|'[^']*'|\S+/gu) ?? []).map((value) => value.replace(/^(?:"([^"]*)"|'([^']*)')$/u, (_match, doubleQuoted, singleQuoted) => doubleQuoted ?? singleQuoted));
}

/** Pi extension entrypoint: /km-native --live --config <experiment-config.json> */
export default function registerNativeKnowledgeMaintenance(pi) {
  let activeRun;
  const execute = async (rawArgs, ctx, signal) => {
    const argv = commandArgs(rawArgs ?? "");
    if (!argv.includes("--live")) throw new Error("native-live-requires-literal---live");
    if (argv.includes("--auth-file")) throw new Error("native-mode-does-not-accept-auth-files");
    const { resolveConfig } = await import("./live-support.mjs");
    const { dryRun, recordPreflightFailure, runLive } = await import("./run.mjs");
    const config = resolveConfig({ argv });
    if (!config.live || config.authFile !== null) throw new Error("native-mode-does-not-accept-auth-files");
    if (!config.vaultRoot) throw new Error("explicit-synthetic-vault-required");
    const fixturePath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "fixtures.json");
    const fixture = JSON.parse(await (await import("node:fs/promises")).readFile(fixturePath, "utf8"));
    const manifest = dryRun({ ...config, live: false, dryRun: true }, fixture);
    const models = [config.solver, config.judge].map(({ provider, model }) => {
      const selected = ctx.modelRegistry.find(provider, model);
      if (!selected || selected.provider !== provider || selected.id !== model || selected.api !== "openai-responses") throw new Error("configured-native-model-unavailable");
      return selected;
    });
    const sdkRoot = installedPackage();
    if (!sdkRoot) throw new Error("installed-pi-runtime-unavailable");
    const { AuthStorage } = await import(pathToFileURL(path.join(sdkRoot, "dist/core/auth-storage.js")).href);
    const sdk = await import(pathToFileURL(path.join(sdkRoot, "dist/index.js")).href);
    const runtime = await createNativeProviderRuntime({ sdk, AuthStorage, hostRegistry: ctx.modelRegistry, models });
    const ownerToken = randomUUID();
    try {
      const result = await runLive(config, { ...manifest, liveEnabled: true, mode: "live", externalWorkdir: config.workdir }, fixture, ownerToken, {
        sdk,
        authModule: { AuthStorage },
        installedPackage: () => sdkRoot,
        runtime,
        nativeMode: true,
        signal,
        nativeBaseUrls: new Map(models.map((model) => [`${model.provider}/${model.id}`, model.baseUrl])),
      });
      ctx.ui.notify(`Knowledge-maintenance run finished: ${result.stopCode ?? "complete"}. Review only sanitized run metadata and artifact output.`, result.stopCode ? "warning" : "info");
    } catch (error) {
      recordPreflightFailure(config.workdir, ownerToken);
      throw error;
    } finally {
      runtime.dispose?.();
    }
  };
  const run = (rawArgs, ctx) => {
    if (activeRun) throw new Error("native-run-already-active");
    const controller = new AbortController();
    const completion = execute(rawArgs, ctx, controller.signal);
    const current = { controller, completion };
    activeRun = current;
    return completion.finally(() => { if (activeRun === current) activeRun = undefined; });
  };

  pi.on("session_shutdown", async () => {
    const current = activeRun;
    if (!current) return;
    current.controller.abort();
    await current.completion.catch(() => undefined);
  });

  pi.registerCommand("km-native", {
    description: "Run the isolated knowledge-maintenance experiment through Pi's active provider (requires --live)",
    handler: run,
  });
  pi.on("input", async (event, ctx) => {
    const match = /^Task: \/km-native(?:\s+(.*))?$/su.exec(event.text);
    if (!match) return { action: "continue" };
    try { await run(match[1] ?? "", ctx); }
    catch { ctx.ui?.notify("Native knowledge-maintenance command stopped; inspect only nonsecret output metadata.", "warning"); }
    return { action: "handled" };
  });
}
