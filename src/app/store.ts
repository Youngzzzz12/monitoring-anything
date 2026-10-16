/* Per-window store: the latest workspace snapshot, live-update status, busy/error state and settings. Views subscribe
 * and rebuild their model from the snapshot on every change (cheap: a few projects). */

import { buildModel, StateCache } from "../model/load";
import type { Model, WorkspaceInfo, WorkspaceSnapshot } from "../model/types";
import { api, events, type AppSettings, type WatchStatus } from "./api";
import { errorMessage } from "../model/rules";

type Listener = () => void;

export class Store {
  snapshot: WorkspaceSnapshot | null = null;
  workspace: WorkspaceInfo | null = null;
  settings: AppSettings | null = null;
  watch: WatchStatus = { running: false, stopping: false, error: null, startedAt: null };
  busy = false;
  error: string | null = null;
  /** Milestones added from this window recently (newest first): Next up shows them at the top for a while. */
  recentAdds: { projectId: string; text: string; at: number }[] = [];
  private cache = new StateCache();
  private listeners = new Set<Listener>();
  private loading: Promise<void> | null = null;
  private reloadTimer: number | null = null;

  async init(): Promise<void> {
    [this.settings, this.workspace, this.watch] = await Promise.all([api.settings(), api.workspaceCurrent(), api.watchStatus()]);
    if (this.workspace) await this.reload();
    void events.workspaceChanged(() => this.scheduleReload());
    void events.collectorEvent(() => this.scheduleReload(50));
    void events.collectorStatus(status => { this.watch = status; this.notify(); });
    void events.settingsChanged(settings => { this.settings = settings; this.notify(); });
    void events.workspaceOpened(info => { this.workspace = info; this.cache = new StateCache(); void this.reload(); });
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  notify(): void { for (const listener of Array.from(this.listeners)) { try { listener(); } catch (error) { console.error(error); } } }

  scheduleReload(delay = 150): void {
    if (this.reloadTimer !== null) window.clearTimeout(this.reloadTimer);
    this.reloadTimer = window.setTimeout(() => { this.reloadTimer = null; void this.reload(); }, delay);
  }

  /** Re-read the workspace; concurrent calls share one read. */
  reload(): Promise<void> {
    if (!this.workspace) return Promise.resolve();
    if (this.loading) return this.loading.then(() => this.reload());
    this.loading = (async () => {
      try { this.snapshot = await api.snapshot(); }
      catch (error) { this.error = errorMessage(error); }
      finally { this.loading = null; }
      this.notify();
    })();
    return this.loading;
  }

  model(): Model {
    if (!this.snapshot) return { projects: [], errors: [] };
    return buildModel(this.snapshot, this.cache);
  }

  /** Remember a just-added milestone (its task text without the checkbox) for 12 hours. */
  rememberAdded(projectId: string, text: string): void {
    const now = Date.now();
    this.recentAdds = [{ projectId, text, at: now }, ...this.recentAdds.filter(item => now - item.at < 12 * 3600 * 1000 && !(item.projectId === projectId && item.text === text))].slice(0, 10);
  }

  recentMilestones(): { projectId: string; text: string }[] {
    const now = Date.now();
    return this.recentAdds.filter(item => now - item.at < 12 * 3600 * 1000).map(({ projectId, text }) => ({ projectId, text }));
  }

  milestonesText(projectId: string): string | null {
    return this.snapshot?.projects.find(project => project.id === projectId)?.milestones ?? null;
  }

  /** Serialize user actions (like the plugin's exclusive()): one write at a time, errors kept for the status line. */
  async exclusive<T>(action: () => Promise<T>): Promise<T> {
    if (this.busy) throw new Error("Wait for the current Dashboard action to finish.");
    this.busy = true; this.error = null; this.notify();
    try { return await action(); }
    catch (error) { this.error = errorMessage(error); throw error; }
    finally { this.busy = false; this.notify(); }
  }
}
