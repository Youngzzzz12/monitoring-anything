import { describe, expect, it } from "vitest";
import { buildModel, health, hierarchyItems, sourceDirectory, StateCache } from "../src/model/load";
import { eligibleParents, slugId, uniqueProjectId, validateDue, validateNewProject, validateParent, validateProjectValues } from "../src/model/rules";
import { snapshot } from "./fixtures";

describe("buildModel", () => {
  it("orders by status, resolves one-level hierarchy and parses milestones", () => {
    const model = buildModel(snapshot());
    expect(model.errors).toEqual([]);
    expect(model.projects.map(project => project.id)).toEqual(["paper", "thesis", "sim"]);
    const thesis = model.projects.find(project => project.id === "thesis")!;
    const paper = model.projects.find(project => project.id === "paper")!;
    expect(paper.parent).toBe(thesis);
    expect(thesis.children).toEqual([paper]);
    expect(paper.type).toBe("paper");
    expect(paper.sources[0].health.state).toBe("fresh");
    expect(paper.sources[0].directory).toBe("C:\\Papers\\Framework");
    expect(paper.rawMilestones).toHaveLength(3);
    expect(thesis.page.next_step).toBe("Write chapter 3");
  });

  it("reports invalid records, unregistered sources and detached sources", () => {
    const base = snapshot();
    base.projects.push({ id: "broken", record: { schemaVersion: "1.0", projectId: "other", title: "x", sourceIds: [] }, milestones: null, recordError: null, milestonesError: null });
    base.projects[2] = { ...base.projects[2], record: { ...(base.projects[2].record as object), sourceIds: ["ghost"] } };
    base.sources.push({ id: "lonely", value: { schemaVersion: "1.0", projectId: "lonely", title: "L", kind: "abaqus" }, error: null });
    const messages = buildModel(base).errors.map(issue => issue.message).join(" | ");
    expect(messages).toMatch(/projectId does not match/);
    expect(messages).toMatch(/unregistered source: ghost/);
    expect(messages).toMatch(/Source has no project: lonely/);
  });

  it("keeps the last good snapshot when a read fails", () => {
    const cache = new StateCache();
    buildModel(snapshot(), cache);
    const failing = snapshot({ states: [{ id: "paper-tex", value: null, error: "disk busy" }] });
    const source = buildModel(failing, cache).projects.find(project => project.id === "paper")!.sources[0];
    expect(source.readError).toBe("disk busy");
    expect(source.state?.projectId).toBe("paper-tex");
    expect(source.health.state).toBe("error");
  });

  it("flags parent loops and keeps them top-level", () => {
    const base = snapshot();
    base.projects[0] = { ...base.projects[0], record: { ...(base.projects[0].record as object), parentId: "thesis" } };
    const model = buildModel(base);
    expect(model.errors.some(issue => /itself as its parent/.test(issue.message))).toBe(true);
    expect(model.projects.find(project => project.id === "thesis")!.parent).toBeNull();
  });
});

describe("health", () => {
  it("derives freshness from the last success and threshold", () => {
    const now = Date.parse("2026-10-10T12:00:00Z");
    const state = (last: string, staleAfterSeconds = 180) => ({ schemaVersion: "1.0", projectId: "s", runtime: { status: "unknown", progress: null }, cases: {}, collection: { health: "fresh", lastSuccessfulAt: last, staleAfterSeconds } });
    expect(health(state("2026-10-10T11:59:00Z"), null, now).state).toBe("fresh");
    expect(health(state("2026-10-10T11:00:00Z"), null, now)).toEqual({ state: "stale", isStale: true });
    expect(health(null, null, now).state).toBe("unconfigured");
    expect(health(state("2026-10-10T11:59:00Z"), "boom", now).state).toBe("error");
  });
});

describe("sourceDirectory", () => {
  it("joins the host mapping and refuses traversal", () => {
    const mappings = new Map([["h", { aliases: { a: "/home/me/paper", b: "relative" } }]]);
    expect(sourceDirectory({ schemaVersion: "1.0", projectId: "s", title: "s", kind: "latex", source: { rootAlias: "a", path: "tex" }, collector: { hostId: "h" } }, mappings)).toBe("/home/me/paper/tex");
    expect(sourceDirectory({ schemaVersion: "1.0", projectId: "s", title: "s", kind: "latex", source: { rootAlias: "a", path: "../x" }, collector: { hostId: "h" } }, mappings)).toBeNull();
    expect(sourceDirectory({ schemaVersion: "1.0", projectId: "s", title: "s", kind: "latex", source: { rootAlias: "b", path: "" }, collector: { hostId: "h" } }, mappings)).toBeNull();
  });
});

describe("hierarchy rules", () => {
  const items = hierarchyItems(buildModel(snapshot()));
  it("offers only top-level parents and keeps parents top-level", () => {
    expect(eligibleParents(items, "sim").map(item => item.projectId)).toEqual(["thesis"]);
    expect(eligibleParents(items, "thesis")).toEqual([]);
    expect(() => validateParent(items, "sim", "paper")).toThrow(/sub-project/);
    expect(() => validateParent(items, "thesis", "sim")).toThrow(/has sub-projects/);
    expect(() => validateParent(items, "sim", "sim")).toThrow(/own parent/);
    expect(validateParent(items, "sim", "thesis")).toBe("thesis");
    expect(validateParent(items, "sim", "")).toBeNull();
  });
  it("validates new projects and edits", () => {
    expect(validateNewProject({ id: "new-one", title: " New ", projectType: "paper", parentId: "thesis" }, items)).toEqual({ id: "new-one", title: "New", projectType: "paper", parentId: "thesis" });
    expect(() => validateNewProject({ id: "paper", title: "Dup", projectType: "paper" }, items)).toThrow(/already used/);
    const patch = validateProjectValues({ stage: "review", health: "paused", nextStep: " Submit ", due: "2026-12-01", type: "paper", parentId: "thesis" }, { projects: items, projectId: "paper", originalParentId: "thesis" });
    expect(patch).toEqual({ stage: "review", health: "paused", nextStep: "Submit", due: "2026-12-01", type: "paper" });
    expect(() => validateProjectValues({ stage: "nope", health: "active", nextStep: "", due: "", type: "paper", parentId: "" }, { projects: items, projectId: "paper", originalParentId: null })).toThrow();
  });
  it("generates readable unique IDs", () => {
    expect(slugId("Lunar Habitat — Thermal Study")).toBe("lunar-habitat-thermal-study");
    expect(slugId("CON")).toBe("project-con");
    expect(uniqueProjectId("Paper", ["paper", "paper-2"])).toBe("paper-3");
    expect(validateDue("")).toBeNull();
    expect(() => validateDue("2026-13-01")).toThrow();
  });
});
