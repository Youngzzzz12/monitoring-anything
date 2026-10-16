/* Project identity, hierarchy and record rules. Ported from the Obsidian plugin's helpers.js so the app enforces
 * exactly the same contract as the collector (one displayed hierarchy level, safe IDs, supported values). */

const RESERVED_IDS = new Set([
  "con", "prn", "aux", "nul",
  ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`),
]);

export function isSafeId(value: unknown): value is string {
  return typeof value === "string" && /^[a-z0-9][a-z0-9_-]{0,63}$/.test(value) && !RESERVED_IDS.has(value);
}

export function slugId(value: string): string {
  let id = String(value || "project").normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^[-_]+|[-_]+$/g, "").slice(0, 64);
  if (!id) id = "project";
  if (RESERVED_IDS.has(id)) id = `project-${id}`;
  return id;
}

export function uniqueProjectId(title: string, taken: Iterable<string> = []): string {
  const used = new Set(taken);
  const base = slugId(title);
  let candidate = base;
  for (let counter = 2; used.has(candidate); counter++) {
    const suffix = `-${counter}`;
    candidate = base.slice(0, 64 - suffix.length).replace(/[-_]+$/, "") + suffix;
  }
  return candidate;
}

export function errorMessage(error: unknown): string {
  if (error && typeof error === "object" && "message" in error) return String((error as { message: unknown }).message);
  return String(error);
}

export const PROJECT_STAGES: Record<string, string> = {
  planned: "Planned", preparation: "Preparation", modeling: "Modelling", writing: "Writing",
  computation: "Computation", postprocessing: "Post-processing", validation: "Validation",
  review: "Review", completed: "Completed", archived: "Archived",
};
export const PROJECT_HEALTH: Record<string, string> = { active: "Active", blocked: "Blocked", paused: "Paused", completed: "Completed" };
export const PROJECT_TYPES: Record<string, string> = { thesis: "Thesis", paper: "Paper", simulation: "Simulation", experiment: "Experiment", other: "Other" };
export const PAPER_STAGES: Record<string, string> = { draft: "Draft", figures: "Figures", references: "References", review: "Review", complete: "Complete" };
export const MAX_PROJECT_TITLE = 200;

export interface HierarchyItem { projectId: string; title: string; parentId: string | null; projectType?: string }

/* Hierarchy has one displayed level: a sub-project cannot be a parent, and a project with sub-projects cannot be
 * given a parent. Empty values are top-level. */
export function normalizeParentId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text && text !== "null" && text !== "~" ? text : null;
}

export function projectHasChildren(projects: HierarchyItem[], projectId: string | null): boolean {
  return Boolean(projectId) && projects.some(item => item.projectId !== projectId && normalizeParentId(item.parentId) === projectId);
}

export function eligibleParents(projects: HierarchyItem[], projectId: string | null = null): HierarchyItem[] {
  if (projectHasChildren(projects, projectId)) return [];
  return projects.filter(item => item.projectId !== projectId && isSafeId(item.projectId) && !normalizeParentId(item.parentId))
    .sort((a, b) => String(a.title).localeCompare(String(b.title)) || a.projectId.localeCompare(b.projectId));
}

export function validateParent(projects: HierarchyItem[], projectId: string, value: unknown): string | null {
  const parentId = normalizeParentId(value);
  if (!parentId) return null;
  if (!isSafeId(parentId)) throw new Error("Choose a valid parent project.");
  if (parentId === projectId) throw new Error("A project cannot be its own parent.");
  if (projectHasChildren(projects, projectId)) throw new Error("This project has sub-projects, so it cannot be placed under another project.");
  const parent = projects.find(item => item.projectId === parentId);
  if (!parent) throw new Error("The selected parent project no longer exists. Choose another parent or None.");
  if (normalizeParentId(parent.parentId)) throw new Error(`${parent.title || parentId} is a sub-project. Choose a top-level project as parent.`);
  return parentId;
}

export function validateDue(value: unknown): string | null {
  const date = String(value ?? "").trim();
  if (!date) return null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error("Use a due date in YYYY-MM-DD format, or leave it empty.");
  const parsed = new Date(date + "T00:00:00Z");
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date) throw new Error("Enter a valid calendar date.");
  return date;
}

export interface NewProjectValues { id: string; title: string; projectType: string; parentId?: string | null }

export function validateTitle(value: unknown): string {
  const title = String(value ?? "").trim();
  if (!title || title.length > MAX_PROJECT_TITLE || /[\x00-\x1f\x7f]/.test(title)) {
    throw new Error(`Enter a project title on one line, up to ${MAX_PROJECT_TITLE} characters.`);
  }
  return title;
}

export function validateNewProject(values: NewProjectValues, projects: HierarchyItem[]): Required<NewProjectValues> {
  const title = validateTitle(values.title);
  if (!Object.hasOwn(PROJECT_TYPES, values.projectType)) throw new Error("Choose a supported project type.");
  const id = String(values.id || "").trim();
  if (!isSafeId(id)) throw new Error("Use a safe lowercase project ID, between 1 and 64 characters.");
  if (projects.some(item => item.projectId === id)) throw new Error("This project ID is already used. Choose another ID.");
  return { id, title, projectType: values.projectType, parentId: validateParent(projects, id, values.parentId) };
}

export interface EditValues { title?: string; stage: string; health: string; nextStep: string; due: string; type: string; parentId: string }

/* Edit project form → project.json patch. An unchanged parent is left as stored, so an old record never blocks edits. */
export function validateProjectValues(values: EditValues, hierarchy: { projects: HierarchyItem[]; projectId: string; originalParentId: string | null }): Record<string, unknown> {
  if (!Object.hasOwn(PROJECT_STAGES, values.stage)) throw new Error("Choose a supported project stage.");
  if (!Object.hasOwn(PROJECT_HEALTH, values.health)) throw new Error("Choose a supported project health.");
  const next = String(values.nextStep || "").trim();
  if (/[\r\n\x00-\x08\x0b\x0c\x0e-\x1f]/.test(next) || next.length > 500) throw new Error("Keep the next step on one line, up to 500 characters.");
  if (!Object.hasOwn(PROJECT_TYPES, values.type)) throw new Error("Choose a supported project type.");
  const patch: Record<string, unknown> = { stage: values.stage, health: values.health, nextStep: next || null, due: validateDue(values.due), type: values.type };
  if (values.title !== undefined) patch.title = validateTitle(values.title);
  if (normalizeParentId(values.parentId) !== normalizeParentId(hierarchy.originalParentId)) {
    patch.parentId = validateParent(hierarchy.projects, hierarchy.projectId, values.parentId);
  }
  return patch;
}
