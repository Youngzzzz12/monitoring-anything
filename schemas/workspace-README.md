# Monitoring Anything workspace

This folder is owned by the Monitoring Anything desktop app. Every file is plain
JSON or Markdown, so it can be read, versioned and backed up like any other folder.

| Path | Contents |
| --- | --- |
| `workspace.json` | Workspace marker and display name |
| `projects/<id>/project.json` | Project record: title, type, parent, stage, health, next step, due date, resources, sources |
| `projects/<id>/milestones.md` | Milestones as a Markdown task list (`- [ ] Title #milestone [due:: 2026-12-01]`) |
| `sources/<id>.json` | Collector configuration for one monitored folder (LaTeX paper, Abaqus job, iSALE batch, custom profile) |
| `local/<host>.json` | This computer's folder mappings (machine-specific) |
| `data/` | Generated snapshots and history; rebuilt by the collector |
| `profiles/` | Saved monitoring profiles and milestone templates |
| `.trash/` | Recoverable removals |

The external paper and simulation folders are only read, never modified.
