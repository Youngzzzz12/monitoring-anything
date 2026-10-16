# Monitoring Anything

A desktop app that keeps track of the things you work on: papers, theses, Word manuscripts, simulations and
machine-learning training runs. Connect a folder and it follows the progress by itself (sections written, words
today, job and run status), next to your projects and milestones. A floating widget keeps the overview on screen.

Your folders are only read; nothing is compiled, run or modified. Everything the app stores lives in a **workspace**,
an ordinary folder of JSON and Markdown files.

> Status: **0.1.0, early release.** Windows and macOS. Builds are not yet code-signed.

<!-- Screenshots: docs/screenshots/overall.png, project.png, widget.png -->

## Features

- **Projects and milestones** with one level of sub-projects, due dates and a "Next up" list (overdue first, then due
  soon, then what you were writing, then each project's next step).
- **Connect a folder** by picking or dropping it. The app recognises LaTeX papers, Word manuscripts, ML training runs
  (Lightning / Keras logs), Abaqus jobs and iSALE batches, previews what it will read, and asks you to confirm.
- **Smart monitoring**: for anything else, an agent (Claude Code, Codex, Gemini CLI, Ollama / LM Studio, or an API)
  designs a monitoring plan from file names and short excerpts. Save it as a type to reuse it without an agent.
- **Live updates** within seconds of saving, plus a periodic check.
- **Floating widget**: projects, Next up and progress at a glance; focus one project to see its chapters or
  sub-projects; complete or add milestones without opening the main window.
- **Use it from your agent**: install the skill from Settings → Agent, then ask Claude Code or Codex "how is my
  thesis going?", "add a milestone" or "track this folder".

## Install

Download the installer for your system from [Releases](../../releases) and run it. Python is bundled; nothing else is
needed. For Smart monitoring, have one of the supported agents installed (the first-run guide finds it).

## Use

1. **Create workspace** (any empty folder) on first start.
2. **Connect** a folder you want to follow, or **New project** for an umbrella such as a thesis.
3. Add milestones and tick them off; turn on **Live updates** and keep working as usual.

## Agents

Settings → Agent → **Use Monitoring Anything from your agent** installs a skill into `~/.claude/skills/` and
`~/.codex/skills/`. Agents work through the `ma` command line the app keeps next to its settings
(`%APPDATA%\io.github.monitoring-anything\ma.cmd` on Windows). They can report progress and manage milestones; a new
folder connection always opens in the app for you to confirm. The app never replaces a skill it did not install.

## Development

Needs Node.js 20+, Rust (stable) with the [Tauri prerequisites](https://tauri.app/start/prerequisites/), and
Python 3.10+ for the collector tests.

```bash
npm install
npm run tauri dev       # run with hot reload
npm test                # frontend tests; also npm run test:rust and npm run test:python
npm run tauri build     # installers in src-tauri/target/release/bundle/
```

`src/` is the interface (TypeScript), `src-tauri/` the desktop shell (Rust), `collector/` the Python collector that
reads folders, `skill/` the agent skill. Workspace file formats: [`schemas/README.md`](schemas/README.md).

## License

[MIT](LICENSE) © 2026 Yang
