// Extension build: every entry becomes one self-contained IIFE (no runtime imports),
// so the MAIN-world observer executes synchronously at document_start.
// Usage: node build.mjs [--watch]

import { build, context } from "esbuild";
import { cpSync, mkdirSync, rmSync } from "node:fs";
import process from "node:process";

const outdir = "dist";
const watch = process.argv.includes("--watch");

rmSync(outdir, { recursive: true, force: true });
mkdirSync(outdir, { recursive: true });
cpSync("static", outdir, { recursive: true });

const options = {
  entryPoints: {
    background: "src/background/index.ts",
    "main-world": "src/content/main-world.ts",
    relay: "src/content/relay.ts",
    popup: "src/popup/popup.ts",
    login: "src/popup/login.ts",
    export: "src/popup/export-page.ts",
  },
  outdir,
  bundle: true,
  format: "iife",
  target: "chrome120",
  sourcemap: "inline",
  logLevel: "info",
};

if (watch) {
  const ctx = await context(options);
  await ctx.watch();
} else {
  await build(options);
}
