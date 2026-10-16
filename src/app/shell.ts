/* Main window: the welcome screen (no workspace yet) or the dashboard page host. Overall shows the overview, projects
 * and attention panels; a project page shows project-overview, progress, milestones, live monitor and resources.
 * Pages are built in a hidden buffer and swapped in once painted, inside a View Transition (ported from the plugin's
 * ResearchDashboardView: forward/back travel, shared title morph, condensed top bar). */

import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { errorMessage } from "../model/rules";
import { OVERALL_LINK } from "../model/types";
import { mountView, type Mode, type ViewContext, type ViewHandle } from "../view/view";
import { h } from "../ui/modal";
import { notice } from "../ui/notice";
import { importDialog, settingsDialog } from "../ui/settings";
import { AppActions, type PageState } from "./actions";
import { api, events, type AgentRequest } from "./api";
import type { Store } from "./store";

const OVERALL_MODES: Mode[] = ["overview", "projects", "attention"];
const PROJECT_MODES: Mode[] = ["project-overview", "paper-progress", "milestones", "runs", "resources"];
const PAINT_TIMEOUT = 1500;
const VIEW_TRANSITION_KEY = Symbol.for("research-dashboard.viewTransition");
const PROJECT_HEADING = '[data-mode="project-overview"] h1.rd-page-title';

interface Page { state: PageState; key: string; grid: HTMLElement; views: ViewHandle[] }

function pageKey(state: PageState): string {
  if (!state.projectId) return "overall";
  return state.chapterId ? `project:${state.projectId}:${state.sourceId}/${state.chapterId}` : `project:${state.projectId}`;
}
function pageDepth(state: PageState | null): number { return !state?.projectId ? 0 : state.chapterId ? 2 : 1; }

export class Shell {
  private scroller: HTMLElement;
  private page: Page | null = null;
  private pending: Page | null = null;
  private epoch = 0;
  private state: PageState = { projectId: null };
  private history: PageState[] = [];
  private scrollPositions = new Map<string, number>();
  private topbar: { el: HTMLElement; back: HTMLButtonElement; title: HTMLElement; edit: HTMLButtonElement; live: HTMLElement } | null = null;
  private actions: AppActions;
  private ctx: ViewContext;
  private dropOverlay: HTMLElement | null = null;
  private dropCheck = 0;

  constructor(host: HTMLElement, private store: Store) {
    this.scroller = h(host, "div", { cls: "rd-native-dashboard" });
    this.actions = new AppActions(store, {
      navigate: state => this.navigate(state),
      current: () => this.state,
      openSettings: section => this.openSettings(section),
      importVault: () => this.importVault(),
    });
    this.ctx = {
      load: async () => this.store.model(),
      bridge: this.actions,
      navigate: path => {
        if (path === OVERALL_LINK) this.navigate({ projectId: null });
        const match = /^rd:\/\/project\/([a-z0-9][a-z0-9_-]{0,63})$/.exec(path);
        if (match) this.navigate({ projectId: match[1] });
      },
      openExternal: target => void (/^(https?|vscode):/i.test(target) ? api.openUrl(target) : api.openPath(target)).catch(error => notice(errorMessage(error), 8000, "error")),
      openWorkspaceFile: relative => {
        const root = this.store.workspace?.root;
        if (!root) return;
        const separator = root.includes("\\") ? "\\" : "/";
        void api.openPath(root + separator + relative.split("/").join(separator)).catch(error => notice(errorMessage(error), 8000, "error"));
      },
      subscribe: listener => this.store.subscribe(listener),
    };
    store.subscribe(() => this.syncTopbar());
    void events.connect(request => {
      if (!this.store.workspace) { notice("Create or open a workspace before connecting folders.", 6000); return; }
      void this.actions.connect({ source: request.source || null }).catch(error => notice(errorMessage(error), 8000, "error"));
    });
    /* `ma connect` (an agent): the plan card opens prefilled; the user confirms. Requests that came with the app's own
     * start are collected once the page is ready. */
    const handleRequest = (request: AgentRequest) => {
      if (!this.store.workspace) { notice("Create or open a workspace before connecting folders.", 6000); return; }
      if (request?.action !== "connect" || typeof request.source !== "string") return;
      void this.actions.connect({ source: request.source, parentId: request.parentId || null,
        preset: { title: request.title || null, profileId: request.profileId || null, plan: request.plan || null, fromAgent: true } })
        .catch(error => notice(errorMessage(error), 8000, "error"));
    };
    void events.request(request => { void api.takeRequests().catch(() => []); handleRequest(request); });
    window.setTimeout(() => void api.takeRequests().then(list => { for (const request of list) handleRequest(request); }).catch(() => {}), 600);
    void events.navigate(target => this.navigate({ projectId: target.projectId || null, sourceId: target.sourceId || null, chapterId: target.chapterId || null }));
    void events.workspaceOpened(() => { this.actions.discovery.reset(); this.actions.discovery.request(); this.render(true); this.maybeAgentSetup(); });
    void events.collectorEvent(() => this.actions.discovery.afterCollection());
    void events.error(message => notice(message, 9000, "error"));
    window.addEventListener("keydown", event => {
      if ((event.altKey && event.key === "ArrowLeft") || (event.key === "Escape" && !document.querySelector(".rd-modal-backdrop") && this.state.projectId)) {
        if (event.key === "Escape" && document.activeElement?.closest?.("input, select, textarea")) return;
        event.preventDefault(); this.back();
      }
      if ((event.ctrlKey || event.metaKey) && event.key === ",") { event.preventDefault(); this.openSettings(); }
    });
    window.addEventListener("mouseup", event => { if (event.button === 3) this.back(); });
    void getCurrentWebview().onDragDropEvent(event => {
      const payload = event.payload;
      if (payload.type === "enter") this.showDrop(payload.paths);
      else if (payload.type === "drop") void this.dropped(payload.paths);
      else if (payload.type === "leave") this.hideDrop();
    }).catch(() => { /* No native drop (web preview): Connect project remains. */ });
  }

  /* ---------- folder drop from the file manager ----------
   * The window takes OS drops natively (Tauri gives real paths). One folder opens the plan card; on a project page the
   * project (its top-level ancestor) is preset as parent, and the card can make the folder its discovery folder. */
  private showDrop(paths: string[]): void {
    if (!this.store.workspace || document.querySelector(".rd-modal-backdrop")) return;
    const projectTitle = this.state.projectId ? this.store.model().projects.find(item => item.id === this.state.projectId)?.title : null;
    if (!this.dropOverlay) {
      this.dropOverlay = h(document.body, "div", { cls: "rd-drop-overlay", attr: { "aria-hidden": "true" } });
      const card = h(this.dropOverlay, "div", { cls: "rd-drop-card" });
      h(card, "div", { cls: "rd-drop-glyph" });
      h(card, "div", { cls: "rd-drop-title" });
      h(card, "div", { cls: "rd-drop-caption" });
    }
    const title = this.dropOverlay.querySelector(".rd-drop-title")!, caption = this.dropOverlay.querySelector(".rd-drop-caption")!;
    const plan = projectTitle ? `A plan opens with ${projectTitle} as parent project` : "A plan opens with the detected monitoring options";
    this.dropOverlay.classList.remove("is-rejecting", "is-leaving");
    title.textContent = "Drop a folder to connect it"; caption.textContent = plan;
    if (paths.length !== 1) { this.dropOverlay.classList.add("is-rejecting"); title.textContent = paths.length ? "Drop one folder at a time" : "Drop a folder from your computer"; caption.textContent = ""; return; }
    const check = ++this.dropCheck;
    void api.pathKind(paths[0]).then(kind => {
      if (check !== this.dropCheck || !this.dropOverlay || kind === "dir") return;
      this.dropOverlay.classList.add("is-rejecting");
      title.textContent = "Drop a folder, not a file"; caption.textContent = "The dashboard monitors project folders.";
    }).catch(() => {});
  }

  private hideDrop(): void {
    this.dropCheck++;
    const overlay = this.dropOverlay;
    if (!overlay) return;
    this.dropOverlay = null;
    if (this.motionMode() === "off") { overlay.remove(); return; }
    overlay.classList.add("is-leaving");
    const remove = () => overlay.remove();
    overlay.addEventListener("transitionend", remove, { once: true });
    window.setTimeout(remove, 300);
  }

  private async dropped(paths: string[]): Promise<void> {
    this.hideDrop();
    if (!this.store.workspace) { notice("Create or open a workspace before connecting folders.", 6000); return; }
    if (document.querySelector(".rd-modal-backdrop")) { notice("Close the open dialog before dropping a folder.", 6000); return; }
    if (paths.length !== 1) { notice(paths.length ? "Drop one folder at a time." : "Drop a folder from your computer.", 6000); return; }
    const kind = await api.pathKind(paths[0]).catch(() => "missing");
    if (kind !== "dir") { notice("Drop a folder, not a file. The dashboard monitors project folders.", 6000); return; }
    void this.actions.connect({ source: paths[0], parentId: this.state.projectId || null, dropped: true }).catch(error => notice(errorMessage(error), 8000, "error"));
  }

  start(): void { this.render(true); this.actions.discovery.request(); this.maybeAgentSetup(); }

  /** First run (once a workspace is open): offer to connect an agent; Skip or Done marks it done. */
  private maybeAgentSetup(): void {
    window.setTimeout(() => {
      if (!this.store.workspace || this.actions.agentSettings().setupDone || document.querySelector(".rd-modal-backdrop")) return;
      this.actions.agentSetupDialog({ firstRun: true });
    }, 1200);
  }

  navigate(state: PageState): void {
    const next = { projectId: state.projectId || null, sourceId: state.sourceId || null, chapterId: state.chapterId || null };
    if (pageKey(next) === pageKey(this.state) && next.sourceId === this.state.sourceId) return;
    if (next.projectId) this.history.push(this.state); else this.history = [];
    this.state = next;
    void this.render();
  }

  back(): void {
    if (!this.state.projectId) return;
    this.state = this.history.pop() || { projectId: null };
    void this.render();
  }

  /* ---------- welcome ---------- */
  private renderWelcome(): void {
    this.clear();
    const card = h(this.scroller, "div", { cls: "rd-app-empty rd-welcome" });
    h(card, "h1", { text: "Monitoring Anything" });
    h(card, "p", { text: "Follow your research projects, milestones and the live progress of LaTeX papers and simulation runs. Start by creating a workspace: a plain folder of JSON and Markdown files that the app owns." });
    const row = h(card, "div", { cls: "rd-welcome-actions" });
    const create = h(row, "button", { text: "Create workspace…", cls: "mod-cta", attr: { type: "button" } });
    create.addEventListener("click", () => void this.createWorkspace());
    const openButton = h(row, "button", { text: "Open workspace…", attr: { type: "button" } });
    openButton.addEventListener("click", () => void this.openWorkspace());
    const recent = (this.store.settings?.recentWorkspaces || []).filter(Boolean);
    if (recent.length) {
      h(card, "h3", { text: "Recent" });
      const list = h(card, "ul", { cls: "rd-welcome-recent" });
      for (const path of recent) {
        const item = h(h(list, "li"), "button", { text: path, attr: { type: "button" } });
        item.addEventListener("click", () => void api.workspaceOpen(path).catch(error => notice(errorMessage(error), 8000, "error")));
      }
    }
    h(card, "p", { cls: "rd-caption", text: "Already using the Obsidian Research Dashboard? Create a workspace, then choose Import from Obsidian in Settings → Workspace." });
  }

  async createWorkspace(): Promise<void> {
    const selected = await openDialog({ directory: true, multiple: false, title: "Choose an empty folder for the new workspace" }).catch(() => null);
    if (typeof selected !== "string" || !selected) return;
    try { await api.workspaceCreate(selected); }
    catch (error) {
      if (!/empty folder/i.test(errorMessage(error))) { notice(errorMessage(error), 9000, "error"); return; }
      const nested = selected.replace(/[\\/]+$/, "") + (selected.includes("\\") ? "\\" : "/") + "Monitoring Anything";
      try { await api.workspaceCreate(nested); notice(`The folder was not empty, so the workspace was created in ${nested}.`, 9000); }
      catch (inner) { notice(errorMessage(inner), 9000, "error"); }
    }
  }

  async openWorkspace(): Promise<void> {
    const selected = await openDialog({ directory: true, multiple: false, title: "Open a Monitoring Anything workspace" }).catch(() => null);
    if (typeof selected !== "string" || !selected) return;
    try { await api.workspaceOpen(selected); }
    catch (error) { notice(errorMessage(error), 9000, "error"); }
  }

  openSettings(section?: string): void {
    if (document.querySelector(".rd-settings-modal")) return;
    settingsDialog({
      section,
      agentProviders: refresh => this.actions.agentProviders(refresh),
      agentSetup: () => this.actions.agentSetupDeps(),
      settings: () => this.store.settings!,
      workspaceRoot: () => this.store.workspace?.root || null,
      openWorkspace: () => this.openWorkspace(),
      createWorkspace: () => this.createWorkspace(),
      importVault: async () => this.importVault(),
    });
  }

  importVault(): void {
    if (!this.store.workspace) { notice("Create or open a workspace first.", 6000); return; }
    importDialog(() => void this.store.reload());
  }

  /* ---------- dashboard pages ---------- */
  private clear(): void {
    this.epoch++;
    for (const page of [this.page, this.pending]) page?.views.forEach(view => view.dispose());
    this.page = null; this.pending = null; this.topbar = null;
    this.scroller.replaceChildren();
    this.scroller.classList.remove("is-navigating");
  }

  private async render(reset = false): Promise<void> {
    if (!this.store.workspace) { this.renderWelcome(); return; }
    if (reset || !this.page) {
      this.clear();
      this.ensureTopbar();
    }
    const epoch = ++this.epoch;
    const state = { ...this.state };
    const buffered = Boolean(this.page);
    if (this.pending) { this.pending.views.forEach(view => view.dispose()); this.pending.grid.remove(); this.pending = null; }
    const grid = h(this.scroller, "div", { cls: state.projectId ? "rd-app-grid is-project" : "rd-app-grid" });
    const page: Page = { state, key: pageKey(state), grid, views: [] };
    if (buffered) { grid.classList.add("is-buffering", "rd-no-entrance"); grid.setAttribute("aria-hidden", "true"); grid.inert = true; this.pending = page; }
    else this.page = page;
    for (const mode of state.projectId ? PROJECT_MODES : OVERALL_MODES) {
      const panel = h(grid, "div", { cls: "rd-app-panel", attr: { "data-mode": mode } });
      page.views.push(mountView(panel, { mode, projectId: state.projectId, sourceId: state.sourceId, chapterId: state.chapterId ?? null, entrance: !buffered }, this.ctx));
    }
    this.syncTopbar();
    if (!buffered) return;
    const navigating = window.setTimeout(() => { if (this.pending === page) this.scroller.classList.add("is-navigating"); }, 300);
    await Promise.race([Promise.all(page.views.map(view => view.painted)), new Promise(resolve => window.setTimeout(resolve, PAINT_TIMEOUT))]);
    window.clearTimeout(navigating);
    if (epoch !== this.epoch || this.pending !== page) return;
    this.pending = null;
    await this.commit(page);
  }

  private async commit(page: Page): Promise<void> {
    const previous = this.page;
    const from = previous?.state || null, fromKey = previous?.key || null;
    this.page = page;
    const direction = !fromKey || fromKey === page.key ? "same" : pageDepth(page.state) < pageDepth(from) ? "back" : "forward";
    if (fromKey) this.scrollPositions.set(fromKey, this.scroller.scrollTop || 0);
    const scrollTop = direction === "back" ? this.scrollPositions.get(page.key) || 0 : direction === "same" ? this.scroller.scrollTop || 0 : 0;
    const swap = () => {
      this.scroller.classList.remove("is-navigating");
      previous?.views.forEach(view => view.dispose());
      previous?.grid.remove();
      page.grid.classList.remove("is-buffering");
      page.grid.removeAttribute("aria-hidden");
      page.grid.inert = false;
      this.syncTopbar();
      this.scroller.scrollTo({ top: scrollTop, behavior: "instant" as ScrollBehavior });
    };
    await this.transition(swap, direction, from, page.state, previous?.grid || null, page.grid);
  }

  private motionMode(): "off" | "subtle" | "full" {
    if (document.body.classList.contains("rd-motion-off") || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return "off";
    return document.body.classList.contains("rd-motion-subtle") ? "subtle" : "full";
  }

  private async transition(swap: () => void, direction: string, from: PageState | null, to: PageState, oldGrid: HTMLElement | null, newGrid: HTMLElement): Promise<void> {
    const doc = document as Document & { startViewTransition?: (update: () => void) => { finished: Promise<void>; ready: Promise<void>; updateCallbackDone: Promise<void>; skipTransition(): void } };
    const win = window as unknown as Record<symbol, { skipTransition?: () => void } | undefined>;
    try { win[VIEW_TRANSITION_KEY]?.skipTransition?.(); } catch (_) { /* finished */ }
    const motion = this.motionMode();
    if (direction === "same" || motion === "off" || typeof doc.startViewTransition !== "function") { swap(); return; }
    const root = document.documentElement;
    const classes = ["rd-vt", direction === "back" ? "rd-vt-back" : "rd-vt-forward"];
    if (motion === "subtle") classes.push("rd-vt-subtle");
    root.classList.remove("rd-vt-forward", "rd-vt-back", "rd-vt-subtle");
    root.classList.add(...classes);
    const surface = getComputedStyle(this.scroller);
    root.style.setProperty("--rd-vt-page-color", surface.backgroundColor);
    root.style.setProperty("--rd-vt-page-image", surface.backgroundImage);
    const named = new Set<HTMLElement>();
    const assign = (element: HTMLElement, name: string, group: string) => { element.style.setProperty("view-transition-name", name); element.style.setProperty("view-transition-class", group); named.add(element); };
    const clearNames = () => { for (const element of named) { element.style.removeProperty("view-transition-name"); element.style.removeProperty("view-transition-class"); element.classList.remove("rd-vt-shared"); } named.clear(); };
    assign(this.scroller, "rd-page-main", "rd-page");
    const title = (side: "old" | "new"): HTMLElement | null => {
      const forward = !from?.projectId && Boolean(to.projectId), back = Boolean(from?.projectId) && !to.projectId;
      if (motion !== "full" || (!forward && !back)) return null;
      const projectId = forward ? to.projectId : from!.projectId;
      const row = `.rd-project-title a[data-href="rd://project/${projectId}"]`;
      const element = (side === "old" ? oldGrid : newGrid)?.querySelector<HTMLElement>((side === "old") === forward ? row : PROJECT_HEADING);
      if (!element || element.getClientRects().length !== 1) return null;
      const box = element.getBoundingClientRect(), pane = this.scroller.getBoundingClientRect();
      return box.width > 0 && box.top >= pane.top + 52 && box.bottom <= pane.bottom ? element : null;
    };
    const source = title("old");
    if (source) { source.classList.add("rd-vt-shared"); assign(source, "rd-title-main", "rd-title"); }
    let active: { finished: Promise<void>; ready: Promise<void>; updateCallbackDone: Promise<void> } | null = null;
    const finish = () => {
      clearNames();
      if (win[VIEW_TRANSITION_KEY] === (active as unknown)) delete win[VIEW_TRANSITION_KEY];
      root.classList.remove(...classes);
      root.style.removeProperty("--rd-vt-page-color"); root.style.removeProperty("--rd-vt-page-image");
    };
    try {
      active = doc.startViewTransition(() => {
        swap();
        if (!source) return;
        const target = title("new");
        if (target) { target.classList.add("rd-vt-shared"); assign(target, "rd-title-main", "rd-title"); }
      });
    } catch (_) { finish(); swap(); return; }
    win[VIEW_TRANSITION_KEY] = active as unknown as { skipTransition(): void };
    active.ready.catch(() => {});
    active.finished.then(finish, finish);
    try { await active.updateCallbackDone; } catch (_) { swap(); }
  }

  /* ---------- condensed top bar (shown once the page header scrolls away) ---------- */
  private ensureTopbar(): void {
    if (this.topbar) return;
    const el = h(this.scroller, "div", { cls: "rd-topbar" });
    const inner = h(el, "div", { cls: "rd-topbar-inner" });
    const leading = h(inner, "div", { cls: "rd-topbar-leading" });
    const back = h(leading, "button", { cls: "rd-topbar-back", text: "Overall", attr: { type: "button", "aria-label": "Back to Overall" } });
    back.addEventListener("click", () => this.navigate({ projectId: null }));
    const title = h(inner, "div", { cls: "rd-topbar-title" });
    const trailing = h(inner, "div", { cls: "rd-topbar-trailing" });
    const edit = h(trailing, "button", { cls: "rd-topbar-chip", text: "Edit project", attr: { type: "button" } });
    edit.addEventListener("click", () => { if (this.page?.state.projectId) void this.actions.editProject(this.page.state.projectId).catch(error => notice(errorMessage(error), 8000, "error")); });
    const live = h(trailing, "div", { cls: "rd-topbar-live" });
    this.topbar = { el, back, title, edit, live };
    const style = getComputedStyle(this.scroller);
    el.style.setProperty("--rd-topbar-inset-top", style.paddingTop);
    el.style.setProperty("--rd-topbar-inset-start", style.paddingLeft);
    el.style.setProperty("--rd-topbar-inset-end", style.paddingRight);
  }

  private syncTopbar(): void {
    const bar = this.topbar, page = this.page;
    if (!bar || !page) return;
    const projectId = page.state.projectId;
    bar.el.setAttribute("data-page", projectId ? "project" : "overall");
    bar.back.hidden = !projectId; bar.edit.hidden = !projectId; bar.live.hidden = Boolean(projectId);
    if (projectId) {
      const headingText = page.grid.querySelector(PROJECT_HEADING)?.textContent?.trim();
      bar.title.textContent = headingText || this.store.model().projects.find(item => item.id === projectId)?.title || "Project";
      bar.edit.disabled = this.store.busy;
    } else {
      const running = this.store.watch.running;
      bar.title.textContent = "Overall";
      bar.live.textContent = `Live updates · ${running ? "Running" : "Stopped"}`;
      bar.live.classList.toggle("is-running", running);
    }
    document.title = projectId ? `${bar.title.textContent} · Monitoring Anything` : `${this.store.workspace?.name || "Workspace"} · Monitoring Anything`;
  }
}
