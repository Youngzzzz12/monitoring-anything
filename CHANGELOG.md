# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Widget as a circle: Shrink in the widget's top bar leaves only the completion ring (the focused project, or the
  average by project). Hovering shows the breakdown beside it (sub-projects, a paper's chapters, or the projects) on
  the side with room; a click opens the widget again; drag it anywhere (its place is remembered).
- README examples (LaTeX thesis, training run, a custom monitoring type) with screenshots from `dev/demo.html`.

### Fixed
- Built-in declarative types (Word manuscript, training run) are no longer labelled Custom.
- Widget title "All projects" (was "Research"); "1 alert" / "1 running source".

## [0.1.0] - 2026-10-16

First public release, named Monitoring Anything (formerly Research Dashboard). Settings and workspaces from earlier
builds are picked up automatically.

### Added
- Tauri 2 desktop app with a main dashboard window, a floating widget and a tray icon.
- App-owned workspace (`workspace.json`, `projects/<id>/project.json` + `milestones.md`, `sources/`, `local/`, `data/`).
- Overall page (stats, milestone capsules, average completion ring, projects with type filter and one-level
  hierarchy, needs attention) and project pages (paper workspace with structure, chapter focus and workbench,
  progress charts, live monitor, results and resources, sub-projects).
- Edit / New project, Add milestone, Remove project and Connect folder dialogs; drag-to-group projects.
- Bundled Python collector with a configurable workspace layout; live updates via its NDJSON watch stream.
- One-time import from the Obsidian Research Dashboard (`import-vault`).
- Settings: workspace, Python interpreter, theme, motion, widget keep-on-top and idle fade, start at login, tray.
- Plan card for connecting folders (ported from the plugin): ranked monitoring types, live preview, Options disclosure,
  profile JSON editor, Save as monitoring type; agent-designed plans through any provider (Claude Code, Codex, Gemini
  CLI, OpenAI-compatible, Anthropic API, custom command) with one repair round, a time limit and Stop.
- API keys in the OS credential store (`keyring`), injected by the Rust side; `plan propose` is only reachable through
  the agent command, and keys are redacted from its output.
- Folder drop from the file manager (native Tauri drop); a drop on a project page presets the parent or sets the
  project's discovery folder.
- Sub-project discovery: discovery folders in Edit project, background and on-demand scans, suggestions in Needs
  attention (Add as sub-project / Ask agent / Dismiss), Auto-accept.
- Settings: Agent (provider, command or endpoint, key, never-send patterns, time limit), Monitoring types (delete
  saved types), Scan for new sub-projects.
- Bundled Python 3.12 runtime (python-build-standalone, pinned and SHA-256 verified by `scripts/fetch-python.mjs`),
  run isolated (`-E -s`); a custom interpreter in Settings still overrides it.
- Widget pointer tracking from the Rust side (`widget://pointer`), so the idle fade works while another app is active.

- Agent setup: first-run guide, automatic use of the first agent found (Claude Code, Codex, Gemini CLI, or a local
  Ollama / LM Studio server), model choice with suggested ids, API key route, Test connection; shown in the plan card
  when Smart monitoring has no working agent. Smart monitoring and Add requirements on the plan card.
- Agent command line `ma` (report, milestones, brief/preview, connect handing off to the plan card for confirmation)
  and the `monitoring-anything` skill for Claude Code and Codex, installed on request from Settings or the first-run
  guide; the app's own unedited copies are updated on start, other copies are left alone.
- Widget: spring idle/wake, dock-like magnification (adjustable), focus on a project (rows go in, ring and back arrow
  come out) with progress columns, complete and add milestones, Connect button and folder drop, remembered size.

### Changed
- Project drag-to-group is pointer-driven (HTML5 drag and drop is unavailable alongside native file drops on Windows):
  a floating label follows the pointer, edges auto-scroll, Escape cancels.
- Settings schema 2: `pythonPath` is a user override only (version 1 stored the auto-detected interpreter).
- Collector: writing-activity day boundaries follow the computer's time zone (named by `RD_TIMEZONE` from the app)
  instead of a fixed Europe/London.
