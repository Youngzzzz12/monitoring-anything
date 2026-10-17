#!/usr/bin/env node
/* Prepare a release's files from the build workflow's artifacts: installers and update bundles with GitHub-friendly
 * names, and latest.json, which installed apps read (releases/latest/download/latest.json) to find and verify updates.
 *
 *   gh run download <run> --dir <artifacts>
 *   node scripts/release-assets.mjs <artifacts> <out> [--notes "text"] [--notes-file release-notes.md]
 *   gh release create v<version> <out>/* --notes-file release-notes.md
 *
 * The version comes from src-tauri/tauri.conf.json; each update bundle needs its .sig (made with the updater key).
 * Without --notes, the update notes are the version's CHANGELOG section; --notes-file also writes the release page
 * text (that section, the downloads and how to open unsigned builds). The release workflow runs this on tag pushes. */

import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const [source, out] = process.argv.slice(2);
if (!source || !out) { console.error("Usage: node scripts/release-assets.mjs <artifacts> <out> [--notes text]"); process.exit(2); }
const option = name => { const at = process.argv.indexOf(name); return at > 0 ? process.argv[at + 1] || "" : null; };
const version = JSON.parse(readFileSync(join(root, "src-tauri", "tauri.conf.json"), "utf8")).version;
/* "## [0.3.1] - date" up to the next "## [" heading. */
function changelogSection() {
  const text = readFileSync(join(root, "CHANGELOG.md"), "utf8");
  const start = text.indexOf(`## [${version}]`);
  if (start < 0) return "";
  const body = text.slice(text.indexOf("\n", start) + 1);
  const end = body.indexOf("\n## [");
  return (end < 0 ? body : body.slice(0, end)).trim();
}
const section = changelogSection();
const notes = option("--notes") ?? section;
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
const notesFile = option("--notes-file");
if (notesFile) {
  const page = [
    "| System | Download |", "| --- | --- |",
    `| Windows 10/11 (x64) | \`Monitoring-Anything_${version}_x64-setup.exe\` |`,
    `| macOS 12+ (Apple Silicon) | \`Monitoring-Anything_${version}_aarch64.dmg\`: open it and drag the app to Applications |`,
    "", "Already on 0.3.0 or later? The app offers this update itself (Settings → General → Updates).", "",
    section || notes, "",
    "### Opening a new download",
    "The installers are not notarized yet:",
    "- **Windows:** on the SmartScreen warning choose **More info → Run anyway**.",
    "- **macOS:** open the app once and choose **Done** at the warning, then **System Settings → Privacy & Security → Open Anyway**. Or run `xattr -dr com.apple.quarantine \"/Applications/Monitoring Anything.app\"` in Terminal.",
    "", "(`latest.json` and the `.app.tar.gz` are for the updater.)", "",
  ].join("\n");
  writeFileSync(notesFile, page);
}
for (const name of readdirSync(out)) console.log(`${name}  ${(statSync(join(out, name)).size / 1048576).toFixed(1)} MB`);
