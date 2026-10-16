import { describe, expect, it } from "vitest";
import { appendMilestone, deleteMilestoneContent, editMilestoneContent, milestoneLabel, milestoneTask, parseMilestones, setMilestoneContent, taskBinding } from "../src/model/milestones";

const FILE = "# Milestones\n\n- [ ] Draft intro [due:: 2026-11-01] #milestone [paper:: p] [chapter:: section-1] [stage:: draft]\n" +
  "```\n- [ ] Not a task #milestone\n```\n\n### Results\n\n- [x] Figures #milestone\n- [ ] Plain task without tag\n";

describe("parseMilestones", () => {
  it("reads tagged tasks outside fenced code with due dates, headings and line numbers", () => {
    const tasks = parseMilestones(FILE, "projects/a/milestones.md");
    expect(tasks.map(task => [task.line, task.completed, task.due, task.heading])).toEqual([[2, false, "2026-11-01", null], [9, true, null, "Results"]]);
    expect(taskBinding(tasks[0], "chapter")).toBe("section-1");
    expect(milestoneLabel(tasks[0])).toBe("Draft intro");
  });
  it("skips frontmatter", () => {
    expect(parseMilestones("---\ntitle: x\n- [ ] no #milestone\n---\n- [ ] yes #milestone\n", "m.md").map(task => task.line)).toEqual([4]);
  });
});

describe("milestoneTask", () => {
  it("escapes Markdown and adds bindings like the collector's templates", () => {
    expect(milestoneTask("Fix *bold* [x]", "2026-12-01", { sourceId: "paper", chapterId: "sec-1", stage: "review" }))
      .toBe("- [ ] Fix \\*bold\\* \\[x\\] [due:: 2026-12-01] #milestone [paper:: paper] [chapter:: sec-1] [stage:: review]");
    expect(milestoneLabel({ text: "Fix \\*bold\\* #milestone" })).toBe("Fix *bold*");
  });
  it("rejects invalid input", () => {
    expect(() => milestoneTask("", null)).toThrow();
    expect(() => milestoneTask("two\nlines", null)).toThrow();
    expect(() => milestoneTask("ok", "2026-02-30")).toThrow();
    expect(() => milestoneTask("ok", null, { sourceId: "Bad Id", chapterId: "x" })).toThrow();
  });
});

describe("appendMilestone", () => {
  it("appends inside the Milestones section, before the next heading", () => {
    const text = "# Milestones\n\n- [ ] A #milestone\n\n# Notes\n\nkeep\n";
    expect(appendMilestone(text, "- [ ] B #milestone")).toBe("# Milestones\n\n- [ ] A #milestone\n- [ ] B #milestone\n\n# Notes\n\nkeep\n");
  });
  it("creates the section when missing", () => {
    expect(appendMilestone("", "- [ ] B #milestone")).toBe("# Milestones\n\n- [ ] B #milestone\n");
    expect(appendMilestone("Intro\n", "- [ ] B #milestone")).toBe("Intro\n\n# Milestones\n\n- [ ] B #milestone\n");
  });
  it("keeps CRLF files CRLF", () => {
    expect(appendMilestone("# Milestones\r\n", "- [ ] B #milestone")).toBe("# Milestones\r\n\r\n- [ ] B #milestone\r\n");
  });
});

describe("setMilestoneContent", () => {
  it("checks a task by exact text and records the accepted hash for chapter milestones", () => {
    const [task] = parseMilestones(FILE, "m.md");
    const hash = "f".repeat(64);
    const next = setMilestoneContent(FILE, { line: task.line, text: task.text, completed: true, sourceId: "p", chapterId: "section-1", sourceHash: hash });
    expect(next.split("\n")[2]).toBe(`- [x] Draft intro [due:: 2026-11-01] #milestone [paper:: p] [chapter:: section-1] [stage:: draft] [accepted_hash:: ${hash}]`);
    const back = setMilestoneContent(next, { line: 2, text: next.split("\n")[2].slice(6), completed: false, sourceId: "p", chapterId: "section-1" });
    expect(back.split("\n")[2]).toBe("- [ ] Draft intro [due:: 2026-11-01] #milestone [paper:: p] [chapter:: section-1] [stage:: draft]");
  });
  it("refuses stale or mismatched requests", () => {
    expect(() => setMilestoneContent(FILE, { line: 2, text: "other", completed: true })).toThrow(/changed/);
    expect(() => setMilestoneContent(FILE, { line: 4, text: "- [ ] Not a task #milestone", completed: true })).toThrow();
    const [task] = parseMilestones(FILE, "m.md");
    expect(() => setMilestoneContent(FILE, { line: 2, text: task.text, completed: true, sourceId: "p", chapterId: "section-1" })).toThrow(/Refresh chapter/);
    expect(() => setMilestoneContent(FILE, { line: 2, text: task.text, completed: true })).toThrow(/different chapter/);
  });
});

describe("edit and delete", () => {
  const file = "# Milestones\n\n- [x] Intro \\*draft\\* [due:: 2026-11-01] #milestone [paper:: p] [chapter:: intro] [stage:: draft] [accepted_hash:: " + "a".repeat(64) + "]\n- [ ] Method #milestone\n";
  const lines = file.split("\n");
  it("renames and re-dates a milestone, keeping its state and binding", () => {
    const text = lines[2].replace(/^- \[x\] /, "");
    const renamed = editMilestoneContent(file, { line: 2, text, title: "Intro first pass" }).split("\n")[2];
    expect(renamed).toBe("- [x] Intro first pass [due:: 2026-11-01] #milestone [paper:: p] [chapter:: intro] [stage:: draft] [accepted_hash:: " + "a".repeat(64) + "]");
    const undated = editMilestoneContent(file, { line: 2, text, due: null }).split("\n")[2];
    expect(undated).toBe("- [x] Intro \\*draft\\* #milestone [paper:: p] [chapter:: intro] [stage:: draft] [accepted_hash:: " + "a".repeat(64) + "]");
    expect(editMilestoneContent(file, { line: 3, text: "Method #milestone", due: "2026-12-24" }).split("\n")[3]).toBe("- [ ] Method [due:: 2026-12-24] #milestone");
    expect(() => editMilestoneContent(file, { line: 3, text: "Other #milestone", title: "x" })).toThrow(/changed/);
    expect(() => editMilestoneContent(file, { line: 3, text: "Method #milestone", title: "" })).toThrow(/title/);
  });
  it("deletes exactly the verified line", () => {
    expect(deleteMilestoneContent(file, { line: 3, text: "Method #milestone" })).toBe(lines.slice(0, 3).join("\n") + "\n");
    expect(() => deleteMilestoneContent(file, { line: 2, text: "Method #milestone" })).toThrow(/changed/);
  });
});
