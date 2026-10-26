import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";
import { installedPackage, resolveRuntimeDependencies } from "./pi-runtime-paths.mjs";

function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "obsidian-pi-paths-")));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const bin = path.join(root, "bin");
  fs.mkdirSync(bin);
  const env = { ...process.env, PATH: bin, PI_CODING_AGENT_PACKAGE: "", PI_MANAGED_INSTALL_ROOT: "" };
  // Keep command lookup available without exposing a real Pi installation.
  fs.symlinkSync(execFileSync("which", ["which"], { encoding: "utf8" }).trim(), path.join(bin, "which"));
  return { root, bin, env };
}

function packageAt(root) {
  fs.mkdirSync(path.join(root, "dist"), { recursive: true });
  fs.writeFileSync(path.join(root, "package.json"), JSON.stringify({ name: "@earendil-works/pi-coding-agent", type: "module" }));
  fs.writeFileSync(path.join(root, "dist/cli.js"), "#!/usr/bin/env node\n", { mode: 0o755 });
  return root;
}

function managedAt(root, version = "1.4.0") {
  const install = path.join(root, "install");
  fs.mkdirSync(install, { recursive: true });
  fs.writeFileSync(path.join(install, "current-version"), `${version}\n`);
  return packageAt(path.join(install, "releases", version, "node_modules/@earendil-works/pi-coding-agent"));
}

test("explicit package override takes precedence", (t) => {
  const { root, env } = fixture(t);
  const selected = packageAt(path.join(root, "selected"));
  assert.equal(installedPackage({ ...env, PI_CODING_AGENT_PACKAGE: selected }), selected);
});

test("discovers a symlinked npm CLI", (t) => {
  const { root, bin, env } = fixture(t);
  const selected = packageAt(path.join(root, "npm/node_modules/@earendil-works/pi-coding-agent"));
  fs.symlinkSync(path.join(selected, "dist/cli.js"), path.join(bin, "pi"));
  assert.equal(installedPackage(env), selected);
});

test("discovers a managed launcher through a symlink without inherited Pi variables", (t) => {
  const { root, bin, env } = fixture(t);
  const agent = path.join(root, "agent");
  const selected = managedAt(agent);
  fs.mkdirSync(path.join(agent, "bin"));
  fs.writeFileSync(path.join(agent, "bin/pi"), "#!/bin/sh\nexit 1\n", { mode: 0o755 });
  fs.symlinkSync(path.join(agent, "bin/pi"), path.join(bin, "pi"));
  assert.equal(installedPackage(env), selected);
});

test("uses an explicitly supplied managed installation root", (t) => {
  const { root, env } = fixture(t);
  const selected = managedAt(path.join(root, "agent"));
  assert.equal(installedPackage({ ...env, PI_MANAGED_INSTALL_ROOT: path.join(root, "agent/install") }), selected);
});

test("the CLI on PATH wins over an inherited managed root", (t) => {
  const { root, bin, env } = fixture(t);
  const selected = packageAt(path.join(root, "npm/node_modules/@earendil-works/pi-coding-agent"));
  managedAt(path.join(root, "other-agent"));
  fs.symlinkSync(path.join(selected, "dist/cli.js"), path.join(bin, "pi"));
  assert.equal(installedPackage({ ...env, PI_MANAGED_INSTALL_ROOT: path.join(root, "other-agent/install") }), selected);
});

test("missing or malformed managed installations are not treated as SDKs", (t) => {
  const { root, env } = fixture(t);
  assert.equal(installedPackage(env), null);
  const install = path.join(root, "agent/install");
  fs.mkdirSync(install, { recursive: true });
  for (const version of ["", ".", "..", "../../outside", "1.4.0"]) {
    fs.writeFileSync(path.join(install, "current-version"), `${version}\n`);
    assert.equal(installedPackage({ ...env, PI_MANAGED_INSTALL_ROOT: install }), null);
  }
});

for (const layout of ["nested", "hoisted", "symlinked"]) {
  test(`resolves import-only dependency exports in a ${layout} installation`, (t) => {
    const { root } = fixture(t);
    const selected = packageAt(path.join(root, "node_modules/@earendil-works/pi-coding-agent"));
    const modules = layout === "nested" ? path.join(selected, "node_modules") : path.join(root, "node_modules");
    const expected = [];
    for (const name of ["@earendil-works/pi-ai", "typebox"]) {
      const dependency = path.join(modules, name);
      const target = layout === "symlinked" ? path.join(root, "store", name) : dependency;
      fs.mkdirSync(target, { recursive: true });
      fs.writeFileSync(path.join(target, "package.json"), JSON.stringify({ name, type: "module", exports: { ".": { import: "./entry.mjs" } } }));
      fs.writeFileSync(path.join(target, "entry.mjs"), "export const fixture = true;\n");
      if (layout === "symlinked") {
        fs.mkdirSync(path.dirname(dependency), { recursive: true });
        fs.symlinkSync(target, dependency);
      }
      expected.push(pathToFileURL(path.join(target, "entry.mjs")).href);
    }
    assert.deepEqual(resolveRuntimeDependencies(selected), expected);
  });
}

test("missing host dependencies fail instead of silently skipping", (t) => {
  const { root } = fixture(t);
  assert.throws(() => resolveRuntimeDependencies(packageAt(path.join(root, "host"))), /ERR_MODULE_NOT_FOUND/);
});

test("required runtime mode fails when Pi is unavailable", (t) => {
  const { env } = fixture(t);
  const script = path.resolve("tests/pi-runtime-contract.mjs");
  const optional = spawnSync(process.execPath, [script], { env, encoding: "utf8" });
  assert.equal(optional.status, 0);
  assert.match(optional.stdout, /UNVERIFIED/);
  const required = spawnSync(process.execPath, [script, "--require-runtime"], { env, encoding: "utf8" });
  assert.notEqual(required.status, 0);
  assert.match(required.stderr, /Pi SDK\/runtime unavailable/);
});
