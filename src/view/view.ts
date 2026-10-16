/* Monitoring Anything view: one panel ("mode") of the dashboard.
 *
 * Ported from the Obsidian vault renderer Research Dashboard/System/views/research-dashboard/view.js (2026-10-10).
 * The DOM, class names, motion contract (value tweens, rolling figures, update glow, View Transitions) and rendering
 * rules are kept as in the reference so research-dashboard.css applies unchanged. What differs:
 *  - data comes from the app workspace model (ViewContext.load) instead of Dataview/the vault;
 *  - actions go to the app's action bridge (ViewContext.bridge) instead of the Obsidian Connector;
 *  - links navigate inside the app (rd://overall, rd://project/<id>) or open files with the system;
 *  - Canvas, Bases and Dataview task-list features are not part of the standalone app.
 */

import { milestoneLabel, taskBinding, type Task } from "../model/milestones";
import { projectStatus as statusOf, resolveHierarchy, kindNames } from "../model/load";
import { OVERALL_LINK, type Chapter, type Model, type PaperDetail, type Project, type Source } from "../model/types";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Dict = Record<string, any>;

export type Mode = "overview" | "projects" | "attention" | "project-overview" | "paper" | "paper-progress" | "milestones" | "runs" | "resources" | "health" | "mini" | "orb";

export interface ViewInput {
  mode: Mode;
  projectId?: string | null;
  sourceId?: string | null;
  chapterId?: string | null;
  /** Play the first-open entrance (false when a page transition already animates the arrival). */
  entrance?: boolean;
}

export interface BridgeStatus {
  running: boolean; busy: boolean; error?: string | null;
  canEdit?: boolean; canAddMilestone?: boolean; canRemove?: boolean; canCreate?: boolean; canGroup?: boolean;
  keepOnTop?: boolean; canKeepOnTop?: boolean; discoveryScanning?: boolean;
  /** Whether the floating widget is shown (the main window's widget button is pressed). */
  widgetVisible?: boolean;
  /** The widget's focused project (null: all projects). */
  focusProjectId?: string | null;
  /** Milestones just added in this window (task text), newest first: Next up puts them on top. */
  recentMilestones?: { projectId: string; text: string }[];
}

/** A new folder in a project's discovery folder (see src/model/cli.ts Suggestion). */
export interface DiscoverySuggestion {
  id: string; projectId: string; parentId: string; folder: string; relative: string; rootName: string;
  bestCandidate: { profileId: string; title: string; projectType: string; confidence: number; sourceKind: string | null; origin: string } | null;
}

export interface NavTarget { inMain?: boolean; newLeaf?: boolean }

/** Actions the view can request. Every method is optional: a missing method hides or disables its control. */
export interface ActionBridge {
  getStatus(): BridgeStatus;
  connect?(): Promise<unknown>;
  createProject?(options?: { parentId?: string }): Promise<unknown>;
  collect?(): Promise<unknown>;
  start?(): Promise<unknown>;
  stop?(): Promise<unknown>;
  editProject?(projectId: string): Promise<unknown>;
  setParent?(projectId: string, parentId: string | null): Promise<unknown>;
  addMilestone?(projectId: string, selection?: { sourceId?: string; chapterId?: string; stage?: string }): Promise<unknown>;
  setMilestone?(projectId: string, request: Dict): Promise<unknown>;
  /** Add one plain milestone (the widget's quick add). */
  quickMilestone?(projectId: string, title: string): Promise<unknown>;
  /** Inline add (the list's composer row): optional due date and paper chapter binding. */
  addMilestoneInline?(projectId: string, input: { title: string; due?: string | null; sourceId?: string; chapterId?: string; stage?: string }): Promise<unknown>;
  /** Rename a milestone and/or change its due date (null clears it). */
  editMilestone?(projectId: string, request: { line: number; text: string; title?: string; due?: string | null }): Promise<unknown>;
  deleteMilestone?(projectId: string, request: { line: number; text: string }): Promise<unknown>;
  /** Choose another folder for a source (the paper moved, or another copy is the one being written). */
  changeSourceFolder?(sourceId: string): Promise<unknown>;
  /** Focus the widget on one project and its sub-projects, or null for all projects. */
  setFocus?(projectId: string | null): Promise<unknown>;
  removeProject?(projectId: string): Promise<unknown>;
  openProject?(projectId: string, target?: NavTarget | boolean): Promise<unknown>;
  openOverall?(target?: NavTarget | boolean): Promise<unknown>;
  openSource?(sourceId: string, chapterId?: string | null): Promise<unknown>;
  focusChapter?(projectId: string, sourceId: string, chapterId: string | null, target?: NavTarget | boolean): Promise<unknown>;
  setKeepOnTop?(value: boolean): Promise<unknown>;
  openSettings?(): Promise<unknown>;
  toggleWidget?(): Promise<unknown>;
  revealWorkspace?(): Promise<unknown>;
  importVault?(): Promise<unknown>;
  /** Sub-project discovery: the current suggestions, Add (plan card) / Ask agent, Dismiss, and an on-demand scan. */
  discoveries?(): { suggestions: DiscoverySuggestion[] };
  addDiscovery?(id: string, options?: { agent?: boolean }): Promise<unknown>;
  dismissDiscovery?(id: string): Promise<unknown>;
  scanDiscoveries?(projectId?: string | null): Promise<unknown>;
}

export interface ViewContext {
  /** A fresh model of every live project (the view narrows it for a project page). */
  load(): Promise<Model>;
  bridge: ActionBridge;
  /** Follow an in-app link (rd://overall, rd://project/<id>). */
  navigate(path: string, newLeaf?: boolean): void;
  /** Open an absolute local path or a web/VS Code URL with the system. */
  openExternal(target: string): void;
  /** Open a workspace-relative file (configuration, snapshot) with the system. */
  openWorkspaceFile(relative: string): void;
  /** Called on data or status changes; returns an unsubscribe function. */
  subscribe(listener: () => void): () => void;
}

export interface ViewHandle { dispose(): void; refresh(force?: boolean): Promise<void>; painted: Promise<void> }

interface UiMemory { disclosures: Map<string, boolean>; paperUi: { sourceId: string | null; chapterId: string | null; stage: string }; projectUi: { type: string; collapsed: Set<string> }; marks: Map<string, unknown> }
/* Disclosures, filters and folded families survive navigation within this window session, like Dataview reruns did. */
const memory = new Map<string, UiMemory>();
const transitionKey = Symbol.for("research-dashboard.viewTransition");
const svgNamespace = "http://www.w3.org/2000/svg";

export function mountView(container: HTMLElement, input: ViewInput, ctx: ViewContext): ViewHandle {
  const mode = String(input?.mode || "projects") as Mode;
  const requestedProject = input?.projectId == null ? null : String(input.projectId);
  const memoryKey = mode + ":" + (requestedProject || "");
  const remembered = memory.get(memoryKey);
  const ui: UiMemory = remembered || { disclosures: new Map(), paperUi: { sourceId: null, chapterId: null, stage: "all" }, projectUi: { type: "all", collapsed: new Set() }, marks: new Map() };
  memory.set(memoryKey, ui);
  const { disclosures, paperUi, projectUi, marks } = ui;
  /* Grouping moves already shown on screen but not yet reflected by the workspace: projectId -> { parentId, until }. */
  const pendingParents = new Map<string, { parentId: string | null; until: number | null }>();
  if (input?.sourceId) paperUi.sourceId = String(input.sourceId);
  if (input && "chapterId" in input) paperUi.chapterId = input.chapterId ? String(input.chapterId) : null;
  const documentRef = container.ownerDocument;
  const clock: any = documentRef.defaultView || window;
  const root = documentRef.createElement("div");
  root.className = input?.entrance === false ? "rd-dashboard" : "rd-dashboard is-entering";
  container.appendChild(root);
  const module = documentRef.createElement("section");
  module.className = `rd-module rd-${mode}`;
  root.appendChild(module);
  let resolvePainted: () => void = () => {};
  const painted = new Promise<void>(resolve => { resolvePainted = resolve; });
  const record: Dict = {
    disposed: false, timer: null, busy: false, unsubscribe: null as null | (() => void),
    drag: null, regrouping: false, regroupTransition: null, hold: false,
    miniAdd: { open: false, projectId: "", text: "", error: "" }, composer: new Map<string, { text: string; due: string; section: string; stage: string }>(), refocus: null as string | null, miniPush: null as null | "forward" | "back", focusHeading: [] as HTMLElement[],
    disclosureNodes: new Map<string, HTMLDetailsElement>(), paintEpoch: 0, refreshQueued: false,
    model: null, painted: false, signature: null as string | null, forcePaint: false, nextMarks: null as Map<string, unknown> | null, animate: false,
    updated: new Set<Element>(), named: [] as HTMLElement[], transition: null, skeletonAt: null as number | null, chapterOrder: [] as string[], focusChapter: null as string | null,
    uid: Math.random().toString(36).slice(2, 8), tip: { node: null as HTMLElement | null, target: null as Element | null, showTimer: null, hideTimer: null, scroll: null as null | (() => void) },
  };
  function dispose() {
    if (record.disposed) return;
    captureDisclosures();
    record.disclosureNodes.clear();
    record.disposed = true;
    if (record.timer !== null) clock.clearInterval(record.timer);
    record.unsubscribe?.();
    record.timer = null;
    hideTip(true);
    if (record.tip.scroll) documentRef.removeEventListener("scroll", record.tip.scroll, true);
    resolvePainted();
  }

  function validId(value: string) { return /^[a-z0-9][a-z0-9_-]{0,63}$/.test(value) && !/^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/.test(value); }
  function sourceKindLabel(source: Source) { return source.display?.label || kindNames[source.kind] || source.kind; }
  const runtimeNames: Record<string, string> = { unconfigured: "Not connected", queued: "Queued", running: "Running", completed: "Run completed", failed: "Run failed", unknown: "Unknown" };
  const healthNames: Record<string, string> = { unconfigured: "Not connected", fresh: "Fresh", stale: "Stale", error: "Collection error" };
  const stageNames: Record<string, string> = { planned: "Planned", preparation: "Preparation", modeling: "Modelling", writing: "Writing", computation: "Computation", postprocessing: "Post-processing", validation: "Validation", review: "Review", completed: "Completed", archived: "Archived" };
  const projectHealthNames: Record<string, string> = { active: "Active", blocked: "Blocked", paused: "Paused", completed: "Completed" };
  const typeNames: Record<string, string> = { thesis: "Thesis", paper: "Paper", simulation: "Simulation", experiment: "Experiment", other: "Other" };
  const typeOrder = Object.keys(typeNames);
  function plural(count: number, one: string, many = one + "s") { return count + " " + (count === 1 ? one : many); }
  const titles: Record<string, string> = {
    overview: "Overall", attention: "Needs attention", projects: "Projects", "project-overview": "Project overview",
    milestones: "Milestones", runs: "Live monitor", resources: "Results & resources",
    health: "Data health", paper: "Paper workspace", "paper-progress": "Progress", mini: "All projects",
  };
  function el<K extends keyof HTMLElementTagNameMap>(parent: Element, tag: K, text?: unknown, className?: string | null): HTMLElementTagNameMap[K] {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    parent.appendChild(node);
    return node;
  }
  const iconPaths: Record<string, string> = {
    project: "M3 7h7l2 2h9v11H3z M3 7V4h7l2 3",
    latex: "M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6",
    abaqus: "M12 3l8 5v8l-8 5-8-5V8z M4 8l8 5 8-5 M12 13v8",
    isale: "M3 8c3-4 5 4 9 0s6 4 9 0 M3 13c3-4 5 4 9 0s6 4 9 0 M3 18c3-4 5 4 9 0s6 4 9 0",
    blocked: "M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9z M12 8v4 M12 16h.01",
    failed: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M15 9l-6 6 M9 9l6 6",
    stale: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 7v5l3 2",
    warning: "M8 2v4 M16 2v4 M3 10h18 M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z",
    file: "M6 3h8l4 4v14H6z M14 3v5h4",
    pin: "M12 17v5 M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2v.8h14v-.8a2 2 0 0 0-1.1-1.8l-1.8-.9a2 2 0 0 1-1.1-1.8V7h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z",
    open: "M7 17L17 7 M8 7h9v9",
    plus: "M12 5v14 M5 12h14",
    connect: "M3 7h7l2 2h9v11H3z M3 7V4h7l2 3 M12 12v5 M9.5 14.5h5",
    custom: "M4 7h9 M17 7h3 M4 17h3 M11 17h9 M15 5v4 M9 15v4",
    refresh: "M20 12a8 8 0 1 1-2.34-5.66 M20 4v5h-5",
    widget: "M3 5h18v14H3z M13 11h5v5h-5z",
    settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z",
  };
  function icon(parent: Element, kind = "project", className: string | null = null) {
    const svg = documentRef.createElementNS(svgNamespace, "svg");
    svg.setAttribute("class", "rd-icon " + (className || (kind === "project" ? "rd-project-icon" : "rd-source-icon")));
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.6"); svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round"); svg.setAttribute("aria-hidden", "true");
    const path = documentRef.createElementNS(svgNamespace, "path");
    path.setAttribute("d", iconPaths[kind] || iconPaths.project);
    svg.appendChild(path); parent.appendChild(svg); return svg;
  }
  function svgNode(parent: Element, tag: string, attributes: Record<string, unknown>): SVGElement {
    const node = documentRef.createElementNS(svgNamespace, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    parent.appendChild(node); return node;
  }
  /* Milestone ring for list rows; the visible count beside it carries the value. */
  function ring(parent: Element, completed: number, total: number, key: string | null = null) {
    const svg = svgNode(parent, "svg", { class: "rd-ring" + (!total ? " is-empty" : completed >= total ? " is-complete" : ""), viewBox: "0 0 36 36", "aria-hidden": "true" });
    svgNode(svg, "circle", { class: "rd-ring-track", cx: 18, cy: 18, r: 15, fill: "none" });
    const share = total ? Math.min(1, completed / total) : null, before = key ? prior(key, share) : undefined;
    if (total && completed > 0 && completed < total) {
      const circumference = 2 * Math.PI * 15;
      const value = svgNode(svg, "circle", { class: "rd-ring-value", cx: 18, cy: 18, r: 15, fill: "none", transform: "rotate(-90 18 18)", "stroke-dasharray": (completed / total * circumference).toFixed(2) + " " + circumference.toFixed(2) });
      if (moved(before, share)) tween(value, { "--rd-from": (((before as number) || 0) * circumference).toFixed(2) + " " + circumference.toFixed(2) });
    }
    if (total && completed >= total) svgNode(svg, "path", { class: "rd-ring-check", d: "M12.5 18.5l3.6 3.6 7.4-7.6", fill: "none" });
    return svg;
  }
  /* Vertical capsule: full-height track, fill from the base, value at the fill edge. */
  function capsule(column: HTMLElement, completed: number, total: number, label: string, extra: Dict = {}) {
    const share = total ? completed / total : null, percent = share === null ? null : Math.round(share * 100);
    const before = extra.key ? prior(extra.key, share) : undefined, change = share !== null && moved(before, share);
    const track = el(column, "span", null, ["rd-capsule-track", extra.track, share === null ? "is-unknown" : ""].filter(Boolean).join(" "));
    if (share !== null) { const fill = el(track, "span", null, ["rd-capsule-fill", extra.fill].filter(Boolean).join(" ")); fill.style.height = share * 100 + "%"; if (change) tween(fill, { "--rd-from": ((before as number) || 0) * 100 + "%" }); }
    const value = el(track, "span", percent === null ? "—" : percent + "%", ["rd-capsule-value", extra.value, percent !== null && percent >= 82 ? "is-inside" : ""].filter(Boolean).join(" "));
    if (share !== null) { value.style.bottom = share * 100 + "%"; if (change) tween(value, { "--rd-from": ((before as number) || 0) * 100 + "%" }); }
    if (change) updated(column);
    el(column, "span", label, ["rd-capsule-label", extra.label].filter(Boolean).join(" "));
    return track;
  }
  function clear(node: Element | null) { node?.replaceChildren(); }
  /* ---------- Motion: value continuity, update cue, tooltips and in-page transitions ---------- */
  function motionOff() {
    try { return documentRef.body?.classList?.contains?.("rd-motion-off") === true || clock.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches === true; }
    catch (_) { return false; }
  }
  function canAnimate() {
    return root.isConnected && documentRef.visibilityState !== "hidden" && (root as any).checkVisibility?.() !== false && !motionOff();
  }
  function later(node: Element, className: string, delay: number, done: (() => void) | null = null) {
    clock.setTimeout(() => { node.classList.remove(className); done?.(); }, delay);
  }
  /* Records a mark for the next paint. Returns undefined when this paint must not animate, null when the mark is new to
   * a data refresh, otherwise the value last shown. */
  function prior(key: string, value: unknown): unknown {
    const before = marks.get(key); record.nextMarks?.set(key, value);
    return record.animate ? before === undefined ? null : before : undefined;
  }
  function moved(before: unknown, value: unknown) { return before !== undefined && JSON.stringify(before) !== JSON.stringify(value); }
  function tween(node: Element, from: Record<string, string>) {
    if (!record.animate) return;
    for (const [name, value] of Object.entries(from)) (node as HTMLElement).style.setProperty(name, value);
    node.classList.add("is-tweening"); later(node, "is-tweening", 700);
  }
  function updated(node: Element | null) { if (record.animate && node) record.updated.add(node); }
  /* Changed figures roll: the old value leaves through a pseudo-element, so textContent stays exact. */
  function figure(node: HTMLElement, key: string, row: Element | null = null) {
    const now = node.textContent || "", before = prior(key, now) as string | null | undefined;
    if (before == null || before === now) return false;
    const lead = (value: string) => parseFloat((/-?\d[\d,.]*/.exec(value)?.[0] || "").replace(/,/g, ""));
    node.dataset.rdFrom = before; node.textContent = ""; el(node, "span", now, "rd-roll-value");
    node.classList.add("is-rolling"); if (lead(now) < lead(before)) node.classList.add("is-down");
    later(node, "is-rolling", 460, () => { node.classList.remove("is-down"); delete node.dataset.rdFrom; });
    updated(row); return true;
  }
  /* One shared tooltip per view, in the top layer so scroll panes never clip it. */
  function hint(node: Element | null, title: string, detail = "", anchor: Element | null = node) {
    if (!node || !anchor) return;
    if (typeof clock.HTMLElement?.prototype?.showPopover !== "function") {
      if (node.namespaceURI !== svgNamespace) (node as HTMLElement).title = detail ? title + " · " + detail : title;
      return;
    }
    node.addEventListener("pointerenter", event => { if ((event as PointerEvent).pointerType !== "touch") showTip(node, anchor, title, detail, false); });
    node.addEventListener("pointerleave", () => hideTip());
    node.addEventListener("focus", () => { if (node.matches?.(":focus-visible") !== false) showTip(node, anchor, title, detail, true); });
    node.addEventListener("blur", () => hideTip(true));
    node.addEventListener("pointerdown", () => hideTip(true));
    node.addEventListener("keydown", event => { if ((event as KeyboardEvent).key === "Escape") hideTip(true); });
  }
  function showTip(target: Element, anchor: Element, title: string, detail: string, immediate: boolean) {
    const tip = record.tip; clock.clearTimeout(tip.hideTimer); clock.clearTimeout(tip.showTimer);
    const open = () => {
      if (record.disposed || !target.isConnected) return;
      if (!tip.node) {
        tip.node = el(root, "div", null, "rd-tooltip"); tip.node.id = "rd-tip-" + record.uid;
        tip.node.setAttribute("role", "tooltip"); tip.node.setAttribute("popover", "manual");
        el(tip.node, "strong", null, "rd-tooltip-title"); el(tip.node, "span", null, "rd-tooltip-detail");
        tip.scroll = () => hideTip(true); documentRef.addEventListener("scroll", tip.scroll, true);
      }
      const node = tip.node as HTMLElement, visible = node.matches(":popover-open");
      node.children[0].textContent = title; node.children[1].textContent = detail; (node.children[1] as HTMLElement).hidden = !detail;
      if (tip.target !== target) { tip.target?.removeAttribute("aria-describedby"); tip.target = target; target.setAttribute("aria-describedby", node.id); }
      node.classList.toggle("is-gliding", visible);
      if (!visible) node.showPopover();
      const box = anchor.getBoundingClientRect(), width = node.offsetWidth, height = node.offsetHeight, below = box.top - height - 10 < 8;
      const x = Math.max(8, Math.min((clock.innerWidth || width + 16) - width - 8, box.left + box.width / 2 - width / 2));
      node.classList.toggle("is-below", below);
      node.style.translate = Math.round(x) + "px " + Math.round(below ? box.bottom + 10 : box.top - height - 10) + "px";
    };
    if (immediate || tip.node?.matches(":popover-open")) open(); else tip.showTimer = clock.setTimeout(open, 120);
  }
  function hideTip(now = false) {
    const tip = record?.tip; if (!tip) return;
    clock.clearTimeout(tip.showTimer); clock.clearTimeout(tip.hideTimer);
    const close = () => { tip.target?.removeAttribute?.("aria-describedby"); tip.target = null; if (tip.node?.matches(":popover-open")) tip.node.hidePopover(); };
    if (now) close(); else tip.hideTimer = clock.setTimeout(close, 80);
  }
  const all = (node: Element | null | undefined, selector: string): HTMLElement[] => Array.from(node?.querySelectorAll?.(selector) || []) as HTMLElement[];
  function unname(node: HTMLElement) { node.style.removeProperty("view-transition-name"); node.style.removeProperty("view-transition-class"); }
  type Namer = (node: Element | null | undefined, id: string, role: string) => boolean;
  /* Surfaces drawn by their group box during a transition; set on html (pseudo-elements inherit from it), cleared on settle. */
  const vtProperties = ["--rd-vt-thumb", "--rd-vt-surface", "--rd-vt-surface-radius", "--rd-vt-backdrop", "--rd-vt-tile-radius"];
  /* cue: optional html class for this transition only, e.g. rd-vt-next / rd-vt-prev when stepping between sections. */
  function transition(update: () => void, scene: (name: Namer) => void, cue: string | null = null) {
    hideTip(true);
    if (typeof (documentRef as any).startViewTransition !== "function" || !canAnimate()) { update(); return null; }
    clock[transitionKey]?.skipTransition?.();
    for (const node of record.named.splice(0)) unname(node);
    root.classList.remove("is-entering", "is-revealing");
    const html = documentRef.documentElement, named: HTMLElement[] = record.named, paired = new Set<string>(), view = clock.innerHeight || html.clientHeight || 1e6;
    html.classList.remove("rd-vt-next", "rd-vt-prev");
    let clip = { top: 0, bottom: view };
    for (let node = root.parentElement; node && node !== documentRef.body; node = node.parentElement) {
      if (!/auto|scroll|hidden|clip/.test(clock.getComputedStyle?.(node)?.overflowY || "")) continue;
      const box = node.getBoundingClientRect(); clip = { top: box.top, bottom: box.bottom }; break;
    }
    /* Snapshots escape scroll clipping, so only name what starts inside the visible pane (or runs off the window). */
    const openBelow = clip.bottom >= view - 40;
    const name = (phase: "old" | "new"): Namer => (node, id, role) => {
      if (!node || typeof (node as HTMLElement).style?.setProperty !== "function") return false;
      if (!(phase === "new" && paired.has(id))) {
        const box = node.getBoundingClientRect?.();
        if (!box?.height || box.top < clip.top - 1 || box.top > clip.bottom || (!openBelow && box.bottom > clip.bottom + 1)) return false;
      }
      (node as HTMLElement).style.setProperty("view-transition-name", "rd-" + id + "-" + record.uid); (node as HTMLElement).style.setProperty("view-transition-class", role.split(" ").map(item => "rd-vt-" + item).join(" "));
      named.push(node as HTMLElement); if (phase === "old") paired.add(id); return true;
    };
    scene(name("old"));
    html.classList.add("rd-vt"); if (cue) html.classList.add(cue);
    let active: any;
    try { active = (documentRef as any).startViewTransition(() => { if (record.disposed) return; update(); scene(name("new")); }); }
    catch (_) { html.classList.remove("rd-vt", "rd-vt-next", "rd-vt-prev"); for (const node of named.splice(0)) unname(node); for (const property of vtProperties) html.style.removeProperty(property); update(); return null; }
    clock[transitionKey] = active; record.transition = active;
    active.ready?.catch?.(() => {});
    const settle = () => {
      if (record.transition === active) { record.transition = null; for (const node of named.splice(0)) unname(node); }
      if (clock[transitionKey] === active) { delete clock[transitionKey]; html.classList.remove("rd-vt", "rd-vt-next", "rd-vt-prev"); for (const property of vtProperties) html.style.removeProperty(property); }
    };
    Promise.resolve(active.finished).then(settle, settle);
    return active;
  }
  function vtSurface(properties: Record<string, string>) {
    const html = documentRef.documentElement;
    for (const [property, value] of Object.entries(properties)) if (typeof value === "string" && value) html.style.setProperty(property, value);
  }
  /* Other panels glide. A translucent tile panel gets an opaque page backing, so nothing passing beneath it shows through. */
  function panelScene(name: Namer) {
    const panel = root.closest?.(".rd-app-panel"), grid = panel?.parentElement;
    if (!grid?.classList?.contains("rd-app-grid")) return;
    const page = grid.closest?.(".rd-native-dashboard");
    Array.from(grid.children).forEach((item, index) => {
      if (item === panel || !item.classList.contains("rd-app-panel") || (item as HTMLElement).hidden) return;
      const style = page ? clock.getComputedStyle?.(item) : null, tile = Boolean(style?.backgroundColor) && !/^(transparent|rgba\(0, 0, 0, 0\))$/.test(style.backgroundColor);
      if (name(item, "panel-" + index, tile ? "panel tile" : "panel") && tile) vtSurface({ "--rd-vt-backdrop": clock.getComputedStyle(page).backgroundColor, "--rd-vt-tile-radius": style.borderRadius });
    });
  }
  /* The chosen chapter morphs between stepper and focused tree above everything; the structure tile keeps a solid surface
   * while it resizes; the workbench slides in; the rest cross-fades. Subtle skips the morph. */
  function chapterScene(chapterId: string | null) {
    const subtle = documentRef.body?.classList?.contains?.("rd-motion-subtle") === true;
    return (name: Namer) => {
      const node = subtle ? null : all(content, ".rd-paper-node").find(item => item.dataset.chapterId === chapterId);
      if (node) name(node, "chapter", "chapter");
      const marker = subtle ? null : all(content, ".rd-paper-node-marker")[0];
      if (marker) name(marker, "marker", "marker");
      const map = all(content, ".rd-paper-map-section")[0];
      if (map && name(map, "surface", "surface")) { const style = clock.getComputedStyle(map); vtSurface({ "--rd-vt-surface": style.backgroundColor, "--rd-vt-surface-radius": style.borderRadius }); }
      if (map) Array.from(map.children).forEach((part, index) => name(part, "map-" + index, "content"));
      const bench = all(content, ".rd-paper-workbench")[0]; if (bench) name(bench, "bench", "bench");
      name(content, "content", "content"); panelScene(name);
    };
  }
  /* The selected-stage thumb glides; tab labels ride above it; the task list cross-fades and what follows glides. */
  function stageScene(name: Namer) {
    const bench = all(content, ".rd-paper-workbench")[0]; if (!bench) return;
    const thumb = all(bench, ".rd-paper-stage-thumb")[0];
    if (thumb && name(thumb, "thumb", "thumb")) vtSurface({ "--rd-vt-thumb": clock.getComputedStyle(thumb).backgroundColor });
    for (const tab of all(bench, ".rd-paper-stage-tab")) name(tab, "tab-" + tab.dataset.stage, "tab");
    const children = Array.from(bench.children), list = children.findIndex(node => node.matches?.(".rd-paper-task-list, .rd-empty"));
    if (list >= 0) children.slice(list).forEach((node, index) => name(node, index ? "tail-" + index : "tasks", index ? "tail" : "tasks"));
  }
  function sourceScene(name: Namer) { name(content, "content", "content"); panelScene(name); }
  /* Type or status filter: the thumb glides between segments, rows that stay glide to their new places. */
  function listScene(name: Namer) {
    if (typeFilter && !typeFilter.hidden) name(typeFilter, "type-filter", "tab");
    const thumb = all(typeFilter, ".rd-type-thumb")[0];
    if (thumb && name(thumb, "type-thumb", "thumb")) vtSurface({ "--rd-vt-thumb": clock.getComputedStyle(thumb).backgroundColor });
    for (const tab of all(typeFilter, ".rd-type-tab")) name(tab, "type-" + tab.dataset.type, "tab");
    for (const row of all(content, ".rd-project-row")) name(row, "row-" + row.dataset.projectId, "row");
    for (const node of all(content, ".rd-empty")) name(node, "rows-empty", "row");
    for (const node of all(content, ".rd-ungroup-zone.is-armed")) name(node, "ungroup-zone", "row");
    panelScene(name);
  }
  function disclosure(parent: Element, key: string, summary: string, className: string) {
    const node = el(parent, "details", null, className);
    node.dataset.disclosureKey = key; node.open = disclosures.get(key) === true;
    /* Reopened by a repaint, not by the user: skip the entrance transition. */
    if (node.open) node.className += " is-restored";
    record.disclosureNodes.set(key, node);
    node.addEventListener("toggle", () => {
      if (!node.open) node.className = node.className.replace(/\s*\bis-restored\b/, "");
      if (!record.disposed && record.disclosureNodes.get(key) === node) disclosures.set(key, Boolean(node.open));
    });
    el(node, "summary", summary);
    return node;
  }
  function captureDisclosures() {
    for (const [key, node] of record.disclosureNodes as Map<string, HTMLDetailsElement>) disclosures.set(key, Boolean(node.open));
  }
  function values(value: unknown): any[] {
    if (value == null) return [];
    if (Array.isArray(value)) return value;
    return [value];
  }
  function text(value: unknown, fallback = "—"): string {
    if (value == null || value === "") return fallback;
    return typeof value === "object" && (value as Dict).path ? String((value as Dict).display || (value as Dict).path) : String(value);
  }
  function number(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) ? value : null; }
  function numericText(value: unknown) {
    const numeric = number(value);
    return numeric === null ? "—" : numeric.toLocaleString("en-GB", { maximumFractionDigits: 6 });
  }
  function milliseconds(value: unknown): number | null {
    if (value == null || value === "") return null;
    const result = Date.parse(String(value));
    return Number.isFinite(result) ? result : null;
  }
  function dateText(value: unknown, includeTime = false) {
    const date = milliseconds(value);
    if (date === null) return "—";
    return new Date(date).toLocaleString("en-GB", includeTime
      ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
      : { year: "numeric", month: "2-digit", day: "2-digit" });
  }
  function dayPassed(value: unknown) {
    const date = milliseconds(value);
    if (date === null) return false;
    const endOfDay = new Date(date);
    endOfDay.setHours(23, 59, 59, 999);
    return endOfDay.getTime() < Date.now();
  }
  /* In-app links: rd://overall and rd://project/<id> navigate; workspace-relative files open with the system. */
  function link(parent: Element, path: string, label: string | null, className = "") {
    const anchor = el(parent, "a", label, `internal-link ${className}`.trim());
    anchor.setAttribute("href", path);
    anchor.setAttribute("data-href", path);
    anchor.addEventListener("click", event => {
      if (event.button !== 0 || event.altKey) return;
      event.preventDefault();
      event.stopPropagation();
      if (path.startsWith("rd://")) ctx.navigate(path, Boolean(event.ctrlKey || event.metaKey));
      else if (path) ctx.openWorkspaceFile(path);
    });
    return anchor;
  }
  function externalTarget(path: unknown): string | null {
    if (typeof path !== "string") return null;
    if (/^https?:\/\//i.test(path)) return path;
    const normalized = path.replace(/\\/g, "/");
    if (/^[A-Za-z]:\//.test(normalized) || normalized.startsWith("//") || normalized.startsWith("/")) return path;
    return null;
  }
  /* VS Code's registered URL handler opens a local folder or file; UNC shares are left to the system link. */
  function editorTarget(path: unknown): string | null {
    if (typeof path !== "string") return null;
    const normalized = path.replace(/\\/g, "/"), encode = (part: string) => encodeURIComponent(part).replace(/%3A/gi, ":");
    if (/^[A-Za-z]:\//.test(normalized)) return "vscode://file/" + normalized.split("/").map(encode).join("/");
    if (normalized.startsWith("/") && !normalized.startsWith("//")) return "vscode://file" + normalized.split("/").map(encode).join("/");
    return null;
  }
  function resourceLink(parent: Element, path: unknown, label: string) {
    const target = externalTarget(path);
    if (target) {
      const anchor = el(parent, "a", label, "external-link");
      anchor.href = /^https?:/i.test(target) ? target : "#";
      anchor.title = String(path);
      anchor.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); ctx.openExternal(target); });
      return anchor;
    }
    if (typeof path === "string" && path && !path.includes("..")) return link(parent, path, label);
    return el(parent, "span", label);
  }
  function badge(parent: Element, state: string | null | undefined, label: string) {
    const node = el(parent, "span", label, "rd-status");
    node.dataset.state = state || "unknown";
    return node;
  }
  function field(parent: Element, label: string, value: string, key: string | null = null) {
    const node = el(parent, "div", null, "rd-field");
    el(node, "span", label, "rd-label");
    const shown = el(node, "span", value, "rd-value");
    if (key) figure(shown, key, node);
    return node;
  }
  function empty(parent: Element, heading: string, message?: string) {
    const node = el(parent, "div", null, "rd-empty");
    el(node, "strong", heading);
    if (message) el(node, "p", message);
  }
  function table(parent: Element, headers: string[]) {
    const wrap = el(parent, "div", null, "rd-table-wrap");
    const node = el(wrap, "table", null, "rd-table");
    const tr = el(el(node, "thead"), "tr");
    for (const heading of headers) el(tr, "th", heading).scope = "col";
    return el(node, "tbody");
  }
  let actionMessage = "";
  function actionBridge(): ActionBridge { return ctx.bridge; }
  function status(): BridgeStatus { return actionBridge().getStatus?.() || { running: false, busy: false }; }
  function renderActions(parent: Element) {
    const bridge = actionBridge();
    const state = status();
    const actions = el(parent, "div", null, "rd-controls");
    const add = (label: string, method: keyof ActionBridge, disabled: boolean, primary = false) => {
      const button = el(actions, "button", label, primary ? "rd-control mod-cta" : "rd-control");
      button.type = "button";
      button.disabled = disabled || typeof bridge[method] !== "function";
      button.addEventListener("click", async event => {
        event.preventDefault(); event.stopPropagation();
        const current = actionBridge() as any;
        if (record.disposed || typeof current?.[method] !== "function" || button.disabled) return;
        button.disabled = true;
        actionMessage = "";
        try { await current[method](); }
        catch (error) { actionMessage = String((error as Error).message || error); }
        if (!record.disposed) paint();
      });
      return button;
    };
    /* Icon buttons with a tooltip (the workspace folder and the Obsidian import live in Settings → Workspace). */
    const iconAction = (label: string, detail: string, method: keyof ActionBridge, kind: string, disabled: boolean, pressed?: boolean) => {
      const button = add("", method, disabled);
      button.classList.add("rd-icon-control");
      button.setAttribute("aria-label", label);
      if (pressed !== undefined) button.setAttribute("aria-pressed", String(pressed));
      icon(button, kind, "rd-control-glyph");
      hint(button, label, detail);
      return button;
    };
    add("Connect project", "connect", state.busy, true);
    createButton(actions, "New project");
    add(state.running ? "Stop live updates" : "Start live updates", state.running ? "stop" : "start", state.busy);
    iconAction("Collect now", state.running ? "Live updates already collect as files change" : "Read every connected folder once", "collect", "refresh", state.busy || state.running);
    if (bridge.toggleWidget) iconAction(state.widgetVisible ? "Hide floating widget" : "Show floating widget", "Progress at a glance, beside your work", "toggleWidget", "widget", false, Boolean(state.widgetVisible));
    if (bridge.openSettings) iconAction("Settings", "Ctrl+,", "openSettings", "settings", false);
    el(parent, "span", state.running ? "Live updates · Running" : "Live updates · Stopped", "rd-live-state" + (state.running ? " is-running" : ""));
    if (state.error) el(parent, "p", `Last action: ${state.error}`, "rd-caption");
    if (actionMessage) el(parent, "p", actionMessage, "rd-caption");
  }
  function sourceHealth(source: Source) { return source.health; }
  function milestones(project: Project): Task[] { return Array.isArray(project.rawMilestones) ? project.rawMilestones : []; }
  function runtime(source: Source | null | undefined): Dict { return source?.state?.runtime || { status: "unconfigured", progress: null }; }
  function cases(source: Source): Dict { return source.state?.cases || {}; }
  function projectStatus(project: Project) { return statusOf(project); }
  function milestoneCount(project: Project) {
    const tasks = milestones(project);
    return { total: tasks.length, completed: tasks.filter(task => task.completed).length };
  }
  function progress(parent: Element, source: Source) {
    const run = runtime(source), value = number(run.progress), current = number(run.currentValue), target = number(run.targetValue);
    if (value === null || value < 0 || value > 1 || current === null || target === null || target <= 0) return;
    const bar = el(parent, "progress", null, "rd-progress");
    bar.max = 1; bar.value = value;
    bar.setAttribute("aria-label", source.title + " single-run progress");
    el(parent, "p", Math.round(value * 100) + "% · " + numericText(current) + " / " + numericText(target) + " " + text(run.unit, "") + " · Single run" + (source.health.isStale || source.readError ? " · Retained value" : ""), "rd-caption");
  }
  async function load(): Promise<Model> {
    const loaded = await ctx.load();
    return { projects: requestedProject ? loaded.projects.filter(project => project.id === requestedProject) : loaded.projects, errors: loaded.errors };
  }
  /* A parent's progress is its own milestones plus every sub-project's. */
  function familyCount(project: Project) {
    const counts = milestoneCount(project);
    for (const child of project.children || []) { const own = milestoneCount(child); counts.total += own.total; counts.completed += own.completed; }
    return counts;
  }
  function countText(counts: { total: number; completed: number }, project: Project) {
    const family = project.children?.length || 0;
    return (counts.total ? counts.completed + " / " + counts.total + " milestones complete" : "No milestones defined") + (family ? " · incl. " + plural(family, "sub-project") : "");
  }
  interface Alert { project: Project; source?: Source; state: string; label: string; message: string; suggestion?: DiscoverySuggestion; folder?: string }
  function attention(projects: Project[]): Alert[] {
    const items: Alert[] = [];
    for (const project of projects) {
      if (projectStatus(project) === "blocked") items.push({ project, state: "blocked", label: "Project blocked", message: text(project.page?.next_step, "Set the next action in Edit project.") });
      for (const source of project.sources) {
        if (runtime(source).status === "failed") items.push({ project, source, state: "failed", label: "Run failed", message: source.title });
        if (sourceHealth(source).state === "error") items.push({ project, source, state: "error", label: "Collection error", message: source.title + " · " + (source.readError || text(source.state?.collection?.error, "Invalid collection metadata")) });
        else if (sourceHealth(source).state === "stale") items.push({ project, source, state: "stale", label: "Stale data", message: source.title + " · Last success " + dateText(source.state?.collection?.lastSuccessfulAt, true) });
      }
      if (projectStatus(project) !== "completed" && dayPassed(project.page?.due)) items.push({ project, state: "warning", label: "Project overdue", message: dateText(project.page.due) });
      for (const task of milestones(project).filter(task => !task.completed && dayPassed(task.due))) items.push({ project, state: "warning", label: "Milestone overdue", message: milestoneLabel(task) });
    }
    return items.concat(discoveryItems(projects));
  }
  /* ---------- Sub-project discovery ----------
   * The app scans each top-level project's discovery folder and keeps new candidate folders in memory; the view only
   * shows them (Needs attention, after real alerts) and hands Add / Ask agent / Dismiss back to the bridge. */
  const candidateKinds: Record<string, string> = { latex: "LaTeX", abaqus: "Abaqus", isale: "iSALE" };
  const typeNouns: Record<string, string> = { thesis: "thesis", paper: "paper", simulation: "simulation", experiment: "experiment", other: "project" };
  /* What a suggested folder looks like: "LaTeX paper" for a built-in type, the saved type's own title otherwise. */
  function suggestionKind(suggestion: DiscoverySuggestion) {
    const best = suggestion.bestCandidate;
    if (!best) return null;
    if (best.origin !== "user" && candidateKinds[String(best.sourceKind)]) return candidateKinds[String(best.sourceKind)] + " " + (typeNouns[best.projectType] || "project");
    return text(best.title, "project");
  }
  function discoverySuggestions(): DiscoverySuggestion[] {
    try { const snapshot = actionBridge().discoveries?.(); return Array.isArray(snapshot?.suggestions) ? snapshot!.suggestions : []; }
    catch (_) { return []; }
  }
  function discoveryItems(projects: Project[]): Alert[] {
    const byId = new Map(projects.map(project => [project.id, project])), items: Alert[] = [];
    for (const suggestion of discoverySuggestions()) {
      if (!suggestion || typeof suggestion.id !== "string") continue;
      /* A sub-project is filed under the top-level project (one displayed level). */
      const project = byId.get(String(suggestion.parentId)) || byId.get(String(suggestion.projectId));
      if (!project) continue;
      const best = suggestion.bestCandidate, kind = suggestionKind(suggestion);
      const confidence = Number(best?.confidence);
      const folder = text(suggestion.relative, "") || String(suggestion.folder || "").split(/[\\/]/).filter(Boolean).pop() || "New folder";
      items.push({ project, suggestion, state: "suggestion", label: "New folder", folder,
        message: best ? kind + " · " + (Number.isFinite(confidence) ? Math.round(confidence * 100) + "% match" : "Matched") : "No monitoring type matched" });
    }
    return items;
  }
  /* Alerts and suggestions are counted apart: a new folder is an offer, not a problem. */
  function attentionCounts(items: Alert[]) {
    const suggestions = items.filter(item => item.state === "suggestion").length;
    return { alerts: items.length - suggestions + model.errors.length, suggestions };
  }
  function suggestionActions(body: Element, suggestion: DiscoverySuggestion) {
    const bridge = actionBridge(), actions = el(body, "div", null, "rd-alert-actions");
    if (typeof bridge.addDiscovery === "function") {
      if (suggestion.bestCandidate) actionButton(actions, "Add as sub-project", true, () => actionBridge().addDiscovery!(suggestion.id));
      else actionButton(actions, "Ask agent", true, () => actionBridge().addDiscovery!(suggestion.id, { agent: true }));
    }
    if (typeof bridge.dismissDiscovery === "function") actionButton(actions, "Dismiss", true, () => actionBridge().dismissDiscovery!(suggestion.id));
  }
  /* One suggested folder: "PhD › " muted, the folder name bold, then what it looks like and how well it matched.
   * Inside a folded group (nested) the parent is already named by the group. */
  function renderSuggestion(list: Element, item: Alert, nested = false) {
    const suggestion = item.suggestion!, project = item.project;
    const li = el(list, "li", null, nested ? "rd-suggestion-item" : "rd-alert rd-suggestion"); li.dataset.state = "suggestion"; li.dataset.suggestion = suggestion.id;
    if (prior("alert:suggestion:" + suggestion.id, 1) === null) updated(li);
    if (!nested) icon(li, "project", "rd-alert-icon");
    const body = el(li, "div", null, "rd-alert-body");
    const where = [suggestion.rootName, suggestion.relative].filter(Boolean).join("/");
    if (where) li.title = where;
    if (!nested) el(body, "span", project.title + " › ", "rd-alert-parent");
    el(body, "span", item.folder!, "rd-alert-title");
    el(body, "p", item.message, "rd-caption");
    suggestionActions(body, suggestion);
  }
  /* More than two new folders under one parent fold into "3 new folders in PhD", opened on demand. */
  function renderSuggestionGroup(list: Element, project: Project, group: Alert[]) {
    const li = el(list, "li", null, "rd-alert rd-suggestion rd-suggestion-group"); li.dataset.state = "suggestion"; li.dataset.suggestionGroup = project.id;
    if (prior("alert:suggestion-group:" + project.id, group.length) === null) updated(li);
    icon(li, "project", "rd-alert-icon");
    const body = el(li, "div", null, "rd-alert-body");
    el(body, "span", group.length + " new folders in ", "rd-alert-parent");
    el(body, "span", project.title, "rd-alert-title");
    el(body, "p", group.map(item => item.folder).join(" · "), "rd-caption");
    const details = disclosure(body, "suggestions:" + project.id, "Review folders", "rd-source-details rd-suggestion-details");
    const nested = el(details, "ul", null, "rd-suggestion-list");
    for (const item of group) renderSuggestion(nested, item, true);
  }
  function actionButton(parent: Element, label: string, enabled: boolean, run: () => Promise<unknown>, className = "rd-control") {
    const button = el(parent, "button", label, className);
    button.type = "button"; button.disabled = !enabled || Boolean(status().busy);
    button.addEventListener("click", async event => {
      event.preventDefault(); event.stopPropagation();
      if (button.disabled || record.disposed) return;
      button.disabled = true; actionMessage = "";
      try { await run(); await refresh(true); }
      catch (error) { actionMessage = String((error as Error).message || error); if (!record.disposed) paint(); }
    });
    return button;
  }
  function editButton(parent: Element, project: Project) {
    const bridge = actionBridge();
    return actionButton(parent, "Edit project", typeof bridge.editProject === "function" && status().canEdit !== false, () => actionBridge().editProject!(project.id));
  }
  function addMilestoneButton(parent: Element, project: Project, options: { sourceId?: string; chapterId?: string; stage?: string; chapterTitle?: string } | undefined = undefined) {
    const bridge = actionBridge();
    if (typeof bridge.addMilestoneInline === "function" && status().canAddMilestone !== false) { milestoneComposer(parent, project, options || {}); return null; }
    return actionButton(parent, "Add milestone", typeof bridge.addMilestone === "function" && status().canAddMilestone !== false, () => actionBridge().addMilestone!(project.id, options));
  }
  /* Repaints wait while the user types into a milestone field, then catch up. */
  function holdWhileEditing(input: HTMLInputElement) {
    input.addEventListener("focus", () => { record.hold = true; });
    input.addEventListener("blur", () => {
      clock.setTimeout(() => {
        if (content.contains(documentRef.activeElement) && (documentRef.activeElement as HTMLElement).matches?.(".rd-task-composer input, .rd-task-composer select, .rd-paper-task-text.is-editing")) return;
        record.hold = false;
        if (record.refreshQueued && !record.disposed) { record.refreshQueued = false; void refresh(); }
      }, 0);
    });
  }
  /* Add a milestone like a to-do list: type, Enter, and the next line is ready (Esc clears). An optional due date sits
   * at the end; on a chapter's workbench the milestone is bound to that chapter and stage. */
  function milestoneComposer(parent: Element, project: Project, options: { sourceId?: string; chapterId?: string; stage?: string; chapterTitle?: string }) {
    const key = "compose:" + project.id + ":" + (options.chapterId || "");
    const state = record.composer.get(key) || { text: "", due: "", section: "", stage: options.stage || "draft" };
    record.composer.set(key, state);
    const form = el(parent, "form", null, "rd-task-composer") as HTMLFormElement;
    form.dataset.composer = key;
    el(form, "span", null, "rd-task-composer-plus").setAttribute("aria-hidden", "true");
    const input = el(form, "input", null, "rd-task-composer-title") as HTMLInputElement;
    input.type = "text"; input.maxLength = 300; input.value = state.text; input.spellcheck = true;
    input.placeholder = options.chapterId ? "Add a milestone to " + (options.chapterTitle || "this section") : "Add a milestone";
    input.setAttribute("aria-label", input.placeholder);
    /* Paper projects: which section it belongs to (fixed on a workbench) and its stage. */
    const paperSource = options.sourceId ? project.sources.find(item => item.id === options.sourceId) : project.sources.find(item => item.kind === "latex" && paperSnapshot(item));
    const paper = paperSnapshot(paperSource);
    let section: HTMLSelectElement | null = null, stage: HTMLSelectElement | null = null;
    if (paper && paperSource) {
      if (!options.chapterId) {
        section = el(form, "select", null, "rd-task-composer-pick rd-task-composer-section") as HTMLSelectElement;
        section.setAttribute("aria-label", "Section");
        const none = el(section, "option", "No section") as HTMLOptionElement; none.value = "";
        const tree = paperTree(paper);
        const walk = (nodes: Chapter[], depth: number) => { for (const chapter of nodes) { const option = el(section!, "option", "\u2003".repeat(depth) + chapter.title) as HTMLOptionElement; option.value = chapter.id; walk(tree.children.get(chapter.id) || [], depth + 1); } };
        walk(tree.roots, 0);
        section.value = state.section;
        section.addEventListener("change", () => { state.section = section!.value; if (stage) stage.hidden = !section!.value; input.focus(); });
      }
      stage = el(form, "select", null, "rd-task-composer-pick rd-task-composer-stage") as HTMLSelectElement;
      stage.setAttribute("aria-label", "Stage");
      for (const [id, name] of Object.entries(PAPER_STAGE_NAMES)) { const option = el(stage, "option", name) as HTMLOptionElement; option.value = id; }
      /* On a workbench the stage tab decides; elsewhere the last choice is kept. */
      if (options.chapterId && options.stage) state.stage = options.stage;
      stage.value = state.stage || "draft";
      stage.hidden = !options.chapterId && !state.section;
      stage.addEventListener("change", () => { state.stage = stage!.value; input.focus(); });
    }
    const due = el(form, "input", null, "rd-task-composer-due") as HTMLInputElement;
    due.type = "date"; due.value = state.due; due.title = "Due date (optional)"; due.setAttribute("aria-label", "Due date (optional)");
    holdWhileEditing(input); holdWhileEditing(due);
    if (section) holdWhileEditing(section as unknown as HTMLInputElement);
    if (stage) holdWhileEditing(stage as unknown as HTMLInputElement);
    /* After an add, the fresh field takes the cursor again for the next one. */
    if (record.refocus === key) { record.refocus = null; clock.requestAnimationFrame(() => { if (input.isConnected) input.focus(); }); }
    input.addEventListener("input", () => { state.text = input.value; });
    due.addEventListener("change", () => { state.due = due.value; input.focus(); });
    form.addEventListener("keydown", event => {
      if ((event as KeyboardEvent).key !== "Escape") return;
      event.preventDefault(); event.stopPropagation();
      state.text = ""; state.due = ""; input.value = ""; due.value = ""; (documentRef.activeElement as HTMLElement | null)?.blur?.();
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const title = input.value.trim();
      if (!title || input.disabled) return;
      input.disabled = true; actionMessage = "";
      try {
        const chapterId = options.chapterId || state.section || undefined;
        await actionBridge().addMilestoneInline!(project.id, { title, due: due.value || null, sourceId: chapterId ? (options.sourceId || paperSource?.id) : undefined, chapterId,
          stage: chapterId ? (stage?.value || options.stage || "draft") : undefined });
        state.text = ""; state.due = "";
        record.refocus = key; record.hold = false; record.refreshQueued = false;
        await refresh(true);
      } catch (error) {
        actionMessage = String((error as Error).message || error); input.disabled = false;
        if (!record.disposed) { record.hold = false; paint(); }
      }
    });
  }
  const PAPER_STAGE_NAMES: Record<string, string> = { draft: "Draft", figures: "Figures", references: "References", review: "Review", complete: "Complete" };
  /* Edit in place: the title becomes a field (Enter or leaving saves, Esc cancels); the date button sets or clears the
   * due date; × deletes (the app offers Undo). */
  function taskEditing(row: HTMLElement, label: HTMLElement, project: Project, task: Task) {
    const bridge = actionBridge();
    const request = { line: task.line, text: String(task.text || "") };
    const run = async (work: () => Promise<unknown>) => {
      actionMessage = "";
      try { await work(); await refresh(true); }
      catch (error) { actionMessage = String((error as Error).message || error); if (!record.disposed) paint(); }
    };
    if (typeof bridge.editMilestone === "function") {
      label.classList.add("is-editable"); label.tabIndex = 0; label.title = "Click to rename";
      /* The title itself becomes editable: same type and place, a caret and a faint underline, no field box. */
      const begin = () => {
        if (label.classList.contains("is-editing") || Boolean(status().busy)) return;
        const original = milestoneLabel(task);
        label.textContent = original;
        label.contentEditable = "plaintext-only";
        if (!label.isContentEditable) label.contentEditable = "true";
        label.classList.add("is-editing"); label.removeAttribute("title"); label.setAttribute("role", "textbox"); label.setAttribute("aria-label", "Milestone title");
        record.hold = true;
        label.focus();
        const range = documentRef.createRange(); range.selectNodeContents(label);
        const selection = documentRef.getSelection(); selection?.removeAllRanges(); selection?.addRange(range);
        let done = false;
        const finish = (save: boolean) => {
          if (done) return; done = true;
          label.removeEventListener("keydown", onKey); label.removeEventListener("blur", onBlur);
          const title = (label.textContent || "").replace(/\s+/g, " ").trim();
          label.contentEditable = "false"; label.classList.remove("is-editing"); label.removeAttribute("role"); label.title = "Click to rename";
          if (save && title && title !== original) { label.textContent = title; void run(() => actionBridge().editMilestone!(project.id, { ...request, title })); return; }
          label.textContent = original;
          record.hold = false;
          if (record.refreshQueued) { record.refreshQueued = false; void refresh(); }
        };
        const onKey = (event: KeyboardEvent) => {
          if (event.key === "Enter") { event.preventDefault(); finish(true); label.blur(); }
          else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); finish(false); label.blur(); }
          else if ((label.textContent || "").length >= 300 && event.key.length === 1 && !event.ctrlKey && !event.metaKey) event.preventDefault();
        };
        const onBlur = () => finish(true);
        label.addEventListener("keydown", onKey);
        label.addEventListener("blur", onBlur);
      };
      label.addEventListener("click", begin);
      label.addEventListener("keydown", event => { if ((event as KeyboardEvent).key === "Enter" && !label.classList.contains("is-editing")) { event.preventDefault(); begin(); } });
    }
    if (typeof bridge.editMilestone !== "function" && typeof bridge.deleteMilestone !== "function") return;
    const actions = el(row, "div", null, "rd-task-actions");
    if (typeof bridge.editMilestone === "function") {
      const dueButton = el(actions, "label", null, "rd-task-action rd-task-due-button" + (task.due ? " has-date" : ""));
      dueButton.title = task.due ? "Due " + dateText(task.due) + " · change or clear" : "Add a due date";
      icon(dueButton, "warning", "rd-task-action-glyph");
      const picker = el(dueButton, "input", null, "rd-task-due-input") as HTMLInputElement;
      picker.type = "date"; picker.value = task.due || ""; picker.setAttribute("aria-label", dueButton.title);
      dueButton.addEventListener("click", event => { if (event.target !== picker) { event.preventDefault(); try { picker.showPicker(); } catch (_) { picker.focus(); } } });
      picker.addEventListener("change", () => void run(() => actionBridge().editMilestone!(project.id, { ...request, due: picker.value || null })));
    }
    if (typeof bridge.deleteMilestone === "function") {
      const remove = el(actions, "button", null, "rd-task-action rd-task-delete") as HTMLButtonElement;
      remove.type = "button"; remove.title = "Delete milestone"; remove.setAttribute("aria-label", "Delete “" + milestoneLabel(task) + "”");
      remove.textContent = "×";
      remove.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); remove.disabled = true; void run(() => actionBridge().deleteMilestone!(project.id, request)); });
    }
  }
  /* createProject(options) is feature-detected; { parentId } files the new project under a parent. */
  function canCreate() { return typeof actionBridge().createProject === "function" && status().canCreate !== false; }
  function createButton(parent: Element, label: string, options: { parentId?: string } | undefined = undefined, className = "rd-control") {
    if (!canCreate()) return null;
    return actionButton(parent, label, true, () => (options ? actionBridge().createProject!(options) : actionBridge().createProject!()), className);
  }
  function projectMenu(parent: Element, project: Project) {
    const bridge = actionBridge();
    const canRemove = typeof bridge.removeProject === "function";
    /* Only a top-level project can take sub-projects: the hierarchy shows one level. */
    const canNest = !project.parent && canCreate();
    const canMove = canGroup() && (canUngroup(project) || parentChoices(project).length > 0);
    const canOpen = project.sources.some(source => source.directory) && typeof bridge.openSource === "function";
    /* A top-level project with a discovery folder can be scanned on demand. */
    const canScan = !project.parent && typeof (project.catalog as Dict | null)?.discovery?.rootAlias === "string" && typeof bridge.scanDiscoveries === "function";
    if (!canRemove && !canNest && !canMove && !canOpen && !canScan) return;
    const menu = disclosure(parent, "project:" + project.id + ":menu", "More", "rd-settings-menu rd-project-menu");
    const items = el(menu, "div", null, "rd-menu-items");
    if (canNest) createButton(items, "Add sub-project", { parentId: project.id });
    if (canScan) {
      const scanning = Boolean(status().discoveryScanning);
      const scan = actionButton(items, scanning ? "Scanning for new folders…" : "Scan for new folders", !scanning, () => {
        menu.open = false; disclosures.set(menu.dataset.disclosureKey!, false);
        return actionBridge().scanDiscoveries!(project.id);
      }, "rd-control rd-scan-discovery");
      scan.classList.add("rd-scan-discovery");
    }
    if (canMove) groupingItems(items, menu, project);
    if (canOpen) for (const source of project.sources.filter(item => item.directory)) {
      actionButton(items, project.sources.length > 1 ? "Open folder · " + source.title : "Open source folder", true, () => actionBridge().openSource!(source.id, null));
    }
    if (typeof bridge.changeSourceFolder === "function") for (const source of project.sources) {
      actionButton(items, project.sources.length > 1 ? "Change folder · " + source.title : "Change source folder…", !status().busy, () => {
        menu.open = false; disclosures.set(menu.dataset.disclosureKey!, false);
        return actionBridge().changeSourceFolder!(source.id);
      });
    }
    if (!canRemove) return;
    const button = actionButton(items, "Remove from dashboard", status().canRemove !== false, () => actionBridge().removeProject!(project.id), "rd-control rd-remove-project");
    button.classList.add("rd-remove-project");
  }
  /* ---------- Grouping: drag a row onto a top-level row, or out of its family ----------
   * The rules mirror the Edit project form (one displayed level): a project with sub-projects stays top-level, a parent
   * must itself be top-level, and nothing is its own parent. The app revalidates against the workspace. */
  function canGroup() { return typeof actionBridge().setParent === "function" && status().canGroup !== false; }
  function parentKey(project: Project | null | undefined) { const value = String(project?.parentId ?? "").trim(); return value && value !== "null" && value !== "~" ? value : null; }
  function groupable(project: Project | null | undefined) { return Boolean(project?.catalog) && validId(project!.id); }
  function hasSubProjects(project: Project) { return model.projects.some(item => item !== project && parentKey(item) === project.id); }
  function canFileUnder(project: Project, target: Project) {
    return groupable(project) && groupable(target) && target !== project && !parentKey(target) && parentKey(project) !== target.id && !hasSubProjects(project);
  }
  function parentChoices(project: Project) { return model.projects.filter(target => canFileUnder(project, target)).sort((a, b) => a.title.localeCompare(b.title, "en-GB") || a.id.localeCompare(b.id)); }
  function canUngroup(project: Project) { return groupable(project) && Boolean(project.parent); }
  function setParentLocally(project: Project, parentId: string | null) {
    project.parentId = parentId || "";
    if (project.page) project.page = { ...project.page, parent_id: parentId };
  }
  /* Keeps a move on screen until the workspace agrees, or 4 s after the write so an external edit is never hidden long. */
  function applyPending(next: Model): Model {
    if (requestedProject || !pendingParents.size || !Array.isArray(next?.projects)) return next;
    let changed = false;
    for (const [id, entry] of pendingParents) {
      const project = next.projects.find(item => item.id === id);
      if (!project || parentKey(project) === entry.parentId || (entry.until !== null && elapsed() > entry.until)) { pendingParents.delete(id); continue; }
      setParentLocally(project, entry.parentId); changed = true;
    }
    if (changed) resolveHierarchy(next.projects);
    return next;
  }
  function focusRow(id: string) {
    const row = all(content, ".rd-project-row").find(item => item.dataset.projectId === id);
    all(row, ".rd-project-title a")[0]?.focus?.({ preventScroll: true });
  }
  /* parentId null moves the project out of its family. focus: return keyboard focus to the moved row (menu use). */
  async function regroup(project: Project, parentId: string | null, focus = false) {
    if (record.disposed || record.regrouping || !canGroup()) return;
    const id = project.id, from = parentKey(project);
    if (from === parentId) return;
    record.regrouping = true; actionMessage = "";
    if (parentId) projectUi.collapsed.delete(parentId);
    pendingParents.set(id, { parentId, until: null });
    const show = (update: () => void) => { record.regroupTransition = transition(() => { if (record.disposed) return; update(); paint(); if (focus) focusRow(id); }, listScene); };
    show(() => applyPending(model));
    try {
      await actionBridge().setParent!(id, parentId);
      const entry = pendingParents.get(id); if (entry?.parentId === parentId) entry.until = elapsed() + 4000;
    } catch (error) {
      if (pendingParents.get(id)?.parentId === parentId) pendingParents.delete(id);
      actionMessage = String((error as Error).message || error);
      if (!record.disposed) show(() => { const moved = model.projects.find(item => item.id === id); if (moved) { setParentLocally(moved, from); resolveHierarchy(model.projects); } });
    } finally { record.regrouping = false; }
    if (!record.disposed) void refresh();
  }
  /* Pointer-driven, not HTML5 drag and drop: the app window takes OS file drops natively (to connect folders), which
   * disables in-page HTML5 drags on Windows. A press that travels a few pixels lifts the row; a floating label follows
   * the pointer, the row or list under it is found by hit-testing, and Escape cancels. */
  const DRAG_THRESHOLD = 6, EDGE = 48, SCROLL_STEP = 14;
  const dropTargets = new WeakMap<Element, { project: Project | null; zone: HTMLElement | null }>();
  /* The hovered target: a calm inset highlight with a small hint, or the ungroup zone. null clears it. */
  function setDropTarget(node: HTMLElement | null, label: string | null = null) {
    const drag = record.drag;
    if (!drag || drag.over === node) return;
    if (drag.over) setClass(drag.over, "is-drop-target", false);
    drag.hint?.remove?.(); drag.hint = null; drag.over = node;
    if (!node) return;
    setClass(node, "is-drop-target", true);
    if (label) { drag.hint = el(node, "span", label, "rd-drop-hint"); drag.hint.setAttribute("aria-hidden", "true"); }
  }
  /* keep: leave the lifted source and highlight in place for the transition's old capture; the repaint replaces them. */
  function endDrag(keep = false) {
    const drag = record.drag;
    if (!drag) return null;
    drag.ghost?.remove?.();
    if (drag.scrollFrame !== null) clock.cancelAnimationFrame(drag.scrollFrame);
    documentRef.removeEventListener("keydown", drag.onKey, true);
    if (!keep) {
      setDropTarget(null);
      setClass(drag.source, "is-dragging", false);
      if (drag.zone) setClass(drag.zone, "is-armed", false);
      setClass(root, "is-grouping", false);
    }
    record.drag = null;
    if (record.refreshQueued && !record.busy && !record.disposed) { record.refreshQueued = false; void refresh(); }
    return drag;
  }
  function wireDropList(list: HTMLElement, zone: HTMLElement | null) { dropTargets.set(list, { project: null, zone }); }
  /* The nearest scrolling ancestor, for edge auto-scroll while a row is lifted. */
  function scrollParent(node: Element | null): HTMLElement | null {
    for (let item = node?.parentElement || null; item; item = item.parentElement) {
      const style = clock.getComputedStyle(item);
      if (/(auto|scroll)/.test(style.overflowY) && item.scrollHeight > item.clientHeight) return item;
    }
    return (documentRef.scrollingElement as HTMLElement | null) || null;
  }
  /* What a release here would do: group under the row under the pointer, move out of the family anywhere else in the
   * list, or nothing. The source row itself, and rows that cannot take it, answer nothing. */
  function hover(drag: Dict, x: number, y: number) {
    drag.x = x; drag.y = y;
    if (drag.ghost) drag.ghost.style.transform = `translate(${Math.round(x + 14)}px, ${Math.round(y + 10)}px)`;
    let target: { node: Element; project: Project | null; zone: HTMLElement | null } | null = null;
    for (let node: Element | null = documentRef.elementFromPoint(x, y); node && node !== documentRef.body; node = node.parentElement) {
      const found = dropTargets.get(node);
      if (found) { target = { node, ...found }; break; }
    }
    if (target?.project) {
      if (canFileUnder(drag.project, target.project)) { setDropTarget(target.node as HTMLElement, "Group under " + target.project.title); drag.action = { parentId: target.project.id }; }
      else { setDropTarget(null); drag.action = null; }
    } else if (target && canUngroup(drag.project)) { setDropTarget(target.zone); drag.action = { parentId: null }; }
    else { setDropTarget(null); drag.action = null; }
  }
  function autoScroll(drag: Dict) {
    drag.scrollFrame = null;
    const scroller = drag.scroller as HTMLElement | null;
    if (record.drag !== drag || !scroller) return;
    const box = scroller === documentRef.scrollingElement ? { top: 0, bottom: clock.innerHeight } : scroller.getBoundingClientRect();
    const step = drag.y < box.top + EDGE ? -SCROLL_STEP * Math.min(1, (box.top + EDGE - drag.y) / EDGE)
      : drag.y > box.bottom - EDGE ? SCROLL_STEP * Math.min(1, (drag.y - box.bottom + EDGE) / EDGE) : 0;
    if (!step) return;
    const before = scroller.scrollTop;
    scroller.scrollTop += step;
    if (scroller.scrollTop !== before) hover(drag, drag.x, drag.y);
    drag.scrollFrame = clock.requestAnimationFrame(() => autoScroll(drag));
  }
  function beginDrag(card: HTMLElement, project: Project, zone: HTMLElement | null, x: number, y: number) {
    hideTip(true);
    documentRef.getSelection?.()?.removeAllRanges?.();
    const drag: Dict = { project, source: card, zone, over: null, hint: null, action: null, x, y, scrollFrame: null, scroller: scrollParent(card), ghost: null, onKey: null };
    drag.onKey = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); endDrag(); } };
    documentRef.addEventListener("keydown", drag.onKey, true);
    record.drag = drag;
    setClass(card, "is-dragging", true); setClass(root, "is-grouping", true);
    drag.ghost = el(root, "div", project.title, "rd-drag-ghost");
    drag.ghost.setAttribute("aria-hidden", "true");
    if (zone && canUngroup(project)) { zone.children[0].textContent = "Move out of " + project.parent!.title; setClass(zone, "is-armed", true); }
    hover(drag, x, y);
  }
  /* A finished drag must not also click the link or button it was released on. */
  function swallowNextClick() {
    const swallow = (event: Event) => { event.preventDefault(); event.stopPropagation(); };
    documentRef.addEventListener("click", swallow, { capture: true, once: true });
    clock.setTimeout(() => documentRef.removeEventListener("click", swallow, { capture: true }), 0);
  }
  function wireDragRow(card: HTMLElement, project: Project, zone: HTMLElement | null) {
    dropTargets.set(card, { project, zone: null });
    if (!groupable(project) || hasSubProjects(project)) return;
    setClass(card, "is-draggable", true);
    for (const anchor of all(card, "a")) anchor.draggable = false;
    el(card, "span", null, "rd-drag-grip").setAttribute("aria-hidden", "true");
    card.addEventListener("pointerdown", event => {
      const from = event.target as HTMLElement | null;
      if (event.button !== 0 || !event.isPrimary || record.disposed || record.regrouping || record.drag || !canGroup()
        || from?.closest?.("button, summary, input, select, textarea, .rd-menu-items")) return;
      const start = { x: event.clientX, y: event.clientY, id: event.pointerId };
      const stop = () => {
        documentRef.removeEventListener("pointermove", move, true);
        documentRef.removeEventListener("pointerup", up, true);
        documentRef.removeEventListener("pointercancel", cancel, true);
        clock.removeEventListener("blur", cancel);
      };
      const move = (moved: PointerEvent) => {
        if (moved.pointerId !== start.id) return;
        if (!record.drag) {
          if (Math.hypot(moved.clientX - start.x, moved.clientY - start.y) < DRAG_THRESHOLD) return;
          if (record.disposed || record.regrouping || !canGroup()) { stop(); return; }
          beginDrag(card, project, zone, moved.clientX, moved.clientY);
        }
        const drag = record.drag;
        if (drag?.source !== card) return;
        moved.preventDefault();
        hover(drag, moved.clientX, moved.clientY);
        if (drag.scrollFrame === null) autoScroll(drag);
      };
      const finish = (ended: Event, drop: boolean) => {
        const pointer = ended as PointerEvent;
        if (typeof pointer.pointerId === "number" && pointer.pointerId !== start.id) return;
        stop();
        const drag = record.drag;
        if (drag?.source !== card) return;
        if (drop) { hover(drag, pointer.clientX, pointer.clientY); swallowNextClick(); }
        const action = drop ? drag.action : null;
        if (action) { endDrag(true); void regroup(drag.project, action.parentId); } else endDrag();
      };
      const up = (ended: PointerEvent) => finish(ended, true);
      const cancel = (ended: Event) => finish(ended, false);
      documentRef.addEventListener("pointermove", move, true);
      documentRef.addEventListener("pointerup", up, true);
      documentRef.addEventListener("pointercancel", cancel, true);
      clock.addEventListener("blur", cancel);
    });
  }
  /* Keyboard and menu equivalent of dragging: Move into <top-level project>, Remove from <parent>. */
  function groupingItems(items: HTMLElement, menu: HTMLDetailsElement, project: Project) {
    if (!canGroup() || !groupable(project)) return;
    const busy = Boolean(status().busy);
    const add = (label: string, parentId: string | null) => {
      const button = el(items, "button", label, "rd-control rd-group-move"); button.type = "button"; button.disabled = busy;
      button.addEventListener("click", event => {
        event.preventDefault(); event.stopPropagation();
        if (button.disabled || record.disposed) return;
        menu.open = false; disclosures.set(menu.dataset.disclosureKey!, false);
        void regroup(project, parentId, true);
      });
    };
    if (canUngroup(project)) add("Remove from " + project.parent!.title, null);
    for (const target of parentChoices(project)) add("Move into " + target.title, target.id);
  }
  function milestoneProgress(parent: Element, project: Project) {
    const counts = familyCount(project), family = project.children?.length || 0, wrap = el(parent, "div", null, "rd-milestone-progress");
    if (!counts.total) { el(wrap, "span", "Milestones not defined", "rd-progress-label"); return; }
    const summary = el(wrap, "div", null, "rd-progress-summary");
    figure(el(summary, "strong", counts.completed + " / " + counts.total, "rd-progress-count"), "count:" + project.id);
    el(summary, "span", "milestones complete" + (family ? " · incl. " + plural(family, "sub-project") : ""), "rd-progress-label");
    const bar = el(wrap, "progress", null, "rd-progress rd-milestone-bar"); bar.max = counts.total; bar.value = counts.completed;
    bar.setAttribute("aria-label", project.title + " milestones completed");
  }
  function sourceSummary(parent: Element, project: Project) {
    const meta = el(parent, "div", null, "rd-source-summary"), issues = project.sources.filter(source => source.health.state === "error" || source.health.state === "stale");
    /* A project with no sources is a deliberate umbrella, not a broken connection. */
    if (project.catalog && !project.sources.length) { el(meta, "span", "No sources"); return; }
    el(meta, "span", project.sources.length + " " + (project.sources.length === 1 ? "source" : "sources"));
    if (issues.length) badge(meta, issues.some(source => source.health.state === "error") ? "error" : "stale", issues.length + " source " + (issues.length === 1 ? "issue" : "issues"));
    else if (project.sources.length) badge(meta, project.sources.every(source => source.health.state === "fresh") ? "fresh" : "unconfigured", project.sources.every(source => source.health.state === "fresh") ? "Fresh" : "Awaiting data");
    else badge(meta, "unconfigured", "Not connected");
    const running = project.sources.filter(source => runtime(source).status === "running").length;
    if (running) el(meta, "span", running + " running");
  }
  const header = el(module, "header", null, mode === "overview" ? "rd-overall-header" : mode === "project-overview" || mode === "paper" ? "rd-hero-header" : mode === "mini" ? "rd-mini-header" : "rd-module-header");
  /* The circle widget (orb) has no header: the ring is the whole view. */
  if (mode === "orb") header.hidden = true;
  const headingCopy = el(header, "div", null, "rd-header-copy");
  /* A sub-project's page names its parent above the title; the h1 itself stays untouched (the page transition morphs it). */
  const crumb = mode === "project-overview" || mode === "paper" ? el(headingCopy, "nav", null, "rd-project-breadcrumb") : null;
  if (crumb) { crumb.hidden = true; crumb.setAttribute("aria-label", "Parent project"); }
  const big = mode === "overview" || mode === "project-overview" || mode === "paper";
  const heading = el(headingCopy, big ? "h1" : "h2", titles[mode] || "Project details", big ? "rd-page-title" : mode === "mini" ? "rd-mini-title" : "rd-section-title");
  const description = el(headingCopy, "p", null, "rd-description");
  const headerActions = mode === "overview" || mode === "project-overview" || mode === "paper" || mode === "mini" ? el(header, "div", null, "rd-header-actions") : null;
  const toolbar = mode === "projects" ? el(module, "div", null, "rd-toolbar") : null;
  let search = "", selectedStatus = "all";
  if (toolbar) {
    const searchInput = el(toolbar, "input");
    searchInput.type = "search"; searchInput.placeholder = "Search projects";
    searchInput.setAttribute("aria-label", "Search projects, software or next steps");
    const statusSelect = el(toolbar, "select"); statusSelect.setAttribute("aria-label", "Filter project status");
    for (const [value, label] of [["all", "All statuses"], ...Object.entries(projectHealthNames)]) { const option = el(statusSelect, "option", label); option.value = value; }
    searchInput.addEventListener("input", () => { search = searchInput.value.trim().toLowerCase(); paint(); });
    statusSelect.addEventListener("change", () => { const value = statusSelect.value; transition(() => { selectedStatus = value; paint(); }, listScene); });
  }
  /* Segmented type filter (filled per paint: it only offers the types present, with their counts). */
  const typeFilter = mode === "projects" ? el(module, "div", null, "rd-type-filter") : null;
  if (typeFilter) { typeFilter.hidden = true; typeFilter.setAttribute("role", "group"); typeFilter.setAttribute("aria-label", "Project type"); }
  const content = el(module, "div", null, "rd-content");
  let model: Model = { projects: [], errors: [] };
  function cardHeader(parent: Element, title: string, hintText?: string) {
    const top = el(parent, "div", null, "rd-card-header");
    el(top, "h3", title, "rd-card-title");
    if (hintText) el(top, "span", hintText, "rd-card-hint");
    return top;
  }
  function projectProgressChart(parent: Element, projects: Project[]) {
    const card = el(parent, "section", null, "rd-card rd-capsule-card");
    cardHeader(card, "Milestone progress", "By project");
    /* One column per top-level project; a parent's column carries its sub-projects' milestones too. */
    const columns = projects.filter(project => !project.parent);
    if (!columns.length) { empty(card, "No projects yet", "Connect a project to follow its milestones."); return; }
    const chart = el(card, "div", null, "rd-capsule-chart");
    for (const project of columns) {
      const counts = familyCount(project), family = project.children?.length || 0, column = link(chart, project.dashboard, null, "rd-capsule");
      const track = capsule(column, counts.completed, counts.total, project.title, { key: "capsule:" + project.id });
      const summary = project.title + ": " + (counts.total ? counts.completed + " of " + counts.total + " milestones complete" : "no milestones defined") + (family ? ", including " + plural(family, "sub-project") : "");
      column.setAttribute("aria-label", summary);
      hint(column, project.title, countText(counts, project), track);
    }
  }
  /* Completion is averaged by project, not pooled: each top-level project (with its sub-projects) counts once,
   * so a 72-milestone thesis does not drown a 9-milestone paper. */
  function milestoneDonut(parent: Element, projects: Project[]) {
    const card = el(parent, "section", null, "rd-card rd-donut-card");
    cardHeader(card, "Milestones", "Average by project");
    const rated = projects.filter(project => !project.parent).map(project => ({ project, counts: familyCount(project), share: 0 })).filter(item => item.counts.total);
    if (!rated.length) { empty(card, "No milestones defined", "Add deliverables in a project to see overall progress."); return; }
    for (const item of rated) item.share = item.counts.completed / item.counts.total;
    const share = rated.reduce((sum, item) => sum + item.share, 0) / rated.length, percent = Math.round(share * 100);
    const overdue = projects.flatMap(project => milestones(project)).filter(task => !task.completed && dayPassed(task.due)).length;
    const body = el(card, "div", null, "rd-donut-body"), ringBox = el(body, "div", null, "rd-donut");
    ringBox.setAttribute("role", "img"); ringBox.setAttribute("aria-label", percent + "% average completion across " + plural(rated.length, "project"));
    hint(ringBox, percent + "% average completion", rated.map(item => item.project.title + " " + Math.round(item.share * 100) + "%").join(" · "));
    const radius = 50, stroke = 11, circumference = 2 * Math.PI * radius;
    const svg = svgNode(ringBox, "svg", { viewBox: "0 0 120 120", "aria-hidden": "true" });
    svgNode(svg, "circle", { class: "rd-donut-track", cx: 60, cy: 60, r: radius, fill: "none", "stroke-width": stroke });
    const dash = Math.max(0.01, share * circumference), before = prior("donut:average", share > 0 && share < 1 ? [+dash.toFixed(2)] : null) as number[] | null | undefined;
    const attributes = { class: "rd-donut-segment rd-donut-done", cx: 60, cy: 60, r: radius, fill: "none", "stroke-width": stroke, transform: "rotate(-90 60 60)" };
    if (share >= 1) svgNode(svg, "circle", attributes);
    else if (share > 0) {
      const arc = svgNode(svg, "circle", { ...attributes, "stroke-linecap": "round", "stroke-dasharray": dash.toFixed(2) + " " + circumference.toFixed(2) });
      if (moved(before, [+dash.toFixed(2)])) tween(arc, { "--rd-from": (before?.[0] ?? 0.01) + " " + circumference.toFixed(2), "--rd-from-offset": "0" });
    }
    const center = el(ringBox, "div", null, "rd-donut-center");
    figure(el(center, "strong", percent + "%", "rd-donut-value"), "donut:value");
    el(center, "span", "avg of " + plural(rated.length, "project"), "rd-donut-label");
    if (overdue) el(body, "p", plural(overdue, "overdue milestone"), "rd-caption rd-donut-note");
  }
  function renderOverview(projects: Project[]) {
    const sources = projects.flatMap(project => project.sources);
    const counts = attentionCounts(attention(projects));
    description.textContent = plural(sources.filter(source => runtime(source).status === "running").length, "running source") + " · " + plural(counts.alerts, "alert") + (counts.suggestions ? " · " + plural(counts.suggestions, "suggestion") : "");
    clear(headerActions); renderActions(headerActions!);
    const bento = el(content, "div", null, "rd-overview-bento");
    projectProgressChart(bento, projects);
    const strip = el(bento, "div", null, "rd-kpi-strip");
    for (const [label, count, state] of [["Active", projects.filter(project => projectStatus(project) === "active").length, "active"], ["Blocked", projects.filter(project => projectStatus(project) === "blocked").length, "blocked"], ["Completed", projects.filter(project => projectStatus(project) === "completed").length, "completed"], ["Total projects", projects.length, "total"]] as [string, number, string][]) {
      const item = el(strip, "div", null, "rd-kpi"); item.dataset.state = state;
      figure(el(item, "strong", count, "rd-kpi-value"), "kpi:" + state); el(item, "span", label, "rd-kpi-label");
    }
    milestoneDonut(bento, projects);
  }
  const alertIcons: Record<string, string> = { blocked: "blocked", failed: "failed", error: "failed", stale: "stale", warning: "warning", suggestion: "project" };
  function renderAttention(projects: Project[]) {
    const items = attention(projects), counts = attentionCounts(items);
    const affected = new Set(items.filter(item => item.state !== "suggestion").map(item => item.project.id)).size;
    description.textContent = plural(counts.alerts, "alert") + " · " + (counts.suggestions ? plural(counts.suggestions, "suggestion") : affected + " " + (affected === 1 ? "project" : "projects") + " affected");
    if (!items.length && !model.errors.length) { empty(content, "No current alerts", projects.length ? "Sources and priorities are checked independently." : "Alerts appear after a project is connected."); return; }
    const list = el(content, "ul", null, "rd-list rd-alert-list");
    /* Suggestions for one parent stay together; more than two fold into one row. */
    const groups = new Map<string, Alert[]>();
    for (const item of items) if (item.state === "suggestion") groups.set(item.project.id, [...(groups.get(item.project.id) || []), item]);
    for (const item of items) {
      if (item.state === "suggestion") {
        const group = groups.get(item.project.id)!;
        if (group.length <= 2) renderSuggestion(list, item);
        else if (group[0] === item) renderSuggestionGroup(list, item.project, group);
        continue;
      }
      const li = el(list, "li", null, "rd-alert"); li.dataset.state = item.state;
      if (prior("alert:" + [item.project.id, item.state, item.label, item.message].join("|"), 1) === null) updated(li);
      icon(li, alertIcons[item.state] || "warning", "rd-alert-icon");
      const body = el(li, "div", null, "rd-alert-body");
      if (item.project.parent) el(body, "span", item.project.parent.title + " › ", "rd-alert-parent");
      link(body, item.project.dashboard, item.project.title, "rd-alert-title");
      el(body, "p", item.message, "rd-caption");
      badge(li, item.state, item.label);
    }
    for (const issue of model.errors) {
      const li = el(list, "li", null, "rd-alert"); li.dataset.state = "error";
      icon(li, "failed", "rd-alert-icon");
      const body = el(li, "div", null, "rd-alert-body");
      el(body, "p", issue.message, "rd-caption");
      if (issue.path) { const details = disclosure(body, "issue:" + issue.path + ":" + issue.message, "Details", "rd-source-details"); link(details, issue.path, "Inspect configuration"); }
      badge(li, "error", "Configuration issue");
    }
  }
  function kindLabel(project: Project) {
    if (project.sources?.length) return [...new Set(project.sources.map(source => source.display ? source.display.label : kindNames[source.kind]).filter(Boolean))].join(" · ");
    return project.kinds.filter(kind => kindNames[kind]).map(kind => kindNames[kind]).join(" · ");
  }
  /* One project row. options: child, last, context, toggle, stem, compact (see the reference view). */
  function projectRow(parent: Element, project: Project, options: Dict = {}) {
    const family = project.children?.length || 0, counts = familyCount(project);
    const card = el(parent, "article", null, ["rd-project-row", family ? "is-parent" : "", options.child ? "is-child" : "", options.last ? "is-last" : "", options.context ? "is-context" : ""].filter(Boolean).join(" "));
    card.dataset.projectId = project.id;
    if (options.child) el(card, "span", null, "rd-family-connector").setAttribute("aria-hidden", "true");
    if (options.stem) el(card, "span", null, "rd-family-stem").setAttribute("aria-hidden", "true");
    hint(ring(card, counts.completed, counts.total, "ring:" + project.id), project.title, countText(counts, project));
    const rowState = [counts.completed, counts.total, projectStatus(project), text(project.page?.next_step, "")].join("|");
    if (moved(prior("row:" + project.id, rowState), rowState)) updated(card);
    const main = el(card, "div", null, "rd-row-main"), meta = el(main, "div", null, "rd-row-heading");
    link(el(meta, "h3", null, "rd-project-title"), project.dashboard, project.title);
    el(main, "p", text(project.page?.next_step, "Define the first deliverable"), "rd-row-next-action");
    const secondary = el(main, "div", null, "rd-row-meta");
    el(secondary, "span", typeNames[project.type] || typeNames.other, "rd-row-type");
    const kinds = kindLabel(project); if (kinds) el(secondary, "span", kinds);
    /* The sub-project count doubles as the family's disclosure, so the chevron sits with what it folds. */
    const toggle = family ? (options.toggle ? el(secondary, "button", plural(family, "sub-project"), "rd-family-toggle") : el(secondary, "span", plural(family, "sub-project"), "rd-row-family")) : null;
    if (toggle && options.toggle) (toggle as HTMLButtonElement).type = "button";
    if (project.page?.due) el(secondary, "span", "Due " + dateText(project.page.due));
    sourceSummary(secondary, project);
    const progressArea = el(card, "div", null, "rd-row-progress"); milestoneProgress(progressArea, project);
    badge(el(card, "div", null, "rd-row-status"), projectStatus(project), projectHealthNames[projectStatus(project)] || "Not set");
    if (!options.compact) { const actions = el(card, "div", null, "rd-row-actions"); projectMenu(actions, project); }
    return { card, toggle: options.toggle ? toggle as HTMLButtonElement : null };
  }
  function setClass(node: Element, name: string, on: boolean) { node.classList.toggle(name, on); }
  /* The filter offers only the types present, with counts; one type alone needs no filter. */
  function renderTypeFilter(projects: Project[]) {
    if (!typeFilter) return;
    const scrolled = typeFilter.scrollLeft;
    clear(typeFilter);
    const counts = new Map<string, number>();
    for (const project of projects) counts.set(project.type, (counts.get(project.type) || 0) + 1);
    const present = typeOrder.filter(type => counts.has(type));
    if (present.length < 2 || !counts.has(projectUi.type)) projectUi.type = "all";
    typeFilter.hidden = present.length < 2;
    if (typeFilter.hidden) return;
    el(typeFilter, "span", null, "rd-type-thumb").setAttribute("aria-hidden", "true");
    for (const type of ["all", ...present]) {
      const selected = projectUi.type === type, tab = el(typeFilter, "button", null, "rd-type-tab" + (selected ? " is-selected" : ""));
      tab.type = "button"; tab.dataset.type = type; tab.setAttribute("aria-pressed", String(selected));
      el(tab, "span", type === "all" ? "All" : typeNames[type], "rd-type-label");
      if (type !== "all") el(tab, "span", counts.get(type), "rd-type-count");
      tab.addEventListener("click", () => { if (!record.disposed && projectUi.type !== type) transition(() => { projectUi.type = type; paint(); }, listScene); });
    }
    /* On a narrow pane the filter scrolls sideways: a rebuild keeps its offset and the selected segment in view. */
    const selected = all(typeFilter, ".rd-type-tab.is-selected")[0], width = typeFilter.clientWidth || 0;
    let left = scrolled;
    if (selected && width) left = Math.min(Math.max(left, selected.offsetLeft + selected.offsetWidth + 4 - width), Math.max(0, selected.offsetLeft - 4));
    typeFilter.scrollLeft = left;
  }
  function renderProjects(projects: Project[]) {
    description.textContent = plural(projects.length, "project");
    renderTypeFilter(projects);
    const filtering = projectUi.type !== "all" || selectedStatus !== "all" || Boolean(search);
    const haystack = (project: Project) => [project.title, text(project.page?.next_step, ""), project.id, typeNames[project.type], kindLabel(project), project.parent?.title || ""].join(" ").toLowerCase();
    const matches = (project: Project) => (projectUi.type === "all" || project.type === projectUi.type) && (selectedStatus === "all" || projectStatus(project) === selectedStatus) && (!search || haystack(project).includes(search));
    /* A family shows when its parent or any sub-project matches; a parent shown only for its sub-projects stays quiet. */
    const families: { project: Project; children: Project[]; context: boolean }[] = [];
    for (const project of projects) {
      if (project.parent) continue;
      const children = (project.children || []).filter(matches), own = matches(project);
      if (own || children.length) families.push({ project, children, context: !own });
    }
    if (!families.length) { empty(content, projects.length ? "No matching projects" : "No projects yet", projects.length ? "Adjust the filters." : "Use Connect project or New project above."); return; }
    const grid = el(content, "div", null, "rd-project-list");
    const dragging = !requestedProject && canGroup() && !status().busy;
    const rows: [HTMLElement, Project][] = [], row = (parent: Element, project: Project, options: Dict) => { const result = projectRow(parent, project, options); rows.push([result.card, project]); return result; };
    for (const { project, children, context } of families) {
      if (!project.children?.length) { row(grid, project, { context }); continue; }
      const group = el(grid, "div", null, "rd-project-family");
      /* Folding is for browsing; while filtering, every matching sub-project stays in view. */
      const foldable = !filtering && children.length > 0, open = children.length > 0 && (filtering || !projectUi.collapsed.has(project.id));
      const { toggle } = row(group, project, { context, toggle: foldable, stem: children.length > 0 });
      if (!children.length) continue;
      setClass(group, "is-open", open);
      const nest = el(group, "details", null, "rd-family-children"); nest.open = open; nest.id = "rd-family-" + record.uid + "-" + project.id;
      el(nest, "summary", plural(children.length, "sub-project"));
      children.forEach((child, index) => row(nest, child, { child: true, last: index === children.length - 1 }));
      if (!toggle) continue;
      const label = () => { toggle.setAttribute("aria-expanded", String(nest.open)); toggle.setAttribute("aria-label", (nest.open ? "Hide " : "Show ") + plural(children.length, "sub-project") + " of " + project.title); };
      toggle.setAttribute("aria-controls", nest.id); label();
      toggle.addEventListener("click", event => {
        event.preventDefault(); event.stopPropagation();
        if (record.disposed) return;
        nest.open = !nest.open;
        if (nest.open) projectUi.collapsed.delete(project.id); else projectUi.collapsed.add(project.id);
        setClass(group, "is-open", nest.open); label();
      });
    }
    if (dragging) {
      /* A slim drop zone at the end of the list appears only while a sub-project is lifted. */
      const zone = rows.some(([, project]) => canUngroup(project)) ? el(grid, "div", null, "rd-ungroup-zone") : null;
      if (zone) { zone.setAttribute("aria-hidden", "true"); el(zone, "span", null, "rd-ungroup-label"); }
      wireDropList(grid, zone);
      for (const [card, project] of rows) wireDragRow(card, project, zone);
    }
    if (actionMessage) el(content, "p", actionMessage, "rd-caption");
  }
  /* Sub-projects on a parent's page: the same rows as Overall, on the page, without row menus. */
  function renderSubProjects(parent: Element, project: Project) {
    if (project.parent) return;
    const children = project.children || [];
    if (!children.length) return;
    const section = el(parent, "section", null, "rd-subprojects"), top = el(section, "div", null, "rd-subprojects-heading");
    const copy = el(top, "div", null, "rd-subprojects-copy");
    el(copy, "h2", "Sub-projects", "rd-section-title");
    const counts = children.map(milestoneCount).reduce((sum, item) => ({ total: sum.total + item.total, completed: sum.completed + item.completed }), { total: 0, completed: 0 });
    el(copy, "span", plural(children.length, "project") + (counts.total ? " · " + counts.completed + " / " + counts.total + " milestones" : ""), "rd-subprojects-count");
    createButton(top, "Add sub-project", { parentId: project.id });
    const list = el(section, "div", null, "rd-project-list rd-subproject-list");
    for (const child of children) projectRow(list, child, { compact: true });
  }
  function paperSnapshot(source: Source | null | undefined): PaperDetail | null {
    const paper = values(source?.state?.details).find((detail: Dict) => detail?.kind === "paper" && detail.schemaVersion === 1) as PaperDetail | undefined;
    if (!paper || !Array.isArray(paper.chapters)) return null;
    const seen = new Set<string>(), chapters = paper.chapters.filter(chapter => {
      if (!chapter || typeof chapter.id !== "string" || !chapter.id || seen.has(chapter.id) || typeof chapter.title !== "string") return false;
      seen.add(chapter.id); return true;
    });
    return { ...paper, chapters };
  }
  interface Tree { byId: Map<string, Chapter>; children: Map<string, Chapter[]>; roots: Chapter[]; descendants(id: string): Set<string> }
  function paperTree(paper: PaperDetail): Tree {
    const byId = new Map(paper.chapters.map(chapter => [chapter.id, chapter]));
    const children = new Map<string, Chapter[]>(), roots: Chapter[] = [];
    for (const chapter of paper.chapters) {
      let parent = chapter.parentId, cursor = parent;
      const visited = new Set([chapter.id]);
      while (cursor && byId.has(cursor) && !visited.has(cursor)) { visited.add(cursor); cursor = byId.get(cursor)!.parentId; }
      if (cursor && visited.has(cursor)) parent = null;
      if (!parent || !byId.has(parent)) roots.push(chapter);
      else { if (!children.has(parent)) children.set(parent, []); children.get(parent)!.push(chapter); }
    }
    const descendants = (id: string) => {
      const ids = new Set<string>(), visit = (current: string) => { if (ids.has(current)) return; ids.add(current); for (const child of children.get(current) || []) visit(child.id); };
      visit(id); return ids;
    };
    return { byId, children, roots, descendants };
  }
  function boundMilestones(project: Project, sourceId: string, chapterIds: Set<string>) {
    return milestones(project).filter(task => taskBinding(task as any, "paper") === sourceId && chapterIds.has(taskBinding(task as any, "chapter")));
  }
  function binding(task: Task, key: string) { return taskBinding(task as any, key); }
  function acceptedSummary(tasks: Task[]) { return { total: tasks.length, accepted: tasks.filter(task => task.completed === true).length }; }
  function signedWords(value: unknown, missing = "—") { const count = number(value); return count === null ? missing : (count > 0 ? "+" : "") + numericText(count); }
  function paperDateLabel(paper: PaperDetail, value: unknown, includeTimezone = true) {
    const observed = milliseconds(value);
    if (observed === null) return "—";
    try { return new Date(observed).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: paper.activity?.timezone || undefined }) + (includeTimezone ? " " + text(paper.activity?.timezone, "") : ""); }
    catch (_) { return dateText(value, true); }
  }
  function paperBaselineLabel(paper: PaperDetail, period: "today" | "week") {
    const observed = paper.activity?.[period], baseline = milliseconds(observed?.baselineAt);
    if (baseline === null || observed?.available === false) return "No baseline";
    return paperDateLabel(paper, observed?.baselineAt);
  }
  function trackedPeriod(paper: PaperDetail | null, period: "today" | "week") { const activity = paper?.activity?.[period]; return activity?.coverage === "partial" || activity?.baselineKind === "first-observation"; }
  function periodLabel(paper: PaperDetail | null, period: "today" | "week") { const label = period === "today" ? "Today" : "Last 7 days"; return label + (trackedPeriod(paper, period) ? " (tracked)" : ""); }
  function trackingRange(paper: PaperDetail) {
    const periods = (["today", "week"] as const).filter(period => trackedPeriod(paper, period) && milliseconds(paper.activity?.[period]?.baselineAt) !== null);
    if (!periods.length) return "";
    if (periods.length === 2 && paper.activity!.today!.baselineAt === paper.activity!.week!.baselineAt) return "Tracking since " + paperDateLabel(paper, paper.activity!.today!.baselineAt, false);
    return periods.map(period => (period === "today" ? "Today" : "Last 7 days") + " since " + paperDateLabel(paper, paper.activity![period]!.baselineAt, false)).join(" · ");
  }
  function paperAction(parent: Element, label: string, method: keyof ActionBridge, args: unknown[], className = "rd-control") {
    const button = el(parent, "button", label, className); button.type = "button";
    button.disabled = typeof (actionBridge() as any)[method] !== "function";
    button.addEventListener("click", async event => {
      event.preventDefault(); event.stopPropagation();
      if (record.disposed || button.disabled) return;
      button.disabled = true; actionMessage = "";
      try { await (actionBridge() as any)[method](...args); }
      catch (error) { actionMessage = String((error as Error).message || error); }
      if (!record.disposed) paint();
    });
    return button;
  }
  function selectChapter(id: string | null) {
    /* Returning to the overview morphs the focused branch root back into its stepper slot. */
    const morph = id ?? all(content, ".rd-paper-map.is-focused .rd-paper-node")[0]?.dataset.chapterId ?? null;
    /* Between two sections the workbench travels like a scroll: forward in document order moves up, back moves down. */
    const order: string[] = record.chapterOrder || [], from = order.indexOf(paperUi.chapterId || ""), to = order.indexOf(id || "");
    const cue = from >= 0 && to >= 0 && from !== to ? (to > from ? "rd-vt-next" : "rd-vt-prev") : null;
    transition(() => { paperUi.chapterId = id; paperUi.stage = "all"; paint(); }, chapterScene(morph), cue);
  }
  function wireProgressChapter(row: HTMLButtonElement, project: Project, source: Source, chapter: Chapter) {
    const canFocus = () => typeof actionBridge().focusChapter === "function";
    row.disabled = !canFocus();
    row.addEventListener("click", async event => {
      event.preventDefault(); event.stopPropagation();
      if (record.disposed || row.disabled || !canFocus()) return;
      row.disabled = true; actionMessage = "";
      try { await actionBridge().focusChapter!(project.id, source.id, chapter.id); }
      catch (error) { actionMessage = String((error as Error).message || error); }
      if (!record.disposed) paint();
    });
  }
  function paperSourceSelector(parent: Element, sources: Source[], project: Project, source: Source) {
    if (sources.length === 1) return;
    const selector = el(parent, "select", null, "rd-control rd-paper-source-select"); selector.setAttribute("aria-label", mode === "paper-progress" ? "Progress paper source" : "Paper source");
    for (const item of sources) { const option = el(selector, "option", item.title); option.value = item.id; }
    selector.value = source.id;
    selector.addEventListener("change", async () => {
      const selectedId = selector.value;
      if (record.disposed || !sources.some(item => item.id === selectedId)) return;
      const previousId = paperUi.sourceId;
      paperUi.sourceId = selectedId; paperUi.chapterId = null; paperUi.stage = "all"; actionMessage = "";
      if (typeof actionBridge().focusChapter === "function") {
        selector.disabled = true;
        try { await actionBridge().focusChapter!(project.id, selectedId, null); }
        catch (error) { paperUi.sourceId = previousId; actionMessage = String((error as Error).message || error); }
      }
      if (!record.disposed) transition(() => paint(), sourceScene);
    });
  }
  function paperNode(parent: Element, chapter: Chapter, project: Project, source: Source, tree: Tree, compact = false, collapsed = false) {
    const button = el(parent, "button", null, "rd-paper-node" + (compact ? " is-small" : "")); button.type = "button";
    button.dataset.chapterId = chapter.id; button.setAttribute("aria-label", "Focus " + chapter.title);
    button.setAttribute("aria-pressed", String(paperUi.chapterId === chapter.id));
    if (paperUi.chapterId === chapter.id) el(button, "span", null, "rd-paper-node-marker").setAttribute("aria-hidden", "true");
    if (record.focusChapter === chapter.id) { record.focusChapter = null; button.focus({ preventScroll: true }); }
    el(button, "span", chapter.title, "rd-paper-node-title");
    const childCount = tree.descendants(chapter.id).size - 1;
    if (collapsed && childCount) el(button, "span", childCount + " " + (childCount === 1 ? "section" : "sections"), "rd-paper-section-count rd-paper-node-meta");
    const tasks = boundMilestones(project, source.id, tree.descendants(chapter.id));
    if (tasks.some(task => task.completed && binding(task, "accepted_hash") && tree.byId.get(binding(task, "chapter"))?.sourceHash !== binding(task, "accepted_hash"))) {
      const marker = el(button, "span", "•", "rd-paper-change-marker"); marker.title = "Changed since acceptance"; marker.setAttribute("aria-label", "Changed since acceptance");
      button.title = "Changed since acceptance"; button.setAttribute("aria-label", "Focus " + chapter.title + "; changed since acceptance");
    }
    button.addEventListener("click", () => selectChapter(chapter.id));
    return button;
  }
  function renderPaperBranch(parent: Element, chapter: Chapter, project: Project, source: Source, tree: Tree, depth = 0) {
    const branch = el(parent, "div", null, "rd-paper-branch");
    paperNode(branch, chapter, project, source, tree, depth > 0);
    const children = tree.children.get(chapter.id) || [];
    if (children.length) {
      const branches = el(branch, "div", null, "rd-paper-children");
      for (const child of children) renderPaperBranch(branches, child, project, source, tree, depth + 1);
    }
  }
  function paperMap(parent: Element, paper: PaperDetail, project: Project, source: Source, tree: Tree, selected: Chapter | undefined) {
    const section = el(parent, "section", null, "rd-paper-map-section"), top = el(section, "div", null, "rd-paper-section-heading");
    el(top, "h2", selected ? "Chapter structure" : "Structure");
    if (typeof actionBridge().openSource === "function" && source.directory) paperAction(top, selected ? "Open file" : "Open main file", "openSource", [source.id, selected?.id || null]);
    if (!paper.chapters.length) { empty(section, "No sections found", "Save a chapter or section heading in the connected main document."); return; }
    const viewport = el(section, "div", null, "rd-paper-map-viewport");
    viewport.setAttribute("aria-label", selected ? selected.title + " structure" : "Paper structure");
    const map = el(viewport, "div", null, "rd-paper-map" + (selected ? " is-focused" : ""));
    if (selected) {
      const branchRoot = tree.roots.find(chapter => tree.descendants(chapter.id).has(selected.id)) || selected;
      renderPaperBranch(map, branchRoot, project, source, tree);
      /* ↑ / ↓ step through every section in document order, crossing into neighbouring chapters. */
      viewport.addEventListener("keydown", event => {
        const step = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
        if (!step || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || record.disposed) return;
        const order = paper.chapters.map(chapter => chapter.id), next = order[order.indexOf(paperUi.chapterId || "") + step];
        if (!next) return;
        event.preventDefault(); record.focusChapter = next; selectChapter(next);
      });
    } else {
      const anchor = el(map, "div", null, "rd-paper-root");
      el(anchor, "strong", paper.title || source.title);
      const branches = el(map, "div", null, "rd-paper-root-branches");
      for (const chapter of tree.roots) {
        const branch = el(branches, "div", null, "rd-paper-branch");
        paperNode(branch, chapter, project, source, tree, false, true);
      }
    }
  }
  function chapterProgressChart(parent: Element, project: Project, source: Source, tree: Tree) {
    const section = el(parent, "section", null, "rd-paper-chart"), headingNode = el(section, "div", null, "rd-paper-section-heading");
    el(headingNode, "h3", "Chapter progress");
    el(section, "p", "Accepted / defined milestones", "rd-caption");
    if (!tree.roots.some(chapter => boundMilestones(project, source.id, tree.descendants(chapter.id)).length)) {
      empty(section, "Add chapter milestones", "Choose a chapter and an acceptance criterion.");
      addMilestoneButton(section, project, { sourceId: source.id }); return;
    }
    const rows = el(section, "div", null, "rd-paper-progress-rows");
    for (const chapter of tree.roots) {
      const counts = acceptedSummary(boundMilestones(project, source.id, tree.descendants(chapter.id))), row = el(rows, "button", null, "rd-paper-chart-row"); row.type = "button"; row.dataset.chapterId = chapter.id;
      const key = "progress:" + source.id + ":" + chapter.id, before = prior(key, [counts.accepted, counts.total]) as number[] | null | undefined;
      el(row, "span", chapter.title, "rd-paper-chart-label");
      const track = el(row, "span", null, "rd-paper-progress-track" + (!counts.total ? " is-unknown" : ""));
      if (counts.total) {
        const fill = el(track, "span", null, "rd-paper-progress-fill"); fill.style.width = (counts.accepted / counts.total * 100) + "%";
        if (moved(before, [counts.accepted, counts.total])) tween(fill, { "--rd-from": (before?.[1] ? before[0] / before[1] * 100 : 0) + "%" });
      }
      figure(el(row, "span", counts.total ? Math.round(counts.accepted / counts.total * 100) + "%" : "—", "rd-paper-chart-value"), key + ":value");
      if (before != null && moved(before, [counts.accepted, counts.total])) updated(row);
      row.setAttribute("aria-label", chapter.title + ": " + (counts.total ? counts.accepted + " of " + counts.total + " milestones accepted" : "no milestones defined"));
      hint(row, chapter.title, counts.total ? counts.accepted + " / " + counts.total + " milestones accepted" : "No milestones defined", track);
      wireProgressChapter(row, project, source, chapter);
    }
  }
  function chapterChangeChart(parent: Element, paper: PaperDetail, tree: Tree, project: Project, source: Source) {
    const section = el(parent, "section", null, "rd-paper-chart"), headingNode = el(section, "div", null, "rd-paper-section-heading");
    el(headingNode, "h3", "Writing activity");
    const legend = el(headingNode, "div", null, "rd-paper-change-legend");
    el(legend, "span", periodLabel(paper, "today"), "rd-paper-today").title = "Observed change since " + paperBaselineLabel(paper, "today");
    el(legend, "span", periodLabel(paper, "week"), "rd-paper-week").title = "Observed change since " + paperBaselineLabel(paper, "week");
    el(section, "p", "Net body words", "rd-caption");
    const observations = tree.roots.flatMap(chapter => [number(chapter.deltaToday), number(chapter.deltaWeek)]).filter((value): value is number => value !== null);
    if (!observations.length) { empty(section, "No comparison history yet", "Keep live updates on to track saved changes."); return; }
    const low = Math.min(0, ...observations), high = Math.max(0, ...observations), span = Math.max(1, high - low), origin = -low / span * 100;
    const rows = el(section, "div", null, "rd-paper-change-rows"), originBefore = prior("writing:" + source.id + ":origin", origin) as number | null | undefined;
    const since = (period: "today" | "week") => { const at = paper.activity?.[period]; return milliseconds(at?.baselineAt) === null || at?.available === false ? "no baseline" : "since " + paperDateLabel(paper, at?.baselineAt, false); };
    const baselines = "Today " + since("today") + " · Last 7 days " + since("week");
    for (const chapter of tree.roots) {
      const row = el(rows, "button", null, "rd-paper-change-row"); row.type = "button"; row.dataset.chapterId = chapter.id;
      const key = "writing:" + source.id + ":" + chapter.id;
      el(row, "span", chapter.title, "rd-paper-chart-label");
      const bars = el(row, "span", null, "rd-paper-change-bars");
      for (const [fieldName, className] of [["deltaToday", "rd-paper-today"], ["deltaWeek", "rd-paper-week"]] as const) {
        const value = number(chapter[fieldName]), track = el(bars, "span", null, "rd-paper-change-track");
        const zero = el(track, "span", null, "rd-paper-change-zero"); zero.style.left = origin + "%";
        if (moved(originBefore ?? undefined, origin)) tween(zero, { "--rd-from": originBefore + "%" });
        const geometry = value === null ? null : [(value < 0 ? (value - low) / span * 100 : origin), Math.abs(value) / span * 100], before = prior(key + ":" + fieldName, geometry) as number[] | null | undefined;
        if (geometry) {
          const fill = el(track, "span", null, "rd-paper-change-fill " + className); fill.style.left = geometry[0] + "%"; fill.style.width = geometry[1] + "%";
          if (moved(before, geometry)) tween(fill, { "--rd-from-left": (before?.[0] ?? origin) + "%", "--rd-from-width": (before?.[1] ?? 0) + "%" });
        }
      }
      const labels = el(row, "span", null, "rd-paper-change-values");
      figure(el(labels, "span", signedWords(chapter.deltaToday), "rd-paper-today"), key + ":today", row); figure(el(labels, "span", signedWords(chapter.deltaWeek), "rd-paper-week"), key + ":week", row);
      row.setAttribute("aria-label", chapter.title + ": " + periodLabel(paper, "today") + " " + signedWords(chapter.deltaToday, "No comparable snapshot") + ", " + periodLabel(paper, "week") + " " + signedWords(chapter.deltaWeek, "No comparable snapshot") + " words");
      hint(row, chapter.title, baselines, bars);
      wireProgressChapter(row, project, source, chapter);
    }
    const axis = el(section, "div", null, "rd-paper-change-axis"); figure(el(axis, "span", numericText(low)), "writing:" + source.id + ":axis-low"); figure(el(axis, "span", numericText(high)), "writing:" + source.id + ":axis-high");
    const range = trackingRange(paper); if (range) el(section, "p", range, "rd-caption rd-paper-tracking-range");
    if (tree.roots.some(chapter => number(chapter.deltaToday) === null || number(chapter.deltaWeek) === null)) el(section, "p", "— No comparable snapshot", "rd-caption");
  }
  /* "Overdue · 3 days", "Due today", "Due in 4 days" (within two weeks), else the date. */
  function dueLabel(task: Task): { text: string; overdue: boolean } {
    const at = milliseconds(task.due);
    if (at === null) return { text: "Due " + text(task.due, ""), overdue: false };
    const day = 24 * 3600 * 1000, start = new Date(); start.setHours(0, 0, 0, 0);
    const days = Math.round((new Date(at).setHours(0, 0, 0, 0) - start.getTime()) / day);
    if (!task.completed && days < 0) return { text: "Overdue · " + plural(-days, "day"), overdue: true };
    if (task.completed || days > 14) return { text: "Due " + dateText(task.due), overdue: false };
    return { text: days === 0 ? "Due today" : days === 1 ? "Due tomorrow" : "Due in " + days + " days", overdue: false };
  }
  /* Which milestones the widget's Next up (all projects) currently shows, for the "Next up" mark in the lists. */
  function nextReasons(): Map<string, string> {
    const projects = (record.model || model)?.projects || [];
    return new Map(nextMilestones(projects, 3).items.map(item => [item.project.id + ":" + item.task.line, item.reason]));
  }
  /* Checkable milestone rows. Without a paper source (any project), bindings are simply not resolved. */
  function renderPaperTasks(parent: Element, tasks: Task[], project: Project, source: Source | null, tree: Tree | null) {
    if (!tasks.length) { empty(parent, "No milestones here", "Add an outcome with a clear acceptance criterion."); return; }
    const list = el(parent, "div", null, "rd-paper-task-list");
    for (const task of tasks) {
      const row = el(list, "div", null, "rd-paper-task" + (task.completed ? " is-accepted" : "")), line = el(row, "div", null, "rd-paper-task-line"), label = el(line, "label", null, "rd-paper-task-label");
      const checkbox = el(label, "input"); checkbox.type = "checkbox"; checkbox.checked = Boolean(task.completed);
      checkbox.setAttribute("aria-label", "Complete “" + milestoneLabel(task) + "”");
      const chapterId = binding(task, "chapter"), boundSourceId = binding(task, "paper"), boundSource = boundSourceId && boundSourceId !== source?.id ? project.sources.find(item => item.id === boundSourceId) : source;
      const boundPaper = boundSource ? paperSnapshot(boundSource) : null, chapter = boundSource && boundSource === source ? tree?.byId.get(chapterId) : boundPaper?.chapters.find(item => item.id === chapterId), bridge = actionBridge();
      checkbox.disabled = typeof bridge.setMilestone !== "function" || Boolean(status().busy) || Boolean(boundSourceId && chapterId && !chapter);
      const title = el(line, "span", milestoneLabel(task), "rd-paper-task-text");
      if (dayPassed(task.due) && !task.completed) title.classList.add("is-overdue");
      taskEditing(line, title, project, task);
      const info = el(row, "div", null, "rd-paper-task-meta"), stage = binding(task, "stage");
      if (stage) el(info, "span", ({ draft: "Draft", figures: "Figures", references: "References", review: "Review", complete: "Complete" } as Record<string, string>)[stage] || stage);
      if (!source && task.heading) el(info, "span", task.heading);
      if (boundSourceId && chapterId && !chapter) el(info, "span", "Section unavailable · " + chapterId, "rd-paper-changed");
      if (task.due) { const due = dueLabel(task); el(info, "span", due.text, due.overdue ? "rd-due is-overdue" : "rd-due"); }
      const next = nextReasons().get(project.id + ":" + task.line);
      if (next) el(info, "span", next === "writing" ? "Next up · writing" : "Next up", "rd-next-chip");
      if (task.completed && chapter && binding(task, "accepted_hash") && binding(task, "accepted_hash") !== chapter.sourceHash) el(info, "span", "Changed since acceptance", "rd-paper-changed");
      checkbox.addEventListener("change", async () => {
        if (record.disposed || checkbox.disabled) return;
        checkbox.disabled = true; actionMessage = "";
        try { await actionBridge().setMilestone!(project.id, { line: task.line, text: String(task.text || ""), completed: checkbox.checked, sourceId: binding(task, "paper") || null, chapterId: chapterId || null, sourceHash: chapter?.sourceHash || null }); await refresh(true); }
        catch (error) { actionMessage = String((error as Error).message || error); if (!record.disposed) paint(); }
      });
    }
  }
  function paperWorkbench(parent: Element, selected: Chapter, project: Project, source: Source, tree: Tree) {
    const panel = el(parent, "section", null, "rd-paper-workbench"), top = el(panel, "div", null, "rd-paper-section-heading");
    el(top, "h2", selected.title);
    if (source.directory) paperAction(top, "Open source", "openSource", [source.id, selected.id]);
    const summary = el(panel, "div", null, "rd-paper-workbench-summary"), bench = "bench:" + source.id + ":" + selected.id;
    field(summary, "Body words", numericText(selected.bodyWords), bench + ":words");
    field(summary, periodLabel(paperSnapshot(source), "today"), signedWords(selected.deltaToday), bench + ":today"); field(summary, periodLabel(paperSnapshot(source), "week"), signedWords(selected.deltaWeek), bench + ":week");
    const tabs = el(panel, "div", null, "rd-paper-stage-tabs"); tabs.setAttribute("aria-label", "Milestone stage");
    el(tabs, "span", null, "rd-paper-stage-thumb").setAttribute("aria-hidden", "true");
    for (const [id, name] of [["all", "All"], ["draft", "Draft"], ["figures", "Figures"], ["references", "References"], ["review", "Review"], ["complete", "Complete"]]) {
      const tab = el(tabs, "button", name, "rd-paper-stage-tab" + (paperUi.stage === id ? " is-selected" : "")); tab.type = "button"; tab.dataset.stage = id; tab.setAttribute("aria-pressed", String(paperUi.stage === id));
      tab.addEventListener("click", () => { if (paperUi.stage !== id) transition(() => { paperUi.stage = id; paint(); }, stageScene); });
    }
    const tasks = boundMilestones(project, source.id, new Set([selected.id])).filter(task => paperUi.stage === "all" || binding(task, "stage") === paperUi.stage);
    renderPaperTasks(panel, tasks, project, source, tree);
    const actions = el(panel, "div", null, "rd-controls"); addMilestoneButton(actions, project, { sourceId: source.id, chapterId: selected.id, stage: paperUi.stage === "all" ? "draft" : paperUi.stage, chapterTitle: selected.title });
    if ((tree.children.get(selected.id) || []).length) el(panel, "p", "Milestones shown belong to this section. Select a child section for its own workbench.", "rd-caption");
    const evidence = disclosure(panel, "paper:" + source.id + ":chapter:" + selected.id + ":evidence", "Source evidence", "rd-source-details");
    el(evidence, "p", text(selected.file, "Source unavailable") + (number(selected.line) !== null ? " · Line " + selected.line : ""), "rd-caption");
    field(evidence, "Last source change", dateText(selected.modifiedAt, true));
    if (number(selected.ownBodyWords) !== null && selected.ownBodyWords !== selected.bodyWords) field(evidence, "Words excluding child sections", numericText(selected.ownBodyWords));
  }
  function renderPaperWorkspace(parent: Element, project: Project) {
    const sources = project.sources.filter(source => source.kind === "latex" && paperSnapshot(source));
    if (!sources.length) return false;
    const source = sources.find(item => item.id === paperUi.sourceId) || sources[0]; paperUi.sourceId = source.id;
    const paper = paperSnapshot(source)!, tree = paperTree(paper); const selected = tree.byId.get(paperUi.chapterId || "");
    if (!selected) { paperUi.chapterId = null; paperUi.stage = "all"; }
    record.chapterOrder = paper.chapters.map(chapter => chapter.id);
    root.closest?.(".rd-app-grid")?.classList.add("has-paper");
    const workspace = el(parent, "div", null, "rd-paper-workspace"), toolbarNode = el(workspace, "div", null, "rd-paper-toolbar");
    if (selected) { const back = el(toolbarNode, "button", "← Paper overview", "rd-control rd-paper-back"); back.type = "button"; back.addEventListener("click", () => selectChapter(null)); }
    paperSourceSelector(toolbarNode, sources, project, source);
    const state = status(), collection = headerActions ? el(headerActions, "div", null, "rd-paper-status") : toolbarNode;
    if (state.running) badge(collection, "active", "Monitoring");
    else if (typeof actionBridge().start === "function") { const start = paperAction(collection, "Start live updates", "start", []); start.disabled = Boolean(state.busy); }
    else badge(collection, "unconfigured", "Collector unavailable");
    if (source.health.state === "error" || source.health.isStale) badge(collection, source.health.state, source.health.state === "error" ? "Error" : "Stale");
    if (selected?.parentId && tree.byId.has(selected.parentId)) {
      const ancestors: Chapter[] = []; let cursor = tree.byId.get(selected.parentId); const visited = new Set([selected.id]);
      while (cursor && !visited.has(cursor.id)) { visited.add(cursor.id); ancestors.unshift(cursor); cursor = tree.byId.get(cursor.parentId || ""); }
      const breadcrumb = el(workspace, "nav", null, "rd-paper-breadcrumb"); breadcrumb.setAttribute("aria-label", "Chapter path");
      for (const ancestor of ancestors) { const button = el(breadcrumb, "button", ancestor.title, "rd-paper-breadcrumb-link"); button.type = "button"; button.addEventListener("click", () => selectChapter(ancestor.id)); el(breadcrumb, "span", "/"); }
      el(breadcrumb, "span", selected.title);
    }
    if (source.health.state === "error" || source.health.isStale) el(workspace, "p", "Retained measurements · " + text(source.readError || source.state?.collection?.error, "Check the collector and source connection."), "rd-paper-sync-warning");
    const body = el(workspace, "div", null, selected ? "rd-paper-focus-layout" : "rd-paper-overview-layout");
    paperMap(body, paper, project, source, tree, selected);
    if (selected) paperWorkbench(body, selected, project, source, tree);
    else {
      const unbound = milestones(project).filter(task => !binding(task, "chapter") || !binding(task, "paper") || binding(task, "paper") === source.id && !tree.byId.has(binding(task, "chapter")));
      const projectTasks = disclosure(workspace, "paper:" + source.id + ":project-milestones", "Project milestones · " + unbound.length, "rd-source-details rd-paper-project-milestones");
      renderPaperTasks(projectTasks, unbound, project, source, tree); addMilestoneButton(projectTasks, project);
    }
    if (project.milestoneReadError) el(workspace, "p", "Milestones could not be read: " + project.milestoneReadError, "rd-paper-sync-warning");
    return true;
  }
  function paperProgressDetails(parent: Element, source: Source, paper: PaperDetail) {
    const info = disclosure(parent, "paper:" + source.id + ":measurement-policy", "Details", "rd-source-details rd-paper-progress-details"), fields = el(info, "div", null, "rd-fields");
    field(fields, "Source", source.title);
    field(fields, "Last successful collection", paperDateLabel(paper, source.state?.collection?.lastSuccessfulAt || paper.observedAt));
    field(fields, "Today baseline", paperBaselineLabel(paper, "today")); field(fields, "Last 7 days baseline", paperBaselineLabel(paper, "week"));
    if (paper.activity?.trackingStartedAt) field(fields, "Tracking started", paperDateLabel(paper, paper.activity.trackingStartedAt));
    if (paper.countingMethod) field(fields, "Counting", typeof paper.countingMethod === "string" ? paper.countingMethod : JSON.stringify(paper.countingMethod));
    el(info, "p", "Chapter progress counts manually accepted milestones, including child sections. Words and successful compilation do not determine acceptance.", "rd-caption");
    el(info, "p", "Writing activity is net body-word change between saved observations. Top-level chapters include child sections. Last 7 days includes today; decreases may reflect editing. — means no comparable snapshot, while 0 is a measured zero.", "rd-caption");
    if ((["today", "week"] as const).some(period => trackedPeriod(paper, period))) el(info, "p", "Tracked periods begin at the first available observation in that window. They cover the displayed range, rather than the full day or week.", "rd-caption");
    if ((["today", "week"] as const).some(period => paper.activity?.[period]?.baselineKind === "boundary" || paper.activity?.[period]?.coverage === "full")) el(info, "p", "Full periods use the nearest saved observation at or before the boundary, within 2 hours. The actual baseline timestamps are shown above.", "rd-caption");
    for (const warning of values(paper.warnings)) el(info, "p", typeof warning === "string" ? warning : text(warning?.message || warning?.code), "rd-caption");
  }
  function renderPaperProgress(projects: Project[]) {
    const project = projects[0], sources = project?.sources.filter(source => source.kind === "latex" && paperSnapshot(source)) || [];
    const panel = root.closest?.(".rd-app-panel") as HTMLElement | null, available = Boolean(project && sources.length);
    root.hidden = !available; if (panel) panel.hidden = !available;
    if (!available) { description.textContent = ""; return; }
    const source = sources.find(item => item.id === paperUi.sourceId) || sources[0]; paperUi.sourceId = source.id;
    const paper = paperSnapshot(source)!, tree = paperTree(paper);
    description.textContent = "";
    const workspace = el(content, "div", null, "rd-paper-workspace rd-paper-progress-workspace"), toolbarNode = el(workspace, "div", null, "rd-paper-toolbar");
    paperSourceSelector(toolbarNode, sources, project, source);
    if (source.health.state === "error" || source.health.isStale) el(workspace, "p", "Retained measurements · " + text(source.readError || source.state?.collection?.error, "Check the collector and source connection."), "rd-paper-sync-warning");
    const charts = el(workspace, "div", null, "rd-paper-chart-grid");
    chapterProgressChart(charts, project, source, tree); chapterChangeChart(charts, paper, tree, project, source);
    if (!tree.roots.length) empty(charts, "No sections found", "Save a chapter or section heading in the connected main document.");
    paperProgressDetails(workspace, source, paper);
    if (project.milestoneReadError) el(workspace, "p", "Milestones could not be read: " + project.milestoneReadError, "rd-paper-sync-warning");
    if (actionMessage) el(workspace, "p", actionMessage, "rd-caption");
  }
  function renderProjectOverview(projects: Project[]) {
    const project = projects[0];
    clear(headerActions);
    if (crumb) { clear(crumb); crumb.hidden = !project?.parent; }
    if (!project) { empty(content, "Project unavailable", "Return to Overall to connect or select a project."); link(content, OVERALL_LINK, "Back to Overall", "rd-action"); return; }
    if (crumb && project.parent) { link(crumb, project.parent.dashboard, project.parent.title, "rd-crumb-link"); el(crumb, "span", "›", "rd-crumb-separator").setAttribute("aria-hidden", "true"); }
    heading.textContent = project.title; description.textContent = [typeNames[project.type] || typeNames.other, kindLabel(project)].filter(Boolean).join(" · ");
    link(headerActions!, OVERALL_LINK, "Back to Overall", "rd-back-link"); editButton(headerActions!, project); projectMenu(headerActions!, project);
    if (renderPaperWorkspace(content, project)) { renderSubProjects(content, project); if (actionMessage) el(content, "p", actionMessage, "rd-caption"); return; }
    root.closest?.(".rd-app-grid")?.classList.remove("has-paper");
    const hero = el(content, "section", null, "rd-project-hero"), action = el(hero, "div", null, "rd-hero-action");
    el(action, "span", "Next action", "rd-eyebrow"); el(action, "h2", text(project.page?.next_step, "Define the first deliverable and acceptance criteria"), "rd-next-action");
    const meta = el(hero, "div", null, "rd-hero-meta"); badge(meta, projectStatus(project), projectHealthNames[projectStatus(project)] || "Not set");
    el(meta, "span", stageNames[project.page?.stage] || text(project.page?.stage, "Planned"));
    if (project.page?.due) el(meta, "span", "Due " + dateText(project.page.due));
    const progressArea = el(hero, "div", null, "rd-hero-progress"); milestoneProgress(progressArea, project); sourceSummary(progressArea, project);
    renderSubProjects(content, project);
    if (actionMessage) el(content, "p", actionMessage, "rd-caption");
  }
  function renderMilestones(projects: Project[]) {
    const tasks = projects.flatMap(project => milestones(project));
    const completed = tasks.filter(task => task.completed).length;
    description.textContent = tasks.length ? completed + " / " + tasks.length + " complete · Click a checkbox to update" : "Deliverables and acceptance criteria";
    if (projects.length === 1) { const controls = el(content, "div", null, "rd-controls"); addMilestoneButton(controls, projects[0]); }
    if (!tasks.length) { empty(content, "No milestones defined", "Add a deliverable with a clear acceptance criterion."); return; }
    for (const project of projects) {
      const list = milestones(project);
      if (!list.length) continue;
      const wrap = el(content, "div", null, "rd-task-container");
      if (!requestedProject) link(el(wrap, "h3"), project.dashboard, project.title);
      const overdue = list.filter(task => !task.completed && dayPassed(task.due)).length;
      if (overdue) badge(wrap, "warning", overdue + " overdue");
      const source = project.sources.find(item => paperSnapshot(item)) || null, paper = paperSnapshot(source);
      renderPaperTasks(wrap, list, project, source, paper ? paperTree(paper) : null);
    }
    if (projects.some(project => project.milestoneReadError)) el(content, "p", "Milestones could not be read: " + projects.map(project => project.milestoneReadError).filter(Boolean).join(" · "), "rd-caption");
  }
  function sourceProvenance(parent: Element, source: Source) {
    const details = disclosure(parent, "source:" + source.id + ":provenance", "Data source & freshness", "rd-source-details");
    const collection: Dict = source.state?.collection || {}, fields = el(details, "div", null, "rd-fields");
    const run = runtime(source), counts = cases(source);
    if (source.kind !== "latex") { field(fields, "Current / target", numericText(run.currentValue) + " / " + numericText(run.targetValue) + " " + text(run.unit, "")); field(fields, "Step", text(run.step)); }
    if (source.kind === "isale" || number(counts.planned) !== null || number(counts.completed) !== null) { field(fields, "Cases complete / planned", numericText(counts.completed) + " / " + numericText(counts.planned)); field(fields, "Running / failed", numericText(counts.running) + " / " + numericText(counts.failed)); }
    field(fields, "Last successful collection", dateText(collection.lastSuccessfulAt, true));
    field(fields, "Last attempt", dateText(collection.attemptedAt, true));
    field(fields, "Source updated", dateText(collection.sourceModifiedAt, true));
    field(fields, "Collector host", text(collection.hostId || source.config?.collector?.hostId));
    field(fields, "Parser", text(source.state?.provenance?.parserVersion));
    if (source.readError || collection.error) el(details, "p", source.readError || text(collection.error), "rd-caption");
    const paths = values(source.state?.provenance?.sourceFiles);
    if (paths.length) { const list = el(details, "ul", null, "rd-resource-list"); for (const path of paths) { const value = typeof path === "string" ? path : path?.path; if (value) resourceLink(el(list, "li"), value, value); } }
    if (source.directory) resourceLink(details, source.directory, "Open source folder");
    if (typeof actionBridge().changeSourceFolder === "function") actionButton(details, "Change folder…", !status().busy, () => actionBridge().changeSourceFolder!(source.id), "rd-control rd-change-folder");
    link(details, "sources/" + source.id + ".json", "Source configuration", "rd-action");
    if (source.state) link(details, "data/" + source.id + ".json", "Snapshot JSON", "rd-action");
  }
  /* Declarative profiles report named values as details {kind: "metric", label, value, unit}; extraction warnings stay
   * folded away. A missing value shows as a dash, never as zero. */
  function sourceMetrics(card: Element, source: Source) {
    const details = values(source.state?.details);
    const metrics = details.filter((detail: Dict) => detail?.kind === "metric" && (detail.label || detail.id));
    if (metrics.length) {
      const row = el(card, "div", null, "rd-metrics");
      for (const metric of metrics.slice(0, 24)) {
        const item = el(row, "span", null, "rd-metric");
        const value = typeof metric.value === "string" ? metric.value : numericText(metric.value);
        el(item, "span", value + (metric.unit && value !== "—" ? " " + text(metric.unit, "") : ""), "rd-metric-value");
        el(item, "span", text(metric.label || metric.id), "rd-metric-label");
      }
    }
    const notes = details.filter((detail: Dict) => detail?.kind === "extraction").flatMap((detail: Dict) => values(detail.warnings)).map(item => String(item)).filter(Boolean);
    if (notes.length) {
      const box = disclosure(card, "source:" + source.id + ":extraction", "Extraction notes · " + notes.length, "rd-source-details rd-extraction-notes");
      const list = el(box, "ul", null, "rd-extraction-list");
      for (const note of notes.slice(0, 20)) el(list, "li", note, "rd-caption");
    }
  }
  /* A project page hides a panel that can never have content for it (e.g. Live monitor for a project without sources). */
  function concealPanel(hidden: boolean) { root.hidden = hidden; const panel = root.closest?.(".rd-app-panel") as HTMLElement | null; if (panel) panel.hidden = hidden; }
  function sourcelessPage(projects: Project[]) { return Boolean(requestedProject) && projects.length === 1 && !projects[0].sources.length; }
  function renderRuns(projects: Project[]) {
    concealPanel(sourcelessPage(projects)); if (root.hidden) { description.textContent = ""; return; }
    const sources = projects.flatMap(project => project.sources.map(source => ({ project, source })));
    description.textContent = sources.length + " connected " + (sources.length === 1 ? "source" : "sources") + " · Technical status";
    if (!sources.length) { empty(content, "No sources connected", "Connect a paper or simulation folder to this project."); return; }
    const list = el(content, "div", null, "rd-source-list");
    for (const { project, source } of sources) {
      const card = el(list, "article", null, "rd-source-card"), top = el(card, "div", null, "rd-source-heading");
      icon(top, source.display ? "custom" : source.kind);
      el(top, "h3", source.title); badge(top, runtime(source).status, runtimeNames[runtime(source).status] || "Unknown"); badge(top, source.health.state, healthNames[source.health.state]);
      if (!requestedProject) link(card, project.dashboard, project.title, "rd-action");
      if (source.health.isStale || source.readError || source.state?.collection?.health === "error") el(card, "p", "Showing retained measurements · Check data source details", "rd-caption");
      const run = runtime(source), counts = cases(source), summary = el(card, "div", null, "rd-run-summary");
      el(summary, "span", sourceKindLabel(source), "rd-source-kind");
      if (source.kind === "latex" && run.status === "completed") el(summary, "span", "Compilation succeeded");
      if (source.kind === "isale" || (source.display && (number(counts.planned) !== null || number(counts.completed) !== null))) el(summary, "span", numericText(counts.completed) + " / " + numericText(counts.planned) + " cases complete");
      progress(card, source);
      sourceMetrics(card, source);
      const details = values(source.state?.details).filter((detail: Dict) => detail.kind === "chapter" || detail.kind === "case");
      if (details.length) {
        const observations = disclosure(card, "source:" + source.id + ":observations", source.kind === "latex" ? "Chapter file checks · " + details.length : "Case records · " + details.length, "rd-source-details");
        const tbody = table(observations, ["Name", "Observation", "Source / time"]);
        for (const detail of details) {
          const tr = el(tbody, "tr"); el(tr, "td", text(detail.name || detail.id));
          const observation = el(tr, "td");
          if (detail.kind === "chapter") el(observation, "span", detail.exists === true ? "File exists" : detail.exists === false ? "File missing" : "Unknown");
          else badge(observation, detail.status || "unknown", runtimeNames[detail.status] || text(detail.status, "Unknown"));
          if (detail.reason) el(observation, "p", detail.reason, "rd-caption");
          const cell = el(tr, "td");
          if (detail.source) resourceLink(cell, detail.source, "Source file");
          else el(cell, "span", dateText(typeof detail.modifiedAt === "number" ? new Date(detail.modifiedAt * 1000).toISOString() : detail.modifiedAt, true));
        }
        el(observations, "p", "File presence and run completion do not measure research acceptance.", "rd-caption");
      }
      sourceProvenance(card, source);
    }
  }
  function renderResources(projects: Project[]) {
    concealPanel(sourcelessPage(projects) && !values(projects[0].page?.resources).length && !values(projects[0].page?.knowledge).length);
    if (root.hidden) { description.textContent = ""; return; }
    description.textContent = "Verified outputs and project links";
    const resources: Dict[] = [], seen = new Set<string>();
    const add = (project: Project, path: unknown, label: string, note: string) => { const key = project.id + "|" + path; if (!path || seen.has(key)) return null; seen.add(key); const item: Dict = { project, path, label, note }; resources.push(item); return item; };
    for (const project of projects) {
      for (const source of project.sources) {
        for (const artifact of values(source.state?.artifacts)) {
          if (milliseconds(artifact.verifiedAt) !== null) add(project, artifact.path, String(artifact.path || "").replace(/\\/g, "/").split("/").pop() || "Output file", text(artifact.type, "File") + " · Verified " + dateText(artifact.verifiedAt, true) + (source.health.state !== "fresh" ? " · " + healthNames[source.health.state] : ""));
        }
        const folder = source.directory ? add(project, source.directory, "Source folder · " + source.title, sourceKindLabel(source)) : null;
        if (folder) folder.editor = true;
      }
      for (const resource of values(project.page?.resources)) {
        if (typeof resource === "string" || resource?.path) add(project, typeof resource === "string" ? resource : resource.path, text(resource.label || resource.display || resource.path || resource), "Project resource");
      }
      for (const knowledge of values(project.page?.knowledge)) if (knowledge?.path || typeof knowledge === "string") add(project, knowledge.path || knowledge, text(knowledge), "Related knowledge");
    }
    if (!resources.length) { empty(content, "No results linked yet", "Verified outputs appear after collection. Project resources can include files, folders and web links."); return; }
    const list = el(content, "ul", null, "rd-resource-list");
    for (const resource of resources) {
      const li = el(list, "li", null, "rd-resource-item"); resourceLink(li, resource.path, resource.label);
      const editor = resource.editor ? editorTarget(resource.path) : null;
      if (editor) { const open = el(li, "a", "Open in VS Code", "rd-action rd-open-editor"); open.href = "#"; open.title = resource.path; open.addEventListener("click", event => { event.preventDefault(); ctx.openExternal(editor); }); }
      if (!requestedProject) link(li, resource.project.dashboard, resource.project.title, "rd-action");
      el(li, "p", resource.note, "rd-caption");
    }
  }
  function renderHealth(projects: Project[]) {
    description.textContent = "Collection status by source";
    const sources = projects.flatMap(project => project.sources);
    if (!sources.length) { empty(content, "No sources connected"); return; }
    const list = el(content, "div", null, "rd-source-list");
    for (const source of sources) { const card = el(list, "article", null, "rd-source-card"); el(card, "h3", source.title); badge(card, source.health.state, healthNames[source.health.state]); sourceProvenance(card, source); }
  }
  /* ---------- Mini: compact widgets for the floating window ----------
   * Nothing here navigates inside the widget: every action asks the app to act in the main window ({ inMain: true }). */
  const mainWindow = { inMain: true };
  async function miniAct(control: HTMLButtonElement, run: (bridge: ActionBridge) => Promise<unknown> | unknown) {
    if (record.disposed || control.disabled) return;
    control.disabled = true; actionMessage = "";
    try { await run(actionBridge()); }
    catch (error) { actionMessage = String((error as Error).message || error); }
    if (!record.disposed) paint();
  }
  function miniButton(parent: Element, className: string, label: string | null, method: keyof ActionBridge, run: (bridge: ActionBridge) => Promise<unknown> | unknown) {
    const button = el(parent, "button", null, className); button.type = "button";
    if (label) button.setAttribute("aria-label", label);
    button.disabled = typeof (actionBridge() as any)[method] !== "function";
    button.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); void miniAct(button, run); });
    return button;
  }
  function miniSection(parent: Element, title: string, className: string, aside: string | null = null) {
    const section = el(parent, "section", null, "rd-mini-section " + className), top = el(section, "div", null, "rd-mini-section-head");
    el(top, "h3", title, "rd-mini-section-title");
    if (aside) el(top, "span", aside, "rd-mini-section-aside");
    return section;
  }
  function shortDate(value: unknown) {
    const date = milliseconds(value);
    return date === null ? "—" : new Date(date).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  }
  /* Live updates (start/stop), keep-on-top for the widget, and the full dashboard. */
  function miniHeader(parent: Element) {
    const bridge = actionBridge(), state = status(), running = Boolean(state.running);
    const method = running ? "stop" : "start";
    const live = miniButton(parent, "rd-mini-live" + (running ? " is-running" : ""), running ? "Live updates running. Stop live updates" : "Live updates stopped. Start live updates", method, current => (current as any)[method]());
    el(live, "span", running ? "Live" : "Paused", "rd-mini-live-label");
    if (state.busy) live.disabled = true;
    hint(live, running ? "Live updates running" : "Live updates stopped", running ? "Click to stop" : "Click to start");
    if (typeof bridge.setKeepOnTop === "function" && state.canKeepOnTop) {
      const pinned = Boolean(state.keepOnTop);
      const pin = miniButton(parent, "rd-mini-icon" + (pinned ? " is-pressed" : ""), "Keep on top", "setKeepOnTop", current => current.setKeepOnTop!(!pinned));
      pin.setAttribute("aria-pressed", String(pinned)); icon(pin, "pin", "rd-mini-glyph");
      hint(pin, pinned ? "Kept on top" : "Keep on top", pinned ? "Click to let other windows cover it" : "Stay above other windows");
    }
    if (typeof bridge.connect === "function") {
      const connect = miniButton(parent, "rd-mini-icon", "Connect a folder", "connect", current => current.connect!());
      icon(connect, "connect", "rd-mini-glyph");
      hint(connect, "Connect a folder", "Opens the plan card in the dashboard, where an agent can design the monitoring. You can also drop a folder on the widget.");
    }
    const focusId = status().focusProjectId, focused = focusId ? model.projects.find(project => project.id === focusId) || null : null;
    const open = focused
      ? miniButton(parent, "rd-mini-icon", "Open " + focused.title + " in the dashboard", "openProject", current => current.openProject!(focused.id, mainWindow))
      : miniButton(parent, "rd-mini-icon", "Open dashboard", "openOverall", current => current.openOverall!(mainWindow));
    icon(open, "open", "rd-mini-glyph"); hint(open, focused ? "Open " + focused.title : "Open dashboard", "In the main window");
  }
  /* Quick add: one line under Next up (project, title, Enter). Repaints wait while it is open so typing is never lost. */
  /* Moving between All projects and a project (or a sub-project) inside the widget: rows go in, the ring and the back
   * arrow come out; the content pushes in from the side it came from. */
  async function miniFocus(id: string | null, direction: "forward" | "back") {
    if (record.disposed || typeof actionBridge().setFocus !== "function") return;
    record.miniAdd.open = false; record.hold = false; record.miniPush = direction;
    try { await actionBridge().setFocus!(id); }
    catch (error) { record.miniPush = null; actionMessage = String((error as Error).message || error); }
    if (!record.disposed) void refresh(true);
  }
  /* The title: "All projects" for all projects. Focused: a back line ("‹ PhD thesis" or "‹ All projects") beside the actions,
   * and the project's name on its own line below, across the whole width (up to two lines). */
  function miniFocusHeading(focus: Project | null) {
    for (const node of record.focusHeading || []) node.remove();
    record.focusHeading = [];
    header.classList.toggle("is-focused", Boolean(focus));
    heading.hidden = Boolean(focus);
    heading.textContent = titles.mini;
    if (!focus) return;
    const target = focus.parent || null;
    const back = el(headingCopy, "button", null, "rd-mini-back") as HTMLButtonElement;
    headingCopy.insertBefore(back, heading);
    back.type = "button";
    back.setAttribute("aria-label", target ? "Back to " + target.title : "Back to all projects");
    svgNode(svgNode(back, "svg", { viewBox: "0 0 20 20", "aria-hidden": "true", class: "rd-mini-back-glyph" }), "path", { d: "M12 4.5L6.5 10l5.5 5.5", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" });
    el(back, "span", target ? target.title : "All projects", "rd-mini-back-label");
    back.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); void miniFocus(target ? target.id : null, "back"); });
    const name = el(header, "h2", focus.title, "rd-mini-title rd-mini-focus-name");
    name.title = focus.title;
    record.focusHeading = [back, name];
  }
  function miniQuickAdd(section: Element, projects: Project[], preferred: Project | null = null) {
    const state = record.miniAdd, bridge = actionBridge();
    if (typeof bridge.quickMilestone !== "function" || !projects.length) return;
    const head = section.querySelector(".rd-mini-section-head")!;
    const toggle = el(head, "button", null, "rd-mini-icon rd-mini-add-toggle" + (state.open ? " is-pressed" : "")) as HTMLButtonElement;
    toggle.type = "button"; toggle.setAttribute("aria-label", state.open ? "Close quick add" : "Add a milestone"); toggle.setAttribute("aria-expanded", String(state.open));
    icon(toggle, "plus", "rd-mini-glyph");
    const close = () => { state.open = false; state.error = ""; record.hold = false; paint(); if (record.refreshQueued) { record.refreshQueued = false; void refresh(); } };
    toggle.addEventListener("click", event => {
      event.preventDefault(); event.stopPropagation();
      if (state.open) { close(); return; }
      state.open = true; state.error = "";
      if (preferred) state.projectId = preferred.id;
      else if (!projects.some(project => project.id === state.projectId)) state.projectId = (projects.find(project => !project.parent) || projects[0]).id;
      paint();
      (content.querySelector(".rd-mini-add input") as HTMLInputElement | null)?.focus();
    });
    if (!state.open) return;
    const form = el(section, "form", null, "rd-mini-add is-open") as HTMLFormElement;
    const select = el(form, "select", null, "rd-mini-add-project") as HTMLSelectElement;
    select.setAttribute("aria-label", "Project");
    for (const project of projects) {
      const option = el(select, "option", (project.parent ? project.parent.title + " › " : "") + project.title) as HTMLOptionElement;
      option.value = project.id;
    }
    select.value = state.projectId;
    const input = el(form, "input", null, "rd-mini-add-title") as HTMLInputElement;
    input.type = "text"; input.maxLength = 300; input.placeholder = "New milestone, then Enter"; input.value = state.text; input.setAttribute("aria-label", "Milestone");
    if (state.error) el(form, "p", state.error, "rd-mini-add-error");
    record.hold = true;
    select.addEventListener("change", () => { state.projectId = select.value; input.focus(); });
    input.addEventListener("input", () => { state.text = input.value; });
    form.addEventListener("keydown", event => { if ((event as KeyboardEvent).key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); } });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const title = input.value.trim();
      if (!title || input.disabled) return;
      input.disabled = true; select.disabled = true; state.error = "";
      try {
        await actionBridge().quickMilestone!(select.value, title);
        state.text = ""; state.open = false; record.hold = false;
        record.refreshQueued = false; await refresh(true);
      } catch (error) {
        state.error = String((error as Error).message || error);
        input.disabled = false; select.disabled = false; paint(); (content.querySelector(".rd-mini-add input") as HTMLInputElement | null)?.focus();
      }
    });
  }
  /* Same arc geometry and tween contract as the Overall donut, at widget size. */
  function miniRing(parent: Element, share: number | null) {
    const radius = 26, stroke = 6, circumference = 2 * Math.PI * radius;
    const svg = svgNode(parent, "svg", { viewBox: "0 0 64 64", "aria-hidden": "true" });
    svgNode(svg, "circle", { class: "rd-donut-track", cx: 32, cy: 32, r: radius, fill: "none", "stroke-width": stroke });
    if (share === null || share <= 0) { prior("mini:ring", null); return; }
    const attributes = { class: "rd-donut-segment rd-donut-done", cx: 32, cy: 32, r: radius, fill: "none", "stroke-width": stroke, transform: "rotate(-90 32 32)" };
    if (share >= 1) { prior("mini:ring", null); svgNode(svg, "circle", attributes); return; }
    const dash = Math.max(0.01, share * circumference), before = prior("mini:ring", [+dash.toFixed(2)]) as number[] | null | undefined;
    const arc = svgNode(svg, "circle", { ...attributes, "stroke-linecap": "round", "stroke-dasharray": dash.toFixed(2) + " " + circumference.toFixed(2) });
    if (moved(before, [+dash.toFixed(2)])) tween(arc, { "--rd-from": (before?.[0] ?? 0.01) + " " + circumference.toFixed(2), "--rd-from-offset": "0" });
  }
  /* Average completion by top-level project (as Overall), beside Active · Blocked · Alerts. */
  function miniSummary(parent: Element, projects: Project[], tops: Project[], alerts: number, focus: Project | null = null) {
    const section = el(parent, "section", null, "rd-mini-section rd-mini-summary");
    const rated = tops.map(project => ({ project, counts: familyCount(project) })).filter(item => item.counts.total);
    const share = rated.length ? rated.reduce((sum, item) => sum + item.counts.completed / item.counts.total, 0) / rated.length : null;
    const percent = share === null ? null : Math.round(share * 100), box = el(section, "div", null, "rd-mini-ring");
    box.setAttribute("role", "img"); box.setAttribute("aria-label", percent === null ? "No milestones defined" : percent + "% average completion across " + plural(rated.length, "project"));
    miniRing(box, share);
    figure(el(el(box, "div", null, "rd-mini-ring-center"), "strong", percent === null ? "—" : percent + "%", "rd-mini-ring-value"), "mini:average");
    hint(box, percent === null ? "No milestones defined" : percent + "% average completion", focus && typeof actionBridge().setFocus === "function" ? "Click for all projects" : rated.map(item => item.project.title + " " + Math.round(item.counts.completed / item.counts.total * 100) + "%").join(" · "));
    if (focus && typeof actionBridge().setFocus === "function") {
      box.classList.add("is-link"); box.setAttribute("role", "button"); box.tabIndex = 0;
      box.setAttribute("aria-label", (percent === null ? "No milestones defined" : percent + "% complete") + ". Show all projects");
      const toAll = (event: Event) => { event.preventDefault(); event.stopPropagation(); void miniFocus(null, "back"); };
      box.addEventListener("click", toAll);
      box.addEventListener("keydown", event => { if ((event as KeyboardEvent).key === "Enter" || (event as KeyboardEvent).key === " ") toAll(event); });
    }
    const side = el(section, "div", null, "rd-mini-summary-side");
    /* Focused: progress bars instead of the counts (sub-projects, or the paper's top-level chapters). */
    if (focus && miniProgressBars(side, focus)) {
      el(side, "p", countText(familyCount(focus), focus), "rd-mini-caption");
      return;
    }
    const stats = el(side, "div", null, "rd-mini-stats");
    const counts: [string, number, string][] = [["Active", projects.filter(project => projectStatus(project) === "active").length, "active"], ["Blocked", projects.filter(project => projectStatus(project) === "blocked").length, "blocked"], ["Alerts", alerts, "alerts"]];
    for (const [label, count, state] of counts) {
      const stat = el(stats, "div", null, "rd-mini-stat" + (count ? "" : " is-zero")); stat.dataset.state = state;
      figure(el(stat, "strong", count, "rd-mini-stat-value"), "mini:" + state, stat); el(stat, "span", label, "rd-mini-stat-label");
    }
    el(side, "p", focus ? countText(familyCount(focus), focus) : rated.length ? "Average completion · " + plural(rated.length, "project") : "No milestones defined yet", "rd-mini-caption");
  }
  /* Unlabelled vertical bars for a focused project: one per sub-project (milestone completion, family-wide), or, for a
   * paper without sub-projects, one per top-level chapter (accepted / defined chapter milestones, as Chapter progress
   * on the project page). Hovering names the bar; clicking opens the detail in the dashboard. False when there is
   * nothing to chart (the counts are shown instead). */
  type Bar = { key: string; title: string; done: number; total: number; open: (bridge: ActionBridge) => Promise<unknown> | unknown; method: keyof ActionBridge };
  function focusBars(focus: Project): Bar[] {
    let bars: Bar[] = [];
    if (focus.children?.length) {
      bars = focus.children.map(child => { const counts = familyCount(child); return { key: "sub:" + child.id, title: child.title, done: counts.completed, total: counts.total, method: "openProject" as const, open: (bridge: ActionBridge) => bridge.openProject!(child.id, mainWindow) }; });
    } else {
      const source = focus.sources.find(item => item.kind === "latex" && paperSnapshot(item));
      if (source) {
        const tree = paperTree(paperSnapshot(source)!);
        bars = tree.roots.map(chapter => {
          const counts = acceptedSummary(boundMilestones(focus, source.id, tree.descendants(chapter.id)));
          return { key: "chapter:" + source.id + ":" + chapter.id, title: chapter.title, done: counts.accepted, total: counts.total, method: "focusChapter" as const, open: (bridge: ActionBridge) => bridge.focusChapter!(focus.id, source.id, chapter.id, mainWindow) };
        });
      }
    }
    return bars;
  }
  function miniProgressBars(parent: Element, focus: Project): boolean {
    const bars = focusBars(focus);
    if (!bars.length) return false;
    const chart = el(parent, "div", null, "rd-mini-bars" + (bars.length > 12 ? " is-dense" : ""));
    chart.setAttribute("role", "list");
    for (const bar of bars) {
      const share = bar.total ? bar.done / bar.total : null, percent = share === null ? null : Math.round(share * 100);
      const button = miniButton(chart, "rd-mini-bar" + (share === null ? " is-empty" : share >= 1 ? " is-complete" : ""), bar.title + ": " + (percent === null ? "no milestones defined" : percent + "% · " + bar.done + " of " + bar.total + " milestones") + ". Open in the dashboard", bar.method, bar.open);
      button.setAttribute("role", "listitem");
      const fill = el(button, "span", null, "rd-mini-bar-fill");
      const key = "mini-bar:" + bar.key, before = prior(key, share) as number | null | undefined;
      fill.style.height = ((share ?? 0) * 100).toFixed(1) + "%";
      if (share !== null && before != null && Math.abs(before - share) > 0.001) tween(fill, { "--rd-from": (before * 100).toFixed(1) + "%" });
      hint(button, bar.title, percent === null ? "No milestones defined" : percent + "% · " + bar.done + " / " + bar.total + " milestones");
    }
    return true;
  }
  /* One row per top-level project; a parent carries its sub-projects' milestones, as on Overall. */
  function miniProjects(parent: Element, tops: Project[], title = "Projects") {
    const section = miniSection(parent, title, "rd-mini-projects", tops.length ? String(tops.length) : null);
    if (!tops.length) { el(section, "p", "No projects yet", "rd-mini-empty"); return; }
    const list = el(section, "div", null, "rd-mini-list");
    for (const project of tops) {
      const counts = familyCount(project), family = project.children?.length || 0, state = projectStatus(project);
      const enter = typeof actionBridge().setFocus === "function";
      const row = enter
        ? miniButton(list, "rd-mini-row rd-mini-project", project.title + ": " + countText(counts, project) + ". Show in the widget", "setFocus", () => miniFocus(project.id, "forward"))
        : miniButton(list, "rd-mini-row rd-mini-project", project.title + ": " + countText(counts, project) + ". Open in the main window", "openProject", current => current.openProject!(project.id, mainWindow));
      if (state === "blocked" || state === "completed") row.classList.add(state === "blocked" ? "is-blocked" : "is-complete");
      row.dataset.projectId = project.id;
      ring(row, counts.completed, counts.total, "mini-ring:" + project.id);
      const main = el(row, "span", null, "rd-mini-row-main");
      el(main, "span", project.title, "rd-mini-row-title");
      const meta = [state === "blocked" ? "Blocked" : null, family ? "incl. " + plural(family, "sub-project") : null].filter(Boolean);
      if (meta.length) el(main, "span", meta.join(" · "), "rd-mini-row-meta");
      figure(el(row, "span", counts.total ? Math.round(counts.completed / counts.total * 100) + "%" : "—", "rd-mini-row-value"), "mini-percent:" + project.id, row);
      hint(row, project.title, countText(counts, project));
    }
  }
  /* Net body words across paper sources: top-level chapters already include their sections. null means no comparison. */
  function deltaSum(list: (number | null)[]) { const known = list.filter((value): value is number => value !== null); return known.length ? known.reduce((sum, value) => sum + value, 0) : null; }
  function miniPapers(projects: Project[]) {
    return projects.flatMap(project => project.sources.filter(source => source.kind === "latex" && paperSnapshot(source)).map(source => {
      const paper = paperSnapshot(source)!, roots = paperTree(paper).roots;
      return { project, source, paper, today: deltaSum(roots.map(chapter => number(chapter.deltaToday))), week: deltaSum(roots.map(chapter => number(chapter.deltaWeek))) };
    }));
  }
  function miniWriting(parent: Element, projects: Project[]) {
    const papers = miniPapers(projects);
    if (!papers.length) return;
    const today = deltaSum(papers.map(item => item.today)), week = deltaSum(papers.map(item => item.week)), single = papers.length === 1 ? papers[0] : null;
    const tracked = papers.some(item => trackedPeriod(item.paper, "today"));
    const section = miniSection(parent, "Today", "rd-mini-writing", "Net body words");
    const lead = single
      ? miniButton(section, "rd-mini-writing-lead is-link", "Open " + single.source.title + " in the main window", "focusChapter", current => current.focusChapter!(single.project.id, single.source.id, null, mainWindow))
      : el(section, "div", null, "rd-mini-writing-lead");
    figure(el(lead, "strong", signedWords(today), "rd-mini-writing-value" + (today === null ? " is-unknown" : "")), "mini:today");
    el(lead, "span", tracked ? "words today (tracked)" : "words today", "rd-mini-writing-unit");
    const weekly = el(lead, "span", null, "rd-mini-writing-week");
    figure(el(weekly, "span", signedWords(week)), "mini:week"); el(weekly, "span", " · 7 days");
    hint(lead, single ? single.source.title : plural(papers.length, "paper"), today === null ? "No comparable snapshot yet" : "Saved changes since the start of today");
    if (single) return;
    const list = el(section, "div", null, "rd-mini-list rd-mini-paper-list");
    for (const item of papers) {
      const row = miniButton(list, "rd-mini-row rd-mini-paper", "Open " + item.source.title + " in the main window", "focusChapter", current => current.focusChapter!(item.project.id, item.source.id, null, mainWindow));
      row.dataset.sourceId = item.source.id;
      const main = el(row, "span", null, "rd-mini-row-main"); el(main, "span", item.source.title, "rd-mini-row-title");
      if (item.source.title !== item.project.title) el(main, "span", item.project.title, "rd-mini-row-meta");
      const valuesNode = el(row, "span", null, "rd-mini-paper-values");
      figure(el(valuesNode, "span", signedWords(item.today), "rd-mini-paper-today"), "mini-today:" + item.source.id, row);
      figure(el(valuesNode, "span", signedWords(item.week), "rd-mini-paper-week"), "mini-week:" + item.source.id, row);
    }
  }
  /* The chapter a milestone is bound to, when its paper and section are still collected. */
  function boundChapter(project: Project, task: Task) {
    const sourceId = binding(task, "paper"), chapterId = binding(task, "chapter");
    const source = sourceId && chapterId ? project.sources.find(item => item.id === sourceId) : null, chapter = source ? paperSnapshot(source)?.chapters.find(item => item.id === chapterId) : null;
    return chapter && source ? { source, chapter } : {};
  }
  /* Next up: what to work on next, filled layer by layer up to `limit` (3 for all projects, 5 for a focused one):
   *   0. milestones just added from this window, 1. overdue (longest overdue first), 2. due within the next 14 days (soonest first), 3. what you are writing (open
   *   milestones of the sections with the most recent writing, at most two), 4. each project's next open milestone in
   *   file order, taken in turn so one project cannot fill the list. Completed projects are left out; paused and
   *   blocked ones come after active ones within each layer. Duplicates are skipped. */
  type NextItem = { project: Project; task: Task; source?: Source; chapter?: Chapter; reason: "new" | "overdue" | "due" | "writing" | "next" };
  const DUE_SOON = 14 * 24 * 3600 * 1000;
  function nextMilestones(projects: Project[], limit = 3): { items: NextItem[] } {
    const pool = projects.filter(project => projectStatus(project) !== "completed");
    const late = (project: Project) => ["paused", "blocked"].includes(projectStatus(project)) ? 1 : 0;
    const open = pool.flatMap(project => milestones(project).filter(task => !task.completed).map((task, index) => ({ project, task, index })));
    const picked: NextItem[] = [], seen = new Set<string>();
    const take = (item: { project: Project; task: Task }, reason: NextItem["reason"]) => {
      const key = item.project.id + ":" + item.task.line;
      if (picked.length >= limit || seen.has(key)) return false;
      seen.add(key); picked.push({ project: item.project, task: item.task, ...boundChapter(item.project, item.task), reason });
      return true;
    };
    /* 0. Just added here (newest first): what you add should be the first thing you see. */
    for (const recent of status().recentMilestones || []) {
      const item = open.find(entry => entry.project.id === recent.projectId && entry.task.text === recent.text);
      if (item) take(item, "new");
    }
    const dueAt = (task: Task) => milliseconds(task.due);
    const byDue = (a: { project: Project; task: Task }, b: { project: Project; task: Task }) => late(a.project) - late(b.project) || dueAt(a.task)! - dueAt(b.task)!;
    for (const item of open.filter(entry => !!entry.task.due && dayPassed(entry.task.due)).sort(byDue)) take(item, "overdue");
    const now = Date.now();
    for (const item of open.filter(entry => dueAt(entry.task) !== null && !dayPassed(entry.task.due) && dueAt(entry.task)! - now <= DUE_SOON).sort(byDue)) take(item, "due");
    /* Writing: the section whose own text changed most recently (content fingerprint, so a reworded sentence counts and
     * the exact subsection is known; the deeper section wins a tie with its parent), then today's and the week's word
     * change, then the file's last save. */
    const edited: { project: Project; source: Source; chapter: Chapter; tree: Tree; score: number[] }[] = [];
    for (const project of pool) for (const source of project.sources) {
      const paper = paperSnapshot(source); if (!paper) continue;
      const tree = paperTree(paper);
      const depth = (chapter: Chapter) => { let level = 0; for (let node = chapter; node.parentId && level < 8; level++) { const up = tree.byId.get(node.parentId); if (!up) break; node = up; } return level; };
      for (const chapter of paper.chapters) {
        const changed = milliseconds(chapter.changedAt) ?? 0;
        const today = Math.abs(number(chapter.deltaToday) ?? 0), week = Math.abs(number(chapter.deltaWeek) ?? 0), at = milliseconds(chapter.modifiedAt) ?? 0;
        if (changed || today || week || at) edited.push({ project, source, chapter, tree, score: [late(project) ? 0 : 1, changed, changed ? depth(chapter) : 0, today, week, at] });
      }
    }
    edited.sort((a, b) => { for (let i = 0; i < a.score.length; i++) if (a.score[i] !== b.score[i]) return b.score[i] - a.score[i]; return 0; });
    let writing = 0;
    for (const { project, source, chapter, tree } of edited) {
      if (writing >= 2 || picked.length >= limit) break;
      const visited = new Set<string>();
      for (let node: Chapter | undefined = chapter; node && !visited.has(node.id); node = tree.byId.get(node.parentId || "")) {
        visited.add(node.id);
        const task = milestones(project).find(item => !item.completed && binding(item, "paper") === source.id && binding(item, "chapter") === node!.id);
        if (task) { if (take({ project, task }, "writing")) writing++; break; }
      }
    }
    /* Each project's next open milestone in turn (active projects first, in list order). */
    const queues = [...pool].sort((a, b) => late(a) - late(b)).map(project => open.filter(item => item.project === project));
    for (let round = 0; picked.length < limit && queues.some(queue => queue.length); round++) {
      for (const queue of queues) {
        while (queue.length && seen.has(queue[0].project.id + ":" + queue[0].task.line)) queue.shift();
        const item = queue.shift();
        if (item) take(item, "next");
        if (picked.length >= limit) break;
      }
    }
    return { items: picked };
  }
  /* Complete a milestone from the widget: the ring fills with a check, the row eases away, then the data refreshes.
   * Chapter milestones are accepted against the chapter's current text, as on the project page. */
  async function miniComplete(row: HTMLElement, check: HTMLButtonElement, item: { project: Project; task: Task; chapter?: Chapter }) {
    if (record.disposed || check.disabled) return;
    check.disabled = true; actionMessage = "";
    setClass(row, "is-completing", true);
    const settle = new Promise(resolve => clock.setTimeout(resolve, motionOff() ? 0 : 420));
    try {
      await Promise.all([actionBridge().setMilestone!(item.project.id, { line: item.task.line, text: String(item.task.text || ""), completed: true,
        sourceId: binding(item.task, "paper") || null, chapterId: binding(item.task, "chapter") || null, sourceHash: item.chapter?.sourceHash || null }), settle]);
      await refresh(true);
    } catch (error) {
      setClass(row, "is-completing", false); check.disabled = false;
      actionMessage = String((error as Error).message || error); if (!record.disposed) paint();
    }
  }
  function miniNext(parent: Element, projects: Project[], focus: Project | null = null) {
    const { items } = nextMilestones(projects, focus ? 5 : 3);
    const canAdd = typeof actionBridge().quickMilestone === "function" && projects.length > 0;
    if (!items.length && !canAdd) return;
    const section = miniSection(parent, "Next up", "rd-mini-next");
    miniQuickAdd(section, projects, focus);
    if (!items.length) { if (!record.miniAdd.open) el(section, "p", focus ? "All milestones complete" : "No open milestones", "rd-mini-empty"); return; }
    const list = el(section, "div", null, "rd-mini-list");
    const canCheck = typeof actionBridge().setMilestone === "function";
    for (const item of items) {
      const target = item.chapter && item.source ? item : null, overdue = dayPassed(item.task.due), label = milestoneLabel(item.task);
      const row = el(list, "div", null, "rd-mini-row rd-mini-task" + (overdue ? " is-overdue" : "")) as HTMLElement;
      const check = el(row, "button", null, "rd-mini-check") as HTMLButtonElement;
      check.type = "button"; check.setAttribute("aria-label", "Mark “" + label + "” complete");
      check.disabled = !canCheck || Boolean(status().busy) || Boolean(binding(item.task, "chapter") && !item.chapter);
      svgNode(svgNode(check, "svg", { viewBox: "0 0 20 20", "aria-hidden": "true" }), "path", { d: "M5.5 10.5l3 3 6-6.5" });
      check.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); void miniComplete(row, check, item); });
      hint(check, "Mark complete", target ? "Accepts it against the section's current text" : item.project.title);
      const open = target
        ? miniButton(row, "rd-mini-task-open", label + ". Open " + item.chapter!.title + " in the main window", "focusChapter", current => current.focusChapter!(item.project.id, item.source!.id, item.chapter!.id, mainWindow))
        : miniButton(row, "rd-mini-task-open", label + ". Open " + item.project.title + " in the main window", "openProject", current => current.openProject!(item.project.id, mainWindow));
      const main = el(open, "span", null, "rd-mini-row-main"); el(main, "span", label, "rd-mini-row-title");
      /* Focused on one project, its own milestones need no project name; a sub-project's still say whose they are. */
      const owner = focus && item.project === focus ? null : item.project.parent && item.project.parent !== focus ? item.project.parent.title + " › " + item.project.title : item.project.title;
      const meta = [owner, target ? item.chapter!.title : null].filter(Boolean).join(" · ");
      if (meta) el(main, "span", meta, "rd-mini-row-meta");
      if (milliseconds(item.task.due) !== null) el(open, "span", (overdue ? "Overdue " : "") + shortDate(item.task.due), "rd-mini-task-due");
    }
  }
  /* Alert count with the first alert; hidden when there is nothing to attend to. */
  function miniAlerts(parent: Element, items: Alert[]) {
    const count = items.length + model.errors.length, { alerts, suggestions } = attentionCounts(items);
    if (!count) return;
    /* Suggestions are counted apart from alerts (they come after real alerts in the list). */
    const aside = [alerts ? plural(alerts, "alert") : null, suggestions ? plural(suggestions, "suggestion") : null].filter(Boolean).join(" · ");
    const section = miniSection(parent, "Needs attention", "rd-mini-alerts", aside);
    const first = items[0] || null, issue = first ? null : model.errors[0];
    const row = first
      ? miniButton(section, "rd-mini-row rd-mini-alert", first.label + ": " + first.project.title + ". Open in the main window", "openProject", current => current.openProject!(first.project.id, mainWindow))
      : miniButton(section, "rd-mini-row rd-mini-alert", "Configuration issue. Open the dashboard in the main window", "openOverall", current => current.openOverall!(mainWindow));
    row.dataset.state = first ? first.state : "error";
    icon(row, alertIcons[first?.state || ""] || "failed", "rd-mini-glyph rd-mini-alert-icon");
    const main = el(row, "span", null, "rd-mini-row-main");
    const where = !first ? "" : first.state === "suggestion" ? first.project.title + " › " + first.folder : (first.project.parent ? first.project.parent.title + " › " : "") + first.project.title;
    el(main, "span", first ? first.label + " · " + where : "Configuration issue", "rd-mini-row-title");
    el(main, "span", first ? first.message : issue!.message, "rd-mini-row-meta");
    if (count > 1) {
      const more = miniButton(section, "rd-mini-more", "Show all " + aside + " in the main window", "openOverall", current => current.openOverall!(mainWindow));
      more.textContent = "+" + (count - 1) + " more";
    }
  }
  function renderMini(projects: Project[]) {
    description.textContent = "";
    clear(headerActions); miniHeader(headerActions!);
    const focusId = status().focusProjectId, focus = focusId ? projects.find(project => project.id === focusId) || null : null;
    miniFocusHeading(focus);
    if (record.miniPush) {
      const direction = record.miniPush; record.miniPush = null;
      content.classList.remove("is-pushing-forward", "is-pushing-back");
      void (content as HTMLElement).offsetWidth;
      content.classList.add("is-pushing-" + direction);
      clock.setTimeout(() => content.classList.remove("is-pushing-" + direction), 900);
    }
    if (focus) {
      /* One project and its sub-projects: its ring, its sub-projects, its writing, all its open milestones, its alerts. */
      const family = [focus, ...(focus.children || [])], alerts = attention(family);
      miniSummary(content, family, [focus], attentionCounts(alerts).alerts, focus);
      if (focus.children?.length) miniProjects(content, focus.children, "Sub-projects");
      miniWriting(content, family);
      miniNext(content, family, focus);
      miniAlerts(content, alerts);
      if (actionMessage) el(content, "p", actionMessage, "rd-caption rd-mini-message");
      return;
    }
    const tops = projects.filter(project => !project.parent), alerts = attention(projects);
    miniSummary(content, projects, tops, attentionCounts(alerts).alerts);
    miniProjects(content, tops);
    miniWriting(content, projects);
    miniNext(content, projects);
    miniAlerts(content, alerts);
    if (actionMessage) el(content, "p", actionMessage, "rd-caption rd-mini-message");
  }
  /* The widget shrunk to a circle: just a ring and the percentage (the focused project's completion, family-wide, or the
   * average by top-level project as on Overall). On hover what makes it up (sub-projects, a paper's top-level chapters,
   * or the top-level projects) fans out of the circle like a Dock stack, each a small ring with its name, topped by
   * "Open in dashboard"; widget.ts sizes the window and places the items. Items open their detail in the dashboard;
   * clicking or dragging the circle itself is handled by the widget window. */
  function orbRing(parent: Element, share: number | null, size: number, stroke: number) {
    const radius = (size - stroke) / 2 - 1, circumference = 2 * Math.PI * radius, centre = size / 2;
    const svg = svgNode(parent, "svg", { viewBox: `0 0 ${size} ${size}`, "aria-hidden": "true", class: "rd-orb-ring" });
    svgNode(svg, "circle", { class: "rd-orb-track", cx: centre, cy: centre, r: radius, fill: "none", "stroke-width": stroke });
    if (share === null || share <= 0) return;
    svgNode(svg, "circle", { class: "rd-orb-arc", cx: centre, cy: centre, r: radius, fill: "none", "stroke-width": stroke, "stroke-linecap": share >= 1 ? "butt" : "round",
      "stroke-dasharray": (share >= 1 ? circumference : Math.max(0.01, share * circumference)).toFixed(2) + " " + circumference.toFixed(2), transform: `rotate(-90 ${centre} ${centre})` });
  }
  function renderOrb(projects: Project[]) {
    const focusId = status().focusProjectId, focus = focusId ? projects.find(project => project.id === focusId) || null : null;
    const tops = projects.filter(project => !project.parent);
    let share: number | null;
    if (focus) { const counts = familyCount(focus); share = counts.total ? counts.completed / counts.total : null; }
    else {
      const rated = tops.map(familyCount).filter(counts => counts.total);
      share = rated.length ? rated.reduce((sum, counts) => sum + counts.completed / counts.total, 0) / rated.length : null;
    }
    const percent = share === null ? null : Math.round(share * 100), title = focus ? focus.title : titles.mini;
    const orb = el(content, "button", null, "rd-orb-dial") as HTMLButtonElement;
    orb.type = "button";
    orb.setAttribute("aria-label", title + ": " + (percent === null ? "no milestones defined" : percent + "% complete") + ". Click to open the widget; drag to move it.");
    orbRing(orb, share, 64, 6);
    el(el(orb, "span", null, "rd-orb-center"), "strong", percent === null ? "—" : percent + "%", "rd-orb-value");
    const bars: Bar[] = focus ? focusBars(focus) : tops.map(project => {
      const counts = familyCount(project);
      return { key: "top:" + project.id, title: project.title, done: counts.completed, total: counts.total, method: "openProject" as const, open: (bridge: ActionBridge) => bridge.openProject!(project.id, mainWindow) };
    });
    const shown = bars.slice(0, 6), fan = el(content, "div", null, "rd-orb-fan");
    fan.setAttribute("role", "list");
    shown.forEach((bar, index) => {
      const itemShare = bar.total ? bar.done / bar.total : null, itemPercent = itemShare === null ? null : Math.round(itemShare * 100);
      const item = miniButton(fan, "rd-orb-item" + (itemShare !== null && itemShare >= 1 ? " is-complete" : ""), bar.title + ": " + (itemPercent === null ? "no milestones defined" : itemPercent + "%") + ". Open in the dashboard", bar.method, bar.open);
      item.setAttribute("role", "listitem");
      item.style.setProperty("--rd-fan-index", String(index));
      orbRing(el(item, "span", null, "rd-orb-item-ring"), itemShare, 30, 4);
      const label = el(item, "span", null, "rd-orb-label");
      el(label, "span", bar.title, "rd-orb-label-name");
      el(label, "span", itemPercent === null ? "—" : itemPercent + "%", "rd-orb-label-value");
    });
    const more = bars.length - shown.length;
    const open = focus
      ? miniButton(fan, "rd-orb-item rd-orb-open", "Open " + focus.title + " in the dashboard", "openProject", bridge => bridge.openProject!(focus.id, mainWindow))
      : miniButton(fan, "rd-orb-item rd-orb-open", "Open the dashboard", "openOverall", bridge => bridge.openOverall!(mainWindow));
    open.style.setProperty("--rd-fan-index", String(shown.length));
    el(el(open, "span", null, "rd-orb-label"), "span", (focus ? "Open " + focus.title : "Open dashboard") + (more > 0 ? " · " + more + " more" : "") + " ›", "rd-orb-label-name");
  }
  /* First load: a quiet, text-free outline of each mode's layout until the first paint replaces it. */
  function skeleton() {
    root.className += " is-loading"; record.skeletonAt = elapsed();
    const wrap = el(content, "div", null, "rd-skeleton rd-skeleton-" + mode); wrap.setAttribute("aria-hidden", "true");
    const bone = (parent: Element, kind: string) => el(parent, "span", null, "rd-bone is-" + kind), box = (parent: Element, kind: string) => el(parent, "div", null, "rd-skeleton-" + kind);
    const rows = (count: number, trailing = false) => { for (let index = 0; index < count; index++) { const row = box(wrap, "row"), lines = box(row, "lines"); bone(row, "dot"); row.appendChild(lines); bone(lines, "line"); bone(lines, "short"); if (trailing) bone(row, "pill"); } };
    if (headerActions) { const pills = el(headerActions, "div", null, "rd-skeleton rd-skeleton-actions"); pills.setAttribute("aria-hidden", "true"); bone(pills, "pill"); bone(pills, "pill"); }
    if (mode === "overview") {
      const kpis = box(wrap, "kpis");
      for (let index = 0; index < 4; index++) { const kpi = box(kpis, "kpi"); bone(kpi, "value"); bone(kpi, "short"); }
      const tile = box(wrap, "tile"), chart = box(tile, "chart"), donut = box(tile, "donut");
      bone(chart, "short"); const capsules = box(chart, "capsules"); for (let index = 0; index < 5; index++) bone(capsules, "capsule");
      bone(donut, "short"); const body = box(donut, "donut-body"), legend = box(body, "lines"); bone(body, "ring"); body.appendChild(legend);
      for (let index = 0; index < 3; index++) bone(legend, "line");
    } else if (mode === "project-overview" || mode === "paper") {
      const tile = box(wrap, "tile"); bone(tile, "short");
      const steps = box(tile, "steps"); for (let index = 0; index < 4; index++) bone(box(steps, "step"), "line");
    } else if (mode === "paper-progress") {
      const grid = box(wrap, "columns");
      for (let column = 0; column < 2; column++) { const chart = box(grid, "lines"); bone(chart, "short"); for (let index = 0; index < 3; index++) { const row = box(chart, "bar-row"); bone(row, "line"); bone(row, "bar"); } }
    } else if (mode === "orb") {
      bone(wrap, "ring");
    } else if (mode === "mini") {
      const summary = box(wrap, "row"), lines = box(summary, "lines"); bone(summary, "ring"); summary.appendChild(lines); bone(lines, "line"); bone(lines, "short");
      rows(3);
    } else rows(mode === "projects" || mode === "resources" || mode === "milestones" ? 3 : 2, mode === "projects");
  }
  function endLoading() { root.className = root.className.replace(/\s*\bis-loading\b/, ""); }
  /* The placeholder only appears after 300ms (CSS). If it was on screen, the first paint fades in place (is-revealing). */
  const skeletonDelay = 300;
  function elapsed() { return clock.performance?.now?.() ?? Date.now(); }
  function reveal() {
    if (record.skeletonAt == null || !/\bis-entering\b/.test(root.className) || elapsed() - record.skeletonAt < skeletonDelay) return;
    if (root.closest?.(".rd-no-entrance, .is-buffering") || (root as any).checkVisibility?.({ visibilityProperty: true }) === false) return;
    root.className = root.className.replace(/\bis-entering\b/, "is-revealing");
  }
  /* Signature of everything paint() renders. Timestamps are compared at the minute shown, and collection times only count
   * in modes that display them, so a quiet poll keeps DOM identity. */
  const pageFields = ["title", "project_health", "next_step", "due", "stage", "resources", "knowledge", "parent_id", "project_type"] as const;
  const showsCollection = ["runs", "health", "paper-progress"].includes(mode);
  function signatureValue(key: string, value: unknown) {
    if (key === "revision" || key === "collectorVersion" || (!showsCollection && (key === "attemptedAt" || key === "lastSuccessfulAt" || key === "observedAt"))) return undefined;
    if (typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d/.test(value)) return value.slice(0, 16) + value.slice(19).replace(/^\.\d+/, "");
    return value;
  }
  function taskShape(task: Task) { return [String(task.text || ""), task.completed === true, task.due ?? "", task.line ?? null, task.path ?? null]; }
  function signature(next: Model = model) {
    try {
      return JSON.stringify([new Date().toDateString(), actionMessage, status(), discoverySuggestions(), search, projectUi.type, selectedStatus, paperUi, next.errors,
        next.projects.map(project => [project.id, project.title, project.note, project.dashboard, project.kinds, project.parentId, project.type ?? null, project.parent ? [project.parent.id, project.parent.title] : null, (project.catalog as Dict | null)?.discovery ?? null,
          (project.children || []).map(child => [child.id, child.title, child.type, pageFields.map(name => child.page?.[name] ?? null), milestones(child).map(taskShape), child.sources.map(source => [source.id, source.kind, source.health])]),
          pageFields.map(name => project.page?.[name] ?? null), project.milestoneReadError ?? null, milestones(project).map(taskShape),
          project.sources.map(source => [source.id, source.title, source.kind, source.display?.label ?? null, source.health, source.readError, source.directory, source.config ?? null, source.state])])], signatureValue);
    } catch (_) { return null; }
  }
  /* cause: "data" (refresh with changed data) may tween, roll and glow; "first", "restore" and user paints never do. */
  function paint(cause = "user") {
    if (record.disposed) return;
    /* A rebuilt project list keeps keyboard focus on the same row's title (e.g. after a Move into from its menu). */
    const focused = documentRef.activeElement as HTMLElement | null, focusedRow = focused?.matches?.(".rd-project-title a") && content.contains(focused) ? (focused.closest(".rd-project-row") as HTMLElement | null)?.dataset.projectId : null;
    captureDisclosures(); record.disclosureNodes.clear();
    record.paintEpoch++; clear(content); if (cause === "first") reveal(); endLoading();
    record.animate = cause === "data" && canAnimate(); record.nextMarks = new Map(); record.updated.clear();
    const renderers: Record<string, (projects: Project[]) => void> = { mini: renderMini, orb: renderOrb, overview: renderOverview, "project-overview": renderProjectOverview, paper: renderProjectOverview, "paper-progress": renderPaperProgress, attention: renderAttention, projects: renderProjects, milestones: renderMilestones, runs: renderRuns, resources: renderResources, health: renderHealth };
    try {
      if (renderers[mode]) renderers[mode](model.projects);
      else empty(content, "Unknown module", "Undefined mode: " + mode);
      if (focusedRow) focusRow(focusedRow);
    } finally {
      for (const node of record.updated as Set<Element>) { node.classList.add("is-updated"); later(node, "is-updated", 1700); }
      marks.clear(); for (const [key, value] of record.nextMarks as Map<string, unknown>) marks.set(key, value);
      record.nextMarks = null; record.animate = false; record.updated.clear();
    }
    for (const key of disclosures.keys()) if (!record.disclosureNodes.has(key)) disclosures.delete(key);
    record.model = model; record.painted = true; record.signature = signature();
    if (record.tip.target && !record.tip.target.isConnected) hideTip(true);
  }
  /* force: a user action awaits this refresh and its controls must be rebuilt (re-enabled) even if nothing changed. */
  async function refresh(force = false) {
    if (record.disposed) return;
    record.forcePaint = record.forcePaint || force;
    /* A lifted row or a form being typed into (the widget's quick add) defers repaints until it is let go. */
    if (record.busy || record.drag || record.hold) { record.refreshQueued = true; return; }
    record.busy = true;
    try {
      const loaded = await load();
      /* A repaint would cut a running regroup glide short, and one during a drag would remove the lifted row. */
      const gliding = record.regroupTransition;
      if (gliding && record.transition === gliding) await Promise.resolve(gliding.finished).catch(() => {});
      const next = applyPending(loaded);
      if (record.drag || record.hold) record.refreshQueued = true;
      else if (!record.disposed && !record.refreshQueued) {
        /* Unchanged data keeps the existing DOM: no rebuild, so hover, focus and scroll stay put. */
        const unchanged = record.painted && record.signature !== null && signature(next) === record.signature;
        model = next; record.model = next;
        if (!unchanged) paint(record.painted ? "data" : "first"); else if (record.forcePaint) paint();
        record.forcePaint = false;
      }
    }
    catch (error) { if (!record.disposed) { record.signature = null; clear(content); if (/\bis-loading\b/.test(root.className)) { reveal(); if (headerActions) clear(headerActions); } endLoading(); empty(content, "Dashboard could not be loaded", String((error as Error).message || error)); } }
    finally {
      record.busy = false;
      if (!record.disposed && record.refreshQueued) { record.refreshQueued = false; void refresh(); }
    }
  }
  skeleton();
  void refresh().then(() => {
    if (record.disposed) return;
    /* Announce the first paint so a buffered page can be swapped in. */
    container.dispatchEvent(new CustomEvent("research-dashboard:painted", { bubbles: true, detail: { mode, projectId: requestedProject } }));
    resolvePainted();
    /* Every entrance (rise, bar growth, ring draw) has settled by ~800ms; the first-open classes go at 900ms. */
    clock.setTimeout(() => { root.className = root.className.replace(/\s*\bis-(entering|revealing)\b/g, ""); }, 900);
    record.unsubscribe = ctx.subscribe(() => { if (!record.disposed) void refresh(); });
    record.timer = clock.setInterval(() => { if (root.isConnected) void refresh(); }, 30_000);
  });
  return { dispose, refresh, painted };
}
