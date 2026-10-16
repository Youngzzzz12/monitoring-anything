#!/usr/bin/env node
/* README screenshots and recordings from the demo page (dev/demo.html: the real interface with a mocked backend and
 * made-up projects). Starts its own Vite server and drives Chrome through playwright-core.
 *
 *   node scripts/demo-media.mjs [screenshots|record|all] [name ...]
 *
 * Screenshots go to docs/screenshots/*.png. Recordings are Chrome screencast frames (device pixels, timestamped) in
 * .demo-media/<scene>/; scripts/demo-gifs.py turns them into GIFs. Uses the installed Google Chrome; set DEMO_CHROME to
 * the path of another Chromium-based browser. */

import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import { chromium } from "playwright-core";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const [what = "all", ...only] = process.argv.slice(2);
const wanted = name => !only.length || only.includes(name);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
/* A plain white backdrop behind the transparent widget window. */
const backdrop = "html, body { background: #fff !important; }";
const desktop = { width: 1280, height: 820 };

/* Eased pointer movement, so recordings show the hover effects as a person would trigger them. */
async function glide(page, from, to, ms) {
  const steps = Math.max(8, Math.round(ms / 16));
  for (let i = 1; i <= steps; i++) {
    const t = i / steps, e = t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    await page.mouse.move(from[0] + (to[0] - from[0]) * e, from[1] + (to[1] - from[1]) * e);
    await sleep(ms / steps);
  }
}

const openProject = title => async page => {
  await page.getByText(title, { exact: true }).first().click();
  await page.waitForTimeout(1500);
};

const SCREENSHOTS = [
  { name: "overall", query: "?theme=light" },
  { name: "project", query: "?theme=light", prepare: openProject("Project A1") },
  { name: "case-training", query: "?theme=light", prepare: openProject("Project A3") },
  { name: "case-custom", query: "?theme=light", prepare: openProject("Project B1") },
  /* Projects within projects: the overall list (each family with sub-projects of any type) and a parent's page. */
  { name: "family", query: "?theme=light", element: '.rd-app-panel[data-mode="projects"]', viewport: { width: 1280, height: 1400 } },
  { name: "parent", query: "?theme=light", prepare: openProject("Project A") },
];

const SCENES = [
  { name: "widget", query: "?window=widget&theme=light", size: { width: 340, height: 560 }, scheme: "light", play: async page => {
    await page.addStyleTag({ content: backdrop });
    await page.mouse.move(330, 4);
    await sleep(500);
    await glide(page, [330, 4], [170, 120], 500);
    await glide(page, [170, 120], [170, 540], 1700);
    await glide(page, [170, 540], [170, 190], 900);
    await sleep(300);
    await page.locator('.rd-mini-project[data-project-id="project-a"]').click();
    await sleep(1300);
    await glide(page, [170, 190], [200, 130], 400);
    await glide(page, [200, 130], [260, 130], 700);
    await sleep(400);
    await glide(page, [260, 130], [60, 30], 500);
    await page.locator(".rd-mini-back").click();
    await sleep(1400);
  } },
  { name: "orb", query: "?window=widget&shape=orb&theme=light&focus=project-a", size: { width: 300, height: 310 }, scheme: "light", play: async page => {
    await page.addStyleTag({ content: backdrop });
    await page.mouse.move(290, 10);
    await sleep(700);
    await glide(page, [290, 10], [36, 274], 600);
    await sleep(1300);
    await glide(page, [36, 274], [110, 190], 500);
    await sleep(700);
    await glide(page, [110, 190], [290, 300], 400);
    await page.evaluate(() => window.demoPointer(false));
    await sleep(1100);
    await glide(page, [290, 300], [40, 270], 500);
    await sleep(1400);
  } },
  { name: "drop", query: "?theme=light", size: { width: 1100, height: 700 }, scheme: "light", scale: 1, play: async page => {
    await sleep(500);
    await page.evaluate(() => window.demoDrag("C:\\Demo\\Paper2"));
    await sleep(1300);
    await page.evaluate(() => window.demoDrop("C:\\Demo\\Paper2"));
    await sleep(3200);
  } },
];

async function screenshot(browser, base, shot) {
  const page = await browser.newPage({ viewport: shot.viewport || desktop, deviceScaleFactor: 2, colorScheme: "light" });
  await page.goto(base + shot.query);
  await page.waitForTimeout(2500);
  if (shot.prepare) await shot.prepare(page);
  await page.mouse.move(0, 0);
  await page.waitForTimeout(1500);
  const target = join(root, "docs", "screenshots", shot.name + ".png");
  if (shot.element) await page.locator(shot.element).first().screenshot({ path: target });
  else await page.screenshot({ path: target });
  await page.close();
  console.log("screenshot", shot.name);
}

/* Chrome's screencast sends a frame whenever the page changes, at device pixels, with its timestamp. */
async function record(browser, base, scene) {
  const scale = scene.scale ?? 2, dir = join(root, ".demo-media", scene.name);
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });
  const context = await browser.newContext({ viewport: scene.size, deviceScaleFactor: scale, colorScheme: scene.scheme });
  const page = await context.newPage();
  await page.goto(base + scene.query);
  await page.waitForTimeout(2600);
  const session = await context.newCDPSession(page), frames = [];
  session.on("Page.screencastFrame", async frame => {
    const file = "f" + String(frames.length).padStart(4, "0") + ".png";
    writeFileSync(join(dir, file), Buffer.from(frame.data, "base64"));
    frames.push({ file, t: frame.metadata.timestamp });
    await session.send("Page.screencastFrameAck", { sessionId: frame.sessionId }).catch(() => {});
  });
  await session.send("Page.startScreencast", { format: "png", maxWidth: scene.size.width * scale, maxHeight: scene.size.height * scale, everyNthFrame: 1 });
  await scene.play(page);
  await sleep(200);
  await session.send("Page.stopScreencast");
  writeFileSync(join(dir, "frames.json"), JSON.stringify({ frames }));
  await context.close();
  console.log("recorded", scene.name, frames.length, "frames");
}

const server = await createServer({ root, configFile: join(root, "vite.config.ts"), server: { port: 1430, strictPort: false }, logLevel: "warn" });
await server.listen();
const base = server.resolvedUrls.local[0].replace(/\/?$/, "/") + "dev/demo.html";
const browser = await chromium.launch(process.env.DEMO_CHROME ? { executablePath: process.env.DEMO_CHROME } : { channel: "chrome" });
try {
  if (what === "screenshots" || what === "all") for (const shot of SCREENSHOTS.filter(item => wanted(item.name))) await screenshot(browser, base, shot);
  if (what === "record" || what === "all") for (const scene of SCENES.filter(item => wanted(item.name))) await record(browser, base, scene);
} finally {
  await browser.close();
  await server.close();
}
