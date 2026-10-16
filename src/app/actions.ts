/* The main window's action bridge: what the plugin's Connector did for the Obsidian view, against the app workspace.
 * Record and milestone edits are validated here (ported rules) and written by the Rust side with compare-and-swap;
 * project creation, connection, collection and cleanup run through the bundled Python collector. */

import { getCurrentWindow } from "@tauri-apps/api/window";
import { ask as askDialog, open as openDialog } from "@tauri-apps/plugin-dialog";
import { hierarchyItems } from "../model/load";
import { paperBinding, type PaperBinding, type SetMilestoneRequest } from "../model/milestones";
import { effectivePreset, FALLBACK_PROVIDERS, ignoreGlobs, normalizeAgentSettings, parseCliResult, providerIsLocal, providerProblem, providerRequest,
  type AgentSettings, type Inspection, type LocalServer, type PreviewResult, type ProviderPreset, type TemplateSummary, type Validation } from "../model/cli";
import { renderAgentSetup, type AgentSetupDeps, type AgentTestResult } from "../ui/agent-setup";
import { button, buttons, Modal } from "../ui/modal";
import { errorMessage, validateNewProject, validateParent, validateProjectValues } from "../model/rules";
import type { Project } from "../model/types";
import type { ActionBridge, BridgeStatus, DiscoverySuggestion, NavTarget } from "../view/view";
import { planCard, type AgentResponse, type ConnectInput, type PlanCard, type PlanCardHost, type PlanCardOptions } from "../ui/plan-card";
import { addMilestoneDialog, editProjectDialog, newProjectDialog, removeProjectDialog, type DiscoveryEdit, type PaperChoice } from "../ui/dialogs";
import { notice } from "../ui/notice";
import { api } from "./api";
import { Discovery } from "./discovery";
import { addMilestoneTo, deleteMilestoneIn, editMilestoneIn, paperOf, setMilestoneIn } from "./milestone-writes";
import type { Store } from "./store";

export interface ConnectResult { projectId: string; dashboardProjectId: string; collectionHealth: string; collectionError?: string | null; milestones?: { addedCount?: number; error?: string } }

export interface PageState { projectId: string | null; sourceId?: string | null; chapterId?: string | null }

export interface ShellHooks {
  navigate(state: PageState): void;
  current(): PageState;
  openSettings(section?: string): void;
  importVault(): void;
}

export { paperOf };

export class AppActions implements ActionBridge {
  readonly discovery: Discovery;
  private card: PlanCard | null = null;
  private providers: ProviderPreset[] | null = null;

  constructor(private store: Store, private shell: ShellHooks) {
    this.discovery = new Discovery(store, { withPausedWatcher: action => this.withPausedWatcher(action), dialogOpen: () => Boolean(document.querySelector(".rd-modal-backdrop")) });
    /* An open plan card follows workspace changes (a project removed or added elsewhere). */
    store.subscribe(() => { if (this.card && this.store.snapshot) this.card.updateProjects(hierarchyItems(this.store.model())); });
  }

  getStatus(): BridgeStatus {
    return {
      running: this.store.watch.running, busy: this.store.busy, error: this.store.error || this.store.watch.error,
      canEdit: true, canAddMilestone: true, canRemove: true, canCreate: true, canGroup: true,
      keepOnTop: Boolean(this.store.settings?.widget.keepOnTop), canKeepOnTop: false, discoveryScanning: this.discovery.scanning, recentMilestones: this.store.recentMilestones(),
      widgetVisible: Boolean(this.store.settings?.widget.visible),
    };
  }

  private project(id: string): Project {
    const project = this.store.model().projects.find(item => item.id === id);
    if (!project) throw new Error("This project is no longer in the workspace. Refresh and try again.");
    return project;
  }

  /** One collector writer at a time: pause live updates around connect/remove, then resume. */
  private async withPausedWatcher<T>(action: () => Promise<T>): Promise<T> {
    const wasRunning = this.store.watch.running;
    if (wasRunning) await api.watchStop();
    try { return await action(); }
    finally { if (wasRunning) await api.watchStart().catch(error => notice("Live updates could not restart: " + errorMessage(error), 8000, "error")); }
  }

  /* ---------- Connecting folders: the plan card ---------- */

  agentSettings(): AgentSettings { return normalizeAgentSettings(this.store.settings?.agent); }

  /** Provider presets from `plan providers` (with installed state), loaded once per app session; documented presets otherwise. */
  async agentProviders(refresh = false): Promise<ProviderPreset[]> {
    if (this.providers && !refresh) return this.providers;
    try {
      const result = await api.collector<{ providers?: ProviderPreset[] }>("plan", ["providers"]);
      const list = Array.isArray(result?.providers) ? result.providers.filter(item => item && typeof item.id === "string") : [];
      this.providers = list.length ? list : FALLBACK_PROVIDERS;
    } catch (_) { this.providers = FALLBACK_PROVIDERS; }
    return this.providers;
  }

  /** The chosen provider, or (nothing chosen) the first agent found on this computer. */
  private async agentPreset(): Promise<ProviderPreset | null> {
    const settings = this.agentSettings(), list = await this.agentProviders();
    return effectivePreset(list, settings) || FALLBACK_PROVIDERS.find(item => item.id === settings.providerId) || null;
  }

  /** Whether Smart monitoring can run now (installed CLI, or endpoint + model + key where needed). */
  async agentReady(): Promise<boolean> {
    const preset = await this.agentPreset();
    const overrides = preset ? this.agentSettings().overrides[preset.id] || {} : {};
    const hasKey = preset?.kind === "http" ? await api.agentKeyStatus(preset.id).catch(() => false) : false;
    return !providerProblem(preset, overrides, hasKey);
  }

  /** What the agent setup panel needs (first-run guide, plan card, Settings). */
  agentSetupDeps(): AgentSetupDeps {
    return {
      providers: refresh => this.agentProviders(refresh),
      servers: () => api.collector<{ servers?: LocalServer[] }>("plan", ["detect"], null, 15).then(result => Array.isArray(result?.servers) ? result.servers : []),
      settings: () => this.agentSettings(),
      save: async next => { this.store.settings = await api.updateSettings({ agent: next }); this.store.notify(); },
      keyStatus: providerId => api.agentKeyStatus(providerId),
      setKey: async (providerId, value) => { await api.agentKeySet(providerId, value); },
      test: async prompt => {
        const preset = await this.agentPreset(), agent = this.agentSettings();
        const request = { provider: providerRequest(preset, agent.overrides[preset?.id || ""] || {}), prompt, timeoutSeconds: 120 };
        return parseCliResult<AgentTestResult>("plan", await api.agentTestRaw(crypto.randomUUID(), request));
      },
      openUrl: url => void api.openUrl(url).catch(error => notice(errorMessage(error), 6000, "error")),
      skillStatus: () => api.skillStatus(),
      installSkill: (agents, force) => api.skillInstall(agents, force),
      uninstallSkill: agent => api.skillUninstall(agent),
      confirm: message => askDialog(message, { title: "Monitoring Anything", kind: "warning" }),
    };
  }

  /** The agent setup as a dialog: first run (Skip for now / Done) or from Settings. Either way it counts as done. */
  agentSetupDialog(options: { firstRun?: boolean } = {}): void {
    if (document.querySelector(".rd-agent-setup-modal")) return;
    const modal = new Modal(options.firstRun ? "Connect an agent" : "Agent", "rd-agent-setup-modal");
    modal.description(options.firstRun
      ? "Smart monitoring lets an agent design how a project folder is monitored. Pick the agent once; it only reads file names and short excerpts, never writes or runs anything. You can change this later in Settings → Agent."
      : "The agent designs monitoring plans when you choose Smart monitoring. It only reads file names and short excerpts, never writes or runs anything.");
    const setup = renderAgentSetup(modal.contentEl, this.agentSetupDeps(), { autoInstallSkills: options.firstRun });
    const row = buttons(modal.contentEl);
    const finish = () => { setup.dispose(); modal.close(); };
    if (options.firstRun) button(row, "Skip for now", finish);
    button(row, "Done", finish, true);
    modal.onClose = () => {
      setup.dispose();
      const agent = this.agentSettings();
      if (!agent.setupDone) void this.agentSetupDeps().save({ ...agent, setupDone: true }).catch(() => {});
    };
  }

  private planHost(): PlanCardHost {
    return {
      projects: () => hierarchyItems(this.store.model()),
      inspect: source => api.collector<Inspection>("inspect", ["--source", source], null, 120),
      templates: () => api.collector<{ milestoneTemplates?: TemplateSummary[] }>("milestones", ["list"]).then(result => Array.isArray(result?.milestoneTemplates) ? result.milestoneTemplates : []),
      preview: request => api.collector<PreviewResult>("plan", ["preview"], JSON.stringify(request), 120),
      getProfile: id => api.collector<{ profile?: Record<string, unknown> }>("profiles", ["get", "--id", id]),
      validateProfile: profile => api.collector<Validation>("profiles", ["validate"], JSON.stringify({ profile })),
      saveProfile: profile => this.store.exclusive(() => api.collector<{ saved?: string; unchanged?: boolean }>("profiles", ["save"], JSON.stringify({ profile }))),
      agentSummary: async () => {
        const preset = await this.agentPreset();
        if (!preset) return null;
        const overrides = this.agentSettings().overrides[preset.id] || {};
        return { id: preset.id, title: preset.title || preset.id, installed: preset.installed ?? null, local: providerIsLocal({ kind: preset.kind, baseUrl: overrides.baseUrl ?? preset.baseUrl }) };
      },
      agentTimeoutMinutes: () => this.agentSettings().timeoutMinutes,
      proposePlan: async payload => {
        const agent = this.agentSettings(), preset = await this.agentPreset();
        const request = { ...payload, provider: providerRequest(preset, agent.overrides[preset?.id || ""] || {}), timeoutSeconds: agent.timeoutMinutes * 60, privacy: { exclude: agent.exclude.slice() } };
        const runId = crypto.randomUUID();
        const promise = api.agentProposeRaw(runId, request).then(output => parseCliResult<AgentResponse>("plan", output)).catch(error => {
          const message = errorMessage(error);
          if (/timed out or was stopped/.test(message)) throw new Error("The agent was stopped before it returned a plan (cancelled or over the time limit).");
          throw new Error(message);
        });
        return { promise, cancel: () => void api.agentCancel(runId).catch(() => {}) };
      },
      openAgentSettings: () => this.shell.openSettings("agent"),
      agentReady: () => this.agentReady(),
      mountAgentSetup: (container, onChange) => renderAgentSetup(container, this.agentSetupDeps(), { onChange, skills: false }),
      liveRunning: () => this.store.watch.running,
      connect: request => this.connectPlan(request),
      setDiscoveryFolder: (projectId, folder) => this.setDiscoveryFolder(projectId, folder),
    };
  }

  async connect(options: { source?: string | null; parentId?: string | null; dropped?: boolean; preset?: PlanCardOptions["preset"] } = {}): Promise<void> {
    if (this.card) { notice("The project connection dialog is already open."); return; }
    if (this.store.busy) { notice("Wait for the current Dashboard action to finish."); return; }
    const card: PlanCard = planCard(this.planHost(), { source: options.source, parentId: options.parentId, preset: options.preset, onClose: () => { if (this.card === card) this.card = null; } });
    this.card = card;
  }

  /** Connect the plan card's choice: one collector writer at a time, then live updates as chosen, then the new page. */
  private async connectPlan(request: ConnectInput): Promise<void> {
    await this.store.exclusive(async () => {
      const wasRunning = this.store.watch.running;
      if (wasRunning) await api.watchStop();
      let result: ConnectResult;
      try { result = await api.collector<ConnectResult>("connect", request.args, request.input, 600); }
      catch (error) {
        if (wasRunning) await api.watchStart().catch(() => {});
        throw error;
      }
      this.discovery.forget(item => item.id === request.discoveryId || item.folder === request.source);
      await this.store.reload();
      if (result.collectionHealth === "error") notice(`Project connected. First collection needs attention: ${result.collectionError || "check Live monitor"}`, 12000, "error");
      else notice(`Connected ${request.title}.${result.milestones?.addedCount ? ` Added ${result.milestones.addedCount} milestone${result.milestones.addedCount === 1 ? "" : "s"}.` : ""}`);
      if (result.milestones?.error) notice(`Milestones were not added: ${result.milestones.error}. Add them later from the project page.`, 10000, "error");
      if (wasRunning || request.keepUpdating) {
        try { this.store.watch = await api.watchStart(); }
        catch (error) { notice(`Project connected, but live updates could not start: ${errorMessage(error)}`, 10000, "error"); }
      }
      if (result.dashboardProjectId) this.shell.navigate({ projectId: result.dashboardProjectId });
    });
  }

  /* ---------- Sub-project discovery ---------- */

  discoveries(): { suggestions: DiscoverySuggestion[] } { return { suggestions: this.discovery.suggestions.map(item => ({ ...item, bestCandidate: item.bestCandidate ? { ...item.bestCandidate } : null })) }; }

  /** Needs attention → Add as sub-project / Ask agent: the plan card, preset with the folder, parent and best match. */
  async addDiscovery(id: string, options: { agent?: boolean } = {}): Promise<void> {
    const suggestion = this.discovery.find(id);
    if (!suggestion) throw new Error("This folder suggestion is no longer available. Scan for new folders again.");
    if (await api.pathKind(suggestion.folder) !== "dir") {
      this.discovery.forget(item => item.id === id);
      throw new Error(`${suggestion.relative} no longer exists in the discovery folder.`);
    }
    await this.connect({ source: suggestion.folder, parentId: suggestion.parentId, preset: { profileId: suggestion.bestCandidate?.profileId || null,
      projectId: suggestion.suggestedId, title: suggestion.suggestedTitle, agent: Boolean(options.agent) || !suggestion.bestCandidate, discoveryId: suggestion.id } });
  }

  async dismissDiscovery(id: string): Promise<void> { await this.discovery.dismiss(id); }

  async scanDiscoveries(projectId: string | null = null): Promise<void> { await this.discovery.scan(projectId, { quiet: false }); }

  /** "Use as discovery folder" from a folder dropped on a project page. */
  async setDiscoveryFolder(projectId: string, folder: string): Promise<void> {
    const current = await this.discovery.read(projectId).catch(() => null);
    await this.store.exclusive(() => this.discovery.save(projectId, { folder, autoAccept: Boolean(current?.autoAccept), ignore: current?.ignore || [] }));
    await this.store.reload();
    const title = this.store.model().projects.find(item => item.id === projectId)?.title || projectId;
    notice(`${folder.split(/[\\/]/).filter(Boolean).pop() || folder} is now the discovery folder for ${title}. New subfolders appear in Needs attention.`);
    this.discovery.request({ delay: 0, force: true });
  }

  async createProject(options: { parentId?: string } = {}): Promise<void> {
    const projects = hierarchyItems(this.store.model());
    newProjectDialog(projects, { parentId: options?.parentId ?? null }, input => this.store.exclusive(async () => {
      const values = validateNewProject(input, hierarchyItems(this.store.model()));
      const created = await api.collector<{ dashboardProjectId: string; title: string }>("create-project",
        ["--id", values.id, "--title", values.title, "--type", values.projectType, ...(values.parentId ? ["--parent", values.parentId] : [])]);
      if (created?.dashboardProjectId !== values.id) throw new Error("The project was not created. Check the collector in Settings and retry.");
      await this.store.reload();
      notice(`Created ${created.title || values.title}.`);
      this.shell.navigate({ projectId: values.id });
    }));
  }

  async collect(): Promise<void> {
    if (this.store.watch.running) { notice("Live updates are already collecting. Stop them before running Collect now.", 8000); return; }
    const results = await this.store.exclusive(() => api.collector<{ projectId: string; error?: string }[]>("collect", [], null, 900));
    await this.store.reload();
    const failed = Array.isArray(results) ? results.filter(item => item.error) : [];
    notice(Array.isArray(results) && results.length ? `Collected ${results.length} source(s)${failed.length ? ` · ${failed.length} need attention` : ""}.` : "No connected sources are configured.", failed.length ? 9000 : 5000, failed.length ? "error" : "info");
  }

  async start(): Promise<void> {
    await this.store.exclusive(async () => {
      const checks = await api.collector<{ checks?: { assignedToThisHost?: boolean; health?: string }[] }>("doctor");
      const owned = (checks?.checks || []).filter(item => item.assignedToThisHost);
      if (!owned.length) throw new Error("Connect a project folder on this computer before starting live updates.");
      if (owned.some(item => item.health === "error")) notice("Some source folders are unavailable; Live monitor shows their collection errors.", 9000);
      this.store.watch = await api.watchStart();
    });
  }

  async stop(): Promise<void> {
    await this.store.exclusive(async () => { this.store.watch = await api.watchStop(); });
  }

  async editProject(projectId: string): Promise<void> {
    const project = this.project(projectId);
    const original = await this.discovery.read(projectId).catch(() => null);
    const before = original ? { folder: original.folder, autoAccept: original.autoAccept, ignore: ignoreGlobs(original.ignore).join("\n") } : null;
    editProjectDialog(project, hierarchyItems(this.store.model()), (values, originalParentId, discovery) => this.store.exclusive(async () => {
      const patch = validateProjectValues(values, { projects: hierarchyItems(this.store.model()), projectId, originalParentId });
      await api.projectUpdate(projectId, patch);
      const changed = this.discoveryChange(before, discovery);
      if (changed) await this.discovery.save(projectId, changed);
      await this.store.reload();
      if (changed) this.discovery.request({ delay: 0, force: true });
      notice("Project updated.");
    }), original);
  }

  /** What Save should write for discovery, or null when nothing changed (a cleared folder removes the settings). */
  private discoveryChange(before: DiscoveryEdit | null, after: DiscoveryEdit | null): { clear?: boolean; folder?: string; autoAccept?: boolean; ignore?: string[] } | null {
    if (!before || !after) return null;
    const folder = after.folder.trim(), ignore = ignoreGlobs(after.ignore);
    if (!folder) return before.folder ? { clear: true } : null;
    if (folder === before.folder && after.autoAccept === before.autoAccept && ignore.join("\n") === ignoreGlobs(before.ignore).join("\n")) return null;
    return { folder: folder === before.folder ? undefined : folder, autoAccept: after.autoAccept, ignore };
  }

  async setParent(projectId: string, parentId: string | null): Promise<void> {
    await this.store.exclusive(async () => {
      const projects = hierarchyItems(this.store.model());
      if (!projects.some(item => item.projectId === projectId)) throw new Error("This project is no longer on the dashboard. Refresh and try again.");
      const value = validateParent(projects, projectId, parentId);
      await api.projectUpdate(projectId, { parentId: value });
      await this.store.reload();
    });
  }

  private papers(project: Project): PaperChoice[] {
    return project.sources.map(source => ({ source, paper: paperOf(source) }))
      .filter(item => item.source.kind === "latex" && item.paper)
      .map(item => ({ sourceId: item.source.id, title: item.source.title, chapters: item.paper!.chapters }));
  }

  async addMilestone(projectId: string, selection?: PaperBinding): Promise<void> {
    const project = this.project(projectId);
    const current = this.shell.current();
    const preset = selection ?? (current.projectId === projectId ? { sourceId: current.sourceId || undefined, chapterId: current.chapterId || undefined } : {});
    addMilestoneDialog(project, this.papers(project), preset, async (title, due, input) => {
      const binding = paperBinding(input);
      if (binding.sourceId && !this.papers(this.project(projectId)).some(item => item.sourceId === binding.sourceId && item.chapters.some(chapter => chapter.id === binding.chapterId))) {
        throw new Error("This chapter was removed or renamed. Refresh and choose a current chapter.");
      }
      await addMilestoneTo(this.store, projectId, title, due, binding);
      notice("Milestone added.");
    });
  }

  async setMilestone(projectId: string, request: SetMilestoneRequest): Promise<void> { await setMilestoneIn(this.store, projectId, request); }

  async quickMilestone(projectId: string, title: string): Promise<void> { await addMilestoneTo(this.store, projectId, title); }

  /** The list's inline composer: a title, an optional due date and (on a chapter's workbench) its chapter and stage. */
  async addMilestoneInline(projectId: string, input: { title: string; due?: string | null; sourceId?: string; chapterId?: string; stage?: string }): Promise<void> {
    const binding = paperBinding({ sourceId: input.sourceId, chapterId: input.chapterId, stage: input.stage });
    if (binding.sourceId && !this.papers(this.project(projectId)).some(item => item.sourceId === binding.sourceId && item.chapters.some(chapter => chapter.id === binding.chapterId))) {
      throw new Error("This chapter was removed or renamed. Refresh and choose a current chapter.");
    }
    await addMilestoneTo(this.store, projectId, input.title, input.due || null, binding);
  }

  /** Point a source at another folder: pick it, remap (live updates paused around the write), collect, report. */
  async changeSourceFolder(sourceId: string): Promise<void> {
    const source = this.store.model().projects.flatMap(project => project.sources).find(item => item.id === sourceId);
    if (!source) throw new Error("This source is no longer in the workspace. Refresh and try again.");
    const picked = await openDialog({ directory: true, multiple: false, title: `Choose the folder ${source.title} should follow`, defaultPath: source.directory || undefined });
    if (typeof picked !== "string" || !picked) return;
    const name = picked.split(/[\\/]/).filter(Boolean).pop() || picked;
    if (source.directory && picked.replace(/[\\/]+$/, "").toLowerCase() === source.directory.replace(/[\\/]+$/, "").toLowerCase()) { notice(`${source.title} already follows ${name}.`); return; }
    const result = await this.store.exclusive(() => this.withPausedWatcher(() => api.sourceRemap(sourceId, picked)));
    if (!this.store.watch.running) await api.collector("collect", [], null, 600).catch(() => {});
    await this.store.reload();
    const warnings = Array.isArray(result?.warnings) && result.warnings.length ? ` ${result.warnings.join(" ")}` : "";
    notice(`${source.title} now follows ${name}. Writing activity starts again from this folder.${warnings}`, warnings ? 12000 : 7000, warnings ? "error" : "info");
  }

  async editMilestone(projectId: string, request: { line: number; text: string; title?: string; due?: string | null }): Promise<void> {
    await editMilestoneIn(this.store, projectId, request);
  }

  async deleteMilestone(projectId: string, request: { line: number; text: string }): Promise<void> {
    const undo = await deleteMilestoneIn(this.store, projectId, request);
    notice("Milestone deleted.", 8000, "info", { label: "Undo", run: () => undo().catch(error => notice(errorMessage(error), 8000, "error")) });
  }

  async removeProject(projectId: string): Promise<void> {
    const project = this.project(projectId);
    removeProjectDialog(project, () => this.store.exclusive(async () => {
      let cleanup: string | null = null;
      await this.withPausedWatcher(async () => {
        await api.projectRemove(projectId);
        try { await api.collector("reconcile", ["--apply"]); }
        catch (error) { cleanup = errorMessage(error); }
      });
      if (this.shell.current().projectId === projectId) this.shell.navigate({ projectId: null });
      await this.store.reload();
      if (cleanup) notice(`Project removed. Internal cleanup needs attention: ${cleanup}`, 10000, "error");
      else notice("Project removed. Its files are recoverable from the workspace's .trash folder.");
    }));
  }

  async openProject(projectId: string, _target?: NavTarget | boolean): Promise<void> { this.shell.navigate({ projectId }); }
  async openOverall(_target?: NavTarget | boolean): Promise<void> { this.shell.navigate({ projectId: null }); }
  async focusChapter(projectId: string, sourceId: string, chapterId: string | null): Promise<void> { this.shell.navigate({ projectId, sourceId, chapterId }); }

  async openSource(sourceId: string, chapterId: string | null = null): Promise<void> {
    const source = this.store.model().projects.flatMap(project => project.sources).find(item => item.id === sourceId);
    const paper = paperOf(source);
    const chapter = chapterId ? paper?.chapters.find(item => item.id === chapterId) : null;
    if (chapterId && !chapter) throw new Error("Choose a current paper chapter.");
    const relative = chapter?.file || (chapterId === null && !paper ? null : paper?.mainFile) || null;
    await api.openSource(sourceId, typeof relative === "string" ? relative : null);
  }

  async openSettings(): Promise<void> { this.shell.openSettings(); }
  async importVault(): Promise<void> { this.shell.importVault(); }
  /** Showing the widget switches to it: the main window goes to the tray (the widget's open button or the tray icon
   * brings it back). Hiding the widget from here leaves the main window as it is. */
  async toggleWidget(): Promise<void> {
    const show = !this.store.settings?.widget.visible;
    await api.widgetSetVisible(show);
    if (show) await getCurrentWindow().hide();
  }
  async revealWorkspace(): Promise<void> { if (this.store.workspace) await api.revealPath(this.store.workspace.root); }
}
