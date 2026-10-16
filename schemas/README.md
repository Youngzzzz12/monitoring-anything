# Workspace schemas

A workspace is a plain folder; every file is JSON or Markdown.

| File | Schema |
| --- | --- |
| `workspace.json` | [`workspace.schema.json`](workspace.schema.json) |
| `projects/<id>/project.json` | [`project.schema.json`](project.schema.json) |
| `projects/<id>/milestones.md` | GFM task list, see below |
| `projects/<id>/notes.md` | Optional free text (written by the Obsidian importer; not read in v0.1) |
| `sources/<id>.json` | Collector configuration: [`collector/tools/schemas/project.schema.json`](../collector/tools/schemas/project.schema.json); `projectNote` is `sources/<id>.md` |
| `sources/<id>.md` | Generated source record |
| `local/<host>.json` | Folder mappings of one computer: [`collector/tools/schemas/local.schema.json`](../collector/tools/schemas/local.schema.json) |
| `data/<id>.json` | Collector snapshot: [`collector/tools/schemas/state.schema.json`](../collector/tools/schemas/state.schema.json) |
| `data/history/<id>.json` | Paper word-count observations (collector-owned) |
| `profiles/user/<id>.json` | Monitoring profiles: [`collector/tools/schemas/monitoring-profile.schema.json`](../collector/tools/schemas/monitoring-profile.schema.json) |
| `profiles/milestones/user/<id>.json` | Milestone templates: [`collector/tools/schemas/milestone-template.schema.json`](../collector/tools/schemas/milestone-template.schema.json) |

## milestones.md

Milestones are task lines tagged `#milestone`, optionally with Dataview-style inline fields. The format is identical to
the Obsidian Research Dashboard, so the collector's milestone templates and the importer keep working:

```markdown
# Milestones

- [ ] Define the first deliverable [due:: 2026-12-01] #milestone
### Introduction
- [ ] Background — draft #milestone [paper:: thesis-tex] [chapter:: section-209639ae90d2] [stage:: draft]
- [x] Background — complete #milestone [paper:: thesis-tex] [chapter:: section-209639ae90d2] [stage:: complete] [accepted_hash:: <sha256>]
```

`paper` names a LaTeX source, `chapter` a section ID from its snapshot and `stage` one of draft, figures, references,
review, complete. `accepted_hash` records the section's source hash when it was accepted, so later edits show
"Changed since acceptance". Tasks inside fenced code blocks are ignored.
