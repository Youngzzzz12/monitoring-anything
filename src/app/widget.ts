/* Floating widget: the mini layout in a frameless, transparent window. Rows ask the main window to navigate; the pin
 * toggles keep-on-top; idle translucency fades the whole widget (CSS over a transparent window, so the desktop shows
 * through) after the pointer leaves it, ported from the plugin's IdleFade. Whether the pointer is over the widget
 * comes from the Rust side (`widget://pointer`, polled against the window frame), which also works while another app
 * is active; the page's own pointer events only speed up the reaction. */

import { getCurrentWindow } from "@tauri-apps/api/window";
import { listen } from "@tauri-apps/api/event";
import { errorMessage } from "../model/rules";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { mountView, type ActionBridge, type BridgeStatus } from "../view/view";
import { h } from "../ui/modal";
import { api } from "./api";
import { addMilestoneTo, deleteMilestoneIn, editMilestoneIn, setMilestoneIn } from "./milestone-writes";
import { notice } from "../ui/notice";
import type { Store } from "./store";

const IDLE = { delay: 1200, wake: 1000, min: 0.3, max: 0.9 };

/* Idle state as classes; CSS does the motion (compositor-driven, no frame loop): resting fades and settles back to 96%;
 * waking grows back on a soft spring while the sections rise one after another (is-waking). Motion Off / Reduce motion
 * switch at once. */
class IdleFade {
  private idle = false;
  private inside = true;
  private pressed = false;
  private idleTimer: number | null = null;
  private wakeTimer: number | null = null;

  constructor(region: HTMLElement, private sink: HTMLElement, private enabled: () => boolean, private level: () => number) {
    region.addEventListener("pointerenter", () => this.enter());
    region.addEventListener("pointerleave", () => { this.inside = false; this.arm(); });
    region.addEventListener("pointermove", () => { if (!this.inside) this.enter(); }, { passive: true });
    region.addEventListener("pointerdown", () => { this.pressed = true; this.activity(); }, true);
    region.addEventListener("keydown", () => this.activity(), true);
    window.addEventListener("pointerup", () => { if (this.pressed) { this.pressed = false; if (!this.inside) this.arm(); } }, true);
    window.addEventListener("focus", () => this.activity());
    window.addEventListener("blur", () => { this.inside = false; this.arm(); });
    this.sink.style.setProperty("--rd-idle-opacity", String(this.level()));
  }

  private enter() { this.inside = true; this.disarm(); this.wake(); }
  private activity() { this.disarm(); this.wake(); if (!this.inside) this.arm(); }
  /** The window-frame signal from Rust: authoritative for leaving (the page may never see pointerleave). */
  pointer(inside: boolean) {
    if (inside) { if (!this.inside || this.idle) this.enter(); return; }
    if (this.pressed) return;
    this.inside = false; this.arm();
  }
  private arm() {
    if (!this.enabled()) return;
    if (this.idleTimer !== null) window.clearTimeout(this.idleTimer);
    this.idleTimer = window.setTimeout(() => this.check(), IDLE.delay);
  }
  private disarm() { if (this.idleTimer !== null) window.clearTimeout(this.idleTimer); this.idleTimer = null; }
  private check() {
    this.idleTimer = null;
    if (!this.enabled() || this.inside) return;
    if (this.pressed || document.querySelector(".rd-settings-menu[open], .rd-tooltip:popover-open, .rd-mini-add.is-open")) { this.arm(); return; }
    this.rest();
  }
  private rest() {
    if (this.idle) return;
    this.idle = true;
    this.sink.style.setProperty("--rd-idle-opacity", String(this.level()));
    this.sink.classList.remove("is-waking");
    this.sink.classList.add("is-idle");
  }
  private wake() {
    if (!this.idle) return;
    this.idle = false;
    this.sink.classList.remove("is-idle");
    // Restart the wake keyframes even when a previous wake is still running.
    this.sink.classList.remove("is-waking");
    void this.sink.offsetWidth;
    this.sink.classList.add("is-waking");
    if (this.wakeTimer !== null) window.clearTimeout(this.wakeTimer);
    this.wakeTimer = window.setTimeout(() => { this.wakeTimer = null; this.sink.classList.remove("is-waking"); }, IDLE.wake);
  }
  /** Settings changed: off restores full opacity; a resting widget moves to the new level. */
  refresh() {
    this.sink.style.setProperty("--rd-idle-opacity", String(this.level()));
    if (!this.enabled()) { this.disarm(); this.wake(); return; }
    if (!this.idle && !this.inside) this.arm();
  }
}

export function startWidget(host: HTMLElement, store: Store): void {
  document.body.classList.add("rd-widget-window");
  const frame = h(host, "div", { cls: "rd-widget-frame" });
  const grip = h(frame, "div", { cls: "rd-widget-grip", attr: { "data-tauri-drag-region": "" } });
  h(grip, "span", { cls: "rd-widget-grip-bar", attr: { "data-tauri-drag-region": "", "aria-hidden": "true" } });
  const close = h(grip, "button", { cls: "rd-widget-close", attr: { type: "button", "aria-label": "Hide widget", title: "Hide widget" } });
  close.addEventListener("click", () => void api.widgetSetVisible(false));
  const page = h(frame, "div", { cls: "rd-native-dashboard rd-mini-host" });
  const panel = h(page, "div", { cls: "rd-app-panel", attr: { "data-mode": "mini" } });

  const report = (error: unknown) => { store.error = errorMessage(error); store.notify(); };
  const bridge: ActionBridge = {
    getStatus: (): BridgeStatus => ({ running: store.watch.running, busy: store.busy, error: store.error || store.watch.error, keepOnTop: Boolean(store.settings?.widget.keepOnTop), canKeepOnTop: true, focusProjectId: store.settings?.widget.focusProjectId ?? null, recentMilestones: store.recentMilestones() }),
    start: () => store.exclusive(async () => { store.watch = await api.watchStart(); }),
    stop: () => store.exclusive(async () => { store.watch = await api.watchStop(); }),
    setKeepOnTop: async value => { await api.updateSettings({ widget: { keepOnTop: value } }).catch(report); },
    setMilestone: async (projectId, request) => { await setMilestoneIn(store, projectId, request as never); },
    quickMilestone: async (projectId, title) => { await addMilestoneTo(store, projectId, title); },
    editMilestone: async (projectId, request) => { await editMilestoneIn(store, projectId, request); },
    deleteMilestone: async (projectId, request) => {
      const undo = await deleteMilestoneIn(store, projectId, request);
      notice("Milestone deleted.", 8000, "info", { label: "Undo", run: () => undo().catch(error => notice(errorMessage(error), 8000, "error")) });
    },
    connect: async () => { await api.connectInMain(null); },
    setFocus: async projectId => { store.settings = await api.updateSettings({ widget: { focusProjectId: projectId } }); store.notify(); },
    openOverall: async () => { await api.navigateMain({}); },
    openProject: async projectId => { await api.navigateMain({ projectId }); },
    focusChapter: async (projectId, sourceId, chapterId) => { await api.navigateMain({ projectId, sourceId, chapterId }); },
  };
  if (!store.workspace) {
    const empty = h(panel, "div", { cls: "rd-app-empty" });
    h(empty, "p", { text: "Open or create a workspace in the main window." });
    const button = h(empty, "button", { text: "Open dashboard", attr: { type: "button" } });
    button.addEventListener("click", () => void api.navigateMain({}));
  }
  let view = store.workspace ? mountView(panel, { mode: "mini" }, { load: async () => store.model(), bridge, navigate: () => void api.navigateMain({}), openExternal: () => {}, openWorkspaceFile: () => {}, subscribe: listener => store.subscribe(listener) }) : null;
  let workspaceRoot = store.workspace?.root || null;
  store.subscribe(() => {
    if ((store.workspace?.root || null) === workspaceRoot) return;
    workspaceRoot = store.workspace?.root || null;
    view?.dispose(); panel.replaceChildren();
    view = mountView(panel, { mode: "mini" }, { load: async () => store.model(), bridge, navigate: () => void api.navigateMain({}), openExternal: () => {}, openWorkspaceFile: () => {}, subscribe: listener => store.subscribe(listener) });
  });

  const fade = new IdleFade(document.documentElement, frame,
    () => Boolean(store.settings?.widget.fadeWhenIdle && store.settings?.widget.keepOnTop),
    () => Math.min(IDLE.max, Math.max(IDLE.min, store.settings?.widget.idleOpacity ?? 0.55)));
  store.subscribe(() => fade.refresh());
  void listen<boolean>("widget://pointer", event => fade.pointer(Boolean(event.payload)))
    .then(() => api.widgetPointer()).then(inside => { if (typeof inside === "boolean") fade.pointer(inside); }).catch(() => {});
  window.addEventListener("keydown", event => {
    if (event.key === "Escape" && !(event.target as HTMLElement | null)?.closest?.("input, select, textarea, form")) void getCurrentWindow().hide().then(() => api.widgetSetVisible(false));
  });
  installFolderDrop(frame);
  installDock(frame, () => Math.min(0.08, Math.max(0, store.settings?.widget.magnification ?? 0.05)));
}

/* Dock-like magnification of whole modules: the section under the pointer grows with its card and content (up to the
 * Settings amount, 5% by default), its neighbours less as the pointer is farther from their edges, so moving down the
 * widget reads as a wave. Growth is capped so a card never passes the widget's side padding.
 *
 * Motion is integrated per frame rather than by CSS transitions: a transition restarted on every pointer move resets
 * its velocity each time and stutters. Each section's scale follows its target exponentially (time constant
 * DOCK.follow growing, DOCK.settle shrinking), frame-rate independent, and stops when everything has settled.
 * Off under Motion Subtle/Off, Reduce motion and while resting translucent. */
const DOCK = { reach: 90, follow: 70, settle: 120, barReach: 46, barGrowth: 8 };
export function installDock(frame: HTMLElement, amount: () => number): void {
  let frameRequest = 0, pointerY: number | null = null, pointerX: number | null = null, last = 0;
  const current = new WeakMap<HTMLElement, number>();
  const quiet = () => document.body.classList.contains("rd-motion-off") || document.body.classList.contains("rd-motion-subtle")
    || matchMedia("(prefers-reduced-motion: reduce)").matches || frame.classList.contains("is-idle");
  const target = (section: HTMLElement, hostBox: DOMRect | undefined): number => {
    if (pointerY === null || quiet() || amount() <= 0) return 1;
    const box = section.getBoundingClientRect();
    // Inside the card: full growth; outside: falls off with the distance to its nearer edge. The rect includes the
    // current scale, so the unscaled height is used for the edges.
    const scale = current.get(section) ?? 1, middle = box.top + box.height / 2, half = box.height / scale / 2;
    const gap = Math.max(0, Math.abs(pointerY - middle) - half);
    const t = Math.min(1, gap / DOCK.reach);
    const width = box.width / scale;
    const room = hostBox && width ? Math.max(0, (Math.min(box.left + (box.width - width) / 2 - hostBox.left, hostBox.right - box.right + (box.width - width) / 2) - 2) * 2 / width) : 0;
    return 1 + Math.min(amount(), room) * (1 - t * t) ** 2;
  };
  const step = (now: number) => {
    frameRequest = 0;
    const dt = Math.min(64, last ? now - last : 16);
    last = now;
    const hostBox = frame.querySelector<HTMLElement>(".rd-mini-host")?.getBoundingClientRect();
    const peak = Math.max(0.0001, amount());
    let moving = false;
    for (const section of Array.from(frame.querySelectorAll<HTMLElement>(".rd-mini-section"))) {
      const goal = target(section, hostBox), from = current.get(section) ?? 1;
      const tau = goal > from ? DOCK.follow : DOCK.settle;
      let value = from + (goal - from) * (1 - Math.exp(-dt / tau));
      if (Math.abs(goal - value) < 0.0004) value = goal; else moving = true;
      current.set(section, value);
      if (value === 1) { section.style.removeProperty("scale"); section.style.removeProperty("--rd-lift"); section.classList.remove("is-lifted"); }
      else {
        section.style.scale = value.toFixed(4);
        section.style.setProperty("--rd-lift", Math.min(1, (value - 1) / peak).toFixed(3));
        section.classList.add("is-lifted");
      }
    }
    /* Progress bars dock horizontally: growth (8x the module amount, 40% at the default 5%) falls off with the
     * horizontal distance from the pointer, only while the pointer is over their row. Sizes change in layout, so the
     * neighbours move aside. */
    for (const chart of Array.from(frame.querySelectorAll<HTMLElement>(".rd-mini-bars"))) {
      const box = chart.getBoundingClientRect();
      const over = pointerX !== null && pointerY !== null && !quiet() && amount() > 0
        && pointerY >= box.top - 6 && pointerY <= box.bottom + 6 && pointerX >= box.left - 12 && pointerX <= box.right + 12;
      for (const bar of Array.from(chart.querySelectorAll<HTMLElement>(".rd-mini-bar"))) {
        const rect = bar.getBoundingClientRect(), centre = rect.left + rect.width / 2;
        const d = over ? Math.min(1, Math.abs(pointerX! - centre) / DOCK.barReach) : 1;
        const goal = 1 + amount() * DOCK.barGrowth * (1 - d * d) ** 2, from = current.get(bar) ?? 1;
        let value = from + (goal - from) * (1 - Math.exp(-dt / (goal > from ? DOCK.follow : DOCK.settle)));
        if (Math.abs(goal - value) < 0.002) value = goal; else moving = true;
        current.set(bar, value);
        if (value === 1) bar.style.removeProperty("--rd-bar-m"); else bar.style.setProperty("--rd-bar-m", value.toFixed(3));
      }
    }
    if (moving) frameRequest = requestAnimationFrame(step); else last = 0;
  };
  const schedule = () => { if (!frameRequest) frameRequest = requestAnimationFrame(step); };
  frame.addEventListener("pointermove", event => { pointerY = event.clientY; pointerX = event.clientX; schedule(); }, { passive: true });
  frame.addEventListener("pointerleave", () => { pointerY = null; pointerX = null; schedule(); });
  frame.addEventListener("scroll", schedule, { capture: true, passive: true });
  // Going idle (or a settings change) must also let the sections settle back.
  new MutationObserver(schedule).observe(frame, { attributes: true, attributeFilter: ["class"] });
}

/* A folder dropped on the widget opens the plan card in the dashboard (where an agent can design the plan). */
function installFolderDrop(frame: HTMLElement): void {
  let veil: HTMLElement | null = null, check = 0;
  const hide = () => { check++; veil?.remove(); veil = null; };
  void getCurrentWebview().onDragDropEvent(async event => {
    const payload = event.payload;
    if (payload.type === "enter") {
      hide();
      veil = h(frame, "div", { cls: "rd-widget-drop", attr: { "aria-hidden": "true" } });
      const title = h(veil, "strong", { text: "Drop to connect" }), caption = h(veil, "span", { text: "Opens the plan in the dashboard" });
      const mine = ++check;
      const kind = payload.paths.length === 1 ? await api.pathKind(payload.paths[0]).catch(() => "missing") : "many";
      if (mine !== check || !veil || kind === "dir") return;
      veil.classList.add("is-rejecting");
      title.textContent = kind === "many" ? "One folder at a time" : "Folders only"; caption.textContent = "The dashboard monitors project folders.";
    } else if (payload.type === "leave") hide();
    else if (payload.type === "drop") {
      hide();
      if (payload.paths.length === 1 && await api.pathKind(payload.paths[0]).catch(() => "missing") === "dir") await api.connectInMain(payload.paths[0]).catch(() => {});
    }
  }).catch(() => { /* No native drop: the header's Connect button remains. */ });
}
