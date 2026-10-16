// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn().mockResolvedValue(null) }));

import { connectRequest, effectivePreset, folderTitle, ignoreGlobs, normalizeAgentSettings, normalizeSuggestion, patternList, planOptions, providerIsLocal,
  providerProblem, providerRequest, FALLBACK_PROVIDERS, type Inspection, type PlanOption, type ProviderPreset } from "../src/model/cli";
import { renderAgentSetup, type AgentSetupDeps } from "../src/ui/agent-setup";
import { buildModel } from "../src/model/load";
import { planCard, type PlanCardHost } from "../src/ui/plan-card";
import { mountView, type ActionBridge } from "../src/view/view";
import { snapshot } from "./fixtures";
import { OrbControl } from "../src/app/widget";

const wait = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
async function until(check: () => unknown, timeout = 2000) {
  const start = Date.now();
  while (!check()) { if (Date.now() - start > timeout) throw new Error("Timed out"); await wait(10); }
}

describe("onboarding helpers", () => {
  it("connects agent and edited plans with the profile on stdin", () => {
    const option = { key: "agent", mode: "agent", profileId: null, title: "Sweep", origin: "agent", engine: "declarative", sourceKind: null, projectType: "simulation",
      confidence: 1, threshold: 0, meetsThreshold: true, matched: [], missed: [], milestoneTemplate: null, entries: [] } as PlanOption;
    const profile = { id: "sweep", title: "Sweep" };
    const request = connectRequest({ source: "C:/runs", id: "sweep", title: "Sweep", interval: 300, projectType: "simulation", parentId: "thesis" }, { option, profile, adapter: { root: "." } });
    expect(request.args).toEqual(["--source", "C:/runs", "--id", "sweep", "--title", "Sweep", "--profile-stdin", "--interval", "300", "--stale-after", "900", "--type", "simulation", "--parent", "thesis"]);
    expect(JSON.parse(request.input!)).toEqual({ profile, adapter: { root: "." } });
    expect(() => connectRequest({ source: "C:/runs", id: "x", title: "X", interval: 60 }, { option })).toThrow(/no valid monitoring profile/);
  });

  it("keeps inline milestone templates from candidates", () => {
    const inspection: Inspection = { sourcePath: "C:/p", profiles: [], candidates: [{ profileId: "saved", title: "Saved", engine: "declarative", origin: "user", confidence: 0.9, threshold: 0.5, meetsThreshold: true, milestoneTemplate: [{ title: "Done" }] }] };
    expect(planOptions(inspection)[0].milestoneTemplate).toEqual([{ title: "Done" }]);
  });

  it("builds provider requests without keys and recognises local endpoints", () => {
    const http = FALLBACK_PROVIDERS.find(item => item.id === "openai-compatible")!;
    expect(providerRequest(http, { model: "qwen3" })).toEqual({ id: "openai-compatible", title: http.title, kind: "http", agentic: false, baseUrl: "http://localhost:11434/v1", model: "qwen3" });
    expect(() => providerRequest(FALLBACK_PROVIDERS.find(item => item.id === "custom"))).toThrow(/command/);
    expect(providerIsLocal({ kind: "http", baseUrl: "http://127.0.0.1:1234/v1" })).toBe(true);
    expect(providerIsLocal({ kind: "http", baseUrl: "https://api.anthropic.com" })).toBe(false);
    expect(providerIsLocal({ kind: "cli" })).toBe(false);
    expect(normalizeAgentSettings({ timeoutMinutes: 99, exclude: ["a", ""] })).toEqual({ providerId: "claude-code", overrides: {}, exclude: ["a"], timeoutMinutes: 30, chosen: false, setupDone: false });
  });

  it("parses patterns, folder titles and discovery suggestions", () => {
    expect(patternList("a/**, {b,c}.txt\n a/** \n")).toEqual(["a/**", "{b,c}.txt"]);
    expect(ignoreGlobs("/drafts/\nold-*")).toEqual(["drafts", "old-*"]);
    expect(folderTitle("C:\\Papers\\Regolith_Shielding_Paper\\")).toBe("Regolith Shielding Paper");
    const suggestion = normalizeSuggestion({ id: "s1", projectId: "thesis", folder: "C:/Papers/review", relative: "review", bestCandidate: { profileId: "latex-paper", confidence: 1.4, projectType: "paper", sourceKind: "latex" } });
    expect(suggestion).toMatchObject({ parentId: "thesis", suggestedId: "review", suggestedTitle: "review", bestCandidate: { confidence: 1, subprojectRole: "standalone", origin: "builtin" } });
    expect(normalizeSuggestion({ id: "s2", projectId: "thesis", folder: "relative/path", relative: "x" })).toBeNull();
    expect(normalizeSuggestion({ id: "s3", projectId: "thesis", folder: "C:/a", relative: "../x" })).toBeNull();
  });
});

function host(overrides: Partial<PlanCardHost> = {}): PlanCardHost & { connect: ReturnType<typeof vi.fn> } {
  return {
    projects: () => [{ projectId: "thesis", title: "PhD thesis", parentId: null }],
    inspect: vi.fn().mockResolvedValue({ sourcePath: "C:/runs", title: "Impact_runs", suggestedId: "impact-runs", profiles: [], candidates: [], warnings: [] }),
    templates: vi.fn().mockResolvedValue([]),
    preview: vi.fn().mockResolvedValue({ ok: true, preview: { runtime: { status: "running", currentValue: 25, targetValue: 40, progress: 0.625 } } }),
    getProfile: vi.fn().mockResolvedValue({ profile: null }),
    validateProfile: vi.fn().mockResolvedValue({ ok: true, errors: [] }),
    saveProfile: vi.fn().mockResolvedValue({ saved: "fake-runs" }),
    agentSummary: vi.fn().mockResolvedValue({ id: "custom", title: "Custom command", installed: null, local: false }),
    agentTimeoutMinutes: () => 10,
    agentReady: vi.fn().mockResolvedValue(true),
    mountAgentSetup: vi.fn().mockReturnValue({ ready: async () => true, dispose: () => {} }),
    proposePlan: vi.fn().mockResolvedValue({ promise: Promise.resolve({ ok: true, plan: { profile: { id: "fake-runs", title: "Fake runs", projectType: "simulation", extract: {} }, project: { title: "Impact runs", type: "simulation", parentSuggestion: "thesis" }, rationale: "Logs count iterations." }, validation: { ok: true, errors: [] } }), cancel: vi.fn() }),
    openAgentSettings: vi.fn(),
    liveRunning: () => false,
    connect: vi.fn().mockResolvedValue(undefined),
    setDiscoveryFolder: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as PlanCardHost & { connect: ReturnType<typeof vi.fn> };
}

describe("plan card", () => {
  it("asks an agent when nothing matches, previews its plan and connects it inline", async () => {
    const fake = host();
    planCard(fake, { source: "C:/runs" });
    await until(() => document.querySelector(".rd-plan-nomatch"));
    expect(document.querySelector(".rd-plan-footer-reason")!.textContent).toBe("Use Smart monitoring to design a plan");
    expect((document.querySelector(".rd-plan-connect") as HTMLButtonElement).disabled).toBe(true);
    /* Two choices: Smart monitoring (runs now) and Add requirements (field first). */
    expect(Array.from(document.querySelectorAll(".rd-plan-agent-choices button")).map(node => node.textContent)).toEqual(["Smart monitoring", "Add requirements…"]);
    (document.querySelector(".rd-plan-smart") as HTMLButtonElement).click();
    await until(() => document.querySelector('.rd-plan-option[data-key="agent"]'));
    await until(() => document.querySelector(".rd-plan-progress-caption"));
    expect(document.querySelector(".rd-plan-option.is-selected .rd-plan-rationale")!.textContent).toBe("Logs count iterations.");
    expect(document.querySelector(".rd-plan-progress-caption")!.textContent).toBe("63% complete");
    expect((document.querySelector('[data-rd-focus="name"]') as HTMLInputElement).value).toBe("Impact runs");
    (document.querySelector(".rd-plan-connect") as HTMLButtonElement).click();
    await until(() => fake.connect.mock.calls.length);
    const request = fake.connect.mock.calls[0][0];
    expect(request.args).toEqual(expect.arrayContaining(["--profile-stdin", "--type", "simulation", "--parent", "thesis"]));
    expect(JSON.parse(request.input).profile.id).toBe("fake-runs");
    expect(request.keepUpdating).toBe(true);
    await until(() => !document.querySelector(".rd-plan-card"));
  });

  it("sends special requirements with Add requirements", async () => {
    const fake = host();
    planCard(fake, { source: "C:/runs" });
    await until(() => document.querySelector(".rd-plan-agent-choices"));
    (Array.from(document.querySelectorAll(".rd-plan-agent-choices button")).find(node => node.textContent === "Add requirements…") as HTMLButtonElement).click();
    const field = document.querySelector(".rd-plan-instructions") as HTMLTextAreaElement;
    expect(document.querySelector(".rd-plan-agent-title")!.textContent).toBe("Special requirements");
    field.value = "Count finished cases in results/"; field.dispatchEvent(new Event("input"));
    (Array.from(document.querySelectorAll(".rd-plan-agent-run")).find(node => node.textContent === "Design plan") as HTMLButtonElement).click();
    await until(() => (fake.proposePlan as ReturnType<typeof vi.fn>).mock.calls.length);
    expect((fake.proposePlan as ReturnType<typeof vi.fn>).mock.calls[0][0]).toMatchObject({ source: "C:/runs", instructions: "Count finished cases in results/" });
    await until(() => document.querySelector('.rd-plan-option[data-key="agent"]'));
    (document.querySelector(".rd-modal-close") as HTMLButtonElement).click();
    await until(() => !document.querySelector(".rd-plan-card"));
  });

  it("offers a dropped folder as the discovery folder of the project page it was dropped on", async () => {
    const fake = host({ inspect: vi.fn().mockResolvedValue({ sourcePath: "C:/papers", title: "papers", profiles: [], candidates: [], warnings: [] }) });
    planCard(fake, { source: "C:/papers", parentId: "thesis" });
    await until(() => document.querySelector(".rd-plan-discovery"));
    expect(document.querySelector(".rd-plan-discovery")!.textContent).toBe("Use as discovery folder for PhD thesis");
    (document.querySelector(".rd-plan-discovery") as HTMLButtonElement).click();
    await until(() => (fake.setDiscoveryFolder as ReturnType<typeof vi.fn>).mock.calls.length);
    expect(fake.setDiscoveryFolder).toHaveBeenCalledWith("thesis", "C:/papers");
    await until(() => !document.querySelector(".rd-plan-card"));
  });
});

describe("discovery suggestions in the view", () => {
  it("counts suggestions apart from alerts and hands Add / Dismiss to the bridge", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const addDiscovery = vi.fn().mockResolvedValue(undefined), dismissDiscovery = vi.fn().mockResolvedValue(undefined);
    const suggestions = [
      { id: "s1", projectId: "thesis", parentId: "thesis", folder: "C:/Papers/review", relative: "review", rootName: "Papers", bestCandidate: { profileId: "latex-paper", title: "LaTeX paper", projectType: "paper", confidence: 0.92, sourceKind: "latex", origin: "builtin" } },
      { id: "s2", projectId: "thesis", parentId: "thesis", folder: "C:/Papers/data", relative: "data", rootName: "Papers", bestCandidate: null },
    ];
    const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false }), discoveries: () => ({ suggestions }), addDiscovery, dismissDiscovery };
    const handle = mountView(container, { mode: "attention" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
    await handle.painted;
    expect(container.textContent).toContain("2 suggestions");
    const rows = Array.from(container.querySelectorAll('.rd-alert[data-state="suggestion"]'));
    expect(rows.map(row => row.querySelector(".rd-alert-title")!.textContent)).toEqual(["review", "data"]);
    expect(rows[0].querySelector(".rd-caption")!.textContent).toBe("LaTeX paper · 92% match");
    (Array.from(rows[0].querySelectorAll("button")).find(button => button.textContent === "Add as sub-project") as HTMLButtonElement).click();
    expect(addDiscovery).toHaveBeenCalledWith("s1");
    (Array.from(rows[1].querySelectorAll("button")).find(button => button.textContent === "Ask agent") as HTMLButtonElement).click();
    expect(addDiscovery).toHaveBeenCalledWith("s2", { agent: true });
    handle.dispose();
  });
});

describe("widget milestones", () => {
  it("completes a Next up milestone and adds one from the quick-add line", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    document.body.classList.add("rd-motion-off");
    const setMilestone = vi.fn().mockResolvedValue(undefined), quickMilestone = vi.fn().mockResolvedValue(undefined);
    const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false }), setMilestone, quickMilestone };
    const handle = mountView(container, { mode: "mini" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
    await handle.painted;
    const check = container.querySelector(".rd-mini-task .rd-mini-check") as HTMLButtonElement;
    expect(check.disabled).toBe(false);
    check.click();
    await until(() => setMilestone.mock.calls.length);
    expect(setMilestone.mock.calls[0][1]).toMatchObject({ completed: true });
    (container.querySelector(".rd-mini-add-toggle") as HTMLButtonElement).click();
    const form = container.querySelector(".rd-mini-add") as HTMLFormElement;
    const input = form.querySelector("input")!;
    input.value = "Submit abstract"; input.dispatchEvent(new Event("input"));
    form.querySelector("select")!.value = "sim"; form.querySelector("select")!.dispatchEvent(new Event("change"));
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await until(() => quickMilestone.mock.calls.length);
    expect(quickMilestone).toHaveBeenCalledWith("sim", "Submit abstract");
    document.body.classList.remove("rd-motion-off");
    handle.dispose();
  });
});

describe("widget as a circle", () => {
  const mountOrb = async (focusProjectId: string | null) => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false, focusProjectId }), openProject: vi.fn(), focusChapter: vi.fn() };
    const handle = mountView(container, { mode: "orb" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
    await handle.painted;
    return { container, handle, bridge };
  };

  it("shows the completion ring and the breakdown: projects, sub-projects or a paper's chapters", async () => {
    /* All projects: the average by top-level project (thesis family 2/5, simulation 1/1). */
    const all = await mountOrb(null);
    expect(all.container.querySelector(".rd-mini-header, .rd-module-header")?.closest("header")?.hidden).toBe(true);
    expect(all.container.querySelector(".rd-orb-value")!.textContent).toBe("70%");
    expect(Array.from(all.container.querySelectorAll(".rd-orb-row-name")).map(node => node.textContent)).toEqual(["PhD thesis", "Impact runs"]);
    (all.container.querySelector(".rd-orb-row") as HTMLButtonElement).click();
    await until(() => (all.bridge.openProject as ReturnType<typeof vi.fn>).mock.calls.length);
    expect(all.bridge.openProject).toHaveBeenCalledWith("thesis", expect.anything());
    all.handle.dispose();
    const thesis = await mountOrb("thesis");
    expect(thesis.container.querySelector(".rd-orb-title")!.textContent).toBe("PhD thesis");
    expect(thesis.container.querySelector(".rd-orb-value")!.textContent).toBe("40%");
    expect(Array.from(thesis.container.querySelectorAll(".rd-orb-row-name")).map(node => node.textContent)).toEqual(["Framework paper"]);
    thesis.handle.dispose();
    const paper = await mountOrb("paper");
    expect(Array.from(paper.container.querySelectorAll(".rd-orb-row-name")).map(node => node.textContent)).toEqual(["Introduction", "Method"]);
    expect(paper.container.querySelector(".rd-orb-row-value")!.textContent).toBe("100%");
    paper.handle.dispose();
  });

  it("opens the flyout on hover beside the circle, closes it on leave and expands on a click", async () => {
    const { container, handle } = await mountOrb(null);
    const frame = container;
    const shape = vi.fn(async (kind: "panel" | "orb" | "flyout") => kind === "flyout"
      ? { shape: "flyout" as const, side: "left" as const, orbX: 250, orbY: 0, width: 322, height: 140 }
      : { shape: "orb" as const, side: "right" as const, orbX: 0, orbY: 0, width: 72, height: 72 });
    const expand = vi.fn();
    const control = new OrbControl(frame, expand, shape);
    control.pointer(true);
    await until(() => frame.classList.contains("is-flyout-open"));
    expect(shape).toHaveBeenCalledWith("flyout", expect.any(Array));
    expect(frame.dataset.side).toBe("left");
    expect(frame.style.getPropertyValue("--rd-orb-x")).toBe("250px");
    expect(frame.style.getPropertyValue("--rd-flyout-x")).toBe("0px");
    control.pointer(false);
    expect(frame.classList.contains("is-flyout-open")).toBe(false);
    await until(() => shape.mock.calls.some(call => call[0] === "orb"), 1000);
    await until(() => frame.style.getPropertyValue("--rd-orb-x") === "0px");
    const dial = container.querySelector(".rd-orb-dial") as HTMLButtonElement;
    dial.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 0 }));
    expect(expand).toHaveBeenCalledTimes(1);
    handle.dispose();
  });
});

describe("widget focus", () => {
  it("enters a project from its row and returns through the ring or the back arrow", async () => {
    const mount = async (focusProjectId: string | null) => {
      const container = document.createElement("div");
      document.body.appendChild(container);
      const setFocus = vi.fn().mockResolvedValue(undefined);
      const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false, focusProjectId }), setFocus, quickMilestone: vi.fn(), setMilestone: vi.fn(), openProject: vi.fn() };
      const handle = mountView(container, { mode: "mini" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
      await handle.painted;
      return { container, handle, setFocus };
    };
    const all = await mount(null);
    expect(all.container.querySelector(".rd-mini-back")).toBeNull();
    (all.container.querySelector('.rd-mini-project[data-project-id="thesis"]') as HTMLButtonElement).click();
    await until(() => all.setFocus.mock.calls.length);
    expect(all.setFocus).toHaveBeenCalledWith("thesis");
    all.handle.dispose();

    const focused = await mount("thesis");
    expect(focused.container.querySelector(".rd-mini-focus-name")!.textContent).toBe("PhD thesis");
    expect(focused.container.querySelector(".rd-mini-back-label")!.textContent).toBe("All projects");
    /* Focused on a project with a sub-project: one bar per sub-project instead of Active / Blocked / Alerts. */
    expect(focused.container.querySelector(".rd-mini-stats")).toBeNull();
    expect(focused.container.querySelectorAll(".rd-mini-bar")).toHaveLength(1);
    const titles = Array.from(focused.container.querySelectorAll(".rd-mini-section-title")).map(node => node.textContent);
    expect(titles).toEqual(expect.arrayContaining(["Sub-projects", "Next up"]));
    expect(Array.from(focused.container.querySelectorAll(".rd-mini-projects .rd-mini-row-title")).map(node => node.textContent)).toEqual(["Framework paper"]);
    /* The same rule as for all projects, within the family: the overdue milestone first (orange), no reason labels;
     * then writing and each project's next one (thesis family: 1 + 2 open, up to 5). */
    expect(focused.container.querySelector(".rd-mini-next .rd-mini-section-aside")).toBeNull();
    const rows = Array.from(focused.container.querySelectorAll(".rd-mini-next .rd-mini-task"));
    expect(rows[0].classList.contains("is-overdue")).toBe(true);
    expect(rows[0].querySelector(".rd-mini-row-title")!.textContent).toBe("Chapter 2");
    expect(rows).toHaveLength(3);
    (focused.container.querySelector(".rd-mini-ring.is-link") as HTMLElement).click();
    await until(() => focused.setFocus.mock.calls.length);
    expect(focused.setFocus).toHaveBeenLastCalledWith(null);
    focused.handle.dispose();

    const child = await mount("paper");
    expect(child.container.querySelector(".rd-mini-back-label")!.textContent).toBe("PhD thesis");
    /* A paper without sub-projects: one bar per top-level chapter (Introduction, Method). */
    expect(child.container.querySelectorAll(".rd-mini-bar")).toHaveLength(2);
    expect(child.container.querySelector(".rd-mini-bar")!.getAttribute("aria-label")).toMatch(/^Introduction: 100%/);
    (child.container.querySelector(".rd-mini-back") as HTMLButtonElement).click();
    await until(() => child.setFocus.mock.calls.length);
    expect(child.setFocus).toHaveBeenCalledWith("thesis");
    child.handle.dispose();
  });
});

describe("agent setup", () => {
  const presets = [
    { id: "claude-code", title: "Claude Code", kind: "cli", command: "claude", agentic: true, installed: false, models: ["sonnet", "opus", "haiku"], installUrl: "https://example.org/claude" },
    { id: "codex", title: "Codex CLI", kind: "cli", command: "codex", agentic: true, installed: true, models: ["gpt-5.5", "gpt-5.4"] },
    { id: "gemini-cli", title: "Gemini CLI", kind: "cli", command: "gemini", agentic: true, installed: false },
    { id: "openai-compatible", title: "OpenAI-compatible API", kind: "http", baseUrl: "http://localhost:11434/v1", agentic: false, installed: null, protocol: "openai" },
    { id: "anthropic-api", title: "Anthropic API", kind: "http", baseUrl: "https://api.anthropic.com", agentic: false, installed: null, protocol: "anthropic" },
  ] as ProviderPreset[];

  it("uses the first agent found unless one was chosen, and explains what is missing", () => {
    const settings = normalizeAgentSettings({});
    expect(effectivePreset(presets, settings)?.id).toBe("codex");
    expect(effectivePreset(presets, normalizeAgentSettings({ providerId: "claude-code", chosen: true }))?.id).toBe("claude-code");
    expect(providerProblem(presets[0])).toMatch(/not found/);
    expect(providerProblem(presets[1])).toBe("");
    expect(providerProblem(presets[4], { model: "claude-haiku-5-5" }, false)).toMatch(/API key/);
    expect(providerProblem(presets[4], { model: "claude-haiku-5-5" }, true)).toBe("");
    expect(providerRequest(presets[1], { model: "gpt-5.5" })).toMatchObject({ id: "codex", command: "codex", model: "gpt-5.5" });
  });

  it("lists found agents and local servers, saves a choice and tests the connection", async () => {
    let saved = normalizeAgentSettings({});
    const container = document.createElement("div");
    document.body.appendChild(container);
    const deps: AgentSetupDeps = {
      providers: async () => presets, servers: async () => [{ id: "ollama", title: "Ollama", baseUrl: "http://127.0.0.1:11434/v1", models: ["qwen3"] }],
      settings: () => saved, save: async next => { saved = next; }, keyStatus: async () => false, setKey: vi.fn(),
      test: vi.fn().mockResolvedValue({ ok: true, version: "codex-cli 0.9", durationMs: 2100 }), openUrl: vi.fn(),
    };
    const handle = renderAgentSetup(container, deps);
    await until(() => container.querySelector(".rd-agent-option"));
    const labels = Array.from(container.querySelectorAll(".rd-agent-option strong")).map(node => node.textContent);
    expect(labels).toEqual(["Claude Code", "Codex CLI", "Gemini CLI", "Ollama"]);
    expect(container.querySelector(".rd-agent-option.is-selected strong")!.textContent).toBe("Codex CLI");
    expect(await handle.ready()).toBe(true);
    (Array.from(container.querySelectorAll(".rd-agent-option button")).find(node => node.textContent === "How to install") as HTMLButtonElement).click();
    expect(deps.openUrl).toHaveBeenCalledWith("https://example.org/claude");
    (Array.from(container.querySelectorAll(".rd-agent-option")).find(row => row.textContent!.includes("Ollama"))!.querySelector("button") as HTMLButtonElement).click();
    await until(() => saved.providerId === "openai-compatible");
    expect(saved).toMatchObject({ chosen: true, overrides: { "openai-compatible": { baseUrl: "http://127.0.0.1:11434/v1", model: "qwen3" } } });
    (Array.from(container.querySelectorAll("button")).find(node => node.textContent === "Test connection") as HTMLButtonElement).click();
    await until(() => container.querySelector(".rd-agent-status.is-ok"));
    expect(container.querySelector(".rd-agent-status")!.textContent).toMatch(/^Connected/);
    handle.dispose();
  });

  it("installs the agent skill on request and leaves one set up elsewhere alone", async () => {
    type Target = import("../src/ui/agent-setup").SkillTarget;
    const target = (agent: string, title: string, state: Target["state"], agentPresent = true): Target => ({ agent, title, folder: `C:/Users/me/.${agent}/skills/monitoring-anything`, agentPresent, state });
    let targets = [target("claude", "Claude Code", "missing"), target("codex", "Codex", "missing")];
    const container = document.createElement("div");
    document.body.appendChild(container);
    const deps: AgentSetupDeps = {
      providers: async () => presets, servers: async () => [], settings: () => normalizeAgentSettings({}), save: async () => {}, keyStatus: async () => false,
      setKey: vi.fn(), test: vi.fn(), openUrl: vi.fn(),
      skillStatus: async () => targets,
      installSkill: vi.fn(async (agents: string[]) => (targets = targets.map(t => agents.includes(t.agent) ? { ...t, state: "installed" as const } : t))),
      uninstallSkill: vi.fn(async (agent: string) => (targets = targets.map(t => t.agent === agent ? { ...t, state: "missing" as const } : t))),
    };
    const buttons = () => Array.from(container.querySelectorAll<HTMLButtonElement>(".rd-agent-skill button"));
    const handle = renderAgentSetup(container, deps);
    await until(() => container.querySelector(".rd-agent-skill-line"));
    expect(deps.installSkill).not.toHaveBeenCalled();
    buttons().find(node => node.textContent === "Install for Claude Code and Codex")!.click();
    await until(() => container.querySelectorAll(".rd-agent-skill-state.is-installed").length === 2);
    expect(deps.installSkill).toHaveBeenCalledWith(["claude", "codex"], false);
    buttons().find(node => node.textContent === "Remove")!.click();
    await until(() => container.querySelector(".rd-agent-skill-state.is-missing"));
    expect(deps.uninstallSkill).toHaveBeenCalledWith("claude");
    handle.dispose();

    targets = [target("claude", "Claude Code", "foreign"), target("codex", "Codex", "missing", false)];
    const again = renderAgentSetup(container, deps);
    await until(() => container.querySelector(".rd-agent-skill-state.is-foreign"));
    expect(container.querySelector(".rd-agent-skill")!.textContent).toMatch(/Set up outside the app.*Not on this computer/);
    expect(buttons()).toHaveLength(0);
    again.dispose();
    const card = renderAgentSetup(container, deps, { skills: false });
    await until(() => container.querySelector(".rd-agent-option"));
    expect(container.querySelector(".rd-agent-skill")).toBeNull();
    card.dispose();
  });

  it("asks for an agent on the plan card before Smart monitoring when none can run", async () => {
    let ready = false;
    const fake = host({ agentReady: vi.fn(async () => ready), mountAgentSetup: vi.fn().mockReturnValue({ ready: async () => ready, dispose: () => {} }) });
    planCard(fake, { source: "C:/runs" });
    await until(() => document.querySelector(".rd-plan-smart"));
    (document.querySelector(".rd-plan-smart") as HTMLButtonElement).click();
    await until(() => document.querySelector(".rd-plan-agent-setup"));
    expect(fake.proposePlan).not.toHaveBeenCalled();
    ready = true;
    (Array.from(document.querySelectorAll(".rd-plan-agent-setup button")).find(node => node.textContent === "Continue") as HTMLButtonElement).click();
    await until(() => (fake.proposePlan as ReturnType<typeof vi.fn>).mock.calls.length);
    await until(() => document.querySelector('.rd-plan-option[data-key="agent"]'));
    (document.querySelector(".rd-modal-close") as HTMLButtonElement).click();
    await until(() => !document.querySelector(".rd-plan-card"));
  });
});

describe("next up and in-place milestones", () => {
  it("fills Next up by layers: overdue, then writing, then each project's next", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false }), setMilestone: vi.fn() };
    const handle = mountView(container, { mode: "mini" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
    await handle.painted;
    /* The completed simulation is left out; the blocked paper comes after the active thesis. */
    expect(Array.from(container.querySelectorAll(".rd-mini-next .rd-mini-row-title")).map(node => node.textContent)).toEqual(["Chapter 2", "Method draft", "Method review"]);
    handle.dispose();
  });

  it("adds like a to-do list, renames in place and deletes", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const addMilestoneInline = vi.fn().mockResolvedValue(undefined), editMilestone = vi.fn().mockResolvedValue(undefined), deleteMilestone = vi.fn().mockResolvedValue(undefined);
    const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false }), setMilestone: vi.fn(), addMilestone: vi.fn(), addMilestoneInline, editMilestone, deleteMilestone };
    const handle = mountView(container, { mode: "milestones", projectId: "thesis" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
    await handle.painted;
    const form = container.querySelector("form.rd-task-composer") as HTMLFormElement;
    const input = form.querySelector(".rd-task-composer-title") as HTMLInputElement;
    input.value = "Submit chapter 3"; input.dispatchEvent(new Event("input"));
    (form.querySelector(".rd-task-composer-due") as HTMLInputElement).value = "2030-01-15";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await until(() => addMilestoneInline.mock.calls.length);
    expect(addMilestoneInline).toHaveBeenCalledWith("thesis", { title: "Submit chapter 3", due: "2030-01-15", sourceId: undefined, chapterId: undefined, stage: undefined });
    const title = Array.from(container.querySelectorAll(".rd-paper-task-text")).find(node => node.textContent === "Chapter 2") as HTMLElement;
    expect(title.classList.contains("is-overdue")).toBe(true);
    title.click();
    /* No field box: the title itself is edited. */
    expect(container.querySelector("input.rd-task-edit")).toBeNull();
    expect(title.classList.contains("is-editing")).toBe(true);
    title.textContent = "Chapter 2 (revised)";
    title.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    await until(() => editMilestone.mock.calls.length);
    expect(editMilestone.mock.calls[0][1]).toMatchObject({ title: "Chapter 2 (revised)" });
    await until(() => container.querySelector(".rd-task-delete"));
    (container.querySelector(".rd-task-delete") as HTMLButtonElement).click();
    await until(() => deleteMilestone.mock.calls.length);
    expect(deleteMilestone.mock.calls[0][0]).toBe("thesis");
    handle.dispose();
  });
});

describe("milestone types and new items", () => {
  it("offers section and stage when adding to a paper project", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const addMilestoneInline = vi.fn().mockResolvedValue(undefined);
    const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false }), setMilestone: vi.fn(), addMilestoneInline };
    const handle = mountView(container, { mode: "milestones", projectId: "paper" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
    await handle.painted;
    const form = container.querySelector("form.rd-task-composer") as HTMLFormElement;
    const section = form.querySelector(".rd-task-composer-section") as HTMLSelectElement, stage = form.querySelector(".rd-task-composer-stage") as HTMLSelectElement;
    expect(Array.from(section.options).map(option => option.textContent!.trim())).toEqual(["No section", "Introduction", "Method", "Setup"]);
    expect(stage.hidden).toBe(true);
    section.value = "method"; section.dispatchEvent(new Event("change"));
    expect(stage.hidden).toBe(false);
    stage.value = "review"; stage.dispatchEvent(new Event("change"));
    const input = form.querySelector(".rd-task-composer-title") as HTMLInputElement;
    input.value = "Method review pass"; input.dispatchEvent(new Event("input"));
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await until(() => addMilestoneInline.mock.calls.length);
    expect(addMilestoneInline.mock.calls[0][1]).toMatchObject({ title: "Method review pass", sourceId: "paper-tex", chapterId: "method", stage: "review" });
    handle.dispose();
  });

  it("puts a just-added milestone at the top of Next up", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false, recentMilestones: [{ projectId: "paper", text: "Method review #milestone [paper:: paper-tex] [chapter:: method] [stage:: review]" }] }), setMilestone: vi.fn() };
    const handle = mountView(container, { mode: "mini" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
    await handle.painted;
    expect(Array.from(container.querySelectorAll(".rd-mini-next .rd-mini-row-title")).map(node => node.textContent)[0]).toBe("Method review");
    handle.dispose();
  });
});

describe("writing detection", () => {
  it("ranks the section whose text changed most recently first, even without a word-count change", async () => {
    const data = snapshot();
    const state = data.states[0].value as { details: { chapters: Record<string, unknown>[] }[] };
    state.details[0].chapters.find(chapter => chapter.id === "method-a")!.changedAt = new Date(Date.now() - 60_000).toISOString();
    const paper = data.projects.find(item => item.id === "paper")!;
    paper.milestones += "- [ ] Setup draft #milestone [paper:: paper-tex] [chapter:: method-a] [stage:: draft]\n";
    const container = document.createElement("div");
    document.body.appendChild(container);
    const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false, focusProjectId: "paper" }), setMilestone: vi.fn(), setFocus: vi.fn() };
    const handle = mountView(container, { mode: "mini" }, { load: async () => buildModel(data), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
    await handle.painted;
    /* Focused on the paper (no overdue or due items): the changed subsection's milestone leads. */
    expect(Array.from(container.querySelectorAll(".rd-mini-next .rd-mini-row-title")).map(node => node.textContent)[0]).toBe("Setup draft");
    handle.dispose();
  });
});

describe("change source folder", () => {
  it("offers Change folder in the project menu and the source details", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const changeSourceFolder = vi.fn().mockResolvedValue(undefined);
    const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false }), changeSourceFolder, removeProject: vi.fn() };
    const handle = mountView(container, { mode: "projects" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
    await handle.painted;
    const button = Array.from(container.querySelectorAll(".rd-menu-items button")).find(node => node.textContent === "Change source folder…") as HTMLButtonElement;
    expect(button).toBeTruthy();
    button.click();
    await until(() => changeSourceFolder.mock.calls.length);
    expect(changeSourceFolder).toHaveBeenCalledWith("paper-tex");
    handle.dispose();
  });
});

describe("ma connect", () => {
  it("opens the plan card with the agent's plan checked and previewed, waiting for Connect", async () => {
    const fake = host();
    const plan = { schemaVersion: "1.0", profile: { id: "fake-runs", title: "Fake runs", projectType: "simulation", extract: {} }, project: { title: "Impact runs", type: "simulation" }, rationale: "From the agent in the chat." };
    planCard(fake, { source: "C:/runs", preset: { plan, fromAgent: true } });
    await until(() => document.querySelector('.rd-plan-option[data-key="agent"]'));
    expect(fake.validateProfile).toHaveBeenCalled();
    expect(fake.proposePlan).not.toHaveBeenCalled();
    expect(document.querySelector(".rd-plan-option.is-selected .rd-plan-rationale")!.textContent).toBe("From the agent in the chat.");
    await until(() => document.querySelector(".rd-plan-progress-caption"));
    expect(fake.connect).not.toHaveBeenCalled();
    (document.querySelector(".rd-plan-connect") as HTMLButtonElement).click();
    await until(() => fake.connect.mock.calls.length);
    expect(JSON.parse(fake.connect.mock.calls[0][0].input).profile.id).toBe("fake-runs");
    await until(() => !document.querySelector(".rd-plan-card"));
  });
});
