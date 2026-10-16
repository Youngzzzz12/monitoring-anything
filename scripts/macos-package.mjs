#!/usr/bin/env node
/* macOS packaging after `tauri build` (run on macOS): sign every executable and library in the app ad hoc, then the
 * app itself, verify the signature, smoke-test the bundled Python, and rebuild the .dmg with LZMA compression (ULMO,
 * macOS 10.15+), which is about half the size of the default zlib image.
 *
 * Without a Developer ID, a downloaded app whose nested code (here the bundled Python's binaries and libraries) is not
 * signed is reported as "damaged"; signing each Mach-O file ad hoc avoids that, so the usual Open Anyway flow applies.
 *
 *   node scripts/macos-package.mjs [--skip-dmg] */

import { execFileSync, spawnSync } from "node:child_process";
import { closeSync, existsSync, mkdirSync, mkdtempSync, openSync, readdirSync, readSync, rmSync, statSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

if (process.platform !== "darwin") { console.log("macos-package: not macOS, nothing to do."); process.exit(0); }

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const bundle = join(root, "src-tauri", "target", "release", "bundle");
const run = (command, args, options = {}) => execFileSync(command, args, { stdio: "inherit", ...options });

const macos = join(bundle, "macos");
const app = readdirSync(macos).filter(name => name.endsWith(".app")).map(name => join(macos, name))[0];
if (!app) throw new Error(`No .app in ${macos}; run tauri build first.`);

/* Mach-O files start with one of these magic numbers (thin 32/64-bit either endianness, or universal). */
const MAGIC = new Set(["feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca"]);
function isMachO(path) {
  const fd = openSync(path, "r");
  try { const buffer = Buffer.alloc(4); return readSync(fd, buffer, 0, 4, 0) === 4 && MAGIC.has(buffer.toString("hex")); }
  finally { closeSync(fd); }
}
function walk(dir, found = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name), stat = statSync(path, { throwIfNoEntry: false });
    if (!stat) continue;
    if (stat.isDirectory()) walk(path, found);
    else if (stat.isFile() && stat.size >= 4 && isMachO(path)) found.push(path);
  }
  return found;
}

const resources = join(app, "Contents", "Resources");
const nested = walk(resources);
console.log(`Signing ${nested.length} executables and libraries inside the app (ad hoc)…`);
for (const file of nested) run("codesign", ["--force", "--sign", "-", "--timestamp=none", file], { stdio: "pipe" });
run("codesign", ["--force", "--sign", "-", "--timestamp=none", app]);
run("codesign", ["--verify", "--deep", "--strict", "--verbose=2", app]);

const python = join(resources, "python", "bin", "python3");
if (existsSync(python)) {
  const checked = spawnSync(python, ["-E", "-s", "-c", "import json, sqlite3, zipfile, xml.etree.ElementTree, zoneinfo; print('bundled python ok')"], { encoding: "utf8" });
  if (checked.status !== 0) throw new Error(`The bundled Python does not run: ${checked.stderr || checked.stdout}`);
  console.log(checked.stdout.trim());
}

if (!process.argv.includes("--skip-dmg")) {
  const dmgDir = join(bundle, "dmg");
  mkdirSync(dmgDir, { recursive: true });
  const old = readdirSync(dmgDir).filter(name => name.endsWith(".dmg"));
  const name = old[0] || `${app.split("/").pop().replace(/\.app$/, "")}_aarch64.dmg`;
  for (const file of old) rmSync(join(dmgDir, file));
  const staging = mkdtempSync(join(tmpdir(), "ma-dmg-"));
  run("ditto", [app, join(staging, app.split("/").pop())]);
  symlinkSync("/Applications", join(staging, "Applications"));
  const target = join(dmgDir, name);
  run("hdiutil", ["create", "-volname", app.split("/").pop().replace(/\.app$/, ""), "-srcfolder", staging, "-fs", "HFS+", "-format", "ULMO", "-ov", target]);
  rmSync(staging, { recursive: true, force: true });
  /* Check the image the way a user gets it: mount, verify the app inside, detach. */
  const mount = mkdtempSync(join(tmpdir(), "ma-mount-"));
  run("hdiutil", ["attach", target, "-nobrowse", "-readonly", "-mountpoint", mount]);
  try { run("codesign", ["--verify", "--deep", "--strict", "--verbose=2", join(mount, app.split("/").pop())]); }
  finally { run("hdiutil", ["detach", mount]); }
  console.log(`${target}: ${(statSync(target).size / 1048576).toFixed(1)} MB`);
}
