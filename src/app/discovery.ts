/* Sub-project discovery (ported from the plugin's Connector): each top-level project may name a discovery folder; its
 * direct subfolders that are not yet sources are scored by the collector (`discover`, read-only) and shown as
 * suggestions in Needs attention. Suggestions live in memory; dismissals are written by the collector. Background
 * scans run shortly after the workspace opens and at most every ten minutes while live updates collect; a project's
 * "Auto-accept" connects confident standalone matches without asking. */

import { normalizeSuggestion, type Suggestion } from "../model/cli";
import { errorMessage, isSafeId } from "../model/rules";
import { notice } from "../ui/notice";
import { api } from "./api";
import type { Store } from "./store";

export const DISCOVERY = { openDelay: 1500, minGap: 60 * 1000, collectGap: 10 * 60 * 1000, autoConfidence: 0.8 };

export interface DiscoverySettings { configured: boolean; available: boolean; folder: string; autoAccept: boolean; ignore: string[] }

interface AcceptHooks {
  /** Pause live updates around a collector write (one writer at a time). */
  withPausedWatcher<T>(action: () => Promise<T>): Promise<T>;
  dialogOpen(): boolean;
}

export class Discovery {
  suggestions: Suggestion[] = [];
  dismissedBy: Record<string, number> = {};
  scanning = false;
  error: string | null = null;
  private checkedAt = 0;
  private timer: number | null = null;
  private forceNext = false;
  private run: Promise<unknown> | null = null;
  private autoFailed = new Set<string>();

  constructor(private store: Store, private hooks: AcceptHooks) {}

  /** Top-level projects whose record names a discovery folder. */
  projects(): { projectId: string; title: string; autoAccept: boolean }[] {
    return this.store.model().projects.filter(project => !project.parent).flatMap(project => {
      const discovery = (project.catalog as Record<string, unknown> | null)?.discovery as Record<string, unknown> | undefined;
      return typeof discovery?.rootAlias === "string" ? [{ projectId: project.id, title: project.title, autoAccept: discovery.autoAccept === true }] : [];
    });
  }

  private set(suggestions: Suggestion[], dismissedBy: Record<string, number> | null = null): void {
    const before = JSON.stringify([this.suggestions, this.dismissedBy]);
    this.suggestions = suggestions;
    if (dismissedBy) this.dismissedBy = dismissedBy;
    if (JSON.stringify([this.suggestions, this.dismissedBy]) !== before) this.store.notify();
  }

  /** Debounced background scan; repeated requests within the delay collapse into one. */
  request(options: { delay?: number; force?: boolean } = {}): void {
    if (this.store.settings?.discoveryScan === false && !options.force) return;
    const delay = Number.isFinite(options.delay) ? options.delay! : DISCOVERY.openDelay;
    const force = Boolean(options.force) || this.forceNext;
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.forceNext = force;
    this.timer = window.setTimeout(() => {
      this.timer = null; this.forceNext = false;
      void this.scan(null, { quiet: true, force }).catch(() => null);
    }, delay);
  }

  /** After a live-update collection cycle: scan at most once per collectGap. */
  afterCollection(now = Date.now()): void {
    if (this.store.settings?.discoveryScan === false || this.timer !== null || this.run) return;
    if (now - this.checkedAt < DISCOVERY.collectGap) return;
    this.request({ delay: 0, force: true });
  }

  /** Scan every discovery folder, or one project's ({ quiet: false } reports the result). */
  async scan(projectId: string | null = null, options: { quiet?: boolean; force?: boolean } = {}): Promise<unknown> {
    const quiet = options.quiet !== false;
    if (projectId != null && !isSafeId(projectId)) throw new Error("Choose a valid project.");
    if (this.run) {
      if (quiet) return this.run;
      await this.run.catch(() => null);
    }
    if (quiet && !options.force && Date.now() - this.checkedAt < DISCOVERY.minGap) return null;
    const run = this.scanOnce(projectId, quiet);
    this.run = run;
    try { return await run; }
    finally { if (this.run === run) this.run = null; }
  }

  private async scanOnce(projectId: string | null, quiet: boolean): Promise<unknown> {
    if (!this.store.workspace) return null;
    const configured = this.projects();
    this.checkedAt = Date.now();
    if (projectId ? !configured.some(item => item.projectId === projectId) : !configured.length) {
      if (projectId && !quiet) throw new Error("Set a discovery folder for this project in Edit project first.");
      if (!projectId) this.set([], {});
      return null;
    }
    if (!quiet) { this.scanning = true; this.store.notify(); }
    let result: { suggestions?: unknown[]; projects?: { projectId?: string; dismissed?: number }[] };
    try { result = await api.collector("discover", projectId ? ["--project", projectId] : ["--all"], null, 120); }
    catch (error) {
      this.error = errorMessage(error);
      if (!quiet) throw error;
      return null;
    } finally {
      if (!quiet) { this.scanning = false; this.store.notify(); }
    }
    const incoming = (Array.isArray(result?.suggestions) ? result.suggestions : []).map(normalizeSuggestion).filter(Boolean) as Suggestion[];
    const live = new Set(configured.map(item => item.projectId));
    const dismissedBy: Record<string, number> = projectId ? { ...this.dismissedBy } : {};
    for (const item of Array.isArray(result?.projects) ? result.projects : []) if (isSafeId(item?.projectId)) dismissedBy[item.projectId] = Number(item.dismissed) || 0;
    const kept = projectId ? this.suggestions.filter(item => item.projectId !== projectId && live.has(item.projectId)) : [];
    this.error = null;
    this.set([...kept, ...incoming], dismissedBy);
    if (!quiet) {
      const title = configured.find(item => item.projectId === projectId)?.title || projectId;
      notice(incoming.length ? `Found ${incoming.length} new folder${incoming.length === 1 ? "" : "s"} for ${title}. See Needs attention.` : `No new folders for ${title}.`);
    }
    await this.autoAccept();
    return result;
  }

  /** Suggestions the per-project Auto-accept setting lets the app connect without asking. */
  private autoAcceptable(): Suggestion[] {
    return this.suggestions.filter(item => item.autoAccept && item.bestCandidate && item.bestCandidate.subprojectRole !== "part"
      && item.bestCandidate.confidence >= DISCOVERY.autoConfidence && !this.autoFailed.has(item.id));
  }

  private async autoAccept(): Promise<void> {
    for (const suggestion of this.autoAcceptable()) {
      if (this.store.busy || this.hooks.dialogOpen()) return;
      try { await this.accept(suggestion); }
      catch (error) {
        this.autoFailed.add(suggestion.id);
        notice(`${suggestion.relative} was not added automatically: ${errorMessage(error)} It stays in Needs attention.`, 10000, "error");
      }
    }
  }

  private async accept(suggestion: Suggestion): Promise<void> {
    const result = await this.store.exclusive(() => this.hooks.withPausedWatcher(() => api.collector<{ connect?: { milestones?: { addedCount?: number }; collectionHealth?: string; collectionError?: string } }>("discover",
      ["accept", "--project", suggestion.projectId, "--folder", suggestion.relative, "--min-confidence", String(DISCOVERY.autoConfidence)], null, 600)));
    this.set(this.suggestions.filter(item => item.id !== suggestion.id));
    await this.store.reload();
    const connected = result?.connect || {};
    const parent = this.store.model().projects.find(item => item.id === suggestion.parentId)?.title || suggestion.projectTitle || suggestion.parentId;
    const added = connected.milestones?.addedCount ? ` with ${connected.milestones.addedCount} milestone${connected.milestones.addedCount === 1 ? "" : "s"}` : "";
    notice(`Added ${suggestion.suggestedTitle} as a sub-project of ${parent}${added} (auto-accept).`, 10000);
    if (connected.collectionHealth === "error") notice(`${suggestion.suggestedTitle}: the first collection needs attention. ${connected.collectionError || ""}`.trim(), 10000, "error");
  }

  find(id: string): Suggestion | undefined { return this.suggestions.find(item => item.id === id); }

  /** A folder became a source (or vanished): drop its suggestion at once and let a rescan settle the rest. */
  forget(predicate: (item: Suggestion) => boolean): void {
    const remaining = this.suggestions.filter(item => !predicate(item));
    if (remaining.length !== this.suggestions.length) { this.set(remaining); this.request({ delay: 0, force: true }); }
  }

  async dismiss(id: string): Promise<void> {
    const suggestion = this.find(id);
    if (!suggestion) return;
    await api.collector("discover", ["dismiss", "--project", suggestion.projectId, "--folder", suggestion.relative]);
    this.set(this.suggestions.filter(item => item.id !== id), { ...this.dismissedBy, [suggestion.projectId]: (Number(this.dismissedBy[suggestion.projectId]) || 0) + 1 });
  }

  /** A project's discovery settings for Edit project (`discover show`: the folder comes from this computer's mapping). */
  async read(projectId: string): Promise<DiscoverySettings> {
    const result = await api.collector<Record<string, unknown>>("discover", ["show", "--project", projectId]);
    return { configured: result?.configured === true, available: result?.available === true, folder: typeof result?.folder === "string" ? result.folder : "",
      autoAccept: result?.autoAccept === true, ignore: Array.isArray(result?.ignore) ? (result.ignore as unknown[]).filter(item => typeof item === "string") as string[] : [] };
  }

  /** Write discovery settings through `discover configure` (the folder stays in this computer's mapping file). */
  async save(projectId: string, changes: { clear?: boolean; folder?: string; autoAccept?: boolean; ignore?: string[] }): Promise<void> {
    if (!isSafeId(projectId)) throw new Error("Choose a valid project.");
    const args = ["configure", "--project", projectId];
    if (changes.clear) args.push("--clear");
    else {
      if (changes.folder) args.push("--folder", changes.folder);
      args.push("--auto-accept", changes.autoAccept ? "on" : "off");
      if (changes.ignore?.length) for (const glob of changes.ignore) args.push("--ignore", glob);
      else args.push("--clear-ignore");
    }
    await api.collector("discover", args);
    if (changes.clear) {
      const dismissedBy = { ...this.dismissedBy }; delete dismissedBy[projectId];
      this.set(this.suggestions.filter(item => item.projectId !== projectId), dismissedBy);
    }
    this.autoFailed.clear();
  }

  reset(): void { this.set([], {}); this.checkedAt = 0; this.autoFailed.clear(); }
}
