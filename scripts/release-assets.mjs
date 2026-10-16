#!/usr/bin/env node
/* Prepare a release's files from the build workflow's artifacts: installers and update bundles with GitHub-friendly
 * names, and latest.json, which installed apps read (releases/latest/download/latest.json) to find and verify updates.
 *
 *   gh run download <run> --dir <artifacts>
 *   node scripts/release-assets.mjs <artifacts> <out> [--notes "text"]
 *   gh release create v<version> <out>/* --notes-file ...
 *
 * The version comes from src-tauri/tauri.conf.json; each update bundle needs its .sig (made with the updater key). */

import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const [source, out] = process.argv.slice(2);
if (!source || !out) { console.error("Usage: node scripts/release-assets.mjs <artifacts> <out> [--notes text]"); process.exit(2); }
const notesAt = process.argv.indexOf("--notes");
const notes = notesAt > 0 ? process.argv[notesAt + 1] || "" : "";
const version = JSON.parse(readFileSync(join(root, "src-tauri", "tauri.conf.json"), "utf8")).version;
const repo = "https://github.com/Youngzzzz12/monitoring-anything";

function files(dir) {
  return readdirSync(dir).flatMap(name => { const path = join(dir, name); return statSync(path).isDirectory() ? files(path) : [path]; });
}
const all = files(source);
const find = test => all.find(path => test(basename(path)));
/* "Monitoring Anything_0.3.0_x64-setup.exe" -> "Monitoring-Anything_0.3.0_x64-setup.exe" */
const published = path => basename(path).replace(/ /g, "-");

mkdirSync(out, { recursive: true });
const platforms = {};
/* Copy an update bundle under its published name and record its signature and download address. */
const add = (platform, bundle, name) => {
  if (!bundle) { console.warn(`No update bundle for ${platform}`); return; }
  if (!existsSync(bundle + ".sig")) throw new Error(`${basename(bundle)} has no .sig: was TAURI_SIGNING_PRIVATE_KEY set for the build?`);
  copyFileSync(bundle, join(out, name));
  platforms[platform] = { signature: readFileSync(bundle + ".sig", "utf8").trim(), url: `${repo}/releases/download/v${version}/${encodeURIComponent(name)}` };
};
const setup = find(name => name.endsWith("-setup.exe") && name.includes(`_${version}_`));
add("windows-x86_64", setup, setup && published(setup));
/* Tauri names the macOS update bundle after the app only; publish it with the version and architecture like the dmg. */
add("darwin-aarch64", find(name => name.endsWith(".app.tar.gz")), `Monitoring-Anything_${version}_aarch64.app.tar.gz`);
const dmg = find(name => name.endsWith(".dmg") && name.includes(`_${version}_`));
if (dmg) copyFileSync(dmg, join(out, published(dmg)));
else console.warn(`No ${version} dmg`);
if (!setup) console.warn(`No ${version} Windows installer`);
const manifest = { version, notes, pub_date: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), platforms };
writeFileSync(join(out, "latest.json"), JSON.stringify(manifest, null, 2) + "\n");
for (const name of readdirSync(out)) console.log(`${name}  ${(statSync(join(out, name)).size / 1048576).toFixed(1)} MB`);
