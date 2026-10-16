/* The plan card: connect a folder by choosing what to monitor. Ported from the Obsidian plugin's ConnectProjectModal.
 *
 * Steps: a folder (picked, pasted or dropped on the window) → `inspect` ranks the monitoring types that match → the card
 * preselects the best one, previews what it would read from the current files (`plan preview`), and asks only for a
 * name and a parent project. Everything else folds into Options (type, milestones, check interval, live updates, the
 * project ID and a JSON editor for the monitoring profile). When nothing fits, or the user asks, an agent designs a plan
 * (read-only, through the Rust side so the key never reaches this page); a plan can be saved as a monitoring type so
 * folders like it are recognised without an agent next time. Nothing is written until Connect. */

import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { bestOption, confidenceText, connectRequest, elapsedText, folderTitle, INTERVAL_CHOICES, planOptions, previewRows, ruleSummary,
  templatesForType, type Inspection, type PlanOption, type PreviewResult, type TemplateSummary, type Validation } from "../model/cli";
import { eligibleParents, errorMessage, isSafeId, normalizeParentId, PROJECT_TYPES, slugId, uniqueProjectId, type HierarchyItem } from "../model/rules";
import type { AgentSetupHandle } from "./agent-setup";
import { dropdown, fillSelect, h, Modal, setBusy, setting, textInput, toggle } from "./modal";
import { notice } from "./notice";

export interface AgentSummary { id: string; title: string; installed: boolean | null; local: boolean }
export interface AgentResponse {
  ok?: boolean; plan?: Record<string, unknown> | null; preview?: Record<string, unknown> | null; validation?: Validation;
  warnings?: string[]; errors?: string[]; repaired?: boolean; provider?: { id?: string; durationMs?: number };
}
export interface AgentRun { promise: Promise<AgentResponse>; cancel(): void }
export interface ConnectInput { args: string[]; input: string | null; title: string; keepUpdating: boolean; discoveryId: string | null; source: string }

export interface PlanCardHost {
  projects(): HierarchyItem[];
  inspect(source: string): Promise<Inspection>;
  templates(): Promise<TemplateSummary[]>;
  preview(request: Record<string, unknown>): Promise<PreviewResult>;
  getProfile(id: string): Promise<{ profile?: Record<string, unknown> | null }>;
  validateProfile(profile: Record<string, unknown>): Promise<Validation>;
  saveProfile(profile: Record<string, unknown>): Promise<{ saved?: string; unchanged?: boolean }>;
  agentSummary(): Promise<AgentSummary | null>;
  agentTimeoutMinutes(): number;
  proposePlan(payload: Record<string, unknown>): Promise<AgentRun>;
  openAgentSettings(): void;
  /** Whether an agent can run now; when not, the card shows the setup panel first. */
  agentReady(): Promise<boolean>;
  mountAgentSetup(container: HTMLElement, onChange: () => void): AgentSetupHandle;
  liveRunning(): boolean;
  connect(request: ConnectInput): Promise<void>;
  setDiscoveryFolder(projectId: string, folder: string): Promise<void>;
}

export interface PlanCardOptions {
  source?: string | null;
  parentId?: string | null;
  /** A discovery suggestion presets the monitoring type, name, ID and (for unmatched folders) the agent panel. */
  preset?: { profileId?: string | null; projectId?: string | null; title?: string | null; agent?: boolean; discoveryId?: string | null;
    /** A plan designed by an agent through `ma connect` (checked and previewed here before Connect). */
    plan?: Record<string, unknown> | null; fromAgent?: boolean } | null;
  onClose?: () => void;
}

type Focus = { key?: string; focus?: string } | null;
interface PreviewState { status: "idle" | "loading" | "ready" | "error"; result: PreviewResult | null; key: string | null; error: string }
interface EditorState { text: string; dirty: boolean; errors: string[]; warnings: string[]; status: string }

const cleanPath = (value: string) => String(value || "").trim().replace(/^"(.*)"$/, "$1");
const isAbsolute = (value: string) => /^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(value);

export function planCard(host: PlanCardHost, options: PlanCardOptions = {}): PlanCard {
  const card = new PlanCard(host, options);
  card.open();
  return card;
}

export class PlanCard {
  private modal!: Modal;
  private root!: HTMLElement;
  private sourcePath: string;
  private requestedParentId: string | null;
  private preset: { source: string; profileId: string | null; projectId: string | null; title: string; agent: boolean; discoveryId: string | null; plan: Record<string, unknown> | null } | null;
  private inspection: Inspection | null = null;
  private profileId = "";
  private selectedKey: string | null = null;
  private projectTitle = ""; private projectId = ""; private projectType = "other"; private parentId = ""; private milestoneTemplate = "";
  private interval = 60; private keepUpdating = true;
  private busy = false; private closed = false;
  private idEdited = false; private titleEdited = false; private typeEdited = false; private parentEdited = false; private templateEdited = false;
  private dashboardProjectId = "";
  private hierarchy: HierarchyItem[] = [];
  private templates: TemplateSummary[] = [];
  private parentNotice = "";
  private agentPlan: PlanOption | null = null;
  private customPlan: PlanOption | null = null;
  private agent = { open: false, running: false, startedAt: 0, run: null as AgentRun | null, cancelled: false, instructions: "", error: "", timer: null as number | null, seq: 0, setup: false };
  private setupHandle: AgentSetupHandle | null = null;
  private provider: AgentSummary | null = null;
  private previewState: PreviewState = { status: "idle", result: null, key: null, error: "" };
  private previewTimer: number | null = null;
  private previewSeq = 0;
  private editor: EditorState = { text: "", dirty: false, errors: [], warnings: [], status: "" };
  private saveForm: { title: string; id: string; idEdited: boolean; error: string } | null = null;
  private optionsSource: Inspection | null = null; private optionsCache: PlanOption[] = [];
  private othersOpen = false; private notesOpen = false; private optionsOpen = false;
  private enteredStep: string | null = null;
  private focusNext: Focus = null;
  private optionRows = new Map<string, HTMLElement>();
  private focusables = new Map<string, HTMLElement>();
  /* Live nodes of the current render. */
  private errorEl: HTMLElement | null = null; private progressEl: HTMLElement | null = null; private pathInput: HTMLInputElement | null = null;
  private titleInput: HTMLInputElement | null = null; private idInput: HTMLInputElement | null = null; private summaryEl: HTMLElement | null = null;
  private previewEl: HTMLElement | null = null; private optionsEl: HTMLDetailsElement | null = null; private optionsValuesEl: HTMLElement | null = null;
  private templateSelect: HTMLSelectElement | null = null; private editorEl: HTMLTextAreaElement | null = null; private editorStatusEl: HTMLElement | null = null;
  private elapsedEl: HTMLElement | null = null; private saveIdInput: HTMLInputElement | null = null;

  private onClosed: (() => void) | null;

  constructor(private host: PlanCardHost, options: PlanCardOptions) {
    this.onClosed = options.onClose || null;
    this.sourcePath = options.source ? String(options.source) : "";
    this.requestedParentId = normalizeParentId(options.parentId) || null;
    const preset = options.preset;
    this.preset = preset ? {
      source: this.sourcePath, profileId: isSafeId(preset.profileId) ? preset.profileId : null, projectId: isSafeId(preset.projectId) ? preset.projectId : null,
      title: typeof preset.title === "string" ? preset.title.trim() : "", agent: Boolean(preset.agent), discoveryId: typeof preset.discoveryId === "string" ? preset.discoveryId : null,
      plan: preset.plan && typeof preset.plan === "object" ? preset.plan : null,
    } : null;
  }

  open(): void {
    this.modal = new Modal("Connect project folder", "rd-connect-modal rd-plan-shell");
    this.modal.contentEl.replaceChildren();
    this.root = h(this.modal.contentEl, "div", { cls: "rd-connect-modal rd-plan-card" });
    this.modal.onClose = () => this.onClose();
    this.hierarchy = this.host.projects();
    void this.host.agentSummary().then(summary => { this.provider = summary; if (!this.closed && (this.agent.open || this.agent.running)) this.render(); }).catch(() => {});
    this.render();
    if (this.sourcePath) void this.inspectFolder();
    else window.setTimeout(() => void this.browseFolder({ inspect: true, auto: true }), 60);
  }

  close(): void { this.modal.close(); }

  private onClose(): void {
    this.closed = true;
    this.setupHandle?.dispose();
    this.cancelAgent(true);
    if (this.previewTimer !== null) window.clearTimeout(this.previewTimer);
    this.previewTimer = null;
    this.onClosed?.();
  }

  /** The workspace changed while the card is open (a project was removed, renamed or added). */
  updateProjects(projects: HierarchyItem[]): void {
    if (this.closed) return;
    const removed = this.hierarchy.filter(item => !projects.some(next => next.projectId === item.projectId)).map(item => item.projectId);
    this.hierarchy = projects;
    if (removed.includes(this.parentId)) { this.parentId = ""; this.parentNotice = "The selected parent project was removed. Choose another parent or None."; }
    const selected = this.dashboardProjectId;
    if (selected && removed.includes(selected)) { this.dashboardProjectId = ""; if (this.inspection) this.applyProfileSuggestions(); }
    if (this.inspection && !this.busy) {
      const message = this.errorEl?.textContent || "";
      this.render();
      if (selected && !this.dashboardProjectId) this.showError(new Error("The selected project was removed. Choose another project, or create a new project."));
      else if (message && this.errorEl) this.errorEl.textContent = message;
    }
  }

  private showError(error: unknown): void { if (!this.closed && this.errorEl) this.errorEl.textContent = errorMessage(error); }

  private setBusy(value: boolean, message = ""): void {
    this.busy = value;
    this.modal.busyLock = value;
    setBusy(this.root, value);
    if (this.progressEl) this.progressEl.textContent = message;
  }

  // ---------- Options ----------

  private baseOptions(): PlanOption[] {
    if (this.optionsSource !== this.inspection) { this.optionsSource = this.inspection; this.optionsCache = planOptions(this.inspection); }
    return this.optionsCache;
  }

  private options(): PlanOption[] { return [this.agentPlan, this.customPlan, ...this.baseOptions()].filter(Boolean) as PlanOption[]; }

  private legacy(): boolean { return Boolean(this.inspection) && !Array.isArray(this.inspection!.candidates); }

  private selectedOption(): PlanOption | null {
    const options = this.options();
    const chosen = options.find(item => item.key === this.selectedKey);
    if (chosen) return chosen;
    const byEntry = this.profileId ? options.find(item => item.entries?.some(entry => entry.id === this.profileId)) : null;
    return byEntry || bestOption(options) || null;
  }

  private selectedEntry(option = this.selectedOption()) {
    const entries = option?.entries || [];
    return entries.find(item => item.id === this.profileId) || entries[0] || null;
  }

  /** The profile object behind an agent or edited plan. */
  private planProfile(option: PlanOption | null): Record<string, unknown> | null {
    return option && (option.mode === "agent" || option.mode === "custom") ? option.profile || null : null;
  }

  private selectOption(key: string): void {
    if (this.busy) return;
    const option = this.options().find(item => item.key === key);
    if (!option) return;
    this.selectedKey = key;
    if (!option.entries?.some(entry => entry.id === this.profileId)) this.profileId = option.entries?.[0]?.id || "";
    this.editor = { text: "", dirty: false, errors: [], warnings: [], status: "" };
    this.saveForm = null;
    this.applyProfileSuggestions();
    this.focusNext = { key };
    this.render();
    this.schedulePreview();
  }

  /** Radio-group keys: arrows move and select (wrapping), Home/End jump to the first or last option. */
  private optionKey(event: KeyboardEvent, key: string): void {
    const step = ({ ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 } as Record<string, number>)[event.key];
    if (!step && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    const keys = this.options().map(item => item.key);
    if (!keys.length) return;
    const index = keys.indexOf(key);
    const next = keys[event.key === "Home" ? 0 : event.key === "End" ? keys.length - 1 : (index + step + keys.length) % keys.length];
    if (next === this.selectedKey) this.optionRows.get(next)?.focus();
    else this.selectOption(next);
  }

  private applyProfileSuggestions(): void {
    const option = this.selectedOption();
    if (!option || !this.inspection) return;
    if (!this.selectedKey) this.selectedKey = option.key;
    const entry = this.selectedEntry(option);
    if (entry && this.profileId !== entry.id) this.profileId = entry.id;
    const suggestion = entry || option;
    if (!this.titleEdited) {
      const fallback = folderTitle(this.inspection.title || this.sourcePath);
      this.projectTitle = this.dashboardProjectId ? (suggestion.suggestedTitle || ("label" in suggestion ? suggestion.label : null) || option.title || fallback) : fallback;
      if (this.titleInput) this.titleInput.value = this.projectTitle;
    }
    if (!this.idEdited) {
      const suggested = suggestion.suggestedId || option.suggestedId || this.inspection.suggestedId;
      this.projectId = isSafeId(suggested) ? suggested : slugId(this.projectTitle);
      if (this.idInput) this.idInput.value = this.projectId;
    }
    if (!this.typeEdited) this.projectType = option.projectType || "other";
    if (!this.templateEdited) this.milestoneTemplate = this.defaultTemplate(option);
  }

  private templateChoices(option = this.selectedOption()): [string, string][] {
    const choices = templatesForType(this.templates, this.projectType).map(item => [item.id, item.title || item.id] as [string, string]);
    if (option && Array.isArray(option.milestoneTemplate) && option.milestoneTemplate.length) choices.unshift(["profile", `From this monitoring type (${option.milestoneTemplate.length})`]);
    return choices;
  }

  private defaultTemplate(option: PlanOption | null): string {
    const choices = this.templateChoices(option);
    if (Array.isArray(option?.milestoneTemplate) && choices.some(([value]) => value === "profile")) return "profile";
    return typeof option?.milestoneTemplate === "string" && choices.some(([value]) => value === option.milestoneTemplate) ? option.milestoneTemplate : "";
  }

  private eligibleParentList(): HierarchyItem[] { return eligibleParents(this.hierarchy); }

  /** A drop on a project page presets its top-level ancestor as parent (one displayed level). */
  private applyRequestedParent(): void {
    const requested = this.requestedParentId;
    if (!requested || this.parentEdited) return;
    const target = this.hierarchy.find(item => item.projectId === requested);
    const top = normalizeParentId(target?.parentId) || requested;
    if (this.eligibleParentList().some(item => item.projectId === top)) { this.parentId = top; this.parentNotice = ""; }
    else { this.parentId = ""; this.parentNotice = "The project you dropped onto is unavailable as a parent. Choose a top-level project or None."; }
  }

  // ---------- Rendering ----------

  /** Progressive disclosure: the selected option, its preview, Name and Parent show by default; the rest folds into one
   * Options disclosure. Without a selection the card shows only the no-match message and Ask agent. */
  private render(): void {
    if (this.closed) return;
    const container = this.root;
    const restore = this.focusNext || this.activeFocus();
    this.focusNext = null;
    container.replaceChildren();
    const step = this.inspection ? "plan" : "folder";
    container.classList.toggle("is-entering", this.enteredStep !== step);
    this.enteredStep = step;
    this.titleInput = null; this.idInput = null; this.summaryEl = null; this.previewEl = null; this.optionsEl = null; this.optionsValuesEl = null;
    this.templateSelect = null; this.editorEl = null; this.editorStatusEl = null; this.elapsedEl = null; this.saveIdInput = null;
    this.optionRows = new Map(); this.focusables = new Map();
    if (!this.inspection) { this.renderFolderStep(container); this.applyFocus(restore); return; }
    const option = this.selectedOption();
    if (option && !this.selectedKey) this.selectedKey = option.key;

    const header = h(container, "div", { cls: "rd-plan-header" });
    h(header, "p", { text: this.dashboardProjectId ? "Add a source" : "New monitored project", cls: "rd-plan-eyebrow" });
    h(header, "h2", { text: folderTitle(this.inspection.title || this.sourcePath) || this.sourcePath, cls: "rd-plan-title" });
    const where = h(header, "div", { cls: "rd-plan-where" });
    h(where, "p", { text: this.sourcePath, cls: "rd-connect-source rd-plan-path", attr: { title: this.sourcePath } });
    const change = this.focusable(h(where, "button", { text: "Change folder", cls: "rd-plan-link", attr: { type: "button" } }), "change-folder");
    change.addEventListener("click", () => { if (!this.busy) this.backToFolder(); });
    const discoveryTarget = this.discoveryTarget();
    if (discoveryTarget) {
      const use = h(where, "button", { text: `Use as discovery folder for ${discoveryTarget.title}`, cls: "rd-plan-link rd-plan-discovery",
        attr: { type: "button", title: "Scan this folder for new sub-projects instead of monitoring it" } });
      use.addEventListener("click", () => void this.useAsDiscoveryFolder(discoveryTarget.projectId));
    }

    this.renderOptions(container, option);
    if (option || this.legacy()) {
      this.renderPreviewSection(container);
      this.renderProjectFields(container, option);
    }
    this.renderFooter(container, option);
    this.applyFocus(restore);
  }

  private focusable<T extends HTMLElement>(node: T, id: string): T {
    node.setAttribute("data-rd-focus", id);
    this.focusables.set(id, node);
    return node;
  }

  private activeFocus(): Focus {
    const active = document.activeElement as HTMLElement | null;
    if (!active || !this.root?.contains(active)) return null;
    const key = active.getAttribute("data-key");
    if (key) return { key };
    const focus = active.getAttribute("data-rd-focus");
    return focus ? { focus } : null;
  }

  private applyFocus(target: Focus): void {
    if (!target || this.closed) return;
    const node = target.key ? this.optionRows.get(target.key) : target.focus ? this.focusables.get(target.focus) : null;
    if (node && !(node as HTMLButtonElement).disabled) node.focus();
  }

  /** The drop target project that may take this folder as its discovery folder (a drop on a project page only). */
  private discoveryTarget(): HierarchyItem | null {
    if (!this.requestedParentId || this.preset?.discoveryId || !this.parentId || this.dashboardProjectId) return null;
    const target = this.hierarchy.find(item => item.projectId === this.parentId);
    return target && !normalizeParentId(target.parentId) ? target : null;
  }

  private async useAsDiscoveryFolder(projectId: string): Promise<void> {
    if (this.busy || this.closed) return;
    if (this.errorEl) this.errorEl.textContent = "";
    this.setBusy(true, "Saving the discovery folder…");
    try { await this.host.setDiscoveryFolder(projectId, this.sourcePath); if (!this.closed) { this.setBusy(false); this.close(); } }
    catch (error) { this.showError(error); }
    finally { if (!this.closed) this.setBusy(false); }
  }

  private renderFolderStep(container: HTMLElement): void {
    h(container, "h2", { text: "Choose a project folder" });
    h(container, "p", { text: "Choose the folder containing your paper, simulation files or case folders. Files stay where they are and are only read. You can also drop a folder onto the dashboard.", cls: "rd-connect-description" });
    this.errorEl = h(container, "div", { cls: "rd-connect-error", attr: { role: "alert" } });
    const row = setting(container, "Project folder", "Select a folder, or paste its full local path.");
    this.pathInput = textInput(row, this.sourcePath, { placeholder: "C:\\Research\\My project", onChange: value => { this.sourcePath = cleanPath(value); } });
    this.pathInput.classList.add("rd-connect-path");
    this.focusable(this.pathInput, "path");
    this.pathInput.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); void this.inspectFolder(); } });
    const browse = h(row.control, "button", { text: "Browse…", cls: "rd-plan-chip-button", attr: { type: "button" } });
    browse.addEventListener("click", () => void this.browseFolder({ inspect: true }));
    const footer = h(container, "div", { cls: "rd-plan-footer rd-plan-folder-footer" });
    this.progressEl = h(h(footer, "div", { cls: "rd-plan-footer-leading" }), "div", { cls: "rd-connect-progress", attr: { role: "status" } });
    const trailing = h(footer, "div", { cls: "rd-plan-footer-trailing" });
    const cancel = h(trailing, "button", { text: "Cancel", cls: "rd-plan-chip-button is-quiet", attr: { type: "button" } });
    cancel.addEventListener("click", () => this.close());
    const inspect = h(trailing, "button", { text: "Inspect folder", cls: "mod-cta rd-plan-connect", attr: { type: "button" } });
    inspect.addEventListener("click", () => void this.inspectFolder());
  }

  private backToFolder(): void {
    this.cancelAgent(true);
    this.inspection = null; this.agentPlan = null; this.customPlan = null; this.selectedKey = null; this.profileId = "";
    this.previewState = { status: "idle", result: null, key: null, error: "" };
    this.focusNext = { focus: "path" };
    this.render();
  }

  private renderOptions(container: HTMLElement, selected: PlanOption | null): void {
    const section = h(container, "section", { cls: "rd-plan-section rd-plan-monitoring" });
    const head = h(section, "div", { cls: "rd-plan-section-head" });
    h(head, "h3", { text: "What to monitor" });
    h(head, "p", { text: this.legacy() ? "Detected from the folder's files." : "Detected from file patterns. Nothing is written until you connect.", cls: "rd-plan-caption" });
    const options = this.options();
    const others = options.filter(item => item.key !== selected?.key);
    if (!selected && !this.legacy()) {
      const none = h(section, "div", { cls: "rd-plan-nomatch" });
      h(none, "p", { text: "No monitoring type fits this folder yet", cls: "rd-plan-nomatch-title" });
      h(none, "p", { text: "An agent can design a monitoring plan from the folder's file names and structure.", cls: "rd-plan-caption" });
      if (!this.agent.open && !this.agent.running) this.renderAgentChoices(none, false);
    }
    const group = h(section, "div", { cls: "rd-plan-options", attr: { role: "radiogroup", "aria-label": "Monitoring type" } });
    const tabStop = selected?.key || others[0]?.key;
    if (selected) this.renderOption(group, selected, true, tabStop);
    if (others.length) {
      const more = h(group, "details", { cls: "rd-plan-weak" });
      more.open = this.othersOpen;
      more.addEventListener("toggle", () => { this.othersOpen = more.open; });
      this.focusable(h(more, "summary", { text: `Other matches · ${others.length}` }), "other-matches");
      const list = h(more, "div", { cls: "rd-plan-options" });
      for (const option of others) this.renderOption(list, option, false, tabStop);
    }
    if (selected && selected.entries?.length > 1) {
      const row = setting(section, "Entry", "This folder has several connectable entries; choose the one to monitor.");
      dropdown(row, selected.entries.map(entry => [entry.id, entry.label || entry.id] as [string, string]), this.profileId, value => {
        this.profileId = value;
        this.applyProfileSuggestions();
        this.renderProfileSummary();
        this.schedulePreview();
      });
    }
    if (selected) this.renderPlanProblems(section, selected);
    if (this.legacy() && this.inspection?.warnings?.length) this.renderNotes(section, this.inspection.warnings);
    if (this.agent.setup) { this.renderAgentSetupStep(section); return; }
    if (!this.legacy() && selected && !this.agent.open && !this.agent.running && !this.agentPlan) this.renderAgentChoices(section, true);
    if (!this.legacy() && (this.agent.open || this.agent.running || this.agentPlan)) this.renderAgent(section);
  }

  /* No working agent yet: connect one here, then Continue runs what was asked (Smart monitoring or Design plan). */
  private renderAgentSetupStep(section: HTMLElement): void {
    this.setupHandle?.dispose();
    const panel = h(section, "div", { cls: "rd-plan-agent rd-plan-agent-setup" });
    const head = h(panel, "div", { cls: "rd-plan-agent-head" });
    h(head, "span", { text: "Connect an agent first", cls: "rd-plan-agent-title" });
    h(panel, "p", { cls: "rd-plan-privacy", text: "Pick the agent that designs the plan. It only reads file names and short excerpts; nothing is written. This is saved for next time." });
    this.setupHandle = this.host.mountAgentSetup(panel, () => { void this.host.agentSummary().then(summary => { this.provider = summary; }).catch(() => {}); });
    const actions = h(panel, "div", { cls: "rd-plan-agent-actions" });
    const cancel = h(actions, "button", { text: "Cancel", cls: "rd-plan-chip-button is-quiet", attr: { type: "button" } });
    cancel.addEventListener("click", () => { this.agent.setup = false; this.setupHandle?.dispose(); this.setupHandle = null; this.focusNext = { focus: "smart" }; this.render(); });
    const go = this.focusable(h(actions, "button", { text: "Continue", cls: "rd-plan-chip-button rd-plan-agent-run", attr: { type: "button" } }), "agent-continue");
    go.addEventListener("click", async () => {
      if (!(await this.setupHandle?.ready())) { this.agent.error = ""; const hint = panel.querySelector(".rd-agent-status"); hint?.classList.add("is-error"); return; }
      this.agent.setup = false; this.setupHandle?.dispose(); this.setupHandle = null;
      this.provider = await this.host.agentSummary().catch(() => this.provider);
      await this.askAgent();
    });
  }

  private renderOption(list: HTMLElement, option: PlanOption, checked: boolean, tabStop: string | undefined): void {
    const row = h(list, "button", { cls: `rd-plan-option${checked ? " is-selected" : ""}${option.mode === "agent" ? " is-agent" : ""}`,
      attr: { type: "button", role: "radio", "aria-checked": checked ? "true" : "false", "data-key": option.key, tabindex: option.key === tabStop ? "0" : "-1" } });
    row.addEventListener("click", () => { if (!checked) this.selectOption(option.key); });
    row.addEventListener("keydown", event => this.optionKey(event, option.key));
    this.optionRows.set(option.key, row);
    h(row, "span", { cls: "rd-plan-radio", attr: { "aria-hidden": "true" } });
    const body = h(row, "div", { cls: "rd-plan-option-body" });
    const top = h(body, "div", { cls: "rd-plan-option-top" });
    h(top, "span", { text: option.title, cls: "rd-plan-option-title" });
    const origin = option.mode === "agent" ? "Agent plan" : option.mode === "custom" ? "Edited" : option.savedAs || option.origin === "user" ? "Saved type" : option.mode === "legacy" ? "Detected" : "Built-in";
    h(top, "span", { text: origin, cls: "rd-plan-chip" });
    if (option.mode === "library") {
      const confidence = h(top, "span", { cls: "rd-plan-confidence" });
      h(confidence, "span", { text: confidenceText(option.confidence) });
      const meter = h(confidence, "span", { cls: "rd-plan-meter", attr: { "aria-hidden": "true" } });
      h(meter, "span").style.setProperty("width", `${Math.round(Math.max(0, Math.min(1, option.confidence)) * 100)}%`);
    }
    if (option.mode === "agent" || option.mode === "custom") {
      if (option.rationale) h(body, "p", { text: option.rationale, cls: "rd-plan-rationale" });
      if (option.warnings?.length) {
        const warnings = h(body, "ul", { cls: "rd-plan-rules is-warnings" });
        for (const message of option.warnings.slice(0, 6)) h(warnings, "li", { text: String(message) });
      }
      if (option.savedAs) h(body, "p", { text: `Saved as monitoring type “${option.savedTitle || option.savedAs}”.`, cls: "rd-plan-caption" });
      return;
    }
    const rules = ruleSummary(option.matched);
    if (rules.shown.length) {
      const lines = h(body, "ul", { cls: "rd-plan-rules" });
      for (const line of rules.shown) h(lines, "li", { text: line });
      if (rules.more) h(lines, "li", { text: `+${rules.more} more`, cls: "is-more" });
    } else if (option.mode === "library" && option.missed.length) {
      h(body, "p", { text: ruleSummary(option.missed, 1).shown[0] || "", cls: "rd-plan-caption" });
    }
    if (checked) { this.summaryEl = h(body, "ul", { cls: "rd-plan-rules rd-connect-summary" }); this.renderProfileSummary(); }
  }

  /** An invalid agent plan: one plain sentence and a fix action; the validator's messages wait under Details. */
  private renderPlanProblems(section: HTMLElement, option: PlanOption): void {
    if (option.mode !== "agent" || option.validation?.ok !== false) return;
    const errors = (option.validation.errors || []).map(String);
    const box = h(section, "div", { cls: "rd-plan-problems", attr: { role: "group", "aria-label": "Plan problems" } });
    const line = h(box, "div", { cls: "rd-plan-problems-line" });
    const count = errors.length || 1;
    h(line, "p", { text: `This plan has ${count} ${count === 1 ? "problem" : "problems"}.`, cls: "rd-plan-problems-title" });
    if (!this.agent.running) {
      const fix = this.focusable(h(line, "button", { text: "Ask agent to fix", cls: "rd-plan-chip-button", attr: { type: "button" } }), "fix-plan");
      fix.addEventListener("click", () => this.askAgentToFix());
    }
    if (errors.length) {
      const details = h(box, "details", { cls: "rd-plan-problem-details" });
      h(details, "summary", { text: `Details · ${errors.length}` });
      const list = h(details, "ul", { cls: "rd-plan-option-errors" });
      for (const message of errors.slice(0, 12)) h(list, "li", { text: message });
    }
  }

  private askAgentToFix(): void {
    const option = this.selectedOption();
    if (this.busy || this.agent.running || !option) return;
    const errors = (option.validation?.errors || []).map(String).slice(0, 12);
    this.agent.instructions = ["Fix these problems in the plan:", ...errors.map(message => `- ${message}`)].join("\n");
    this.agent.open = true; this.agent.error = "";
    this.focusNext = { focus: "instructions" };
    this.render();
  }

  /** One "Notes · N" disclosure (folder detection notes, then preview notes). */
  private renderNotes(parent: HTMLElement, warnings: unknown[]): void {
    const list = (warnings || []).map(String).filter(Boolean);
    if (!list.length) return;
    const notes = h(parent, "details", { cls: "rd-connect-warnings rd-plan-notes rd-plan-preview-notes" });
    notes.open = this.notesOpen;
    notes.addEventListener("toggle", () => { this.notesOpen = notes.open; });
    h(notes, "summary", { text: `Notes · ${list.length}` });
    for (const message of list.slice(0, 20)) h(notes, "p", { text: message });
  }

  private renderProfileSummary(): void {
    if (!this.summaryEl) return;
    this.summaryEl.replaceChildren();
    const entry = this.selectedEntry();
    if (!entry || !(entry.summary || []).length || this.previewState.result?.preview) return;
    for (const line of entry.summary || []) h(this.summaryEl, "li", { text: String(line) });
  }

  // ---------- Agent ----------

  /* Two ways to let an agent design the plan: Smart monitoring runs at once from the folder alone; Add requirements opens
   * a field for special requirements first. Either way the plan lands on the card to check, customise and connect.
   * compact: one quiet line under a detected type ("Or let an agent design it: …"). */
  private renderAgentChoices(parent: HTMLElement, compact: boolean): void {
    const provider = this.provider;
    const line = h(parent, "div", { cls: compact ? "rd-plan-ask-line" : "rd-plan-agent-choices" });
    if (compact) h(line, "span", { text: "Or let an agent design it:", cls: "rd-plan-caption" });
    const smart = this.focusable(h(line, "button", { text: "Smart monitoring", cls: compact ? "rd-plan-link rd-plan-smart" : "rd-plan-chip-button rd-plan-ask rd-plan-smart", attr: { type: "button" } }), "smart");
    smart.title = "The agent designs a monitoring plan from this folder now; you check it before connecting";
    smart.addEventListener("click", () => void this.smartMonitoring());
    if (compact) h(line, "span", { text: "·", cls: "rd-plan-caption", attr: { "aria-hidden": "true" } });
    const custom = this.focusable(h(line, "button", { text: "Add requirements…", cls: compact ? "rd-plan-link" : "rd-plan-chip-button is-quiet", attr: { type: "button" } }), "requirements");
    custom.title = "Tell the agent what matters first (files to follow, metrics, what counts as done)";
    custom.addEventListener("click", () => this.openAgent());
    if (!compact) {
      const note = h(parent, "p", { cls: "rd-plan-privacy rd-plan-choices-privacy" });
      note.textContent = provider?.local ? `${provider.title} runs on this computer and only reads file names and short excerpts. ` : `Uses ${provider?.title || "the agent chosen in Settings"}: file names and short excerpts are sent, nothing is written. `;
      const settings = h(note, "button", { text: "Agent settings", cls: "rd-plan-link", attr: { type: "button" } });
      settings.addEventListener("click", () => this.host.openAgentSettings());
    }
  }

  /** Smart monitoring: ask the agent straight away, without instructions. */
  private async smartMonitoring(): Promise<void> {
    if (this.busy || this.agent.running || this.legacy()) return;
    this.agent.instructions = "";
    await this.askAgent();
  }

  /** Add requirements: open the field for special requirements, then Design plan. */
  private openAgent(): void {
    if (this.busy || this.legacy()) return;
    this.agent.open = true; this.agent.error = "";
    this.focusNext = { focus: "instructions" };
    this.render();
  }

  private renderAgent(section: HTMLElement): void {
    const panel = h(section, "div", { cls: `rd-plan-agent${this.agent.running ? " is-running" : ""}` });
    const provider = this.provider;
    const head = h(panel, "div", { cls: "rd-plan-agent-head" });
    const smartRun = this.agent.running && !this.agentPlan && !String(this.agent.instructions || "").trim();
    h(head, "span", { text: this.agentPlan ? "Revise with an agent" : smartRun ? "Smart monitoring" : "Special requirements", cls: "rd-plan-agent-title" });
    h(head, "span", { cls: "rd-plan-caption", text: provider ? `${provider.title}${provider.installed === false ? " · not found on this computer" : ""}` : "No agent configured" });
    const settings = h(head, "button", { text: "Settings", cls: "rd-plan-link", attr: { type: "button" } });
    settings.addEventListener("click", () => this.host.openAgentSettings());
    h(panel, "p", { cls: "rd-plan-privacy", text: provider?.local
      ? "Runs on this computer. The agent reads file names and short text excerpts; nothing is written."
      : `Sends file names and short text excerpts of this folder to ${provider?.title || "the selected provider"}. Excluded patterns are set in Settings. Nothing is written.` });
    const instructions = this.focusable(h(panel, "textarea", { cls: "rd-plan-instructions",
      attr: { rows: "3", placeholder: this.agentPlan ? "What should change? e.g. also monitor the Abaqus jobs under runs/" : "What matters here? e.g. follow the iterations in logs/*.log, count finished cases in results/, done when all 12 cases have a .odb", "aria-label": this.agentPlan ? "What should change" : "Special requirements for the agent" } }), "instructions");
    instructions.value = this.agent.instructions;
    instructions.hidden = smartRun;
    instructions.addEventListener("input", () => { this.agent.instructions = instructions.value; });
    const actions = h(panel, "div", { cls: "rd-plan-agent-actions" });
    if (this.agent.running) {
      const status = h(actions, "div", { cls: "rd-plan-agent-status", attr: { role: "status" } });
      h(status, "span", { cls: "rd-plan-spinner", attr: { "aria-hidden": "true" } });
      this.elapsedEl = h(status, "span", { text: this.agentStatusText() });
      const stop = this.focusable(h(actions, "button", { text: "Stop", cls: "rd-plan-chip-button rd-plan-agent-run", attr: { type: "button" } }), "agent-run");
      stop.addEventListener("click", () => this.cancelAgent());
      instructions.disabled = true;
    } else {
      if (!this.agentPlan) {
        const close = h(actions, "button", { text: "Not now", cls: "rd-plan-chip-button is-quiet", attr: { type: "button" } });
        close.addEventListener("click", () => { this.agent.open = false; this.focusNext = { focus: "requirements" }; this.render(); });
      }
      const run = this.focusable(h(actions, "button", { text: this.agentPlan ? "Revise plan" : "Design plan", cls: "rd-plan-chip-button rd-plan-agent-run", attr: { type: "button" } }), "agent-run");
      run.addEventListener("click", () => void this.askAgent());
    }
    if (this.agent.error) h(panel, "p", { text: this.agent.error, cls: "rd-connect-error rd-plan-agent-error" });
  }

  private agentStatusText(): string {
    const minutes = this.host.agentTimeoutMinutes();
    const limit = Number.isFinite(minutes) && minutes > 0 ? ` · stops after ${minutes} min` : "";
    return `Designing a plan${this.provider ? ` with ${this.provider.title}` : ""} · ${elapsedText(Date.now() - this.agent.startedAt)}${limit}`;
  }

  private tickAgent(): void {
    if (this.agent.timer !== null) window.clearTimeout(this.agent.timer);
    this.agent.timer = null;
    if (this.closed || !this.agent.running) return;
    if (this.elapsedEl) this.elapsedEl.textContent = this.agentStatusText();
    this.agent.timer = window.setTimeout(() => this.tickAgent(), 1000);
  }

  private async askAgent(): Promise<void> {
    if (this.busy || this.agent.running || this.closed) return;
    if (!(await this.host.agentReady().catch(() => true))) {
      if (this.closed) return;
      this.agent.setup = true;
      this.focusNext = { focus: "agent-continue" };
      this.render();
      return;
    }
    const seq = ++this.agent.seq;
    const payload: Record<string, unknown> = { source: this.sourcePath };
    const instructions = String(this.agent.instructions || "").trim();
    if (instructions) payload.instructions = instructions;
    if (this.agentPlan?.plan) payload.previousPlan = this.agentPlan.plan;
    let run: AgentRun;
    try { run = await this.host.proposePlan(payload); }
    catch (error) { this.agent.error = errorMessage(error); this.agent.open = true; this.focusNext = { focus: "instructions" }; this.render(); return; }
    if (this.closed || this.agent.seq !== seq) { run.cancel(); return; }
    Object.assign(this.agent, { open: true, running: true, startedAt: Date.now(), cancelled: false, error: "", run });
    this.focusNext = { focus: "agent-run" };
    this.render();
    this.tickAgent();
    try {
      const response = await run.promise;
      if (this.closed || this.agent.seq !== seq) return;
      this.agent.running = false; this.agent.run = null;
      this.applyAgentResponse(response, { focus: true });
    } catch (error) {
      if (this.closed || this.agent.seq !== seq) return;
      const cancelled = this.agent.cancelled;
      Object.assign(this.agent, { running: false, run: null, error: cancelled ? "" : errorMessage(error) });
      this.focusNext = { focus: "instructions" };
      this.render();
    }
  }

  private applyAgentResponse(response: AgentResponse, options: { focus?: boolean } = {}): void {
    const plan = response?.plan && typeof response.plan === "object" ? response.plan : null;
    const profile = plan ? (plan.profile && typeof plan.profile === "object" ? plan.profile as Record<string, unknown> : plan) : null;
    if (!plan || !profile) {
      this.agent.error = Array.isArray(response?.errors) && response.errors.length ? response.errors.join(" ") : "The agent did not return a plan.";
      if (options.focus) this.focusNext = { focus: "instructions" };
      this.render();
      return;
    }
    const outer = response.preview && typeof response.preview === "object" ? response.preview : null;
    const preview: PreviewResult | null = outer && outer.preview && typeof outer.preview === "object" ? outer as PreviewResult : outer ? { ok: true, preview: outer, warnings: [], errors: [] } : null;
    const warnings = [...(Array.isArray(plan.warnings) ? plan.warnings : []), ...(Array.isArray(response.warnings) ? response.warnings : [])].map(String);
    const validation: Validation = response.validation && typeof response.validation === "object" ? response.validation : { ok: response.ok !== false, errors: response.errors || [] };
    const project = (plan.project && typeof plan.project === "object" ? plan.project : {}) as Record<string, unknown>;
    const projectType = Object.hasOwn(PROJECT_TYPES, String(project.type)) ? String(project.type) : Object.hasOwn(PROJECT_TYPES, String(profile.projectType)) ? String(profile.projectType) : "other";
    const extract = (profile.extract && typeof profile.extract === "object" ? profile.extract : {}) as Record<string, unknown>;
    this.agentPlan = {
      key: "agent", mode: "agent", profileId: null, title: String(profile.title || "Agent plan"), origin: "agent", engine: "declarative", sourceKind: null,
      plan, profile, adapter: preview?.adapter && extract.adapter ? preview.adapter : null, rationale: plan.rationale ? String(plan.rationale) : "",
      warnings, validation, preview, projectType, milestoneTemplate: (profile.milestoneTemplate ?? null) as PlanOption["milestoneTemplate"],
      confidence: 1, threshold: 0, meetsThreshold: true, matched: [], missed: [], entries: [], repaired: Boolean(response.repaired),
      suggestedTitle: typeof project.title === "string" ? project.title : null,
    };
    if (this.agentPlan.repaired && validation.ok !== false) this.agentPlan.warnings!.unshift("The first answer did not validate; the agent corrected it once.");
    this.agent.instructions = "";
    this.selectedKey = "agent";
    this.editor = { text: "", dirty: false, errors: [], warnings: [], status: "" };
    if (!this.typeEdited) this.projectType = projectType;
    if (!this.templateEdited) this.milestoneTemplate = this.defaultTemplate(this.agentPlan);
    if (!this.titleEdited && !this.dashboardProjectId && typeof project.title === "string" && project.title.trim()) {
      this.projectTitle = project.title.trim();
      if (!this.idEdited) this.projectId = uniqueProjectId(this.projectTitle, this.hierarchy.map(item => item.projectId));
    }
    const suggestedParent = normalizeParentId(project.parentSuggestion);
    if (suggestedParent && !this.parentEdited && !this.requestedParentId && !this.dashboardProjectId
        && this.eligibleParentList().some(item => item.projectId === suggestedParent)) this.parentId = suggestedParent;
    this.previewState = preview ? { status: preview.ok === false && !preview.preview ? "error" : "ready", result: preview, key: this.previewKey(), error: "" }
      : { status: "idle", result: null, key: null, error: "" };
    if (options.focus) this.focusNext = { key: "agent" };
    this.render();
    if (!preview && validation.ok !== false) this.schedulePreview(0);
  }

  private cancelAgent(silent = false): void {
    if (this.agent.timer !== null) window.clearTimeout(this.agent.timer);
    this.agent.timer = null;
    if (!this.agent.running) return;
    this.agent.cancelled = true; this.agent.running = false;
    const run = this.agent.run;
    this.agent.run = null;
    this.agent.seq++;
    try { run?.cancel(); } catch (_) { /* The agent already exited. */ }
    if (!silent && !this.closed) { this.agent.error = ""; this.focusNext = { focus: "agent-run" }; this.render(); }
  }

  // ---------- Preview ----------

  private previewRequest(option = this.selectedOption()): Record<string, unknown> | null {
    if (!option || this.legacy() || !this.sourcePath) return null;
    if (option.mode === "library") {
      const request: Record<string, unknown> = { source: this.sourcePath, profileId: option.profileId };
      if (option.engine === "adapter" && this.profileId) request.inspectedProfileId = this.profileId;
      return request;
    }
    if (option.mode === "agent" || option.mode === "custom") {
      if (!option.profile || option.validation?.ok === false) return null;
      return option.adapter ? { source: this.sourcePath, profile: option.profile, adapter: option.adapter } : { source: this.sourcePath, profile: option.profile };
    }
    return null;
  }

  private previewKey(): string | null { const request = this.previewRequest(); return request ? JSON.stringify(request) : null; }

  private schedulePreview(delay = 350): void {
    if (this.previewTimer !== null) window.clearTimeout(this.previewTimer);
    this.previewTimer = null;
    if (this.closed) return;
    const option = this.selectedOption();
    if (option?.mode === "agent" && option.preview && this.previewState.key === this.previewKey()) { this.renderPreview(); return; }
    const request = this.previewRequest(option);
    if (!request) { this.previewState = { status: "idle", result: null, key: null, error: "" }; this.renderPreview(); return; }
    const key = JSON.stringify(request);
    if (key === this.previewState.key && this.previewState.status !== "error") { this.renderPreview(); return; }
    this.previewState = { ...this.previewState, status: "loading", key };
    this.renderPreview();
    this.previewTimer = window.setTimeout(() => { this.previewTimer = null; void this.runPreview(request, key); }, delay);
  }

  private async runPreview(request: Record<string, unknown>, key: string): Promise<void> {
    const seq = ++this.previewSeq;
    try {
      const result = await this.host.preview(request);
      if (this.closed || seq !== this.previewSeq || this.previewState.key !== key) return;
      this.previewState = { status: result?.ok === false && !result?.preview ? "error" : "ready", result, key, error: "" };
      const option = this.selectedOption();
      if ((option?.mode === "agent" || option?.mode === "custom") && result?.adapter && (option.profile?.extract as Record<string, unknown> | undefined)?.adapter) option.adapter = result.adapter;
    } catch (error) {
      if (this.closed || seq !== this.previewSeq || this.previewState.key !== key) return;
      this.previewState = { status: "error", result: null, key, error: errorMessage(error) };
    }
    this.renderPreview();
  }

  private renderPreviewSection(container: HTMLElement): void {
    if (this.legacy()) return;
    const section = h(container, "section", { cls: "rd-plan-section rd-plan-preview-section" });
    this.previewEl = h(section, "div", { cls: "rd-plan-preview", attr: { "aria-live": "polite" } });
    this.renderPreview();
  }

  private renderPreview(): void {
    const panel = this.previewEl;
    if (!panel || this.closed) return;
    panel.replaceChildren();
    const state = this.previewState, option = this.selectedOption();
    const notes = (extra: unknown[] = []) => this.renderNotes(panel, [...(this.inspection?.warnings || []), ...extra]);
    const head = h(panel, "div", { cls: "rd-plan-preview-head" });
    h(head, "span", { text: "Preview from current files", cls: "rd-plan-preview-title" });
    const status = h(head, "span", { cls: "rd-plan-caption rd-plan-preview-status" });
    panel.classList.toggle("is-loading", state.status === "loading");
    this.renderProfileSummary();
    if (state.status === "loading") {
      status.textContent = "Reading files…";
      if (!state.result) { const bones = h(panel, "div", { cls: "rd-plan-stats is-skeleton" }); for (let index = 0; index < 4; index++) h(bones, "div", { cls: "rd-plan-stat" }); return; }
    } else if (state.status === "idle") {
      const copy = option?.validation?.ok === false ? "The preview appears once the plan is valid."
        : option ? "No preview for this plan yet." : "Choose a monitoring type, or ask an agent, to see what would be read.";
      h(panel, "p", { text: copy, cls: "rd-plan-caption" });
      notes();
      return;
    } else if (state.status === "error" && !state.result) {
      status.textContent = "Preview unavailable";
      h(panel, "p", { text: state.error || "The preview could not be read.", cls: "rd-connect-error" });
      notes();
      return;
    } else status.textContent = state.result?.ok === false ? "Needs attention" : "";
    const result = state.result || {};
    const rows = previewRows(result.preview);
    if (rows.progress !== null) {
      const percent = Math.round(rows.progress * 100);
      const line = h(panel, "div", { cls: "rd-plan-progress-line" });
      const bar = h(line, "div", { cls: "rd-plan-progress", attr: { role: "img", "aria-label": `${percent}% complete` } });
      h(bar, "div").style.setProperty("width", `${Math.round(rows.progress * 1000) / 10}%`);
      h(line, "span", { text: `${percent}% complete`, cls: "rd-plan-progress-caption", attr: { "aria-hidden": "true" } });
    }
    const metricText = (metric: { value: string; unit: string }) => `${metric.value}${metric.unit ? ` ${metric.unit}` : ""}`;
    const inline = rows.metrics.length <= 4 && rows.metrics.every(metric => metricText(metric).length <= 12 && metric.label.length <= 24);
    const figures = [...rows.stats, ...(inline ? rows.metrics.map(metric => ({ label: metric.label, value: metricText(metric) })) : [])];
    if (figures.length) {
      const stats = h(panel, "div", { cls: "rd-plan-stats" });
      for (const stat of figures) {
        const item = h(stats, "div", { cls: "rd-plan-stat" });
        h(item, "div", { text: stat.value, cls: "rd-plan-stat-value" });
        h(item, "div", { text: stat.label, cls: "rd-plan-stat-label" });
      }
    } else if (result.preview) h(panel, "p", { text: "No values found yet. The source stays monitored and fills in as files appear.", cls: "rd-plan-caption" });
    if (rows.metrics.length && !inline) {
      const metrics = h(panel, "div", { cls: "rd-plan-metrics" });
      for (const metric of rows.metrics) {
        const item = h(metrics, "div", { cls: "rd-plan-metric" });
        h(item, "span", { text: metric.label, cls: "rd-plan-metric-label" });
        h(item, "span", { text: metricText(metric), cls: "rd-plan-metric-value" });
      }
    }
    const errors = Array.isArray(result.errors) ? result.errors : [];
    if (errors.length) { const list = h(panel, "ul", { cls: "rd-plan-preview-errors" }); for (const message of errors.slice(0, 6)) h(list, "li", { text: String(message) }); }
    notes(Array.isArray(result.warnings) ? result.warnings : []);
  }

  // ---------- Project fields ----------

  private renderProjectFields(container: HTMLElement, option: PlanOption | null): void {
    const section = h(container, "section", { cls: "rd-plan-section rd-plan-fields", attr: { "aria-label": "Project" } });
    const legacy = this.legacy();
    if (this.hierarchy.length || this.dashboardProjectId) {
      const row = setting(section, "Add to");
      const select = dropdown(row, [["", "New project"], ...this.hierarchy.map(project => [project.projectId, project.title] as [string, string])], this.dashboardProjectId, value => {
        this.dashboardProjectId = value;
        this.applyProfileSuggestions();
        this.render();
      });
      this.focusable(select, "add-to");
    }
    const nameRow = setting(section, this.dashboardProjectId ? "Source name" : "Name", this.dashboardProjectId ? "A label for this source within the selected project." : "");
    this.titleInput = textInput(nameRow, this.projectTitle, { maxLength: 200, onChange: value => {
      this.projectTitle = value; this.titleEdited = true;
      if (!this.idEdited) { this.projectId = slugId(value); if (this.idInput) this.idInput.value = this.projectId; }
    } });
    this.focusable(this.titleInput, "name");
    if (!this.dashboardProjectId && !legacy) {
      const row = setting(section, "Parent project", this.parentNotice || "");
      dropdown(row, [["", "None (top-level project)"], ...this.eligibleParentList().map(parent => [parent.projectId, parent.title] as [string, string])], this.parentId, value => { this.parentId = value; this.parentEdited = true; });
    }
    const more = this.optionsEl = h(section, "details", { cls: "rd-plan-more rd-plan-advanced" });
    const summary = this.focusable(h(more, "summary", { cls: "rd-plan-more-summary" }), "options");
    h(summary, "span", { text: "Options", cls: "rd-plan-more-title" });
    this.optionsValuesEl = h(summary, "span", { cls: "rd-plan-more-values" });
    this.updateOptionsSummary();
    const body = h(more, "div", { cls: "rd-plan-more-body" });
    if (!this.dashboardProjectId && !legacy) {
      dropdown(setting(body, "Type"), Object.entries(PROJECT_TYPES), this.projectType, value => {
        this.projectType = value; this.typeEdited = true;
        if (!this.templateChoices().some(([choice]) => choice === this.milestoneTemplate)) this.milestoneTemplate = "";
        this.fillTemplates();
        this.updateOptionsSummary();
      });
    }
    if (!legacy) {
      this.templateSelect = dropdown(setting(body, "Milestones", "Optional starting tasks from the template library. Existing tasks are kept."), [], "", value => {
        this.milestoneTemplate = value; this.templateEdited = true; this.updateOptionsSummary();
      });
      this.fillTemplates();
      dropdown(setting(body, "Check for changes", "How often to re-check if a change is missed."), Object.entries(INTERVAL_CHOICES), String(this.interval), value => {
        this.interval = Number(value) || 60; this.updateOptionsSummary();
      });
    }
    toggle(setting(body, "Live updates", this.host.liveRunning() ? "Live updates are running and pick this source up after connecting." : "Start live updates after connecting, so the dashboard refreshes as files are saved."),
      this.keepUpdating, value => { this.keepUpdating = value; this.updateOptionsSummary(); });
    const idRow = setting(body, "Project ID", this.dashboardProjectId ? "Identifies this source. The selected project keeps its own ID." : "Generated from the name: lowercase letters, digits, - and _.");
    this.idInput = textInput(idRow, this.projectId, { maxLength: 64, onChange: value => { this.projectId = value.trim(); this.idEdited = true; } });
    this.focusable(this.idInput, "project-id");
    if (!legacy) this.renderEditor(body, option);
    more.open = Boolean(this.optionsOpen || this.editor.dirty || this.editor.errors.length || this.editor.status);
    more.addEventListener("toggle", () => {
      this.optionsOpen = more.open;
      if (more.open && !legacy && !this.editor.text && !this.editor.dirty) void this.loadEditor();
    });
  }

  /** "Paper · No milestones · Every minute · Live updates on": the Options summary restates what is folded inside. */
  private optionsSummaryText(): string {
    const legacy = this.legacy(), parts: string[] = [];
    if (!this.dashboardProjectId && !legacy) parts.push(PROJECT_TYPES[this.projectType] || PROJECT_TYPES.other);
    if (!legacy) {
      const choice = this.milestoneTemplate ? this.templateChoices().find(([value]) => value === this.milestoneTemplate) : null;
      const inline = this.selectedOption()?.milestoneTemplate;
      parts.push(!choice ? "No milestones" : choice[0] === "profile" && Array.isArray(inline) ? `${inline.length} ${inline.length === 1 ? "milestone" : "milestones"}` : choice[1]);
      parts.push(INTERVAL_CHOICES[this.interval] || `Every ${this.interval} s`);
    }
    parts.push(this.keepUpdating ? "Live updates on" : "Live updates off");
    return parts.join(" · ");
  }

  private updateOptionsSummary(): void { if (this.optionsValuesEl) this.optionsValuesEl.textContent = this.optionsSummaryText(); }

  private revealProjectId(): void {
    this.optionsOpen = true;
    if (this.optionsEl) this.optionsEl.open = true;
    this.idInput?.focus();
  }

  private fillTemplates(): void {
    if (!this.templateSelect) return;
    const choices = this.templateChoices();
    if (this.milestoneTemplate && !choices.some(([value]) => value === this.milestoneTemplate)) this.milestoneTemplate = "";
    fillSelect(this.templateSelect, [["", "None"], ...choices], this.milestoneTemplate);
  }

  private renderEditor(parent: HTMLElement, option: PlanOption | null): void {
    const box = h(parent, "div", { cls: "rd-plan-editor" });
    const head = h(box, "div", { cls: "rd-plan-editor-head" });
    h(head, "span", { text: "Monitoring profile (JSON)", cls: "rd-plan-editor-title" });
    h(head, "span", { text: option?.mode === "library" && option.engine === "adapter" ? "Built-in types use the existing adapter; edit a copy to customise." : "Detection and extraction rules. Validate, then use the edited profile.", cls: "rd-plan-caption" });
    const area = h(box, "textarea", { cls: "rd-plan-json", attr: { rows: "12", spellcheck: "false", "aria-label": "Monitoring profile JSON" } });
    area.value = this.editor.text;
    area.addEventListener("input", () => { this.editor.text = area.value; this.editor.dirty = true; this.editor.status = ""; });
    this.editorEl = area;
    const actions = h(box, "div", { cls: "rd-plan-editor-actions" });
    const load = h(actions, "button", { text: "Reset to selected type", cls: "rd-plan-chip-button is-quiet", attr: { type: "button" } });
    load.addEventListener("click", () => { this.editor.dirty = false; void this.loadEditor(true); });
    const validate = h(actions, "button", { text: "Validate", cls: "rd-plan-chip-button", attr: { type: "button" } });
    validate.addEventListener("click", () => void this.validateEditor());
    const use = h(actions, "button", { text: "Use edited profile", cls: "rd-plan-chip-button", attr: { type: "button" } });
    use.addEventListener("click", () => void this.useEditedProfile());
    this.editorStatusEl = h(box, "div", { cls: "rd-plan-editor-status", attr: { role: "status" } });
    this.renderEditorStatus();
  }

  private renderEditorStatus(): void {
    const el = this.editorStatusEl;
    if (!el) return;
    el.replaceChildren();
    if (this.editor.status) h(el, "p", { text: this.editor.status, cls: "rd-plan-caption" });
    if (this.editor.errors.length) { const list = h(el, "ul", { cls: "rd-plan-option-errors" }); for (const message of this.editor.errors.slice(0, 12)) h(list, "li", { text: message }); }
    if (this.editor.warnings.length) { const list = h(el, "ul", { cls: "rd-plan-rules is-warnings" }); for (const message of this.editor.warnings.slice(0, 12)) h(list, "li", { text: message }); }
  }

  private async loadEditor(force = false): Promise<void> {
    const option = this.selectedOption();
    if (!option || (this.editor.dirty && !force)) return;
    let profile = this.planProfile(option);
    try { if (!profile && option.mode === "library" && option.profileId) profile = (await this.host.getProfile(option.profileId))?.profile || null; }
    catch (error) { this.editor.status = `Could not load the profile: ${errorMessage(error)}`; this.renderEditorStatus(); return; }
    if (this.closed || this.selectedOption() !== option || (this.editor.dirty && !force)) return;
    this.editor = { text: profile ? JSON.stringify(profile, null, 2) : "", dirty: false, errors: [], warnings: [], status: profile ? "" : "No profile to show for this option." };
    if (this.editorEl) this.editorEl.value = this.editor.text;
    this.renderEditorStatus();
  }

  private parseEditor(): Record<string, unknown> | null {
    try {
      const value = JSON.parse(this.editor.text);
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("The profile must be a JSON object.");
      return value.profile && typeof value.profile === "object" ? value.profile : value;
    } catch (error) {
      this.editor.errors = [`JSON: ${errorMessage(error)}`]; this.editor.warnings = []; this.editor.status = "";
      this.renderEditorStatus();
      return null;
    }
  }

  private async validateEditor(): Promise<Record<string, unknown> | null> {
    if (this.busy) return null;
    const profile = this.parseEditor();
    if (!profile) return null;
    this.editor.status = "Validating…"; this.renderEditorStatus();
    try {
      const result = await this.host.validateProfile(profile);
      if (this.closed) return null;
      this.editor.errors = Array.isArray(result?.errors) ? result.errors.map(String) : [];
      this.editor.warnings = Array.isArray(result?.warnings) ? result.warnings.map(String) : [];
      this.editor.status = result?.ok ? "Valid profile." : "";
      this.renderEditorStatus();
      return result?.ok ? profile : null;
    } catch (error) {
      if (!this.closed) { this.editor.status = ""; this.editor.errors = [errorMessage(error)]; this.renderEditorStatus(); }
      return null;
    }
  }

  private async useEditedProfile(): Promise<void> {
    const profile = await this.validateEditor();
    if (!profile || this.closed) return;
    const base = this.selectedOption();
    this.customPlan = {
      key: "custom", mode: "custom", profileId: null, title: String(profile.title || "Edited profile"), origin: "custom", engine: "declarative", sourceKind: null,
      profile, adapter: null, rationale: base ? `Edited from ${base.title}.` : "", warnings: [], validation: { ok: true, errors: [] },
      confidence: 1, threshold: 0, meetsThreshold: true, matched: [], missed: [], entries: [],
      projectType: Object.hasOwn(PROJECT_TYPES, String(profile.projectType)) ? String(profile.projectType) : "other",
      milestoneTemplate: (profile.milestoneTemplate ?? null) as PlanOption["milestoneTemplate"],
      plan: base?.plan ? { ...base.plan, profile } : { profile },
    };
    this.selectedKey = "custom";
    this.editor.dirty = false;
    this.editor.status = "Using the edited profile.";
    if (!this.typeEdited) this.projectType = this.customPlan.projectType;
    if (!this.templateEdited) this.milestoneTemplate = this.defaultTemplate(this.customPlan);
    this.render();
    this.schedulePreview(0);
  }

  // ---------- Footer: save as monitoring type, connect ----------

  /** Cancel and the one ink action on the right; on the left, everything about that action: why it is unavailable, an
   * error, or progress, plus Save as monitoring type. */
  private renderFooter(container: HTMLElement, option: PlanOption | null): void {
    const profile = this.planProfile(option);
    if (this.saveForm && profile) this.renderSaveForm(container);
    const footer = h(container, "div", { cls: "rd-plan-footer" });
    const leading = h(footer, "div", { cls: "rd-plan-footer-leading" });
    if (profile && option && !this.saveForm) {
      const save = h(leading, "button", { text: option.savedAs ? "Save changes as monitoring type…" : "Save as monitoring type…", cls: "rd-plan-link", attr: { type: "button" } });
      save.disabled = option.validation?.ok === false;
      save.addEventListener("click", () => {
        const title = String(profile.title || this.projectTitle || "Monitoring type").trim();
        this.saveForm = { title, id: option.savedAs || (isSafeId(profile.id) ? String(profile.id) : slugId(title)), idEdited: false, error: "" };
        this.render();
      });
    }
    const reason = this.connectBlockReason(option);
    if (reason) h(leading, "p", { text: reason, cls: "rd-plan-footer-reason", attr: { id: "rd-plan-footer-reason" } });
    this.errorEl = h(leading, "div", { cls: "rd-connect-error", attr: { role: "alert" } });
    this.progressEl = h(leading, "div", { cls: "rd-connect-progress", attr: { role: "status" } });
    const trailing = h(footer, "div", { cls: "rd-plan-footer-trailing" });
    const cancel = this.focusable(h(trailing, "button", { text: "Cancel", cls: "rd-plan-chip-button is-quiet", attr: { type: "button" } }), "cancel");
    cancel.addEventListener("click", () => this.close());
    const connect = this.focusable(h(trailing, "button", { text: this.dashboardProjectId ? "Add source" : "Connect", cls: "mod-cta rd-plan-connect", attr: { type: "button" } }), "connect");
    connect.addEventListener("click", () => void this.connectProject());
    connect.disabled = Boolean(reason);
    if (reason) connect.setAttribute("aria-describedby", "rd-plan-footer-reason");
  }

  private connectBlockReason(option: PlanOption | null): string {
    if (this.agent.running) return "Waiting for the agent…";
    if (!option && !this.legacy()) return "Use Smart monitoring to design a plan";
    if (option?.validation?.ok === false) return "Fix the plan to connect";
    return "";
  }

  private renderSaveForm(container: HTMLElement): void {
    const form = this.saveForm!;
    const box = h(container, "div", { cls: "rd-plan-save" });
    h(box, "p", { text: "Save this plan as a monitoring type. Folders like this one are then recognised without an agent.", cls: "rd-plan-caption" });
    textInput(setting(box, "Name"), form.title, { maxLength: 200, onChange: value => {
      form.title = value;
      if (!form.idEdited) { form.id = slugId(value); if (this.saveIdInput) this.saveIdInput.value = form.id; }
    } });
    this.saveIdInput = textInput(setting(box, "ID", "Lowercase letters, digits, - and _."), form.id, { maxLength: 64, onChange: value => { form.id = value.trim(); form.idEdited = true; } });
    if (form.error) h(box, "p", { text: form.error, cls: "rd-connect-error" });
    const actions = h(box, "div", { cls: "rd-plan-editor-actions" });
    const cancel = h(actions, "button", { text: "Cancel", cls: "rd-plan-chip-button is-quiet", attr: { type: "button" } });
    cancel.addEventListener("click", () => { this.saveForm = null; this.render(); });
    const save = h(actions, "button", { text: "Save monitoring type", cls: "rd-plan-chip-button", attr: { type: "button" } });
    save.addEventListener("click", () => void this.saveMonitoringType());
  }

  private async saveMonitoringType(): Promise<void> {
    const form = this.saveForm, option = this.selectedOption(), profile = this.planProfile(option);
    if (!form || !option || !profile || this.busy) return;
    const title = String(form.title || "").trim();
    if (!title || title.length > 200 || /[\r\n]/.test(title)) { form.error = "Enter a name on one line."; this.render(); return; }
    if (!isSafeId(form.id)) { form.error = "Use a lowercase ID of letters, digits, - or _ (up to 64 characters)."; this.render(); return; }
    const saved = { ...JSON.parse(JSON.stringify(profile)), id: form.id, title };
    this.setBusy(true, "Saving monitoring type…");
    try {
      const result = await this.host.saveProfile(saved);
      if (this.closed) return;
      option.profile = saved; option.title = title; option.savedAs = result?.saved || form.id; option.savedTitle = title;
      if (option.plan) option.plan = { ...option.plan, profile: saved };
      this.saveForm = null;
      notice(`Saved monitoring type “${title}”.${result?.unchanged ? " It was already up to date." : ""}`);
    } catch (error) { form.error = errorMessage(error); }
    finally { if (!this.closed) { this.setBusy(false); this.render(); } }
  }

  // ---------- Folder and inspection ----------

  private async browseFolder(options: { inspect?: boolean; auto?: boolean } = {}): Promise<void> {
    if (this.busy || this.closed) return;
    let selected: string | null = null;
    try {
      const picked = await openDialog({ directory: true, multiple: false, title: "Choose a research project folder", defaultPath: isAbsolute(this.sourcePath) ? this.sourcePath : undefined });
      if (this.closed) return;
      if (typeof picked === "string" && picked) { selected = picked; this.sourcePath = picked; if (this.pathInput) this.pathInput.value = picked; }
    } catch (error) {
      if (!options.auto) this.showError(error);
    }
    if (!selected) { this.pathInput?.focus(); return; }
    if (options.inspect) await this.inspectFolder();
  }

  private async inspectFolder(): Promise<void> {
    if (this.busy || this.closed) return;
    if (this.errorEl) this.errorEl.textContent = "";
    if (!isAbsolute(this.sourcePath)) { this.showError(new Error("Choose a folder or enter a full absolute path.")); return; }
    this.setBusy(true, "Reading the folder…");
    const requestedSource = this.sourcePath;
    try {
      const [inspection, templates] = await Promise.all([this.host.inspect(requestedSource), this.host.templates().catch(() => [] as TemplateSummary[])]);
      if (this.closed || this.sourcePath !== requestedSource) return;
      const legacy = !inspection || !Array.isArray(inspection.candidates);
      if (!inspection || (legacy && (!Array.isArray(inspection.profiles) || !inspection.profiles.length))) {
        const messages = inspection && Array.isArray(inspection.warnings) ? inspection.warnings.join(" ") : "";
        throw new Error(messages || "No supported project was detected. Select a LaTeX, Abaqus, or iSALE project folder.");
      }
      this.hierarchy = this.host.projects();
      this.templates = legacy ? [] : templates;
      this.dashboardProjectId = "";
      this.inspection = { ...inspection, warnings: Array.isArray(inspection.warnings) ? inspection.warnings : [] };
      this.sourcePath = inspection.sourcePath || this.sourcePath;
      this.agentPlan = null; this.customPlan = null;
      const presetApplies = Boolean(this.preset) && requestedSource === this.preset!.source;
      const preferred = presetApplies && this.preset!.profileId ? this.options().find(item => item.profileId === this.preset!.profileId && item.meetsThreshold) : null;
      const best = preferred || bestOption(this.options());
      this.selectedKey = best?.key || null;
      this.profileId = best?.entries?.[0]?.id || "";
      this.idEdited = false; this.titleEdited = false; this.typeEdited = false; this.templateEdited = false;
      /* Unmatched folders offer Smart monitoring / Add requirements; a suggestion's "Ask agent" opens the requirements. */
      this.agent.open = !legacy && Boolean(presetApplies && this.preset!.agent && best);
      this.applyRequestedParent();
      if (best) this.applyProfileSuggestions();
      else {
        this.projectTitle = folderTitle(inspection.title || this.sourcePath);
        this.projectId = isSafeId(inspection.suggestedId) ? inspection.suggestedId : slugId(this.projectTitle);
      }
      if (presetApplies && this.preset!.title) { this.projectTitle = this.preset!.title; this.titleEdited = true; }
      if (presetApplies && this.preset!.projectId) { this.projectId = this.preset!.projectId; this.idEdited = true; }
      this.busy = false; this.modal.busyLock = false;
      this.focusNext = best ? { key: best.key } : { focus: "smart" };
      /* A plan from `ma connect`: check it, then show it as the agent plan (preview follows); the user confirms. */
      if (presetApplies && this.preset!.plan) {
        const plan = this.preset!.plan;
        const profile = (plan.profile && typeof plan.profile === "object" ? plan.profile : plan) as Record<string, unknown>;
        const validation = await this.host.validateProfile(profile).catch(error => ({ ok: false, errors: [errorMessage(error)] }));
        if (this.closed) return;
        this.applyAgentResponse({ ok: validation.ok, plan: plan.profile ? plan : { profile: plan }, validation, warnings: ["Designed by your agent. Check the preview, then Connect."] }, { focus: true });
        if (presetApplies && this.preset!.title) { this.projectTitle = this.preset!.title; this.titleEdited = true; this.render(); }
        return;
      }
      this.render();
      this.schedulePreview(0);
    } catch (error) { this.showError(error); }
    finally { if (!this.closed) this.setBusy(false); }
  }

  private async connectProject(): Promise<void> {
    if (this.busy || !this.inspection) return;
    if (this.errorEl) this.errorEl.textContent = "";
    if (this.agent.running) { this.showError(new Error("Wait for the agent, or stop it, before connecting.")); return; }
    if (!this.projectTitle.trim()) { this.titleInput?.focus(); this.showError(new Error("Enter a project name.")); return; }
    if (!isSafeId(this.projectId)) { this.revealProjectId(); this.showError(new Error("Use a safe lowercase project ID, between 1 and 64 characters.")); return; }
    const option = this.selectedOption();
    if (!option) { this.showError(new Error("Choose a monitoring type, or ask an agent for a plan.")); return; }
    if (option.validation?.ok === false) { this.showError(new Error("This plan did not validate. Revise it or edit the profile first.")); return; }
    const projects = this.host.projects();
    if (this.dashboardProjectId && !projects.some(item => item.projectId === this.dashboardProjectId)) {
      this.showError(new Error("Choose an existing project from the list, or create a new project.")); return;
    }
    let request: { args: string[]; input: string | null };
    try {
      /* A saved declarative type connects by ID, so the source follows later edits to the type. */
      const declarativeSaved = option.savedAs && !(option.profile?.extract as Record<string, unknown> | undefined)?.adapter;
      const choice = declarativeSaved
        ? { option: { ...option, mode: "library" as const, profileId: option.savedAs!, engine: "declarative" as const, entries: [] } }
        : { option, entryId: this.profileId, profile: this.planProfile(option), adapter: option.adapter || null };
      request = connectRequest({ source: this.sourcePath, id: this.projectId, title: this.projectTitle.trim(), interval: this.interval,
        dashboardProjectId: this.dashboardProjectId || null, projectType: this.projectType, parentId: this.parentId || null,
        milestoneTemplate: this.milestoneTemplate || null }, choice);
    } catch (error) { this.showError(error); return; }
    this.setBusy(true, "Connecting and collecting the first status…");
    try {
      await this.host.connect({ ...request, title: this.projectTitle.trim(), keepUpdating: this.keepUpdating, discoveryId: this.preset?.discoveryId || null, source: this.sourcePath });
      if (!this.closed) { this.setBusy(false); this.close(); }
    } catch (error) {
      if (!this.closed) {
        const message = errorMessage(error);
        this.setBusy(false);
        this.render();
        if (this.errorEl) this.errorEl.textContent = message;
      }
    } finally { if (!this.closed && this.busy) this.setBusy(false); }
  }
}
