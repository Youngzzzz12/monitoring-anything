/* Widget preview with fixture data (see widget-preview.html). Query: focus=<projectId>, theme=dark|light, width=<px>. */

import "../src/styles/research-dashboard.css";
import "../src/styles/connector.css";
import "../src/styles/app.css";
import "../src/styles/plan-card.css";
import { buildModel } from "../src/model/load";
import { mountView, type ActionBridge } from "../src/view/view";
import { snapshot } from "../tests/fixtures";
import { installDock } from "../src/app/widget";

const params = new URLSearchParams(location.search);
const dark = params.get("theme") !== "light";
document.body.classList.add(dark ? "theme-dark" : "theme-light", "rd-widget-window");
document.documentElement.style.colorScheme = dark ? "dark" : "light";

const data = snapshot();
/* A long project title, as real paper titles are. */
const paper = data.projects.find(item => item.id === "paper")!;
(paper.record as Record<string, unknown>).title = "A very long paper title that has to wrap or clip inside the widget (draft for submission)";

/* chapters=N: replace the paper's chapters with N top-level chapters at varied progress (to check dense bars). */
const chapterCount = Number(params.get("chapters")) || 0;
if (chapterCount) {
  const state = data.states.find(item => item.id === "paper-tex")!.value as { details: { chapters: unknown[] }[] };
  const chapters = Array.from({ length: chapterCount }, (_, i) => ({ id: "c" + i, title: "Chapter " + (i + 1), parentId: null, bodyWords: 500, ownBodyWords: 500, deltaToday: 0, deltaWeek: 0, sourceHash: String(i).padStart(64, "0") }));
  state.details[0].chapters = chapters;
  const lines = chapters.flatMap((chapter, i) => Array.from({ length: 4 }, (_, k) => `- [${k < (i * 3) % 5 ? "x" : " "}] ${chapter.title} step ${k + 1} #milestone [paper:: paper-tex] [chapter:: ${chapter.id}] [stage:: draft]`));
  paper.milestones = "# Milestones

" + lines.join("
") + "
";
}

let focus: string | null = params.get("focus");
const listeners = new Set<() => void>();
const log = (name: string) => async (...args: unknown[]) => { console.log("[bridge]", name, ...args); };
const bridge: ActionBridge = {
  getStatus: () => ({ running: false, busy: false, keepOnTop: true, canKeepOnTop: true, focusProjectId: focus }),
  setFocus: async id => { focus = id; listeners.forEach(listener => listener()); },
  setMilestone: log("setMilestone"), quickMilestone: log("quickMilestone"), connect: log("connect"),
  openOverall: log("openOverall"), openProject: log("openProject"), focusChapter: log("focusChapter"),
  start: log("start"), stop: log("stop"), setKeepOnTop: log("setKeepOnTop"),
};

const host = document.getElementById("app")!;
const win = document.createElement("div");
win.className = "rd-preview-window";
win.style.setProperty("--w", (Number(params.get("width")) || 320) + "px");
host.appendChild(win);
const frame = document.createElement("div");
frame.className = "rd-widget-frame";
win.appendChild(frame);
const grip = document.createElement("div");
grip.className = "rd-widget-grip";
grip.innerHTML = '<span class="rd-widget-grip-bar"></span><button class="rd-widget-close" type="button" aria-label="Hide widget"></button>';
frame.appendChild(grip);
const page = document.createElement("div");
page.className = "rd-native-dashboard rd-mini-host";
frame.appendChild(page);
const panel = document.createElement("div");
panel.className = "rd-app-panel";
panel.dataset.mode = "mini";
page.appendChild(panel);

mountView(panel, { mode: "mini" }, {
  load: async () => buildModel(data), bridge, navigate: () => {}, openExternal: () => {}, openWorkspaceFile: () => {},
  subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
});

installDock(frame, () => (Number(params.get("magnify")) || 5) / 100);
