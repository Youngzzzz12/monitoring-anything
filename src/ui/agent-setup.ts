/* Connect an agent: one panel used by the first-run guide, the plan card (when Smart monitoring has no working agent)
 * and Settings. It lists the agents found on this computer (Claude Code, Codex, Gemini CLI on PATH; Ollama and
 * LM Studio servers), lets the user pick one and a model (llm-wiki's verified ids are suggested), offers an API key
 * route (Anthropic API or an OpenAI-compatible endpoint, key kept in the OS credential store) and tests the connection
 * with one tiny read-only request. Every change is saved at once. */

import { AUTO_ORDER, effectivePreset, normalizeAgentSettings, providerProblem, type AgentSettings, type LocalServer,
  type ProviderPreset } from "../model/cli";
import { errorMessage } from "../model/rules";
import { h } from "./modal";

export interface AgentTestResult { ok: boolean; version?: string | null; durationMs?: number; error?: string | null; response?: string | null }

export interface AgentSetupDeps {
  providers(refresh?: boolean): Promise<ProviderPreset[]>;
  servers(): Promise<LocalServer[]>;
  settings(): AgentSettings;
  save(next: AgentSettings): Promise<void>;
  keyStatus(providerId: string): Promise<boolean>;
  setKey(providerId: string, value: string | null): Promise<void>;
  /** Run `plan test` for the provider as configured now (prompt: one tiny request). */
  test(prompt: boolean): Promise<AgentTestResult>;
  openUrl(url: string): void;
  /** The monitoring-anything skill for Claude Code and Codex (optional: the plan card leaves it out). */
  skillStatus?(): Promise<SkillTarget[]>;
  installSkill?(agents: string[], force: boolean): Promise<SkillTarget[]>;
  uninstallSkill?(agent: string): Promise<SkillTarget[]>;
  confirm?(message: string): Promise<boolean>;
}

export interface SkillTarget { agent: string; title: string; folder: string; agentPresent: boolean; state: "missing" | "installed" | "outdated" | "modified" | "foreign" }

export interface AgentSetupHandle { ready(): Promise<boolean>; dispose(): void }

const BLURBS: Record<string, string> = {
  "claude-code": "Uses your Claude Code login; reads the folder with read-only tools.",
  codex: "Uses your Codex login; runs in a read-only sandbox.",
  "gemini-cli": "Uses your Gemini CLI login (best effort).",
};

/* localhost and 127.0.0.1 name the same server. */
function sameBase(a: unknown, b: unknown): boolean {
  const norm = (value: unknown) => String(value || "").trim().replace(/\/+$/, "").replace("//localhost", "//127.0.0.1");
  return Boolean(a) && norm(a) === norm(b);
}

export function renderAgentSetup(container: HTMLElement, deps: AgentSetupDeps, options: { onChange?: () => void; skills?: boolean; autoInstallSkills?: boolean } = {}): AgentSetupHandle {
  const root = h(container, "div", { cls: "rd-agent-setup" });
  let providers: ProviderPreset[] | null = null, servers: LocalServer[] = [], hasKey = false, disposed = false;
  let testState: { status: "idle" | "running" | "ok" | "error"; text: string } = { status: "idle", text: "" };
  let apiOpen = false;
  let skills: SkillTarget[] | null = null, skillError = "", skillBusy = false, autoInstalled: string[] = [];
  const showSkills = options.skills !== false && typeof deps.skillStatus === "function";

  const current = () => {
    const settings = normalizeAgentSettings(deps.settings());
    const preset = providers ? effectivePreset(providers, settings) : null;
    return { settings, preset, overrides: preset ? settings.overrides[preset.id] || {} : {} };
  };
  const save = async (changes: Partial<AgentSettings>) => {
    await deps.save(normalizeAgentSettings({ ...deps.settings(), ...changes }));
    testState = { status: "idle", text: "" };
    options.onChange?.();
    render();
  };
  const choose = (preset: ProviderPreset, overrides: Record<string, unknown> = {}) => {
    const settings = normalizeAgentSettings(deps.settings());
    return save({ providerId: preset.id, chosen: true, overrides: { ...settings.overrides, [preset.id]: { ...(settings.overrides[preset.id] || {}), ...overrides } } });
  };

  async function refreshKey() {
    const { preset } = current();
    hasKey = preset?.kind === "http" ? await deps.keyStatus(preset.id).catch(() => false) : false;
  }

  async function runTest() {
    testState = { status: "running", text: "Testing the connection…" };
    render();
    try {
      const result = await deps.test(true);
      testState = result.ok
        ? { status: "ok", text: `Connected${result.version ? " · " + result.version : ""}${result.durationMs ? ` · answered in ${(result.durationMs / 1000).toFixed(1)} s` : ""}` }
        : { status: "error", text: result.error || "The agent did not answer." };
    } catch (error) { testState = { status: "error", text: errorMessage(error) }; }
    if (!disposed) render();
  }

  function render() {
    if (disposed) return;
    root.replaceChildren();
    if (!providers) { h(root, "p", { cls: "rd-caption", text: "Looking for agents on this computer…" }); return; }
    const { settings, preset, overrides } = current();

    const found = h(root, "div", { cls: "rd-agent-list", attr: { role: "radiogroup", "aria-label": "Agent" } });
    const rows: { preset: ProviderPreset; label: string; detail: string; available: boolean; apply: () => Promise<void> }[] = [];
    for (const id of AUTO_ORDER) {
      const item = providers.find(entry => entry.id === id);
      if (!item) continue;
      rows.push({ preset: item, label: item.title, detail: item.installed ? BLURBS[id] || "Found on this computer." : "Not found on this computer.",
        available: item.installed === true, apply: () => choose(item) });
    }
    const http = providers.find(entry => entry.id === "openai-compatible");
    for (const server of servers) {
      if (!http) break;
      rows.push({ preset: http, label: server.title, detail: server.models.length ? `Running here · ${server.models.length} model${server.models.length === 1 ? "" : "s"} · nothing leaves this computer` : "Running here · no models pulled yet",
        available: server.models.length > 0, apply: () => choose(http, { baseUrl: server.baseUrl, model: server.models[0] || "" }) });
    }
    for (const row of rows) {
      const isServer = row.preset.id === "openai-compatible", server = isServer ? servers.find(item => item.title === row.label) : null;
      const selected = preset?.id === row.preset.id && (!isServer || sameBase(overrides.baseUrl, server?.baseUrl));
      const line = h(found, "div", { cls: "rd-agent-option" + (selected ? " is-selected" : "") + (row.available ? "" : " is-missing") });
      h(line, "span", { cls: "rd-agent-dot", attr: { "aria-hidden": "true" } });
      const copy = h(line, "div", { cls: "rd-agent-copy" });
      h(copy, "strong", { text: row.label });
      h(copy, "span", { cls: "rd-caption", text: row.detail });
      if (row.available && !selected) {
        const use = h(line, "button", { text: "Use", cls: "rd-plan-chip-button", attr: { type: "button", role: "radio", "aria-checked": "false" } });
        use.addEventListener("click", () => void row.apply());
      } else if (selected) h(line, "span", { cls: "rd-agent-chip", text: settings.chosen ? "Selected" : "Selected · found automatically" });
      else if (row.preset.installUrl) {
        const how = h(line, "button", { text: "How to install", cls: "rd-plan-link", attr: { type: "button" } });
        how.addEventListener("click", () => deps.openUrl(row.preset.installUrl!));
      }
    }
    if (!rows.some(row => row.available)) h(root, "p", { cls: "rd-caption rd-agent-none", text: "No agent was found on this computer. Install one of the agents above (then press Check again), or use an API key below." });
    const again = h(root, "button", { text: "Check again", cls: "rd-plan-link rd-agent-again", attr: { type: "button" } });
    again.addEventListener("click", () => void load(true));

    /* The API route: Anthropic API or any OpenAI-compatible endpoint. */
    const api = h(root, "details", { cls: "rd-agent-api" });
    api.open = apiOpen || (preset?.kind === "http" && !servers.some(server => sameBase(server.baseUrl, overrides.baseUrl)));
    api.addEventListener("toggle", () => { apiOpen = api.open; });
    h(api, "summary", { text: "Use an API key instead" });
    const apiBody = h(api, "div", { cls: "rd-agent-api-body" });
    const httpPresets = providers.filter(entry => entry.kind === "http");
    const apiPreset = preset?.kind === "http" ? preset : httpPresets.find(entry => entry.id === "anthropic-api") || httpPresets[0];
    if (apiPreset) {
      const apiOverrides = settings.overrides[apiPreset.id] || {};
      const kind = h(apiBody, "select", { cls: "rd-agent-field", attr: { "aria-label": "API" } });
      for (const entry of httpPresets) { const option = h(kind, "option", { text: entry.title }); option.value = entry.id; }
      kind.value = apiPreset.id;
      kind.addEventListener("change", () => { const next = httpPresets.find(entry => entry.id === kind.value); if (next) void choose(next); });
      const url = h(apiBody, "input", { cls: "rd-agent-field", attr: { type: "text", placeholder: apiPreset.baseUrl || "https://…", "aria-label": "Base URL" } });
      url.value = String(apiOverrides.baseUrl ?? apiPreset.baseUrl ?? "");
      url.addEventListener("change", () => void choose(apiPreset, { baseUrl: url.value.trim() }));
      const model = modelField(apiBody, apiPreset, String(apiOverrides.model ?? apiPreset.model ?? ""), value => void choose(apiPreset, { model: value }));
      model.classList.add("rd-agent-field");
      const key = h(apiBody, "input", { cls: "rd-agent-field", attr: { type: "password", autocomplete: "off", placeholder: hasKey && preset?.id === apiPreset.id ? "•••••••• (saved in the credential store)" : "API key (kept in this computer's credential store)", "aria-label": "API key" } });
      key.addEventListener("change", async () => {
        const value = key.value.trim();
        if (!value) return;
        try { await deps.setKey(apiPreset.id, value); key.value = ""; hasKey = true; await choose(apiPreset); }
        catch (error) { testState = { status: "error", text: errorMessage(error) }; render(); }
      });
      if (apiPreset.installUrl) {
        const link = h(apiBody, "button", { text: apiPreset.id === "anthropic-api" ? "Get an API key" : "About Ollama", cls: "rd-plan-link", attr: { type: "button" } });
        link.addEventListener("click", () => deps.openUrl(apiPreset.installUrl!));
      }
    }

    /* The selected agent: model and connection test. */
    if (preset) {
      const box = h(root, "div", { cls: "rd-agent-selected" });
      const top = h(box, "div", { cls: "rd-agent-selected-line" });
      h(top, "span", { cls: "rd-caption", text: "Model" });
      const server = servers.find(item => sameBase(item.baseUrl, overrides.baseUrl));
      modelField(top, preset, String(overrides.model ?? preset.model ?? ""), value => void choose(preset, { model: value }), server?.models);
      const problem = providerProblem(preset, overrides, hasKey);
      const test = h(top, "button", { text: testState.status === "running" ? "Testing…" : "Test connection", cls: "rd-plan-chip-button", attr: { type: "button" } });
      test.disabled = testState.status === "running" || Boolean(problem);
      test.addEventListener("click", () => void runTest());
      const status = h(box, "p", { cls: "rd-agent-status is-" + (problem ? "error" : testState.status), attr: { role: "status" } });
      status.textContent = problem || testState.text || (preset.kind === "cli" ? "Test once to confirm it answers (one tiny read-only request)." : "Test once to confirm the endpoint answers.");
    }
    if (showSkills) renderSkills();
  }

  /* The other direction: a skill so Claude Code and Codex can use Monitoring Anything from a chat (set up monitoring,
   * report progress, manage milestones). Installed only on request; a skill the app did not install is left alone. */
  function renderSkills() {
    const box = h(root, "div", { cls: "rd-agent-skill" });
    h(box, "strong", { text: "Use Monitoring Anything from your agent" });
    h(box, "p", { cls: "rd-caption", text: "Adds a skill so Claude Code and Codex can set up monitoring, report progress and manage milestones when you ask them in a chat. New connections still open here for you to confirm." });
    if (!skills) { h(box, "p", { cls: "rd-caption", text: skillError || "Checking…" }); return; }
    if (autoInstalled.length) h(box, "p", { cls: "rd-agent-status is-ok", text: `Installed for ${autoInstalled.join(" and ")}. Remove it below if you'd rather not.` });
    const runSkill = async (work: () => Promise<SkillTarget[]>) => {
      skillBusy = true; skillError = ""; render();
      try { skills = await work(); } catch (error) { skillError = errorMessage(error); }
      skillBusy = false; render();
    };
    const missing = skills.filter(item => item.agentPresent && item.state === "missing");
    for (const item of skills) {
      const line = h(box, "div", { cls: "rd-agent-skill-line" });
      h(line, "span", { text: item.title, cls: "rd-agent-skill-name" });
      const state = !item.agentPresent && item.state === "missing" ? "Not on this computer"
        : { missing: "Not installed", installed: "Installed", outdated: "Update available", modified: "Installed · edited since", foreign: "Set up outside the app" }[item.state];
      h(line, "span", { text: state, cls: "rd-caption rd-agent-skill-state is-" + item.state });
      h(line, "span", { cls: "rd-agent-skill-spacer" });
      const action = (label: string, work: () => Promise<SkillTarget[]>, quiet = false) => {
        const button = h(line, "button", { text: label, cls: quiet ? "rd-plan-link" : "rd-plan-chip-button", attr: { type: "button" } });
        button.disabled = skillBusy;
        button.addEventListener("click", () => void runSkill(work));
      };
      if (item.agentPresent && item.state === "missing" && missing.length < 2) action("Install", () => deps.installSkill!([item.agent], false));
      if (item.state === "outdated") action("Update", () => deps.installSkill!([item.agent], false));
      if ((item.state === "installed" || item.state === "outdated") && deps.uninstallSkill) action("Remove", () => deps.uninstallSkill!(item.agent), true);
      if (item.state === "modified") action("Replace", async () => {
        if (deps.confirm && !(await deps.confirm(`Replace the edited skill in ${item.folder} with this version?`))) return skills!;
        return deps.installSkill!([item.agent], true);
      }, true);
    }
    if (missing.length >= 2) {
      const all = h(box, "button", { text: "Install for " + missing.map(item => item.title).join(" and "), cls: "rd-plan-chip-button rd-agent-skill-all", attr: { type: "button" } });
      all.disabled = skillBusy;
      all.addEventListener("click", () => void runSkill(() => deps.installSkill!(missing.map(item => item.agent), false)));
    }
    if (skillError) h(box, "p", { cls: "rd-agent-status is-error", text: skillError });
  }

  /* A model field: the suggestions (or a server's models) in a list, free text allowed; empty = the agent's default. */
  function modelField(parent: HTMLElement, preset: ProviderPreset, value: string, onChange: (value: string) => void, extra: string[] = []): HTMLInputElement {
    const listId = "rd-agent-models-" + preset.id + "-" + Math.random().toString(36).slice(2, 7);
    const input = h(parent, "input", { cls: "rd-agent-model", attr: { type: "text", list: listId, placeholder: preset.kind === "cli" ? "Default" : "model id", "aria-label": "Model", spellcheck: "false" } });
    input.value = value;
    const list = h(parent, "datalist", { attr: { id: listId } });
    for (const model of [...new Set([...(extra || []), ...(preset.models || [])])]) { const option = h(list, "option"); option.value = model; }
    input.addEventListener("change", () => onChange(input.value.trim()));
    return input;
  }

  async function load(refresh = false) {
    providers = null; render();
    const [list, found] = await Promise.all([deps.providers(refresh).catch(() => [] as ProviderPreset[]), deps.servers().catch(() => [] as LocalServer[])]);
    if (disposed) return;
    providers = list; servers = found;
    await refreshKey();
    render();
    if (showSkills) {
      try {
        skills = await deps.skillStatus!();
        /* First run: install the skill for every agent found that has none yet (Remove undoes it). */
        const missing = options.autoInstallSkills && deps.installSkill ? skills.filter(item => item.agentPresent && item.state === "missing") : [];
        if (missing.length) {
          skills = await deps.installSkill!(missing.map(item => item.agent), false);
          autoInstalled = missing.map(item => item.title);
        }
      } catch (error) { skillError = errorMessage(error); }
      if (!disposed) render();
    }
  }
  void load(true);

  return {
    async ready() {
      if (!providers) await load(false);
      await refreshKey();
      const { preset, overrides } = current();
      return !providerProblem(preset, overrides, hasKey);
    },
    dispose() { disposed = true; root.remove(); },
  };
}
