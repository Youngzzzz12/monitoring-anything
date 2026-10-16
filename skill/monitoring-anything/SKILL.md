---
name: monitoring-anything
description: Track projects with the Monitoring Anything desktop app. Use when the user wants to monitor or track a project folder (paper, thesis, Word manuscript, simulation, ML training runs), asks how a project or paper is going, what to work on next, or wants to add, complete, rename, re-date or delete milestones.
---

# Monitoring Anything

The Monitoring Anything app follows projects: milestones, LaTeX/Word writing progress, simulation and
training runs. Drive it through its command line `ma`; every command prints one JSON object (`ok`, then data or `error`).

## Find `ma`

The app writes the launcher each time it starts:

- Windows: `%APPDATA%\io.github.monitoring-anything\ma.cmd`
- macOS: `~/Library/Application Support/io.github.monitoring-anything/ma`
- Linux: `~/.config/io.github.monitoring-anything/ma`

If it is missing, ask the user to start Monitoring Anything once. `ma where` shows the app, Python and workspace in use.
`ma` acts on the workspace open in the app (override with `--workspace PATH` before the command).

## Rules

- Research folders are read only: never run, compile, move or edit anything in them.
- Connecting a folder always goes through the app's plan card; the user confirms there. Do not edit workspace files
  by hand; use `ma` (milestone edits verify the exact line first).
- Report what `ma` returns; do not guess progress.

## Monitor a folder ("track this project")

1. `ma inspect --folder "<absolute path>"` lists matching monitoring types (`candidates`, best first, `meetsThreshold`).
2. A good match: `ma preview --folder "<path>" --profile-id <id>` to show what it reads now, then
   `ma connect --folder "<path>" --profile-id <id> [--title "..."] [--parent <projectId>]`.
3. No good match: `ma brief` gives the design rules, schema, an example and the saved types. Look at the folder's file
   names and a few short excerpts yourself, write a plan JSON (`{schemaVersion: "1.0", profile, project: {title, type},
   rationale}`) to a temporary file, check it with `ma preview --folder "<path>" --plan <file>` until `preview` shows
   sensible values, then `ma connect --folder "<path>" --plan <file>`.
4. Tell the user the plan card is open in Monitoring Anything and to press Connect (they can adjust type, milestones and
   parent there, and save the plan as a monitoring type for next time).

Built-in types: LaTeX paper, Word manuscript, machine-learning training run (Lightning metrics.csv / Keras history.csv),
Abaqus job, iSALE batch. Use `--parent` to file a project under an existing top-level project (`ma report` lists ids).

## Progress ("how is my thesis going?", "what next?")

- `ma report` - all projects: average completion, per-project milestones (family-wide), overdue items, sources (health,
  words today), and `nextUp` (3 items, the dashboard's rule).
- `ma report --project <id>` - one project with its sub-projects and 5 `nextUp` items.
- `nextUp[].reason`: `overdue`, `due` (within 14 days), `writing` (sections edited most recently), `next` (each project's
  next in turn). Summarise in plain language; mention stale or failing sources.

## Milestones

- `ma milestone list --project <id>` - each with `line`, `title`, `completed`, `due`, `chapter`, `stage`.
- `ma milestone add --project <id> --title "..." [--due YYYY-MM-DD] [--section <sectionId> --stage draft|figures|references|review|complete]`
- `ma milestone done|undo --project <id> --match "<part of title>"` (or `--line N`); a section milestone is accepted
  against the section's current text.
- `ma milestone rename --project <id> --match "..." --title "..."`
- `ma milestone due --project <id> --match "..." [--due YYYY-MM-DD]` (no `--due` clears it)
- `ma milestone delete --project <id> --match "..."` (confirm with the user first)

`--match` must fit exactly one milestone; on an ambiguity error, use the listed `line` instead.
