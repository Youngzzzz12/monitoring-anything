/* milestones.md: a GFM task list with Dataview-style inline fields, shared with the collector's milestone templates:
 *   - [ ] Title [due:: 2026-12-01] #milestone [paper:: <source>] [chapter:: <section>] [stage:: draft] [accepted_hash:: <sha>]
 * Parsing, appending and checking are ported from the Obsidian view/plugin (rawMilestones, milestoneTask,
 * appendMilestone, setMilestoneContent) so files stay interchangeable. */

import { isSafeId, PAPER_STAGES, validateDue } from "./rules";

export interface Task {
  text: string;
  completed: boolean;
  /** 0-based line index in the file. */
  line: number;
  path: string;
  milestone: true;
  due: string | null;
  /** Nearest heading above the task (e.g. a chapter group), or null. */
  heading: string | null;
}

const FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const TASK = /^\s*[-*+]\s+\[([ xX])\]\s+(.*)$/;
const HEADING = /^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
const MILESTONE_TAG = /(?:^|\s)#milestone(?:\s|$)/;

interface FenceState { character: string; length: number }

function fenceStep(fence: FenceState | null, line: string): { fence: FenceState | null; marker: boolean } {
  const marker = FENCE.exec(line);
  if (!marker) return { fence, marker: false };
  if (!fence) return { fence: { character: marker[1][0], length: marker[1].length }, marker: true };
  if (marker[1][0] === fence.character && marker[1].length >= fence.length && !marker[2].trim()) return { fence: null, marker: true };
  return { fence, marker: true };
}

/** Milestone tasks outside frontmatter and fenced code. */
export function parseMilestones(markdown: string, path: string): Task[] {
  const tasks: Task[] = [];
  let fence: FenceState | null = null, frontmatter = false, heading: string | null = null;
  for (const [line, content] of String(markdown).split(/\r?\n/).entries()) {
    if (line === 0 && content.replace(/^﻿/, "").trim() === "---") { frontmatter = true; continue; }
    if (frontmatter) { if (/^(---|\.\.\.)\s*$/.test(content)) frontmatter = false; continue; }
    const step = fenceStep(fence, content);
    fence = step.fence;
    if (step.marker || fence) continue;
    const head = HEADING.exec(content);
    if (head) { heading = head[2].toLowerCase() === "milestones" ? null : head[2]; continue; }
    const match = TASK.exec(content);
    if (!match || !MILESTONE_TAG.test(match[2])) continue;
    const due = /\[due::\s*([^\]]+)\]/i.exec(match[2]);
    tasks.push({ text: match[2], completed: match[1].toLowerCase() === "x", line, path, milestone: true, due: due?.[1].trim() || null, heading });
  }
  return tasks;
}

export function milestoneLabel(task: { text?: string }): string {
  return String(task.text || "").replace(/(^|\s)#milestone\b/g, " ").replace(/\[(?:due|paper|chapter|stage|accepted_hash)::[^\]]*\]/gi, "")
    .replace(/\\([\\`*_[\]#<>])/g, "$1").replace(/\s+/g, " ").trim();
}

export function taskBinding(task: { text?: string }, key: string): string {
  const match = String(task.text || "").match(new RegExp("\\[" + key + "::\\s*([^\\]]*)\\]", "i"));
  const value = (task as Record<string, unknown>)[key];
  return match ? match[1].trim() : value == null ? "" : String(value);
}

export interface PaperBinding { sourceId?: string; chapterId?: string; stage?: string }

export function paperBinding(values: PaperBinding = {}): PaperBinding {
  if (!values.sourceId && !values.chapterId) return {};
  if (!isSafeId(values.sourceId) || !/^[a-zA-Z0-9_-]{1,128}$/.test(values.chapterId || "")) throw new Error("Choose an available paper chapter.");
  const stage = values.stage || "draft";
  if (!Object.hasOwn(PAPER_STAGES, stage)) throw new Error("Choose a supported milestone stage.");
  return { sourceId: values.sourceId, chapterId: values.chapterId, stage };
}

/** One task line, escaped like the collector's milestone templates. */
export function milestoneTask(title: string, due: string | null | undefined, binding: PaperBinding = {}): string {
  const text = String(title || "").trim();
  if (!text || text.length > 300 || /[\r\n\x00-\x1f]/.test(text)) throw new Error("Enter a milestone title on one line, between 1 and 300 characters.");
  const date = validateDue(due);
  const plainText = text.replace(/[\\`*_[\]#<>]/g, "\\$&");
  const paper = paperBinding(binding);
  const fields = paper.sourceId ? ` [paper:: ${paper.sourceId}] [chapter:: ${paper.chapterId}] [stage:: ${paper.stage}]` : "";
  return `- [ ] ${plainText}${date ? ` [due:: ${date}]` : ""} #milestone${fields}`;
}

function taskField(text: string, field: string): string | null {
  return new RegExp(`\\[${field}::\\s*([^\\]]+)\\]`).exec(text)?.[1]?.trim() || null;
}

/** Append a task at the end of the Milestones section (a `# Milestones` or `## Milestones` heading), creating it if needed. */
export function appendMilestone(content: string, task: string): string {
  const newline = content.includes("\r\n") ? "\r\n" : "\n";
  let fence: FenceState | null = null, section: { level: number } | null = null, insertion = content.length;
  for (const match of content.matchAll(/^.*(?:\r?\n|$)/gm)) {
    if (!match[0]) continue;
    const line = match[0].replace(/\r?\n$/, "");
    const step = fenceStep(fence, line);
    fence = step.fence;
    if (step.marker || fence) continue;
    const heading = /^ {0,3}(#{1,2})\s+(.+?)\s*#*\s*$/.exec(line);
    if (!heading) continue;
    if (!section && heading[2].toLowerCase() === "milestones") section = { level: heading[1].length };
    else if (section && heading[1].length <= section.level) { insertion = match.index ?? content.length; break; }
  }
  if (fence) throw new Error("Close the unfinished code block in milestones.md before adding a milestone.");
  let before = content.slice(0, insertion).replace(/(\r?\n)+$/, "");
  if (!section) before += (before ? newline + newline : "") + "# Milestones";
  const after = content.slice(insertion);
  /* A task directly after another task keeps the list tight; otherwise a blank line separates it. */
  const lastLine = before.slice(before.lastIndexOf("\n") + 1);
  const gap = TASK.test(lastLine) ? newline : newline + newline;
  return before + gap + task + newline + (after ? newline + after : "");
}

export interface SetMilestoneRequest {
  line: number; text: string; completed: boolean;
  sourceId?: string | null; chapterId?: string | null; sourceHash?: string | null;
}

/** Check or uncheck one task, verified by its exact text; chapter acceptance records the section's source hash. */
export function setMilestoneContent(content: string, request: SetMilestoneRequest): string {
  if (!Number.isSafeInteger(request.line) || request.line < 0 || typeof request.text !== "string" || typeof request.completed !== "boolean") {
    throw new Error("The milestone selection is no longer valid. Refresh and retry.");
  }
  const lines = content.split(/\r?\n/);
  let fence: FenceState | null = null;
  for (const line of lines.slice(0, request.line + 1)) fence = fenceStep(fence, line).fence;
  if (fence) throw new Error("Choose a milestone outside code examples.");
  const task = /^(\s*[-*+]\s+)\[([ xX])\]\s+(.*)$/.exec(lines[request.line] || "");
  if (!task || task[3] !== request.text || !MILESTONE_TAG.test(task[3])) throw new Error("This milestone changed. Refresh the dashboard and retry.");
  const binding = paperBinding({ sourceId: request.sourceId || undefined, chapterId: request.chapterId || undefined });
  if (taskField(task[3], "paper") !== (binding.sourceId || null) || taskField(task[3], "chapter") !== (binding.chapterId || null)) {
    throw new Error("This milestone belongs to a different chapter. Refresh and retry.");
  }
  if (binding.sourceId && request.completed && !/^[a-f0-9]{16,128}$/i.test(request.sourceHash || "")) throw new Error("Refresh chapter data before accepting this milestone.");
  let text = task[3].replace(/\s*\[accepted_hash::\s*[^\]]+\]/g, "");
  if (binding.sourceId && request.completed) text += ` [accepted_hash:: ${request.sourceHash}]`;
  lines[request.line] = `${task[1]}[${request.completed ? "x" : " "}] ${text}`;
  return lines.join(content.includes("\r\n") ? "\r\n" : "\n");
}

export interface EditMilestoneRequest { line: number; text: string; title?: string; due?: string | null }

/** The task line at `line`, verified by its exact text (the file may have changed since the page was drawn). */
function verifiedTask(lines: string[], line: number, text: string): RegExpExecArray {
  if (!Number.isSafeInteger(line) || line < 0 || typeof text !== "string") throw new Error("The milestone selection is no longer valid. Refresh and retry.");
  let fence: FenceState | null = null;
  for (const item of lines.slice(0, line + 1)) fence = fenceStep(fence, item).fence;
  if (fence) throw new Error("Choose a milestone outside code examples.");
  const task = /^(\s*[-*+]\s+)\[([ xX])\]\s+(.*)$/.exec(lines[line] || "");
  if (!task || task[3] !== text || !MILESTONE_TAG.test(task[3])) throw new Error("This milestone changed. Refresh the dashboard and retry.");
  return task;
}

/** Rename a milestone and/or change its due date; its check state, chapter binding and acceptance are kept. */
export function editMilestoneContent(content: string, request: EditMilestoneRequest): string {
  const lines = content.split(/\r?\n/);
  const task = verifiedTask(lines, request.line, request.text);
  const fields = (task[3].match(/\[(?:paper|chapter|stage|accepted_hash)::[^\]]*\]/gi) || []).join(" ");
  const title = request.title === undefined ? milestoneLabel({ text: task[3] }) : String(request.title).trim();
  if (!title || title.length > 300 || /[\r\n\x00-\x1f]/.test(title)) throw new Error("Enter a milestone title on one line, between 1 and 300 characters.");
  const due = request.due === undefined ? taskField(task[3], "due") : validateDue(request.due || "");
  /* A title typed by the user is escaped like a new task; an unchanged title keeps its original escaping. */
  const label = request.title === undefined ? task[3].replace(/\[(?:due|paper|chapter|stage|accepted_hash)::[^\]]*\]/gi, "").replace(/(^|\s)#milestone\b/g, " ").replace(/\s+/g, " ").trim()
    : title.replace(/[\`*_[\]#<>]/g, "\$&");
  lines[request.line] = `${task[1]}[${task[2]}] ${label}${due ? ` [due:: ${due}]` : ""} #milestone${fields ? " " + fields : ""}`;
  return lines.join(content.includes("\r\n") ? "\r\n" : "\n");
}

/** Remove one milestone line (verified by its text). */
export function deleteMilestoneContent(content: string, request: { line: number; text: string }): string {
  const lines = content.split(/\r?\n/);
  verifiedTask(lines, request.line, request.text);
  lines.splice(request.line, 1);
  return lines.join(content.includes("\r\n") ? "\r\n" : "\n");
}
