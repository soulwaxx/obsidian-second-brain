#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const script = fileURLToPath(new URL("./obsidian-second-brain.py", import.meta.url));
const result = spawnSync(process.env.PYTHON || "python3", [script, ...process.argv.slice(2)], {
  stdio: "inherit",
});
if (result.error) {
  console.error(`obsidian-second-brain: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
