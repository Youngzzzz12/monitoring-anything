// @vitest-environment jsdom
/* The dashboard's Next up must match `ma report` (collector/tools/ma.py): both run tests/shared/next-up.json. */
import { describe, expect, it, vi } from "vitest";
import shared from "./shared/next-up.json";
import { buildModel } from "../src/model/load";
import type { WorkspaceSnapshot } from "../src/model/types";
import { mountView, type ActionBridge } from "../src/view/view";

interface Scenario {
  projects: { id: string; title: string; type: string; health: string; parentId: string | null; sourceIds: string[]; milestones: string[] }[];
  chapters: Record<string, { id: string; title: string; parentId: string | null; deltaToday: number; deltaWeek: number; changedMinutesAgo: number | null }[]>;
  expected: { all: string[]; focusThesis: string[] };
}
const scenario = shared as Scenario;

function day(offset: number): string {
  const date = new Date(); date.setDate(date.getDate() + offset);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function snapshot(): WorkspaceSnapshot {
  const now = Date.now(), iso = new Date(now).toISOString();
  return {
    root: "C:/ws", name: "Shared", errors: [], profiles: [], local: [], loadedAt: iso,
    projects: scenario.projects.map(project => ({
      id: project.id, recordError: null, milestonesError: null,
      record: { schemaVersion: "1.0", projectId: project.id, title: project.title, type: project.type, parentId: project.parentId, health: project.health, sourceIds: project.sourceIds },
      milestones: "# Milestones\n\n" + project.milestones.map(line => line.replace(/\{due:(-?\d+)\}/, (_, offset) => `[due:: ${day(Number(offset))}]`)).join("\n") + "\n",
    })),
    sources: Object.keys(scenario.chapters).map(id => ({ id, error: null, value: { schemaVersion: "1.0", projectId: id, dashboardProjectId: "paper", title: id, kind: "latex", source: { rootAlias: id, path: "" }, collector: { hostId: "host", intervalSeconds: 60, staleAfterSeconds: 180 }, adapter: { mainFile: "main.tex" } } })),
    states: Object.entries(scenario.chapters).map(([id, chapters]) => ({ id, error: null, value: {
      schemaVersion: "1.0", projectId: id, title: id, kind: "latex", runtime: { status: "completed", progress: null }, cases: {}, artifacts: [],
      collection: { health: "fresh", attemptedAt: iso, lastSuccessfulAt: iso, staleAfterSeconds: 180, hostId: "host" },
      details: [{ kind: "paper", schemaVersion: 1, title: "Paper", chapters: chapters.map((chapter, index) => ({
        id: chapter.id, title: chapter.title, parentId: chapter.parentId, bodyWords: 100, ownBodyWords: 100, deltaToday: chapter.deltaToday, deltaWeek: chapter.deltaWeek,
        sourceHash: String(index).padStart(64, "0"), changedAt: chapter.changedMinutesAgo === null ? null : new Date(now - chapter.changedMinutesAgo * 60000).toISOString() })) }],
    } })),
  };
}

async function nextUp(focusProjectId: string | null): Promise<string[]> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const bridge: ActionBridge = { getStatus: () => ({ running: false, busy: false, focusProjectId }), setMilestone: vi.fn(), setFocus: vi.fn() };
  const handle = mountView(container, { mode: "mini" }, { load: async () => buildModel(snapshot()), bridge, navigate: vi.fn(), openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {} });
  await handle.painted;
  const titles = Array.from(container.querySelectorAll(".rd-mini-next .rd-mini-row-title")).map(node => node.textContent || "");
  handle.dispose();
  return titles;
}

describe("shared Next up scenario", () => {
  it("all projects", async () => { expect(await nextUp(null)).toEqual(scenario.expected.all); });
  it("focused on the thesis", async () => { expect(await nextUp("thesis")).toEqual(scenario.expected.focusThesis); });
});
