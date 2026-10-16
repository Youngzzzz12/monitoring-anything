import type { WorkspaceSnapshot } from "../src/model/types";

const iso = (offsetMinutes = 0) => new Date(Date.now() + offsetMinutes * 60_000).toISOString();

export function paperState(id: string, chapters: Record<string, unknown>[]): Record<string, unknown> {
  return {
    schemaVersion: "1.0", projectId: id, title: id, kind: "latex",
    runtime: { status: "completed", progress: null }, cases: {},
    collection: { health: "fresh", attemptedAt: iso(), lastSuccessfulAt: iso(-1), staleAfterSeconds: 180, hostId: "host" },
    artifacts: [], details: [{ kind: "paper", schemaVersion: 1, title: "Lunar paper", chapters }],
  };
}

export function snapshot(overrides: Partial<WorkspaceSnapshot> = {}): WorkspaceSnapshot {
  return {
    root: "C:/ws", name: "Test",
    projects: [
      { id: "thesis", record: { schemaVersion: "1.0", projectId: "thesis", title: "PhD thesis", type: "thesis", parentId: null, health: "active", stage: "writing", nextStep: "Write chapter 3", sourceIds: [] },
        milestones: "# Milestones\n\n- [x] Outline #milestone\n- [ ] Chapter 2 [due:: 2000-01-01] #milestone\n", recordError: null, milestonesError: null },
      { id: "paper", record: { schemaVersion: "1.0", projectId: "paper", title: "Framework paper", type: "paper", parentId: "thesis", health: "blocked", sourceIds: ["paper-tex"] },
        milestones: "# Milestones\n\n- [x] Intro draft #milestone [paper:: paper-tex] [chapter:: intro] [stage:: draft] [accepted_hash:: " + "a".repeat(64) + "]\n- [ ] Method draft #milestone [paper:: paper-tex] [chapter:: method] [stage:: draft]\n- [ ] Method review #milestone [paper:: paper-tex] [chapter:: method] [stage:: review]\n",
        recordError: null, milestonesError: null },
      { id: "sim", record: { schemaVersion: "1.0", projectId: "sim", title: "Impact runs", type: "simulation", parentId: null, health: "completed", sourceIds: [] },
        milestones: "# Milestones\n\n- [x] Mesh #milestone\n", recordError: null, milestonesError: null },
    ],
    sources: [{ id: "paper-tex", value: { schemaVersion: "1.0", projectId: "paper-tex", dashboardProjectId: "paper", title: "Paper TeX", kind: "latex", source: { rootAlias: "paper-tex", path: "" }, collector: { hostId: "host", intervalSeconds: 60, staleAfterSeconds: 180 }, adapter: { mainFile: "main.tex" } }, error: null }],
    states: [{ id: "paper-tex", value: paperState("paper-tex", [
      { id: "intro", title: "Introduction", parentId: null, bodyWords: 400, ownBodyWords: 400, deltaToday: 120, deltaWeek: 300, sourceHash: "a".repeat(64) },
      { id: "method", title: "Method", parentId: null, bodyWords: 250, ownBodyWords: 100, deltaToday: -20, deltaWeek: null, sourceHash: "b".repeat(64) },
      { id: "method-a", title: "Setup", parentId: "method", bodyWords: 150, ownBodyWords: 150, deltaToday: 0, deltaWeek: 10, sourceHash: "c".repeat(64) },
    ]), error: null }],
    local: [{ id: "host", value: { schemaVersion: "1.0", aliases: { "paper-tex": "C:\\Papers\\Framework" } }, error: null }],
    profiles: [], errors: [], loadedAt: iso(),
    ...overrides,
  };
}
