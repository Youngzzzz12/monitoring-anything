/* Entry: the same bundle serves the main window (label "main") and the floating widget (label "widget"). */

import "./styles/research-dashboard.css";
import "./styles/connector.css";
import "./styles/app.css";
/* After app.css: the plan card restyles the dialog's generic controls. */
import "./styles/plan-card.css";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { Store } from "./app/store";
import { Shell } from "./app/shell";
import { startWidget } from "./app/widget";
import type { AppSettings } from "./app/api";
import { errorMessage } from "./model/rules";

const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

/** Theme (system/light/dark), motion (Full/Subtle/Off) and backdrop classes, as Obsidian's body classes did. */
function applyAppearance(settings: AppSettings | null): void {
  const body = document.body;
  const theme = settings?.theme || "system";
  const dark = theme === "dark" || (theme === "system" && darkQuery.matches);
  body.classList.toggle("theme-dark", dark);
  body.classList.toggle("theme-light", !dark);
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
  body.classList.toggle("rd-motion-subtle", settings?.motion === "subtle");
  body.classList.toggle("rd-motion-off", settings?.motion === "off");
  body.classList.toggle("rd-plain-backdrop", Boolean(settings?.plainBackdrop));
}

async function main(): Promise<void> {
  const host = document.getElementById("app")!;
  const store = new Store();
  applyAppearance(null);
  try { await store.init(); }
  catch (error) { host.textContent = "Monitoring Anything could not start: " + errorMessage(error); return; }
  applyAppearance(store.settings);
  store.subscribe(() => applyAppearance(store.settings));
  darkQuery.addEventListener("change", () => applyAppearance(store.settings));
  if (getCurrentWindow().label === "widget") startWidget(host, store);
  else new Shell(host, store).start();
}

void main();
