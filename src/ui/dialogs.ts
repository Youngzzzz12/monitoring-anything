/* Edit project, New project, Add milestone and Remove project dialogs (ported from the Obsidian plugin's modals). */

import { eligibleParents, errorMessage, normalizeParentId, PAPER_STAGES, PROJECT_HEALTH, PROJECT_STAGES, PROJECT_TYPES,
  projectHasChildren, uniqueProjectId, type EditValues, type HierarchyItem } from "../model/rules";
import type { PaperBinding } from "../model/milestones";
import type { Chapter, Project } from "../model/types";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { button, buttons, dropdown, fillSelect, h, heading, Modal, setBusy, setting, textInput, toggle } from "./modal";

/** Sub-project discovery fields of Edit project: this computer's folder, Auto-accept and ignore globs (one per line). */
export interface DiscoveryEdit { folder: string; autoAccept: boolean; ignore: string }
export interface DiscoveryInfo { folder: string; autoAccept: boolean; ignore: string[]; available: boolean; configured: boolean }

const entries = (record: Record<string, string>): [string, string][] => Object.entries(record);

async function run(modal: Modal, error: HTMLElement, action: () => Promise<unknown>): Promise<boolean> {
  if (modal.busyLock) return false;
  error.textContent = ""; modal.busyLock = true; setBusy(modal.contentEl, true);
  try { await action(); modal.close(); return true; }
  catch (failure) { if (!modal.closed) error.textContent = errorMessage(failure); return false; }
  finally { modal.busyLock = false; if (!modal.closed) setBusy(modal.contentEl, false); }
}

export function editProjectDialog(project: Project, projects: HierarchyItem[], save: (values: EditValues, originalParentId: string | null, discovery: DiscoveryEdit | null) => Promise<unknown>,
  discoveryInfo: DiscoveryInfo | null = null): void {
  const modal = new Modal("Edit project", "rd-edit-project-modal");
  modal.description(project.title);
  const error = modal.errorBox();
  const originalParentId = normalizeParentId(project.page.parent_id);
  const values: EditValues = {
    title: project.title, stage: project.page.stage || "planned", health: project.page.project_health || "active",
    nextStep: project.page.next_step || "", due: project.page.due || "", type: project.page.project_type || project.type || "other",
    parentId: originalParentId || "",
  };
  textInput(setting(modal.contentEl, "Title"), values.title!, { maxLength: 200, onChange: value => { values.title = value; } });
  dropdown(setting(modal.contentEl, "Stage"), entries(PROJECT_STAGES), values.stage, value => { values.stage = value; });
  dropdown(setting(modal.contentEl, "Health"), entries(PROJECT_HEALTH), values.health, value => { values.health = value; });
  textInput(setting(modal.contentEl, "Next step", "One concrete action to take next."), values.nextStep, { maxLength: 500, onChange: value => { values.nextStep = value; } });
  textInput(setting(modal.contentEl, "Due date", "Optional; leave empty when no date is agreed."), values.due, { type: "date", onChange: value => { values.due = value; } });
  dropdown(setting(modal.contentEl, "Type"), entries(PROJECT_TYPES), values.type, value => { values.type = value; });
  const hasChildren = projectHasChildren(projects, project.id);
  const parents = eligibleParents(projects, project.id);
  const parentRow = setting(modal.contentEl, "Parent project", hasChildren ? "This project has sub-projects, so it stays a top-level project." : "Optional. Show this project under a top-level project.");
  const parentSelect = dropdown(parentRow, [["", "None (top-level project)"], ...parents.map(item => [item.projectId, item.title] as [string, string])], values.parentId, value => { values.parentId = value; });
  if (hasChildren && !values.parentId) parentSelect.disabled = true;
  /* Sub-project discovery (top-level projects; the folder is scanned, never monitored). Auto-accept and Ignore only
   * appear once a folder is chosen. Null when the settings could not be read. */
  const discovery: DiscoveryEdit | null = discoveryInfo ? { folder: discoveryInfo.folder || "", autoAccept: discoveryInfo.autoAccept, ignore: discoveryInfo.ignore.join("\n") } : null;
  if (discovery) {
    heading(modal.contentEl, "Sub-project discovery");
    const topLevel = !originalParentId;
    if (!topLevel) setting(modal.contentEl, "Folder", "Only top-level projects scan a folder; new folders are found under this project's parent.");
    else {
      const folderRow = setting(modal.contentEl, "Folder", discoveryInfo!.configured && !discoveryInfo!.available && discovery.folder
        ? "This folder is unavailable on this computer. Choose it again or clear it."
        : "Optional. New subfolders appear in Needs attention as possible sub-projects. Scanned, never monitored; the path stays on this computer.");
      folderRow.el.classList.add("rd-discovery-folder");
      const input = textInput(folderRow, discovery.folder, { placeholder: "C:\\Research\\Papers", onChange: value => { discovery.folder = value.trim(); syncOptions(); } });
      input.classList.add("rd-connect-path");
      button(folderRow.control, "Choose…", async () => {
        const picked = await openDialog({ directory: true, multiple: false, title: "Choose a folder to scan for new sub-projects", defaultPath: discovery.folder || undefined }).catch(() => null);
        if (typeof picked === "string" && picked) { discovery.folder = picked; input.value = picked; syncOptions(); }
      });
      button(folderRow.control, "Clear", () => { discovery.folder = ""; input.value = ""; syncOptions(); });
      const options = h(modal.contentEl, "div", { cls: "rd-discovery-options" });
      toggle(setting(options, "Auto-accept new folders", "Add folders that match a monitoring type at 80% or more automatically."), discovery.autoAccept, value => { discovery.autoAccept = value; });
      const ignoreRow = setting(options, "Ignore folders", "One name or pattern per line, e.g. drafts or old-*. Hidden folders, .git, tmp, build and node_modules are always skipped.");
      const area = h(ignoreRow.control, "textarea", { cls: "rd-discovery-ignore", attr: { rows: "3", placeholder: "drafts\nold-*", spellcheck: "false" } });
      area.value = discovery.ignore;
      area.addEventListener("input", () => { discovery.ignore = area.value; });
      const syncOptions = () => { options.hidden = !discovery.folder; };
      syncOptions();
    }
  }
  const row = buttons(modal.contentEl);
  button(row, "Cancel", () => modal.close());
  button(row, "Save project", () => void run(modal, error, () => save(values, originalParentId, discovery)), true);
}

export function newProjectDialog(projects: HierarchyItem[], options: { parentId?: string | null }, save: (input: { id: string; title: string; projectType: string; parentId: string | null }) => Promise<unknown>): void {
  const modal = new Modal("New project", "rd-edit-project-modal");
  modal.description("Create a project record. Connect source folders to it later, or use it to group sub-projects.");
  const error = modal.errorBox();
  let title = "", projectType = "other", projectId = "", idEdited = false, parentId = "";
  const requested = normalizeParentId(options.parentId ?? null);
  if (requested) {
    /* A sub-project request attaches to its top-level ancestor, as the dashboard displays it. */
    const target = projects.find(item => item.projectId === requested);
    const top = normalizeParentId(target?.parentId ?? null) || requested;
    if (eligibleParents(projects).some(item => item.projectId === top)) parentId = top;
    else error.textContent = "The requested parent project is unavailable. Choose a top-level project or None.";
  }
  const taken = projects.map(item => item.projectId);
  let idInput: HTMLInputElement | null = null;
  const titleInput = textInput(setting(modal.contentEl, "Title", "The name shown in Overall and on the project page."), title, {
    placeholder: "Lunar habitat thermal study", maxLength: 200,
    onChange: value => { title = value; if (!idEdited) { projectId = uniqueProjectId(value, taken); if (idInput) idInput.value = projectId; } },
  });
  dropdown(setting(modal.contentEl, "Type"), entries(PROJECT_TYPES), projectType, value => { projectType = value; });
  const parentSelect = dropdown(setting(modal.contentEl, "Parent project", "Optional. Show the new project under a top-level project."), [], "", value => { parentId = value; });
  fillSelect(parentSelect, [["", "None (top-level project)"], ...eligibleParents(projects).map(item => [item.projectId, item.title] as [string, string])], parentId);
  const advanced = h(modal.contentEl, "details", { cls: "rd-connect-advanced" });
  h(advanced, "summary", { text: "Advanced options" });
  idInput = textInput(setting(advanced, "Project ID", "Generated from the title. It names the project folder and cannot be changed later."), projectId, {
    onChange: value => { projectId = value.trim(); idEdited = Boolean(projectId); },
  });
  const row = buttons(modal.contentEl);
  button(row, "Cancel", () => modal.close());
  button(row, "Create project", () => void run(modal, error, () => save({ id: projectId || uniqueProjectId(title, taken), title, projectType, parentId: parentId || null })), true);
  window.setTimeout(() => titleInput.focus(), 50);
}

export interface PaperChoice { sourceId: string; title: string; chapters: Chapter[] }

export function addMilestoneDialog(project: Project, papers: PaperChoice[], selection: PaperBinding, save: (title: string, due: string, binding: PaperBinding) => Promise<unknown>): void {
  const modal = new Modal("Add milestone", "rd-edit-project-modal");
  modal.description(project.title);
  const error = modal.errorBox();
  let title = "", due = "";
  let binding: PaperBinding = {};
  if (selection.sourceId && papers.some(item => item.sourceId === selection.sourceId && item.chapters.some(chapter => chapter.id === selection.chapterId))) {
    binding = { sourceId: selection.sourceId, chapterId: selection.chapterId, stage: selection.stage || "draft" };
  } else if (selection.sourceId && selection.chapterId) error.textContent = "The selected chapter was removed or renamed. Choose a current chapter or Project-wide.";
  const titleInput = textInput(setting(modal.contentEl, "Milestone", "A result you can check and accept."), "", { placeholder: "Finish the Introduction draft", maxLength: 300, onChange: value => { title = value; } });
  textInput(setting(modal.contentEl, "Due date", "Optional."), "", { type: "date", onChange: value => { due = value; } });
  if (papers.length) {
    const choices: [string, string][] = [["", "Project-wide"]];
    for (const source of papers) for (const chapter of source.chapters) choices.push([`${source.sourceId}/${chapter.id}`, `${papers.length > 1 ? source.title + " · " : ""}${chapter.parentId ? "  " : ""}${chapter.title}`]);
    dropdown(setting(modal.contentEl, "Chapter", "Leave unassigned for a project-wide milestone."), choices, binding.sourceId ? `${binding.sourceId}/${binding.chapterId}` : "", value => {
      const [sourceId, chapterId] = value.split("/");
      binding = sourceId ? { sourceId, chapterId, stage: binding.stage || "draft" } : {};
    });
    dropdown(setting(modal.contentEl, "Stage", "The chapter outcome this milestone verifies."), entries(PAPER_STAGES), binding.stage || "draft", value => { binding.stage = value; });
  }
  const row = buttons(modal.contentEl);
  button(row, "Cancel", () => modal.close());
  button(row, "Add milestone", () => void run(modal, error, () => save(title, due, binding)), true);
  titleInput.addEventListener("keydown", event => { if (event.key === "Enter") void run(modal, error, () => save(title, due, binding)); });
  window.setTimeout(() => titleInput.focus(), 50);
}

export function removeProjectDialog(project: Project, remove: () => Promise<unknown>): void {
  const modal = new Modal("Remove from dashboard");
  modal.description(project.title);
  h(modal.contentEl, "p", { text: "This moves the project folder to the workspace's .trash folder and archives its collector records. Your external paper and simulation folders stay in place." });
  const error = modal.errorBox();
  const row = buttons(modal.contentEl);
  button(row, "Cancel", () => modal.close());
  const confirm = button(row, "Remove from dashboard", () => void run(modal, error, remove), true);
  confirm.classList.add("mod-warning");
}
