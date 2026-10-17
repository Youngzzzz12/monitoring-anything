# Monitoring Anything

A desktop app that keeps track of the things you work on: papers, theses, Word manuscripts, simulations,
machine-learning training runs, and anything else that leaves files behind. Connect a folder and it follows the
progress by itself, next to your projects and milestones. A floating widget, or a small circle, keeps the overview on
screen, and your agent (Claude Code, Codex) can do all of it for you from a chat.

> Status: **0.3.1, early release** for Windows and macOS (Apple Silicon). Builds are not yet code-signed.

![Overview](docs/screenshots/overall.png)

**How it fits together**

1. [**Connect**](#connect-a-folder) a folder: drop it on the window, ask your agent, or let Smart monitoring design the
   plan. Every way ends on the same plan card, where you confirm.
2. The app [**reads**](#what-it-can-read) it with a monitoring type: built in, rules of your own, or a custom adapter
   an agent writes.
3. [**Organise**](#projects-within-projects) the work into projects with sub-projects of any type, and milestones.
4. [**Follow**](#on-screen-while-you-work) it in the dashboard, the widget or the circle, or
   [**ask your agent**](#ask-your-agent) how things are going.

## Connect a folder

There are three ways in, and they all end on the **plan card**: the detected monitoring type, a preview of what it
reads from the folder's current files, and where the project goes. Nothing is written until you press **Connect**.

| Way in | How |
| --- | --- |
| **Drop or pick it** | Drag a folder from Explorer or Finder onto the window or the widget, or use **Connect project**. Dropped on a project's page, it becomes a sub-project there. |
| **Ask your agent** | In Claude Code or Codex: *"Monitor D:\Data\TestRig; I need the cycles and the peak load."* The agent checks the built-in types, designs a plan (or a custom adapter) if none fits, previews it against the folder, and opens the plan card in the app. |
| **Smart monitoring** | On the plan card, **Smart monitoring** asks the agent you set up in the app to design the plan; **Add requirements** says what you need first. |

![Dropping a folder: the detected type and a preview, before anything is connected](docs/screenshots/drop-connect.gif)

Once connected, live updates pick up changes within seconds of saving, with a periodic check as a fallback. A plan
designed for one folder can be **saved as a monitoring type**, so folders like it are recognised without an agent.

## What it can read

Each folder is read by a **monitoring type**: rules that recognise the folder and say what to read from it.

**Built in**

| Type | What it follows |
| --- | --- |
| LaTeX paper or thesis | Section structure, words written today and this week per section, build status; chapter milestones are accepted against the section's current text |
| Word manuscript | Words, headings, comments and tracked changes of the newest `.docx` |
| Machine-learning training run | Epochs against `max_epochs`, best validation loss, latest losses and accuracy (Lightning `metrics.csv`, Keras `history.csv`), checkpoints |
| Abaqus job | Run status from the `.sta` / `.msg` logs |
| iSALE batch | Case status from each case's metadata |

**Your own: rules.** Plain JSON with read-only rules (globs, regexes, CSV and JSON values, log status), written by an
agent or by hand.

<details>
<summary>Example: a fatigue test rig</summary>

```json
{
  "schemaVersion": "1.0", "id": "fatigue-rig", "title": "Fatigue test rig", "projectType": "experiment",
  "detect": { "allOf": [{ "glob": "rig.json" }], "anyOf": [{ "glob": "logs/*.csv" }] },
  "extract": {
    "runtime": {
      "status": { "fromLog": "logs/*.log", "success": "^Test complete", "failure": "^ABORT", "running": "^Cycle" },
      "currentValue": { "csv": "logs/*.csv", "column": "cycle", "aggregate": "max" },
      "targetValue": { "jsonPath": "rig.json#$.targetCycles" },
      "unit": "cycles"
    },
    "metrics": [{ "id": "peak-load", "label": "Peak load", "unit": "kN",
                  "value": { "csv": "logs/*.csv", "column": "load_kN", "aggregate": "max" } }]
  }
}
```

</details>

**Your own: custom adapters.** When rules cannot read a folder (a binary or proprietary format, values computed across
files), the agent writes a short Python function, `collect(folder, options)`, that returns progress, status, cases and
metrics. The plan card shows its code. It runs only if it passes the automatic check, in a separate process that may
only read the folder (see [Privacy and safety](#privacy-and-safety)); after that, no agent is involved.

<p>
  <img src="docs/screenshots/project.png" alt="A LaTeX paper: structure, chapter progress and writing activity" width="32%">
  <img src="docs/screenshots/case-training.png" alt="A training run: epochs, losses and accuracy" width="32%">
  <img src="docs/screenshots/case-custom.png" alt="A custom type: cycles, cases and metrics from a test rig's logs" width="32%">
</p>

## Projects within projects

A top-level project (a thesis, a grant, a test campaign) holds sub-projects, and **each sub-project can be any kind of
work**: a LaTeX paper next to a Word report, a training run, a simulation batch, a custom-type experiment, or just a
list of milestones with no folder at all. The parent's progress counts its own milestones and every sub-project's, so
the thesis ring moves when any paper or run moves. Sub-projects go one level deep.

<p>
  <img src="docs/screenshots/family.png" alt="The project list: Project A with a LaTeX paper, a Word report, a training run and a simulation; Project B with a custom-type experiment and a milestones-only sub-project" width="40%">
  &nbsp;
  <img src="docs/screenshots/parent.png" alt="A parent project's page: progress across its four sub-projects of different types" width="56%">
</p>

- **Add sub-projects** by dropping a folder on a project's page, dragging a row onto a project, or giving a project a
  discovery folder: new subfolders are suggested as sub-projects.
- **Milestones** have due dates and can be ticked off, renamed or deleted (with Undo). **Next up** picks what to do
  first: overdue items, then what is due within two weeks, then what you were just writing, then each project's next
  step.

## On screen while you work

The **floating widget** keeps projects, today's writing and Next up in view. Sections grow under the pointer like the
macOS Dock; click a project to focus it (its sub-projects or chapters as columns); tick off or add milestones without
opening the main window. Idle, it fades into the background. The widget button in the main window switches to it.

Shrink it to a **circle** when space is short: just the completion ring. Hovering fans out the breakdown like a Dock
stack; a click opens the widget again, and you can drag the circle anywhere on screen.

<p>
  <img src="docs/screenshots/widget.gif" alt="The widget: Dock-like magnification and focusing a project" width="44%">
  &nbsp;
  <img src="docs/screenshots/orb.gif" alt="The circle: its breakdown fans out on hover and folds back" width="44%">
</p>

## Ask your agent

On first start the app installs a skill for the agents it finds (Claude Code, Codex); remove or reinstall it under
Settings → Agent. Then ask in a chat:

| You ask | The agent |
| --- | --- |
| "How is my thesis going?" | reads the progress: completion, Next up, overdue items, stale sources |
| "Add *Submit to journal* due 10 Nov to Paper 1" | adds the milestone (or completes, renames, re-dates one) |
| "Monitor `D:\Data\TestRig`; I need the cycles and the peak load" | finds or designs the monitoring type (a custom adapter if needed) and opens the plan card for you to confirm |

Agents work through the `ma` command line the app keeps next to its settings
(`%APPDATA%\io.github.monitoring-anything\ma.cmd` on Windows).

## Get started

1. Download from [Releases](../../releases): the `.exe` installer for Windows, or the `.dmg` for macOS (Apple Silicon;
   drag the app to Applications). Python is bundled; nothing else is needed. The builds are not code-signed yet:
   on Windows choose **More info → Run anyway**; on macOS, if it will not open, go to **System Settings → Privacy &
   Security → Open Anyway** (or run `xattr -dr com.apple.quarantine "/Applications/Monitoring Anything.app"`).
2. On first start, **create a workspace** (any empty folder): an ordinary folder of JSON and Markdown files where the
   app keeps projects, milestones and collected data. The guide also finds an agent for Smart monitoring and installs
   the skill.
3. **Connect** a folder, or start with **New project** for an umbrella such as a thesis; turn on **Live updates** and
   keep working as usual.

From 0.3.0 the app updates itself: a newer version is offered a little after start (Settings → General → Updates).

## Privacy and safety

- Your folders are **only read**: nothing in them is compiled, run, moved or modified.
- **Agents** see file names and short excerpts only (never-send patterns are configurable), and a new connection always
  waits for your confirmation on the plan card.
- **Custom adapters** run only if they pass the automatic check (reading modules and calls only; nothing that writes,
  starts programs or goes online), in a separate process that refuses anything but reading inside the folder, with a
  time limit.
- **API keys** stay in the system credential store (Windows Credential Manager, macOS Keychain), never in settings,
  logs or the workspace.
- The app never replaces an agent skill it did not install.

## Development

Needs Node.js 20+, Rust (stable) with the [Tauri prerequisites](https://tauri.app/start/prerequisites/), and
Python 3.10+ for the collector tests.

```bash
npm install
npm run tauri dev       # run with hot reload
npm test                # frontend tests; also npm run test:rust and npm run test:python
npm run tauri build     # installers in src-tauri/target/release/bundle/
```

A local `npm run tauri build` signs the update bundle too: set `TAURI_SIGNING_PRIVATE_KEY` (the updater key) and
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, or set `bundle.createUpdaterArtifacts` to false for an unsigned test build.

`src/` is the interface (TypeScript), `src-tauri/` the desktop shell (Rust), `collector/` the Python collector that
reads folders, `skill/` the agent skill. Workspace file formats: [`schemas/README.md`](schemas/README.md).

The README media come from `dev/demo.html` (the app with made-up projects): `npm run demo:media` captures the
screenshots and recordings (Chrome, through playwright-core), `npm run demo:gifs` turns the recordings into GIFs
(Python with Pillow), and `npm run demo:connect` refreshes the plan card's demo data from the collector.

## License

[GNU General Public License v3.0](LICENSE) © 2026 Yang. You may use, study, share and change it; versions you
distribute, changed or not, must stay under the GPL with their source available.
