#!/usr/bin/env node
/* Fetch the Python runtime bundled with the app: a pinned python-build-standalone "install_only_stripped" build for
 * the target platform, verified by SHA-256, extracted to src-tauri/python/ and pruned of what the collector never
 * uses (Tk, IDLE, tests, pip, headers). Runs before `tauri build`; an up-to-date runtime is left untouched.
 *
 *   node scripts/fetch-python.mjs            # target from TAURI_ENV_TARGET_TRIPLE, else this machine
 *   node scripts/fetch-python.mjs --target aarch64-apple-darwin
 *   node scripts/fetch-python.mjs --check    # exit 1 when the runtime is missing or stale, download nothing
 *
 * Source: https://github.com/astral-sh/python-build-standalone (MPL-2.0 build scripts; CPython under the PSF licence).
 * To update: change RELEASE and VERSION, then copy each asset's sha256 from the release page (or the GitHub API
 * `digest` field) into BUILDS. */

import { createHash } from "node:crypto";
import { createReadStream, createWriteStream, existsSync, mkdirSync, readFileSync, renameSync, rmSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { get } from "node:https";
import { dirname, join, relative, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const RELEASE = "20260901";
const VERSION = "3.12.14";
const MINOR = VERSION.split(".").slice(0, 2).join(".");
const BUILDS = {
  "x86_64-pc-windows-msvc": "7c45c9622400d578709a9b2cddbe8124cc21d382409d9f13406d706d28e31b14",
  "aarch64-apple-darwin": "81a359f1cfadd4da11766534c5913791cea55f26e1bb902cacd2a531bb1e4b2b",
  "x86_64-apple-darwin": "65b195c9cedc1fef6767f044f9822069adbd1bd9204d424ece4628776fdc04bb",
  "x86_64-unknown-linux-gnu": "72748da13197c1fb161e3afeef20a6a385ff24f2165e6e2758e47008e7faba4c",
};
/* Removed after extraction (paths relative to the runtime root; Windows and Unix layouts both listed). */
const PRUNE = [
  "include", "Scripts", "share", "tcl", "libs",
  "Lib/test", "Lib/idlelib", "Lib/tkinter", "Lib/turtledemo", "Lib/turtle.py", "Lib/ensurepip", "Lib/lib2to3", "Lib/pydoc_data",
  "Lib/site-packages/pip", "Lib/venv", "Lib/sqlite3", "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll", "DLLs/sqlite3.dll", "DLLs/_sqlite3.pyd",
  "DLLs/_testcapi.pyd", "DLLs/_testinternalcapi.pyd", "DLLs/_testbuffer.pyd", "DLLs/_testimportmultiple.pyd", "DLLs/_testmultiphase.pyd",
  "DLLs/_testsinglephase.pyd", "DLLs/_testconsole.pyd", "DLLs/_testclinic.pyd", "DLLs/_ctypes_test.pyd", "DLLs/xxlimited.pyd", "DLLs/xxlimited_35.pyd",
  `lib/python${MINOR}/test`, `lib/python${MINOR}/idlelib`,
  `lib/python${MINOR}/tkinter`, `lib/python${MINOR}/turtledemo`,
  `lib/python${MINOR}/turtle.py`, `lib/python${MINOR}/ensurepip`,
  `lib/python${MINOR}/lib2to3`, `lib/python${MINOR}/pydoc_data`,
  `lib/python${MINOR}/site-packages/pip`, `lib/python${MINOR}/venv`, `lib/python${MINOR}/sqlite3`, "lib/pkgconfig", "lib/itcl4.2.4", "lib/tcl8", "lib/tcl8.6", "lib/tk8.6", "lib/thread2.8.9",
  "bin/idle3", `bin/idle${MINOR}`, "bin/pip3", "bin/pip", `bin/pip${MINOR}`, "bin/pydoc3", `bin/pydoc${MINOR}`,
];

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dest = join(root, "src-tauri", "python");
const cache = join(root, "src-tauri", "target", "python-cache");
const stampFile = join(dest, ".rd-python.json");

function hostTarget() {
  const arch = process.arch === "arm64" ? "aarch64" : process.arch === "x64" ? "x86_64" : process.arch;
  if (process.platform === "win32") return `${arch}-pc-windows-msvc`;
  if (process.platform === "darwin") return `${arch}-apple-darwin`;
  return `${arch}-unknown-linux-gnu`;
}

function argument(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : null;
}

function download(url, file, redirects = 0) {
  return new Promise((done, fail) => {
    get(url, { headers: { "User-Agent": "monitoring-anything-build" } }, response => {
      if ([301, 302, 303, 307, 308].includes(response.statusCode) && response.headers.location && redirects < 6) {
        response.resume();
        download(new URL(response.headers.location, url).toString(), file, redirects + 1).then(done, fail);
        return;
      }
      if (response.statusCode !== 200) { response.resume(); fail(new Error(`Download failed (${response.statusCode}): ${url}`)); return; }
      const out = createWriteStream(file);
      response.pipe(out);
      out.on("finish", () => out.close(done));
      out.on("error", fail);
    }).on("error", fail);
  });
}

function sha256(file) {
  return new Promise((done, fail) => {
    const hash = createHash("sha256");
    createReadStream(file).on("data", chunk => hash.update(chunk)).on("end", () => done(hash.digest("hex"))).on("error", fail);
  });
}

function size(path) {
  const stat = statSync(path);
  if (!stat.isDirectory()) return stat.size;
  return readdirSync(path).reduce((sum, name) => sum + size(join(path, name)), 0);
}

function readStamp() {
  try { return JSON.parse(readFileSync(stampFile, "utf8")); } catch { return null; }
}

async function main() {
  const target = argument("--target") || process.env.RD_PYTHON_TARGET || process.env.TAURI_ENV_TARGET_TRIPLE || hostTarget();
  const digest = BUILDS[target];
  if (!digest) throw new Error(`No pinned Python build for ${target}. Supported: ${Object.keys(BUILDS).join(", ")}`);
  const asset = `cpython-${VERSION}+${RELEASE}-${target}-install_only_stripped.tar.gz`;
  const stamp = readStamp();
  const current = stamp && stamp.asset === asset && stamp.sha256 === digest;
  if (process.argv.includes("--check")) {
    console.log(current ? `Bundled Python ${VERSION} (${target}) is up to date.` : `Bundled Python is missing or stale; run npm run python:fetch.`);
    process.exit(current ? 0 : 1);
  }
  if (current) { console.log(`Bundled Python ${VERSION} (${target}) is up to date.`); return; }

  mkdirSync(cache, { recursive: true });
  const archive = join(cache, asset);
  if (!existsSync(archive) || (await sha256(archive)) !== digest) {
    const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/${encodeURIComponent(asset)}`;
    console.log(`Downloading ${asset} …`);
    const partial = archive + ".part";
    await download(url, partial);
    const actual = await sha256(partial);
    if (actual !== digest) { rmSync(partial, { force: true }); throw new Error(`Checksum mismatch for ${asset}: expected ${digest}, got ${actual}`); }
    renameSync(partial, archive);
  }

  // The archive holds one top-level `python/` folder; extract beside the destination, then move it into place.
  const staging = join(cache, `extract-${process.pid}`);
  rmSync(staging, { recursive: true, force: true });
  mkdirSync(staging, { recursive: true });
  // Relative paths keep both GNU tar (Git Bash) and bsdtar (Windows, macOS) away from drive-letter parsing.
  const result = spawnSync("tar", ["-xzf", relative(staging, archive)], { cwd: staging, stdio: "inherit" });
  if (result.status !== 0) throw new Error("Extracting the Python archive failed (tar is required).");
  const extracted = join(staging, "python");
  if (!existsSync(extracted)) throw new Error("Unexpected archive layout: no python/ folder.");
  let pruned = 0;
  const sitePackages = [join(extracted, "Lib", "site-packages"), join(extracted, "lib", `python${MINOR}`, "site-packages")];
  const extra = sitePackages.filter(existsSync).flatMap(dir => readdirSync(dir).filter(name => /^pip-.*\.dist-info$/.test(name)).map(name => relative(extracted, join(dir, name))));
  for (const item of [...PRUNE, ...extra]) {
    const path = join(extracted, item);
    if (existsSync(path)) { pruned += size(path); rmSync(path, { recursive: true, force: true }); }
  }
  rmSync(dest, { recursive: true, force: true });
  renameSync(extracted, dest);
  rmSync(staging, { recursive: true, force: true });
  writeFileSync(stampFile, JSON.stringify({ release: RELEASE, version: VERSION, target, asset, sha256: digest }, null, 2) + "\n");
  console.log(`Bundled Python ${VERSION} (${target}) in src-tauri/python: ${(size(dest) / 1048576).toFixed(1)} MiB (pruned ${(pruned / 1048576).toFixed(1)} MiB).`);
}

main().catch(error => { console.error(error.message || error); process.exit(1); });
