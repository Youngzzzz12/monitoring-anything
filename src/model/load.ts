/* Workspace snapshot → dashboard model. Ported from the reference view's load(): validation rules, retained state on
 * read errors, ownership checks, priority order and one-level hierarchy resolution are unchanged; Obsidian/Dataview
 * lookups are replaced by the app workspace files. */

import { parseMilestones } from "./milestones";
import { isSafeId, normalizeParentId, PROJECT_TYPES, type HierarchyItem } from "./rules";
import {
  projectLink, type Health, type Model, type ModelIssue, type Project, type ProjectPage, type ProjectRecord, type Source,
  type SourceConfig, type SourceState, type WorkspaceSnapshot,
} from "./types";

export const kindNames: Record<string, string> = { latex: "LaTeX", abaqus: "Abaqus", isale: "iSALE" };
const builtinProfileIds = new Set(["latex-paper", "abaqus-job", "isale-batch"]);

/** Survives reloads: the last valid snapshot per source, shown (marked as retained) when a read fails. */
export class StateCache {
  states = new Map<string, SourceState>();
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function text(value: unknown, fallback = ""): string {
  return value == null || value === "" ? fallback : String(value);
}

function milliseconds(value: unknown): number | null {
  if (value == null || value === "") return null;
  const result = Date.parse(String(value));
  return Number.isFinite(result) ? result : null;
}

export function health(state: SourceState | null, readError: string | null, now = Date.now()): Health {
  const collection = state?.collection;
  const success = milliseconds(collection?.lastSuccessfulAt);
  const threshold = typeof collection?.staleAfterSeconds === "number" && Number.isFinite(collection.staleAfterSeconds) ? collection.staleAfterSeconds : null;
  const isStale = success !== null && threshold !== null && threshold > 0 && now - success > threshold * 1000;
  if (readError || collection?.health === "error") return { state: "error", isStale };
  if (collection?.health === "unconfigured" || !state) return { state: "unconfigured", isStale: false };
  if (success === null || threshold === null || threshold <= 0) return { state: "error", isStale: false };
  return { state: isStale ? "stale" : "fresh", isStale };
}

function humanizeId(value: string): string {
  const words = String(value).replace(/[-_]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Monitoring profile";
}

/** A declarative source stores a borrowed kind (latex/abaqus/isale) for the snapshot contract; its profile names it. */
export function sourceDisplay(config: SourceConfig | null, profileTitles: Map<string, string>): Source["display"] {
  const adapter = config?.adapter as Record<string, unknown> | undefined;
  const profileId = typeof config?.profileId === "string" ? config.profileId : typeof adapter?.profileId === "string" ? adapter.profileId : null;
  const custom = adapter?.engine === "declarative" || Boolean(profileId && !builtinProfileIds.has(profileId));
  if (!custom) return null;
  const title = (profileId && profileTitles.get(profileId)) || (profileId ? humanizeId(profileId) : "Monitoring profile");
  return { label: "Custom · " + title, title, profileId };
}

function absoluteLocal(path: string): boolean {
  const normalized = path.replace(/\\/g, "/");
  return /^[A-Za-z]:\//.test(normalized) || normalized.startsWith("/");
}

export function sourceDirectory(config: SourceConfig | null, localMappings: Map<string, { aliases: Record<string, unknown> }>): string | null {
  const source = config?.source;
  if (!source || typeof source.path !== "string") return null;
  if (!source.rootAlias) return absoluteLocal(source.path) ? source.path : null;
  const local = localMappings.get(String(config?.collector?.hostId || ""));
  const rootPath = local?.aliases?.[source.rootAlias];
  if (typeof rootPath !== "string" || !absoluteLocal(rootPath) || source.path.split(/[\\/]/).includes("..")) return null;
  const separator = rootPath.includes("\\") && !rootPath.includes("/") ? "\\" : "/";
  const base = rootPath.replace(/[\\/]+$/, "");
  return source.path ? base + separator + source.path.replace(/^[\\/]+/, "").split(/[\\/]/).join(separator) : base;
}

export function projectType(page: ProjectPage, kinds: string[]): string {
  const declared = String(page.project_type ?? "").trim().toLowerCase();
  if (PROJECT_TYPES[declared]) return declared;
  if (kinds.includes("latex")) return "paper";
  if (kinds.includes("abaqus") || kinds.includes("isale")) return "simulation";
  return "other";
}

export function projectStatus(project: Project): string {
  return text(project.page?.project_health, "active");
}

function pageOf(record: ProjectRecord): ProjectPage {
  return {
    title: text(record.title, record.projectId),
    project_type: text(record.type, ""),
    parent_id: normalizeParentId(record.parentId ?? null),
    stage: text(record.stage, "planned"),
    project_health: text(record.health, "active"),
    next_step: record.nextStep == null || record.nextStep === "" ? null : String(record.nextStep),
    due: record.due == null || record.due === "" ? null : String(record.due),
    archived: record.archived === true,
    resources: Array.isArray(record.resources) ? record.resources : [],
    knowledge: Array.isArray(record.knowledge) ? record.knowledge : [],
  };
}

function validRecord(id: string, value: unknown): ProjectRecord {
  if (!isObject(value)) throw new Error("project.json must be a JSON object");
  if (value.schemaVersion !== "1.0") throw new Error("Unsupported project.json schemaVersion");
  if (value.projectId !== id) throw new Error("project.json projectId does not match its folder");
  if (typeof value.title !== "string" || !value.title.trim()) throw new Error("project.json needs a title");
  const sources = value.sourceIds;
  if (!Array.isArray(sources) || sources.some(item => !isSafeId(item)) || new Set(sources).size !== sources.length) {
    throw new Error("Invalid project source membership (sourceIds)");
  }
  return value as unknown as ProjectRecord;
}

/** One displayed level: a project whose parent names a live project hangs under that chain's top-most ancestor.
 * Self-parents and loops stay top-level and surface as configuration issues. */
export function resolveHierarchy(projects: Project[]): ModelIssue[] {
  const byId = new Map(projects.map(project => [project.id, project])), issues: ModelIssue[] = [];
  for (const project of projects) { project.type = projectType(project.page, project.kinds); project.parent = null; project.children = []; }
  for (const project of projects) {
    if (!project.parentId) continue;
    let cursor = project, cycle = false;
    const visited = new Set([project.id]);
    while (cursor.parentId && byId.has(cursor.parentId)) {
      if (visited.has(cursor.parentId)) { cycle = true; break; }
      visited.add(cursor.parentId); cursor = byId.get(cursor.parentId)!;
    }
    if (cycle) { issues.push({ path: `projects/${project.id}/project.json`, message: (project.parentId === project.id ? "Project lists itself as its parent: " : "Parent projects form a loop: ") + project.title }); continue; }
    if (cursor !== project) { project.parent = cursor; cursor.children.push(project); }
  }
  return issues;
}

const priority: Record<string, number> = { blocked: 0, active: 1, paused: 2, unknown: 3, completed: 4 };

export function buildModel(snapshot: WorkspaceSnapshot, cache: StateCache = new StateCache(), now = Date.now()): Model {
  const errors: ModelIssue[] = snapshot.errors.map(message => ({ path: "", message }));
  const profileTitles = new Map(snapshot.profiles.map(item => [item.id, item.title]));
  const localMappings = new Map<string, { aliases: Record<string, unknown> }>();
  for (const entry of snapshot.local) {
    const value = entry.value;
    if (isObject(value) && value.schemaVersion === "1.0" && isObject(value.aliases)) localMappings.set(entry.id, value as { aliases: Record<string, unknown> });
  }
  const configs = new Map<string, SourceConfig>();
  for (const entry of snapshot.sources) {
    const path = `sources/${entry.id}.json`;
    try {
      if (entry.error) throw new Error(entry.error);
      const item = entry.value;
      if (!isObject(item)) throw new Error("Expected a JSON object");
      if (!isSafeId(entry.id) || item.projectId !== entry.id) throw new Error("Source filename does not match its projectId");
      if (item.schemaVersion !== "1.0" || !kindNames[String(item.kind)]) throw new Error("Invalid source schema or adapter kind");
      if (item.dashboardProjectId != null && !isSafeId(item.dashboardProjectId)) throw new Error("Invalid source project ownership");
      configs.set(entry.id, item as unknown as SourceConfig);
    } catch (error) { errors.push({ path, message: String((error as Error).message || error) }); }
  }
  const records = new Map<string, { record: ProjectRecord; milestones: string | null; milestonesError: string | null }>();
  for (const entry of snapshot.projects) {
    const path = `projects/${entry.id}/project.json`;
    try {
      if (entry.recordError) throw new Error(entry.recordError);
      records.set(entry.id, { record: validRecord(entry.id, entry.record), milestones: entry.milestones, milestonesError: entry.milestonesError });
    } catch (error) { errors.push({ path, message: String((error as Error).message || error) }); }
  }
  const claimedIds = new Set(Array.from(records.values()).flatMap(item => item.record.sourceIds));
  const stateEntries = new Map(snapshot.states.map(entry => [entry.id, entry]));
  const sourceIds = new Set([...configs.keys(), ...claimedIds]);
  for (const id of cache.states.keys()) if (!sourceIds.has(id)) cache.states.delete(id);
  const sources = new Map<string, Source>();
  for (const id of sourceIds) {
    const config = configs.get(id) || null;
    let state: SourceState | null = null, readError: string | null = null;
    try {
      const entry = stateEntries.get(id);
      if (entry?.error) throw new Error(entry.error);
      const value = entry?.value;
      if (value == null && cache.states.has(id)) throw new Error("State file is unavailable; showing the last successful read.");
      if (value != null) {
        if (!isObject(value) || value.schemaVersion !== "1.0" || value.projectId !== id || !isObject(value.collection) || !isObject(value.runtime) || !isObject(value.cases)) {
          throw new Error("State schema or source ID mismatch");
        }
        state = value as unknown as SourceState;
        cache.states.set(id, state);
      }
    } catch (error) { readError = String((error as Error).message || error); state = cache.states.get(id) || null; }
    if (!config && claimedIds.has(id)) {
      readError = "Source configuration is unavailable: " + id + (readError ? " · " + readError : "");
      errors.push({ path: `sources/${id}.json`, message: "A project refers to an unregistered source: " + id });
    }
    sources.set(id, {
      id, config, state, readError, health: health(state, readError, now),
      title: text(config?.title || state?.title || id), kind: text(config?.kind || state?.kind, "unknown"),
      display: sourceDisplay(config, profileTitles), directory: sourceDirectory(config, localMappings),
    });
  }
  const projects: Project[] = [];
  const claimedSources = new Set<string>();
  for (const [id, { record, milestones, milestonesError }] of Array.from(records).sort(([a], [b]) => a.localeCompare(b, "en-GB"))) {
    const grouped: Source[] = [];
    for (const sourceId of record.sourceIds) {
      if (claimedSources.has(sourceId)) { errors.push({ path: `projects/${id}/project.json`, message: "Source belongs to more than one project: " + sourceId }); continue; }
      claimedSources.add(sourceId);
      const source = sources.get(sourceId);
      if (!source) continue;
      if (source.config?.dashboardProjectId && source.config.dashboardProjectId !== id) {
        source.readError = "Source ownership does not match its project";
        source.health = health(source.state, source.readError, now);
        errors.push({ path: `sources/${sourceId}.json`, message: source.readError + ": " + sourceId });
      }
      grouped.push(source);
    }
    const page = pageOf(record);
    if (page.archived) continue;
    const note = `projects/${id}/milestones.md`;
    const project: Project = {
      id, catalog: record, page, sources: grouped, title: page.title, note, dashboard: projectLink(id),
      kinds: [...new Set(grouped.map(source => source.kind))], parentId: page.parent_id || "", type: "other",
      parent: null, children: [], rawMilestones: [], milestoneText: milestones,
    };
    if (milestonesError) project.milestoneReadError = milestonesError;
    else if (milestones != null) project.rawMilestones = parseMilestones(milestones, note);
    projects.push(project);
  }
  for (const [id, source] of sources) {
    if (claimedSources.has(id)) continue;
    errors.push({ path: `sources/${id}.json`, message: source.config?.dashboardProjectId ? "Source is detached from its dashboard project: " + id : "Source has no project: " + id });
  }
  projects.sort((a, b) => (priority[projectStatus(a)] ?? 3) - (priority[projectStatus(b)] ?? 3) || a.title.localeCompare(b.title, "en-GB"));
  errors.push(...resolveHierarchy(projects));
  return { projects, errors };
}

/** Live projects with their type and parent, for the Edit / New project forms (one-level rules). */
export function hierarchyItems(model: Model): HierarchyItem[] {
  return model.projects.map(project => ({ projectId: project.id, title: project.title, parentId: normalizeParentId(project.page.parent_id), projectType: project.type }))
    .sort((a, b) => a.title.localeCompare(b.title) || a.projectId.localeCompare(b.projectId));
}
