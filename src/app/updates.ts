/* Updates: a little after the main window starts (and at most once a day), the app reads latest.json from the newest
 * GitHub release, which the release workflow signs with the updater key. A newer version shows a notice with Update:
 * it downloads the signed installer, installs it and restarts. Settings → General can turn the check off or run it now.
 * Updates installed this way are not quarantined by macOS, so no Open Anyway step is needed afterwards. */

import { check, type Update } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import { errorMessage } from "../model/rules";
import { notice } from "../ui/notice";

const LAST_CHECK = "ma-update-last-check";
const DAY = 24 * 60 * 60 * 1000;
let installing = false;

function lastCheck(): number {
  try { return Number(localStorage.getItem(LAST_CHECK)) || 0; } catch { return 0; }
}
function remember(): void {
  try { localStorage.setItem(LAST_CHECK, String(Date.now())); } catch { /* private storage unavailable */ }
}

/** Ask for a newer version; offers it in a notice. Manual checks also say when there is nothing new or it failed. */
export async function checkForUpdate(options: { manual?: boolean } = {}): Promise<Update | null> {
  let update: Update | null = null;
  try { update = await check({ timeout: 20000 }); }
  catch (error) {
    if (options.manual) notice("Could not check for updates: " + errorMessage(error), 8000, "error");
    return null;
  }
  remember();
  if (!update) {
    if (options.manual) notice("Monitoring Anything is up to date.", 5000);
    return null;
  }
  notice(`Version ${update.version} is available.`, 60000, "info", { label: "Update", run: () => install(update!) });
  return update;
}

/** Download, install and restart (Windows installs quietly and restarts the app itself). */
export async function install(update: Update): Promise<void> {
  if (installing) return;
  installing = true;
  notice(`Downloading version ${update.version}…`, 15000);
  try {
    await update.downloadAndInstall();
    await relaunch();
  } catch (error) {
    installing = false;
    notice("The update could not be installed: " + errorMessage(error), 10000, "error");
  }
}

/** Check once, shortly after start, when enabled and not checked in the last day. */
export function startUpdateChecks(enabled: () => boolean): void {
  window.setTimeout(() => {
    if (enabled() && Date.now() - lastCheck() > DAY) void checkForUpdate();
  }, 8000);
}
