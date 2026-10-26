import * as fs from "node:fs";
import * as path from "node:path";
import { execFileSync } from "node:child_process";

function isPiPackage(directory) {
  try {
    return JSON.parse(fs.readFileSync(path.join(directory, "package.json"), "utf8")).name === "@earendil-works/pi-coding-agent";
  } catch {
    return false;
  }
}

function managedPackage(installRoot) {
  try {
    const version = fs.readFileSync(path.join(installRoot, "current-version"), "utf8").trim();
    if (!/^[a-zA-Z0-9._+-]+$/.test(version) || version === "." || version === "..") return null;
    const directory = path.join(installRoot, "releases", version, "node_modules/@earendil-works/pi-coding-agent");
    return isPiPackage(directory) ? fs.realpathSync(directory) : null;
  } catch {
    return null;
  }
}

export function installedPackage(env = process.env) {
  if (env.PI_CODING_AGENT_PACKAGE) return path.resolve(env.PI_CODING_AGENT_PACKAGE);
  try {
    const cli = fs.realpathSync(execFileSync("which", ["pi"], { encoding: "utf8", env, stdio: ["ignore", "pipe", "ignore"] }).trim());
    for (let dir = path.dirname(cli); dir !== path.dirname(dir); dir = path.dirname(dir)) {
      if (isPiPackage(dir)) return dir;
    }
    const managed = managedPackage(path.resolve(path.dirname(cli), "../install"));
    if (managed) return managed;
  } catch { /* Pi is optional for source-only checks. */ }
  return env.PI_MANAGED_INSTALL_ROOT ? managedPackage(env.PI_MANAGED_INSTALL_ROOT) : null;
}

export function resolveRuntimeDependencies(packageRoot) {
  // Resolve from the host with ESM import conditions, not CommonJS require
  // conditions or a fixed node_modules layout. Eval's parent is the supplied cwd.
  return JSON.parse(execFileSync(process.execPath, [
    "--input-type=module", "--eval",
    'console.log(JSON.stringify(["@earendil-works/pi-ai", "typebox"].map((name) => import.meta.resolve(name))))',
  ], { cwd: packageRoot, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }));
}
