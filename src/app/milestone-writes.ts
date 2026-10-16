/* Milestone writes shared by the main window and the widget: validated against the latest snapshot and written by the
 * Rust side with compare-and-swap on milestones.md, so two windows editing at once never overwrite each other. */

import { appendMilestone, deleteMilestoneContent, editMilestoneContent, milestoneTask, paperBinding, setMilestoneContent, type EditMilestoneRequest,
  type PaperBinding, type SetMilestoneRequest } from "../model/milestones";
import type { PaperDetail, Project, Source } from "../model/types";
import { api } from "./api";
import type { Store } from "./store";

export function paperOf(source: Source | undefined | null): PaperDetail | null {
  const paper = (source?.state?.details || []).find(detail => detail?.kind === "paper" && detail.schemaVersion === 1) as PaperDetail | undefined;
  return paper && Array.isArray(paper.chapters) ? paper : null;
}

function project(store: Store, id: string): Project {
  const found = store.model().projects.find(item => item.id === id);
  if (!found) throw new Error("This project is no longer in the workspace. Refresh and try again.");
  return found;
}

/** Check or uncheck one milestone line; a chapter milestone is accepted against the chapter's current source hash. */
export async function setMilestoneIn(store: Store, projectId: string, request: SetMilestoneRequest): Promise<void> {
  await store.exclusive(async () => {
    const binding = paperBinding({ sourceId: request.sourceId || undefined, chapterId: request.chapterId || undefined });
    if (binding.sourceId) {
      const chapter = paperOf(project(store, projectId).sources.find(item => item.id === binding.sourceId))?.chapters.find(item => item.id === binding.chapterId);
      if (!chapter || (request.completed && chapter.sourceHash !== request.sourceHash)) throw new Error("This chapter changed. Refresh its data before accepting the milestone.");
    }
    const original = store.milestonesText(projectId);
    if (original == null) throw new Error("The milestone list is unavailable. Refresh and retry.");
    await api.milestonesWrite(projectId, setMilestoneContent(original, request), original);
  }).finally(() => store.reload());
}

/** Append one milestone task (optionally due and bound to a paper chapter). */
export async function addMilestoneTo(store: Store, projectId: string, title: string, due: string | null = null, binding: PaperBinding = {}): Promise<void> {
  await store.exclusive(async () => {
    const task = milestoneTask(title, due, binding);
    project(store, projectId);
    const original = store.milestonesText(projectId);
    await api.milestonesWrite(projectId, appendMilestone(original ?? "# Milestones\n", task), original);
    store.rememberAdded(projectId, task.replace(/^- \[ \] /, ""));
  }).finally(() => store.reload());
}

/** Rename a milestone or change its due date. */
export async function editMilestoneIn(store: Store, projectId: string, request: EditMilestoneRequest): Promise<void> {
  await store.exclusive(async () => {
    project(store, projectId);
    const original = store.milestonesText(projectId);
    if (original == null) throw new Error("The milestone list is unavailable. Refresh and retry.");
    await api.milestonesWrite(projectId, editMilestoneContent(original, request), original);
  }).finally(() => store.reload());
}

/** Delete one milestone; the returned function puts the file back as it was (unless it changed again since). */
export async function deleteMilestoneIn(store: Store, projectId: string, request: { line: number; text: string }): Promise<() => Promise<void>> {
  let before = "", after = "";
  await store.exclusive(async () => {
    project(store, projectId);
    const original = store.milestonesText(projectId);
    if (original == null) throw new Error("The milestone list is unavailable. Refresh and retry.");
    before = original; after = deleteMilestoneContent(original, request);
    await api.milestonesWrite(projectId, after, original);
  }).finally(() => store.reload());
  return async () => {
    await store.exclusive(() => api.milestonesWrite(projectId, before, after)).catch(() => { throw new Error("The milestones changed after the deletion, so it was not undone."); })
      .finally(() => store.reload());
  };
}
