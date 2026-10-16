/* The real app with a mocked backend and made-up projects, for screenshots and demos (see demo.html).
 * Query: window=widget for the floating widget, shape=orb for it as a circle, theme=dark|light, focus=<projectId> (widget). Not part of the app bundle. */

import { mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import type { WorkspaceSnapshot } from "../src/model/types";

const params = new URLSearchParams(location.search);
const label = params.get("window") === "widget" ? "widget" : "main";
const theme = params.get("theme") === "light" ? "light" : params.get("theme") === "dark" ? "dark" : "system";
const iso = (minutes = 0) => new Date(Date.now() + minutes * 60_000).toISOString();
const day = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
const hash = (n: number) => n.toString(16).padStart(64, "0");

function record(projectId: string, title: string, type: string, parentId: string | null, extra: Record<string, unknown> = {}) {
  return { schemaVersion: "1.0", projectId, title, type, parentId, health: "active", sourceIds: [], ...extra };
}
function collection(minutesAgo = 1) {
  return { health: "fresh", attemptedAt: iso(-minutesAgo), lastSuccessfulAt: iso(-minutesAgo), staleAfterSeconds: 600, hostId: "demo" };
}
function state(id: string, kind: string, runtime: Record<string, unknown>, details: unknown[], extra: Record<string, unknown> = {}) {
  return { schemaVersion: "1.0", projectId: id, title: id, kind, parentId: null, projectNote: "",
           runtime: { status: "unknown", progress: null, step: null, currentValue: null, targetValue: null, unit: null, runId: null, ...runtime },
           cases: {}, collection: collection(), artifacts: [], details, provenance: {}, ...extra };
}
function source(id: string, projectId: string, title: string, kind: string, profileId: string | null) {
  return { id, value: { schemaVersion: "1.0", projectId: id, dashboardProjectId: projectId, title, kind, profileId,
                        source: { rootAlias: id, path: "" }, collector: { hostId: "demo", intervalSeconds: 60, staleAfterSeconds: 600 } }, error: null };
}

/* Project A: an umbrella with two papers (LaTeX and Word); Project B: a training run; Project C: a simulation batch. */
const sections = [
  { id: "intro", title: "Introduction", words: 1450, today: 0, week: 210, changed: -60 * 30 },
  { id: "background", title: "Background", words: 2380, today: 0, week: 0, changed: -60 * 72 },
  { id: "method", title: "Method", words: 2390, own: 820, today: 140, week: 590, changed: -45 },
  { id: "method-setup", title: "Setup", words: 960, today: 140, week: 380, changed: -45, parent: "method" },
  { id: "method-model", title: "Model", words: 610, today: 0, week: 90, changed: -60 * 26, parent: "method" },
  { id: "results", title: "Results", words: 1720, today: 320, week: 940, changed: -12 },
  { id: "discussion", title: "Discussion", words: 540, today: 0, week: 60, changed: -60 * 50 },
  { id: "conclusion", title: "Conclusion", words: 180, today: 0, week: 0, changed: -60 * 24 * 6 },
];
const chapters = sections.map((s, i) => ({ id: s.id, title: s.title, parentId: s.parent || null, bodyWords: s.words, ownBodyWords: (s as { own?: number }).own ?? s.words,
                                           deltaToday: s.today, deltaWeek: s.week, changedAt: iso(s.changed), sourceHash: hash(i + 1) }));
const done = new Set(["intro:draft", "intro:complete", "background:draft", "background:complete", "method-setup:draft", "method-model:draft", "results:draft"]);
const leafSections = sections.filter(s => !sections.some(o => o.parent === s.id));
const paperMilestones = "# Milestones\n\n" + leafSections.flatMap(s => ["draft", "complete"].map(stage => {
  const key = `${s.id}:${stage}`;
  const accepted = done.has(key) ? ` [accepted_hash:: ${hash(sections.indexOf(s) + 1)}]` : "";
  return `- [${done.has(key) ? "x" : " "}] ${s.title} — ${stage} #milestone [paper:: a-paper-tex] [chapter:: ${s.id}] [stage:: ${stage}]${accepted}`;
})).join("\n") + "\n- [ ] Submit to journal [due:: " + day(24) + "] #milestone\n";

const snapshot = (): WorkspaceSnapshot => ({
  root: "C:/Demo/Workspace", name: "Demo",
  projects: [
    { id: "project-a", record: record("project-a", "Project A", "thesis", null, { stage: "writing", nextStep: "Finish the Results section" }),
      milestones: `# Milestones\n\n- [x] Proposal approved #milestone\n- [x] Literature review #milestone\n- [ ] Progress report [due:: ${day(-3)}] #milestone\n- [ ] Full draft [due:: ${day(40)}] #milestone\n`, recordError: null, milestonesError: null },
    { id: "a-paper", record: record("a-paper", "Project A1", "paper", "project-a", { sourceIds: ["a-paper-tex"], nextStep: "Draft the Results section" }),
      milestones: paperMilestones, recordError: null, milestonesError: null },
    { id: "a-report", record: record("a-report", "Project A2", "paper", "project-a", { sourceIds: ["a-report-docx"], nextStep: "Finish the first draft" }),
      milestones: `# Milestones\n\n- [x] Outline #milestone\n- [ ] First draft [due:: ${day(9)}] #milestone\n- [ ] Supervisor comments addressed #milestone\n`, recordError: null, milestonesError: null },
    { id: "project-b", record: record("project-b", "Project B", "simulation", null, { sourceIds: ["b-training"], stage: "running", nextStep: "Run the hyperparameter sweep" }),
      milestones: "# Milestones\n\n- [x] Data pipeline #milestone\n- [x] Baseline model #milestone\n- [ ] Hyperparameter sweep #milestone\n- [ ] Ablation study #milestone\n", recordError: null, milestonesError: null },
    { id: "project-c", record: record("project-c", "Project C", "simulation", null, { sourceIds: ["c-batch"], stage: "running", nextStep: "Wait for the remaining cases" }),
      milestones: "# Milestones\n\n- [x] Mesh convergence #milestone\n- [ ] All planned cases finished #milestone\n- [ ] Figures produced #milestone\n", recordError: null, milestonesError: null },
    { id: "project-e", record: record("project-e", "Project E", "experiment", null, { sourceIds: ["e-rig"], stage: "testing", nextStep: "Test specimen 6" }),
      milestones: "# Milestones\n\n- [x] Rig calibrated #milestone\n- [x] Specimens 1-4 tested #milestone\n- [ ] Specimens 5-8 tested #milestone\n- [ ] S-N curve fitted #milestone\n", recordError: null, milestonesError: null },
    { id: "project-d", record: record("project-d", "Project D", "experiment", null, { health: "completed", nextStep: "Archived" }),
      milestones: "# Milestones\n\n- [x] Test plan #milestone\n- [x] Specimens tested #milestone\n- [x] Data archived #milestone\n", recordError: null, milestonesError: null },
  ],
  sources: [
    source("a-paper-tex", "a-paper", "Paper 1 (LaTeX)", "latex", "latex-paper"),
    source("a-report-docx", "a-report", "Report (Word)", "abaqus", "word-manuscript"),
    source("b-training", "project-b", "Training run", "abaqus", "ml-training"),
    source("c-batch", "project-c", "Simulation batch", "abaqus", "abaqus-job"),
    source("e-rig", "project-e", "Test rig logs", "abaqus", "fatigue-rig"),
  ],
  states: [
    { id: "a-paper-tex", value: state("a-paper-tex", "latex", { status: "completed" }, [{ kind: "paper", schemaVersion: 1, title: "Paper 1", chapters }]), error: null },
    { id: "a-report-docx", value: state("a-report-docx", "abaqus", {}, [
      { kind: "metric", id: "words", label: "Words", value: 6420, unit: null }, { kind: "metric", id: "headings", label: "Headings", value: 14, unit: null },
      { kind: "metric", id: "comments", label: "Comments", value: 3, unit: null }, { kind: "metric", id: "revisions", label: "Tracked changes", value: 11, unit: null }]), error: null },
    { id: "b-training", value: state("b-training", "abaqus", { status: "running", progress: 34 / 50, currentValue: 34, targetValue: 50, unit: "epochs", step: "epoch 34" }, [
      { kind: "metric", id: "best-val-loss", label: "Best val loss", value: 0.218, unit: null }, { kind: "metric", id: "train-loss", label: "Train loss", value: 0.164, unit: null },
      { kind: "metric", id: "val-acc", label: "Val accuracy", value: 0.931, unit: null }]), error: null },
    { id: "c-batch", value: state("c-batch", "abaqus", { status: "running", progress: 0.62, step: "Step 3: impact", currentValue: 62, targetValue: 100, unit: "%" }, [],
      { cases: { planned: 12, completed: 7, running: 2, failed: 0, unknown: 3 } }), error: null },
    { id: "e-rig", value: state("e-rig", "abaqus", { status: "running", progress: 184000 / 500000, currentValue: 184000, targetValue: 500000, unit: "cycles", step: "Specimen 6" }, [
      { kind: "metric", id: "peak-load", label: "Peak load", value: 12.4, unit: "kN" }, { kind: "metric", id: "stiffness-drop", label: "Stiffness drop", value: 3.1, unit: "%" }],
      { cases: { planned: 8, completed: 5, running: 1, failed: 0, unknown: 2 } }), error: null },
  ],
  local: [{ id: "demo", value: { schemaVersion: "1.0", aliases: { "a-paper-tex": "C:\\Demo\\Paper1", "a-report-docx": "C:\\Demo\\Report", "b-training": "C:\\Demo\\Training", "c-batch": "C:\\Demo\\Simulation", "e-rig": "C:\\Demo\\TestRig" } }, error: null }],
  profiles: [
    { id: "latex-paper", title: "LaTeX paper or thesis", origin: "builtin" }, { id: "word-manuscript", title: "Word manuscript", origin: "builtin" },
    { id: "ml-training", title: "Machine-learning training run", origin: "builtin" }, { id: "abaqus-job", title: "Abaqus job", origin: "builtin" },
    { id: "fatigue-rig", title: "Fatigue test rig", origin: "user" },
  ], errors: [], loadedAt: iso(),
} as unknown as WorkspaceSnapshot);

const settings = {
  schemaVersion: 2, workspace: "C:/Demo/Workspace", recentWorkspaces: [], pythonPath: null, motion: "full", theme, plainBackdrop: false,
  closeToTray: true, startLiveOnLaunch: true, discoveryScan: false, agent: { providerId: "claude-code", chosen: true, setupDone: true },
  widget: { visible: true, keepOnTop: true, fadeWhenIdle: false, idleOpacity: 0.6, focusProjectId: params.get("focus"), magnification: 5, bounds: null,
            collapsed: params.get("shape") === "orb", orb: null },
};
const replies: Record<string, unknown> = {
  get_settings: settings, update_settings: settings,
  app_info: { version: "0.1.0", collectorDir: null, platform: "windows", settingsPath: "", launchedByAutostart: false },
  workspace_current: { root: "C:/Demo/Workspace", name: "Demo", created: false, meta: {} },
  watch_status: { running: true, stopping: false, error: null, startedAt: iso(-90) },
  take_requests: [], skill_status: [], python_status: { source: "bundled", path: "", version: "3.12", ok: true, error: null },
};

mockWindows(label);
mockIPC((cmd: string, payload?: Record<string, unknown>) => {
  if (cmd === "workspace_snapshot") return snapshot();
  /* The circle's flyout opens to the right of a circle at the window's corner. */
  if (cmd === "widget_shape") {
    const flyout = payload?.flyout as [number, number] | null;
    return flyout ? { shape: "flyout", side: "right", orbX: 0, orbY: 0, width: 78 + flyout[0], height: Math.max(72, flyout[1]) }
      : { shape: payload?.shape === "panel" ? "panel" : "orb", side: "right", orbX: 0, orbY: 0, width: 72, height: 72 };
  }
  if (cmd in replies) return replies[cmd];
  return null;
}, { shouldMockEvents: true });

/* The window plugin is mocked too; the main window's own frame is the browser page. */
if (label === "widget") document.documentElement.classList.add("rd-demo-widget");
await import("../src/main");
