/* Settings (workspace, collector Python, appearance, widget, system) and the one-time Obsidian import dialog. */

import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { disable as disableAutostart, enable as enableAutostart, isEnabled as autostartEnabled } from "@tauri-apps/plugin-autostart";
import { api, type AppSettings } from "../app/api";
import { AGENT_TIMEOUT, linesList, normalizeAgentSettings, patternList, type AgentSettings, type ProviderPreset } from "../model/cli";
import { renderAgentSetup, type AgentSetupDeps } from "./agent-setup";
import { errorMessage } from "../model/rules";
import { button, buttons, dropdown, h, heading, Modal, setBusy, setting, textInput, toggle, type Row } from "./modal";
import { notice } from "./notice";

export interface SettingsHooks {
  settings(): AppSettings;
  workspaceRoot(): string | null;
  openWorkspace(): Promise<unknown>;
  createWorkspace(): Promise<unknown>;
  importVault(): Promise<unknown>;
  agentProviders(refresh?: boolean): Promise<ProviderPreset[]>;
  agentSetup(): AgentSetupDeps;
  /** Scroll to a section on open ("agent"). */
  section?: string;
}

interface ProfileSummary { id: string; title?: string; origin?: string; description?: string | null; version?: number; createdBy?: string | null }

async function patch(values: Record<string, unknown>): Promise<void> {
  try { await api.updateSettings(values); }
  catch (error) { notice(errorMessage(error), 8000, "error"); }
}

export function settingsDialog(hooks: SettingsHooks): void {
  const modal = new Modal("Settings", "rd-settings-modal");
  const settings = hooks.settings();
  const body = modal.contentEl;

  heading(body, "Workspace");
  const root = hooks.workspaceRoot();
  const workspaceRow = setting(body, "Folder", root || "No workspace is open.");
  if (root) button(workspaceRow.control, "Show folder", () => void api.revealPath(root).catch(error => notice(errorMessage(error), 6000, "error")));
  button(workspaceRow.control, "Open other…", () => { modal.close(); void hooks.openWorkspace(); });
  button(workspaceRow.control, "New…", () => { modal.close(); void hooks.createWorkspace(); });
  const importRow = setting(body, "Import from Obsidian", "One-time copy of an Obsidian vault's Research Dashboard projects, sources and data. The vault is only read.");
  button(importRow.control, "Import…", () => { modal.close(); void hooks.importVault(); });

  heading(body, "Collector");
  /* The bundled runtime is the default; a path here overrides it (for example a Python with extra tools installed). */
  const pythonRow = setting(body, "Python", "The collector runs on the Python bundled with the app. Enter another interpreter (3.10 or newer) only if you need one; clear the field to go back.");
  const pythonInput = textInput(pythonRow, settings.pythonPath || "", { placeholder: "Bundled" });
  pythonInput.classList.add("rd-connect-path");
  const pythonStatus = h(body, "p", { cls: "rd-caption rd-settings-status" });
  const showPython = () => void api.pythonStatus().then(status => {
    const where = status.source === "bundled" ? "Bundled" : status.source === "custom" ? "Custom" : "System";
    pythonStatus.textContent = status.ok ? `${where} Python ${status.version} · ${status.path}` : `${where} Python is not usable: ${status.error || "unknown error"}`;
  }).catch(error => { pythonStatus.textContent = errorMessage(error); });
  const savePython = async () => {
    const value = pythonInput.value.trim().replace(/^"(.*)"$/, "$1");
    if (!value) { await patch({ pythonPath: null }); showPython(); return; }
    const checked = await api.pythonCheck(value);
    if (!checked.ok) { pythonStatus.textContent = `Not usable: ${checked.error || "unknown error"}`; return; }
    await patch({ pythonPath: value }); showPython();
  };
  pythonInput.addEventListener("change", () => void savePython());
  button(pythonRow.control, "Find system Python", async () => {
    pythonStatus.textContent = "Looking for Python…";
    const found = await api.pythonDetect().catch(() => []);
    const best = found.find(item => item.ok);
    if (best) { pythonInput.value = best.path; await patch({ pythonPath: best.path }); showPython(); }
    else pythonStatus.textContent = found.length ? "Only unsupported interpreters were found: " + found.map(item => `${item.path} (${item.version || item.error})`).join(", ") : "No Python 3.10+ found on PATH.";
  });
  showPython();
  toggle(setting(body, "Start live updates at launch", "Begin monitoring connected sources when the app starts."), settings.startLiveOnLaunch, value => void patch({ startLiveOnLaunch: value }));
  toggle(setting(body, "Scan for new sub-projects", "Check each project's discovery folder (set in Edit project) for new subfolders when the app opens and every ten minutes while live updates run."),
    settings.discoveryScan !== false, value => void patch({ discoveryScan: value }));

  const agentSection = h(body, "section", { cls: "rd-settings-section", attr: { id: "rd-settings-agent" } });
  agentSettings(agentSection, hooks);
  const typesSection = h(body, "section", { cls: "rd-settings-section" });
  monitoringTypes(typesSection);

  heading(body, "Appearance");
  dropdown(setting(body, "Theme"), [["system", "Follow system"], ["light", "Light"], ["dark", "Dark"]], settings.theme, value => void patch({ theme: value }));
  dropdown(setting(body, "Motion", "Subtle shortens movement and drops the update glow; Off removes animation. The system Reduce motion setting always wins."), [["full", "Full"], ["subtle", "Subtle"], ["off", "Off"]], settings.motion, value => void patch({ motion: value }));
  toggle(setting(body, "Plain background", "Turn off the tinted backdrop behind the page."), settings.plainBackdrop, value => void patch({ plainBackdrop: value }));

  heading(body, "Floating widget");
  toggle(setting(body, "Show widget", "A small always-available window with overall progress, projects, today's writing, next milestones and alerts."), settings.widget.visible, value => void api.widgetSetVisible(value).catch(error => notice(errorMessage(error), 6000, "error")));
  toggle(setting(body, "Keep on top", "The widget stays above other windows. Its pin button changes the same setting."), settings.widget.keepOnTop, value => void patch({ widget: { keepOnTop: value } }));
  toggle(setting(body, "Fade when idle", "While kept on top, the widget turns translucent about a second after the pointer leaves it."), settings.widget.fadeWhenIdle, value => void patch({ widget: { fadeWhenIdle: value } }));
  const opacityRow = setting(body, "Idle opacity", "How opaque the widget stays while idle (30–90%).");
  const slider = h(opacityRow.control, "input", { attr: { type: "range", min: "30", max: "90", step: "5" } });
  slider.value = String(Math.round(settings.widget.idleOpacity * 100));
  const sliderValue = h(opacityRow.control, "span", { cls: "rd-slider-value", text: slider.value + "%" });
  slider.addEventListener("input", () => { sliderValue.textContent = slider.value + "%"; });
  slider.addEventListener("change", () => void patch({ widget: { idleOpacity: Number(slider.value) / 100 } }));
  const magnifyRow = setting(body, "Hover magnification", "The module under the pointer grows with its card, its neighbours a little, like the macOS Dock (0% turns it off; at most 8%, so cards stay inside the widget). Motion Subtle or Off, and the system Reduce motion setting, keep it still.");
  const magnify = h(magnifyRow.control, "input", { attr: { type: "range", min: "0", max: "8", step: "1" } });
  magnify.value = String(Math.round((settings.widget.magnification ?? 0.05) * 100));
  const magnifyValue = h(magnifyRow.control, "span", { cls: "rd-slider-value", text: magnify.value === "0" ? "Off" : magnify.value + "%" });
  magnify.addEventListener("input", () => { magnifyValue.textContent = magnify.value === "0" ? "Off" : magnify.value + "%"; });
  magnify.addEventListener("change", () => void patch({ widget: { magnification: Number(magnify.value) / 100 } }));

  heading(body, "System");
  const autostartRow = setting(body, "Start at login", "Launch Monitoring Anything in the tray when you sign in.");
  const autostartToggle = toggle(autostartRow, false, async value => {
    try { if (value) await enableAutostart(); else await disableAutostart(); }
    catch (error) { notice(errorMessage(error), 6000, "error"); autostartToggle.checked = !value; }
  });
  void autostartEnabled().then(enabled => { autostartToggle.checked = enabled; }).catch(() => { autostartToggle.disabled = true; });
  toggle(setting(body, "Keep running in the tray", "Closing the main window hides it; live updates and the widget keep running. Quit from the tray icon."), settings.closeToTray, value => void patch({ closeToTray: value }));

  const about = h(body, "p", { cls: "rd-caption rd-settings-about" });
  void api.appInfo().then(info => { about.textContent = `Monitoring Anything ${info.version} · ${info.platform} · collector: ${info.collectorDir || "not found"} · settings: ${info.settingsPath}`; });
  const row = buttons(body);
  button(row, "Done", () => modal.close(), true);
  if (hooks.section === "agent") requestAnimationFrame(() => agentSection.scrollIntoView({ block: "start" }));
}

function flashSaved(row: Row): void {
  row.control.querySelector(".rd-settings-saved")?.remove();
  const note = h(row.control, "span", { text: "Saved", cls: "rd-settings-saved", attr: { role: "status" } });
  window.setTimeout(() => note.remove(), 1600);
}

/** Agent: the provider preset (from `plan providers`), its command and arguments or base URL and model, an API key kept
 * in the OS credential store, never-send patterns and a time limit. */
function agentSettings(section: HTMLElement, hooks: SettingsHooks): void {
  /* Found agents, model, API key and the connection test come from the shared setup panel; the full preset list,
   * command and arguments, never-send patterns and the time limit stay under Advanced. */
  heading(section, "Agent");
  h(section, "p", { cls: "rd-caption rd-settings-note", text: "Used when you choose Smart monitoring or Add requirements on a plan card. The agent reads the folder's file names and short text excerpts and returns a monitoring plan; it never writes files or runs your software." });
  renderAgentSetup(section, hooks.agentSetup(), { onChange: () => { state = normalizeAgentSettings(hooks.settings().agent); } });
  const advanced = h(section, "details", { cls: "rd-settings-advanced" });
  h(advanced, "summary", { text: "Advanced: all presets, command, privacy, time limit" });
  const container = h(advanced, "div");
  /* The section keeps its own copy: a save's settings event may arrive after the next render. */
  let state: AgentSettings = normalizeAgentSettings(hooks.settings().agent);
  const render = (providers: ProviderPreset[] | null) => {
    container.replaceChildren();
    const agent = state;
    const list = providers || [];
    const preset = list.find(item => item.id === agent.providerId) || list[0];
    const save = async (changes: Partial<AgentSettings>) => {
      state = normalizeAgentSettings({ ...state, ...changes });
      await patch({ agent: state });
    };
    const cli = (item: ProviderPreset | undefined) => item?.kind !== "http";
    const providerRow = setting(container, "Provider", !providers ? "Loading providers…" : preset?.notes || (cli(preset) && preset?.installed === false ? "Not found on this computer. Install it or set the full command below." : ""));
    if (!providers) return;
    dropdown(providerRow, list.map(item => [item.id, `${item.title}${!cli(item) ? "" : item.installed === true ? " · installed" : item.installed === false ? " · not found" : ""}`] as [string, string]),
      preset?.id || "", async value => { await save({ providerId: value, chosen: true }); render(providers); });
    if (!preset) return;
    const overrides = agent.overrides[preset.id] || {};
    const saveOverride = (changes: Record<string, unknown>) => save({ overrides: { ...state.overrides, [preset.id]: { ...(state.overrides[preset.id] || {}), ...changes } } });
    if (preset.kind === "http") {
      const anthropic = preset.id === "anthropic-api" || /anthropic\.com/i.test(String(preset.baseUrl || ""));
      const url = textInput(setting(container, "Base URL", anthropic ? "Anthropic API endpoint. Keep https://api.anthropic.com unless you use a proxy." : "OpenAI-compatible endpoint, e.g. http://localhost:11434/v1 for Ollama."),
        overrides.baseUrl ?? preset.baseUrl ?? "", { placeholder: preset.baseUrl || "" });
      url.addEventListener("change", () => void saveOverride({ baseUrl: url.value.trim() }));
      const model = textInput(setting(container, "Model"), overrides.model ?? preset.model ?? "", { placeholder: preset.model || "model name" });
      model.addEventListener("change", () => void saveOverride({ model: model.value.trim() }));
    } else {
      const command = textInput(setting(container, "Command", "Executable name on PATH, or its full path."), overrides.command ?? preset.command ?? "", { placeholder: preset.command || "command" });
      command.classList.add("rd-connect-path");
      command.addEventListener("change", () => void saveOverride({ command: command.value.trim().replace(/^"(.*)"$/, "$1") }));
      const argsRow = setting(container, "Arguments", "One per line. Keep the preset's read-only or sandbox flags.");
      const area = h(argsRow.control, "textarea", { cls: "rd-settings-textarea", attr: { rows: "3", spellcheck: "false" } });
      area.value = (overrides.args ?? preset.args ?? []).join("\n");
      area.addEventListener("change", () => void saveOverride({ args: linesList(area.value) }));
    }
    const keyRow = setting(container, "API key", "Kept in this computer's credential store (Windows Credential Manager, macOS Keychain), never in settings, the workspace or logs. Leave empty when the provider needs none.");
    const key = textInput(keyRow, "", { type: "password", placeholder: "Not set" });
    key.autocomplete = "off";
    void api.agentKeyStatus(preset.id).then(stored => { key.placeholder = stored ? "•••••••• (saved)" : "Not set"; }).catch(error => { keyRow.desc.textContent = errorMessage(error); });
    key.addEventListener("change", async () => {
      const value = key.value.trim();
      if (!value) return;
      try { await api.agentKeySet(preset.id, value); key.value = ""; key.placeholder = "•••••••• (saved)"; flashSaved(keyRow); }
      catch (error) { notice(errorMessage(error), 8000, "error"); }
    });
    button(keyRow.control, "Clear", async () => {
      try { await api.agentKeySet(preset.id, null); key.value = ""; key.placeholder = "Not set"; }
      catch (error) { notice(errorMessage(error), 8000, "error"); }
    });
    const excludeRow = setting(container, "Never send", "Glob patterns the agent never sees, one per line (e.g. data/private/**). Credential-like files are never read.");
    const exclude = h(excludeRow.control, "textarea", { cls: "rd-settings-textarea", attr: { rows: "3", spellcheck: "false", placeholder: "data/private/**\n*.xlsx" } });
    exclude.value = agent.exclude.join("\n");
    exclude.addEventListener("change", () => void save({ exclude: patternList(exclude.value) }));
    const limitRow = setting(container, "Time limit", `Stops a running agent after ${agent.timeoutMinutes} min.`);
    const slider = h(limitRow.control, "input", { attr: { type: "range", min: String(AGENT_TIMEOUT.min), max: String(AGENT_TIMEOUT.max), step: "1" } });
    slider.value = String(agent.timeoutMinutes);
    const value = h(limitRow.control, "span", { cls: "rd-slider-value", text: `${agent.timeoutMinutes} min` });
    slider.addEventListener("input", () => { value.textContent = `${slider.value} min`; limitRow.desc.textContent = `Stops a running agent after ${slider.value} min.`; });
    slider.addEventListener("change", () => void save({ timeoutMinutes: Number(slider.value) }));
  };
  render(null);
  void hooks.agentProviders(true).then(render).catch(() => render([]));
}

/** Monitoring types: saved types can be deleted (archived recoverably by the collector); built-ins are listed by name. */
function monitoringTypes(container: HTMLElement): void {
  heading(container, "Monitoring types");
  const status = h(container, "p", { cls: "rd-caption", text: "Loading monitoring types…" });
  void api.collector<{ profiles?: ProfileSummary[] }>("profiles", ["list"]).then(result => {
    status.remove();
    let profiles = Array.isArray(result?.profiles) ? result.profiles : [];
    const user = profiles.filter(item => item.origin === "user"), builtin = profiles.filter(item => item.origin !== "user");
    if (!user.length) h(container, "p", { cls: "rd-caption rd-settings-note", text: "No saved types yet. Save an agent or edited plan from a plan card and it is recognised in future folders." });
    for (const item of user) {
      const row = setting(container, item.title || item.id, [item.description, `${item.id} · version ${item.version ?? 1}${item.createdBy ? ` · ${item.createdBy}` : ""}`].filter(Boolean).join(" — "));
      let armed = false, timer = 0;
      const remove = button(row.control, "Delete", async () => {
        if (!armed) { armed = true; remove.textContent = "Confirm delete"; timer = window.setTimeout(() => { armed = false; remove.textContent = "Delete"; }, 4000); return; }
        window.clearTimeout(timer); remove.disabled = true;
        try {
          await api.collector("profiles", ["delete", "--id", item.id]);
          profiles = profiles.filter(entry => entry.id !== item.id);
          row.el.remove();
          notice(`Deleted monitoring type “${item.title || item.id}”. A copy is kept in the profile history.`);
        } catch (error) { remove.disabled = false; notice(errorMessage(error), 8000, "error"); }
      });
    }
    if (builtin.length) h(container, "p", { cls: "rd-caption rd-settings-note", text: `Built-in: ${builtin.map(item => item.title || item.id).join(", ")}.` });
  }).catch(error => { status.textContent = `Monitoring types are unavailable: ${errorMessage(error)}`; });
}

interface ImportResult { dryRun: boolean; projects: string[]; sources: string[]; created: number; files: { path: string; action: string }[]; warnings: string[]; note?: string }

export function importDialog(done: () => void): void {
  const modal = new Modal("Import from Obsidian", "rd-edit-project-modal");
  modal.description("Copies projects, milestones, connected sources, folder mappings and snapshots from the Obsidian Research Dashboard into this workspace. The vault is only read, and existing workspace files are never overwritten.");
  const error = modal.errorBox();
  let vault = "";
  const row = setting(modal.contentEl, "Obsidian vault", "The vault root (the folder that contains Research Dashboard/).");
  const input = textInput(row, "", { placeholder: "C:\\Users\\me\\Obsidian\\Vault", onChange: value => { vault = value.trim(); } });
  input.classList.add("rd-connect-path");
  button(row.control, "Browse…", async () => {
    const selected = await openDialog({ directory: true, multiple: false, title: "Choose the Obsidian vault" }).catch(() => null);
    if (typeof selected === "string") { input.value = selected; vault = selected; await check(); }
  });
  const report = h(modal.contentEl, "div", { cls: "rd-import-report" });
  const actions = buttons(modal.contentEl);
  button(actions, "Cancel", () => modal.close());
  button(actions, "Check", () => void check());
  const importButton = button(actions, "Import", () => void execute(), true);
  importButton.disabled = true;

  function show(result: ImportResult) {
    report.replaceChildren();
    const created = result.files.filter(item => item.action === "create" || item.action === "merge").length;
    h(report, "p", { cls: "rd-connect-summary", text: `${result.dryRun ? "Would import" : "Imported"} ${result.projects.length} project(s) and ${result.sources.length} source(s) · ${created} file(s)${result.dryRun ? "" : " written"}.` });
    if (result.projects.length) h(report, "p", { cls: "rd-caption", text: "Projects: " + result.projects.join(", ") });
    if (result.warnings.length) { const ul = h(report, "ul", { cls: "rd-connect-warnings" }); for (const warning of result.warnings.slice(0, 20)) h(ul, "li", { text: warning }); }
    if (!result.dryRun && result.note) h(report, "p", { cls: "rd-caption", text: result.note });
  }

  async function check() {
    if (!vault) { error.textContent = "Choose the vault folder."; return; }
    error.textContent = ""; setBusy(modal.contentEl, true);
    try {
      const result = await api.collector<ImportResult>("import-vault", ["--from", vault, "--dry-run"], null, 120);
      show(result); importButton.disabled = !result.projects.length && !result.created;
    } catch (failure) { error.textContent = errorMessage(failure); importButton.disabled = true; }
    finally { setBusy(modal.contentEl, false); if (importButton.dataset.rdWasDisabled === undefined && !report.childElementCount) importButton.disabled = true; }
  }

  async function execute() {
    error.textContent = ""; modal.busyLock = true; setBusy(modal.contentEl, true);
    try {
      const result = await api.collector<ImportResult>("import-vault", ["--from", vault], null, 300);
      show(result); notice(`Imported ${result.projects.length} project(s) from Obsidian.`);
      importButton.disabled = true; done();
    } catch (failure) { error.textContent = errorMessage(failure); }
    finally { modal.busyLock = false; setBusy(modal.contentEl, false); importButton.disabled = true; }
  }
}
