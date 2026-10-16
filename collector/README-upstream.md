# Dashboard CLI and adapters

Normal use is through the Connector and [[Research Dashboard/Guide|Dashboard guide]]. These commands are for verification, debugging and scripted workflows. Python 3.10+ and the standard library are sufficient.

Run from the vault root; options `--vault` and `--host-id` precede the command.

```powershell
$dashboardPython = '<path to python.exe>'
& $dashboardPython 'Research Dashboard/System/tools/cli.py' --help
& $dashboardPython 'Research Dashboard/System/tools/cli.py' projects
& $dashboardPython 'Research Dashboard/System/tools/cli.py' doctor
& $dashboardPython 'Research Dashboard/System/tools/cli.py' status
```

| Command | Role |
| --- | --- |
| `inspect --source <absolute folder>` | Read-only bounded discovery: connectable entries (`profiles`) plus scored monitoring-profile `candidates` |
| `connect --source ... --id ... --kind ... --title ... --adapter-stdin` | Register a source, create its research project Canvas, collect once; adapter JSON comes from stdin |
| `connect ... --profile <ID> \| --profile-stdin [--type T] [--parent P] [--milestone-template ID\|profile\|none]` | Connect with a library or inline monitoring profile; records `profileId` |
| `connect ... --dashboard-project <ID>` | Attach the source to an existing research project without replacing its notes or Canvas |
| `plan preview` (JSON on stdin) | Dry run of a profile against a folder; no writes |
| `plan manifest --source <folder> [--exclude GLOB ...]` | Bounded folder overview for an agent (tree, extensions, heads, partial profile hits) |
| `plan providers` | Agent provider presets (CLI agents, HTTP chat APIs) with PATH detection |
| `plan propose` (JSON on stdin) | Ask one agent for a monitoring plan; validate, one repair round, dry run; transcript in `data/agent-runs/` |
| `profiles list \| get --id \| validate \| save \| delete --id` | Manage the monitoring-profile library (stdin for validate/save) |
| `milestones list \| apply --project <ID> --template <ID> [--source <ID>] [--dry-run]` | Milestone templates; apply appends idempotently to Project.md |
| `projects` | Read the logical project catalog and validate source membership |
| `create-project --id <ID> --title <title> [--type thesis\|paper\|simulation\|experiment\|other] [--parent <ID>]` | Create a sourceless logical project (record, Canvas, cards, catalog with empty `sourceIds`); never overwrites; parent must be a live top-level project |
| `discover --all \| --project <ID>` | Sub-project discovery scan (read-only): new immediate subfolders of each discovery folder, scored with the profile library |
| `discover configure --project <ID> [--folder <abs> \| --clear] [--auto-accept on\|off] [--ignore GLOB ... \| --clear-ignore]` | Set a top-level project's discovery folder (host-local alias) and options; `discover show --project` reads them |
| `discover dismiss \| restore --project <ID> [--folder <name>]` | Hide (or bring back) a discovered folder; stored by relative name in `data/discovery/state.json` |
| `discover accept --project <ID> --folder <name> [--min-confidence 0.8]` | Connect one discovered folder as a sub-project with its best standalone candidate and milestone template (used by auto-accept) |
| `reconcile [--apply]` | Preview deleted-project cleanup; apply archives verified internal files recoverably in vault trash |
| `register` | Advanced source-only registration; no collection or logical Canvas generation |
| `collect [--project <source ID>]` | Collect once, writing JSON and Markdown snapshots |
| `status [--project <source ID>]` | Read snapshots and calculate freshness; no writes |
| `doctor` | Check configuration, host ownership and directory aliases |
| `watch [--project <source ID>] [--once]` | Foreground saved-change monitoring for LaTeX with periodic fallback; exit does not stop simulations |

## Data contract

`project_catalog.py` owns research project identity and source membership. Its catalog entries live in `System/config/catalog/`. Each refers to one editable `Projects/<ID>/Project.md`, one `Dashboard.canvas`, and zero or more source IDs.

- A sourceless project (`"sourceIds": []`) is valid everywhere: it is listed, collects nothing, and reconciliation treats it like any other project (archived only after its Project.md is deleted).
- Project.md carries `project_type` (`thesis`, `paper`, `simulation`, `experiment`, `other`; missing reads as `other`) and `parent_id` (a logical project ID or null). Folder connections write `project_type` from the first source kind (latex → paper, abaqus/isale → simulation) and `parent_id: null`.
- Hierarchy has one level: a project with `parent_id` cannot be a parent, a project with children cannot be given a parent, and self/cycles are rejected by the CLI and the Connector forms. The view tolerates older violations. See `schemas/project-record.schema.json`.

For compatibility, source configuration and state still call their source identity `projectId`. They use `System/config/projects/<source ID>.json`, `System/records/<source ID>/project.md` and `System/data/projects/<source ID>.json`. Do not confuse the number of source configurations with the number of research projects.

New owned source configurations also include `dashboardProjectId`. Sources whose owner record or catalog was deleted are excluded from collection and status. Independent source-only registrations remain supported. Reconciliation preserves unknown/shared files and all host mappings; it does not remove source folders. Only `reconcile --apply` moves known internal files to `.trash/research-dashboard/` with a recovery manifest.

Source configuration is shared; aliases live in `System/config/local/<host ID>.json`. Only the configured collector host writes a source's state. The collector has no distributed lock, so preserve a single writer for each source. All output paths are fixed within the vault. Relative source paths cannot escape their alias root.

On registration failure, only exact newly created files and new empty folders are rolled back; matching directory aliases are restored. A failed first collection leaves a registered project with an error indicator and usable Canvas. Adding another source preserves existing research notes, tasks, card wrappers and user-edited Canvas geometry.

JSON is authoritative for automatic state. Generated Markdown is a compatibility projection. On collection error, prior valid measurements and their last successful timestamp are retained and marked as an error. A parsing failure does not mean a failed solver. A committed snapshot produces one NDJSON `snapshot-committed` event, immediately notifying the Connector; the view also checks freshness every 30 seconds independently of file modification.

## Adapter scope

- **LaTeX:** Resolves literal source dependencies from `adapter.mainFile` (legacy profiles use the first configured TeX entry), including current includes, bibliography, graphic paths and matching build logs. TeXcount stdin-only body subcounts are used when available; bounded parsing has a labelled heuristic fallback. No source compilation, content-quality inference or automatic milestone acceptance occurs. Log evidence establishes build status; PDF existence alone does not. Dependency changes after a build mark it outdated. Dynamic macro-generated dependencies remain coverage warnings.

### Paper observations and saved-change monitoring

One `details` item with `kind: paper` supplies hierarchical sections, stable literal-label identities, body counts, source fingerprints, dependency coverage and nullable day/week net changes. Parent counts include descendants; totals sum only top-level sections. History lives in `data/history/<source ID>.json`, bounded to 35 days with hourly observations and preserved first observations for London calendar days.

Prefer a valid recorded baseline within two hours before the London day or rolling seven-day boundary when available. Otherwise Today uses the first observation for that London day, and Last 7 days uses the earliest available observation inside its rolling window. These fallbacks are partial observed intervals with actual `baselineAt` and coverage metadata; they do not fabricate a midnight count or require a seven-day wait. `activity.trackingStartedAt` exposes when comparable tracking began. `dayStarts` retains first-day observations separately so later hourly consolidation does not erase the practical daily baseline. Missing or incomparable section observations remain null, and a counting-policy/main-document change starts fresh comparable history. The view exposes actual baseline timestamps, coverage and counting policy.

Normal use needs no baseline script, terminal reset, or manual date maintenance. After reopening/reloading the Connector, start live updates once and save normally. A single collect also creates its first partial observation while monitoring is stopped, but it does not provide continuous monitoring. Chapter milestones and acceptance remain user-defined.

`watcher.py` observes supported LaTeX/source/build file names and stat changes every second, debounces save bursts for one second and serializes collection. It skips linked directories and enforces a 25,000-file scan limit. It does not observe unsaved editor buffers. Sources still reconcile on their configured interval. The watch stream emits NDJSON commit events; errors and superseded collections never masquerade as successful commits. Internal heartbeat output is refreshed every 15 seconds.

Before committing, the collector verifies source read fingerprints, registration, owning project liveness, local mappings and the previous snapshot version. Obsolete results are discarded and the next stable observation can retry. Keep a single collection writer per host/source. Reconciliation also recoverably archives owned paper history when a project is removed.
- **Abaqus:** Reads selected `.sta`, `.msg` and `.log` files. Explicit terminal evidence and configured activity rules determine status. Multiple Steps cannot automatically be combined into one percentage. A progress ratio requires a defined current value, target and unit; a jobs list supplies a planned denominator.
- **iSALE metadata:** Supports the recognised batch workflow. Completion requires the configured status mapping and valid completion evidence. `prepared` is unknown, and a binary output file is not completion evidence. Observed metadata alone leaves planned count unknown; explicit cases define a plan.
- **iSALE regex:** Requires patterns verified against the actual software logs. Progress patterns need a named `value` group, step patterns a `step` group. Missing definitions retain unknown values.

Logs are read with a 2 MiB bound. Binary result databases are not status inputs. Reused append-only logs need clear run boundaries. No adapter compiles, submits or stops a solver, performs post-processing, or modifies raw source files. See `System/examples/` for unconnected adapter options and `schemas/` for contracts.

## Monitoring profiles (smart onboarding core)

A monitoring profile is declarative JSON: detection rules (`detect`), extraction rules (`extract`), a project type, an optional milestone template and a `subprojectRole` (`standalone` | `part`). Schema: `schemas/monitoring-profile.schema.json`; `profiles.py` validates the full semantics.

- **Library.** Built-ins ship in `System/profiles/builtin/` (`latex-paper`, `abaqus-job`, `isale-batch`). User profiles live in `System/profiles/user/<id>.json`; saving over one archives the previous file in `profiles/user/.history/` and increments `origin.version`; deleting moves it there too. Built-in IDs cannot be saved over or deleted. An invalid user profile is reported (`libraryErrors`/`warnings`), never fatal.
- **Built-ins preserve current behaviour.** Their `extract` is `{"adapter": "latex" | "abaqus" | "isale"}`, so collection still runs the existing adapters with the exact options produced by the existing inspector. Detection confirms or caps them: if the existing inspector found a connectable entry of that kind, confidence is at least 0.9; otherwise at most 0.5. Legacy `inspect`/`connect` calls are unchanged.
- **Detection.** All `allOf` rules and at least one `anyOf` rule (if listed) are required; any `noneOf` hit excludes. Confidence = 0.7 + 0.3 × (share of `anyOf` matched), or 1.0 without `anyOf`; partial matches score ≤ 0.6. Rules are `glob` (+`minCount`) or `contains {glob, regex, maxBytes ≤ 256 KiB}` (≤ 50 files opened per rule). Globs are relative, case-insensitive; `**` spans folders; wildcards skip `.git`, `node_modules`, `__pycache__`, venvs; links are never followed.
- **Declarative collector** (`adapters/declarative.py`, selected by `adapter.engine == "declarative"`): `countGlob`/`glob`, `regex` (newest matching file, named group `value` → group 1 → whole match, `match: last|first|count`, number/integer/string coercion), `jsonPath` (`file.json#$.a.b[0].length`; `.key`, `[n]`, `['key']`, `.length` only), `csv` (column by header/index; `last|first|max|min|sum|mean|count`), `fromLog` (runtime status; newest file) / `cases.fromLogs` (one log per case), `artifacts` globs (listed with `verifiedAt: null`) and `metrics` (written to `details` as `{kind: "metric", id, label, value, unit}`). Output uses the existing snapshot fields only (`runtime.status/progress/currentValue/targetValue/unit/step/runId`, `cases.*`, `artifacts`, `details`). Missing evidence gives `null` plus a warning, never zero. Caps: 2 MiB per file, 8 MiB total (≤ 16 MiB via `limits`), 10 s (≤ 20 s), 20 000 directory entries, 500 case logs, 200 artifacts, 100 rules; nested-quantifier regexes are rejected. Nothing is executed or written; no TeXcount call happens in this engine.
- **Display kind.** The view accepts only `latex|abaqus|isale` source kinds, so a declarative source stores its profile's `sourceKind` (default `abaqus`: generic run + case rendering) as `kind`. `kind` then selects only the label/icon. A neutral display kind would need a later view change.
- **Milestone templates.** `System/profiles/milestones/builtin/` (`paper-section-draft-complete`, `simulation-basic`), user templates in `System/profiles/milestones/user/`; schema `schemas/milestone-template.schema.json`. `per-section` needs a collected paper snapshot and adds one task per leaf section and stage, bound with `[paper:: <source>] [chapter:: <section id>] [stage:: draft|complete]`, under `### <top-level section>` inside `## Milestones`. `static` adds plain tasks. Lines follow the Connector's `milestoneTask` escaping. Existing identical bindings (or identical titles for static tasks) are skipped, so re-applying is a no-op. A profile may also carry an inline `milestoneTemplate: [{title, when?}]` (`when` is informational).

## UI contract for smart onboarding

All commands print one JSON document on stdout. Usage, validation-of-action and IO errors print `Type: message` on stderr with exit code 2. Evaluation commands (`plan preview`, `profiles validate`) exit 0 whenever the request parses and report problems in `ok`/`errors`. `connect` keeps its existing rule: exit 1 with a JSON body when the connection succeeded but the first collection failed.

**1. Detection: `inspect --source <abs>`** — unchanged fields `sourcePath`, `title`, `suggestedId`, `profiles` (connectable legacy entries with `adapter`), `warnings`, plus `candidates` sorted by confidence:

```json
{"candidates": [{"profileId": "matlab-sweep", "title": "MATLAB parameter sweep", "projectType": "simulation",
  "sourceKind": "isale", "engine": "declarative", "origin": "user", "confidence": 1.0, "threshold": 0.8,
  "meetsThreshold": true,
  "matched": ["allOf: files matching sweep_config.json — 1 file(s)", "anyOf: files matching results/*.mat — 2 file(s)",
              "noneOf: no files matching main.tex"],
  "missed": [], "excluded": [], "subprojectRole": "standalone", "milestoneTemplate": "simulation-basic",
  "inspectedProfileIds": [], "suggestedId": "sweep-matlab-sweep", "suggestedTitle": "sweep · MATLAB parameter sweep"}]}
```

For `engine: "adapter"` candidates, `inspectedProfileIds` lists the matching entries of `profiles[]` (use their `adapter` for preview/connect) and `suggestedId/suggestedTitle` come from the first one.

**2. Dry run: `plan preview`** — stdin `{"source": "<abs>", "profileId": "<id>"}` or `{"source", "profile": {...}}` (a bare profile or an agent plan `{"profile": {...}}`). Adapter-backed profiles accept optional `"adapter"` (exact options) or `"inspectedProfileId"`; otherwise the first inspected entry of that kind is used.

```json
{"ok": true, "source": "C:\\...\\sweep", "profileId": "matlab-sweep", "engine": "declarative", "sourceKind": "isale",
 "projectType": "simulation", "adapter": {"engine": "declarative", "profileId": "matlab-sweep", "profileVersion": 1, "extract": {}},
 "preview": {"runtime": {"status": "unknown", "progress": 0.248, "step": "solve", "currentValue": 1240, "targetValue": 5000,
                         "unit": "steps", "runId": "latest@1791624659180361700"},
             "cases": {"planned": 4, "completed": 2, "running": null, "failed": null, "unknown": null},
             "artifacts": [{"type": "pdf", "path": "C:\\...\\figures\\a.pdf", "verifiedAt": null, "runId": null}], "artifactCount": 1,
             "chapters": null, "metrics": [{"id": "peak-stress", "label": "Peak stress", "value": 30.25, "unit": "MPa"}],
             "caseRecords": [], "caseRecordCount": 0, "build": null, "sourceFileCount": 6, "sourceFiles": ["..."],
             "sourceModifiedAt": "2026-10-10T09:30:59Z", "elapsedSeconds": 0.016, "collectorVersion": "1.2.0"},
 "warnings": [], "errors": []}
```

For LaTeX, `preview.chapters` is `{"count", "topLevel", "leafCount", "totalWords", "mainFile", "countingMethod", "titles"}` and `preview.build` reports `buildOutdated`; `inspectedProfileId` names the entry used. On failure: `"ok": false`, `"preview": null` (profile/validation errors) or a preview with `errors` (for example values that would not fit the snapshot schema, or "No file matched the profile's patterns").

**3. Connect with a profile: `connect`**

```text
connect --source <abs> --id <sourceId> --title <t> --profile <id>                  # declarative library profile, no stdin
connect --source <abs> --id <sourceId> --title <t> --profile latex-paper --adapter-stdin   # built-in: stdin = inspected adapter
connect --source <abs> --id <sourceId> --title <t> --profile-stdin                 # stdin = profile or {"profile", "adapter"?}
  [--kind latex|abaqus|isale]  (optional with a profile; must match it)
  [--type thesis|paper|simulation|experiment|other] [--parent <top-level project>]  (new project only)
  [--milestone-template <id>|profile|none] [--dashboard-project <id>] [--interval 60] [--stale-after 180]
```

The result adds `profileId`, `kind`, `projectType` (null when attaching), `parentId`, and, when a template was requested, `milestones` (the `milestones apply` result, or `{"error", "addedCount": 0}`; a template failure never undoes the connection). The source config records top-level `profileId`; a declarative adapter is stored as `{"engine": "declarative", "profileId", "profileVersion", "extract"}` (a snapshot of the profile's rules at connect time). `--parent` uses the existing hierarchy rule (live, top-level parent); `--type/--parent` with `--dashboard-project` is rejected before any write.

**4. Milestones**

```text
milestones list                       -> {"milestoneTemplates": [{"id", "title", "description", "mode", "appliesTo", "origin",
                                          "requiresPaperSnapshot", "stages", "taskCount"}], "errors": []}
milestones apply --project <id> --template <id> [--source <paperSourceId>] [--dry-run]
  -> {"projectId", "templateId", "sourceId", "projectNote", "added": ["- [ ] Aim — draft #milestone [paper:: p] [chapter:: section-…] [stage:: draft]"],
      "addedCount", "skipped", "dryRun"}
```

**5. Profile library**

```text
profiles list                 -> {"profiles": [{"id", "title", "description", "projectType", "sourceKind", "engine": "adapter|declarative",
                                  "origin": "builtin|user", "version", "createdBy", "subprojectRole", "milestoneTemplate", "path"}],
                                  "milestoneTemplates": [...], "errors": [{"path", "error"}]}
profiles get --id <id>        -> {"origin": "builtin|user", "profile": {...}}
profiles validate --stdin     -> {"ok": false, "profileId": "x", "errors": ["detect needs at least one allOf or anyOf rule"], "warnings": []}
profiles save --stdin         -> {"saved": "matlab-sweep", "path": "Research Dashboard/System/profiles/user/matlab-sweep.json",
                                  "version": 2, "previousVersionPath": "Research Dashboard/System/profiles/user/.history/matlab-sweep.v1.<stamp>.json",
                                  "unchanged": false}
profiles delete --id <id>     -> {"deleted": "matlab-sweep", "archivedTo": "Research Dashboard/System/profiles/user/.history/matlab-sweep.deleted.<stamp>.json"}
```

`validate`/`save` accept a bare profile or `{"profile": {...}}` (256 KiB max). `save` and `delete` refuse built-in IDs (exit 2).

**6. Agent manifest: `plan manifest --source <abs> [--max-depth 1-8 (4)] [--max-entries 20-2000 (400)] [--excerpts 0-50 (20)] [--excerpt-bytes 128-8192 (2048)]`**

```json
{"schemaVersion": "1.0", "sourcePath": "C:\\...\\sweep", "generatedAt": "…", "limits": {"maxDepth": 4, "maxEntries": 400, "…": "…"},
 "totals": {"files": 6, "directories": 3, "bytes": 124, "ignoredDirectories": 0},
 "extensions": [{"extension": ".mat", "count": 2, "bytes": 2}],
 "tree": [{"path": "logs/", "type": "dir"}, {"path": "logs/latest.log", "type": "file", "size": 33, "modifiedAt": "…"},
          {"path": "build/", "type": "dir", "ignored": true}, {"path": "a/b/c/d/", "type": "dir", "notExpanded": true}],
 "excerpts": [{"path": "logs/latest.log", "size": 33, "head": "Phase: solve\nStep 1240 of 5000\n", "truncated": false}],
 "profiles": [{"profileId", "title", "origin", "engine", "confidence", "threshold", "meetsThreshold", "matched", "missed", "excluded"}],
 "libraryErrors": [], "warnings": [], "elapsedSeconds": 0.05}
```

The tree is breadth-first and capped; totals and the histogram cover every scanned entry (≤ 20 000, ≤ 8 s). Ignored: `.git`, `node_modules`, `build`, `tmp`, `__pycache__`, venvs and editor folders. Heads are taken only from small text files (no NUL bytes, ≤ 3 per extension); credential-like names (`.env`, `*secret*`, `*token*`, `*.pem`, `credentials*`…) are never opened. `profiles` lists library profiles with at least one positive rule hit (rule-only scores; the existing inspector is not run here).

`--exclude GLOB` (repeatable; `privacy.exclude` in `plan propose`) removes matching files and folders from the tree, totals, histogram and excerpts, and the manifest does not descend into excluded folders. A pattern without `/` matches a file or folder name at any depth (like `.gitignore`). A pattern with `/` matches the path from the source root, and a trailing `/**` is the same as the folder itself. `totals.excludedEntries` counts what was left out and `limits.exclude` echoes the normalized globs. Profile detection in `profiles` still runs over the whole folder, but it reports only rule descriptions and counts.

**7. Agent providers: `plan providers`**

```json
{"providers": [
 {"id": "claude-code", "title": "Claude Code", "kind": "cli", "command": "claude",
  "args": ["--print", "--output-format", "json", "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
           "--permission-mode", "dontAsk", "--no-session-persistence", "--strict-mcp-config"],
  "modelArgs": ["--model", "{model}"], "model": "", "agentic": true, "installed": true,
  "path": "C:\\Users\\...\\claude.exe", "notes": "…"},
 {"id": "codex", "kind": "cli", "command": "codex",
  "args": ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--color", "never",
           "--output-last-message", "{outputFile}"], "modelArgs": ["-m", "{model}"], "agentic": true, "…": "…"},
 {"id": "gemini-cli", "kind": "cli", "command": "gemini", "args": ["--output-format", "json", "--approval-mode", "default"],
  "modelArgs": ["--model", "{model}"], "agentic": true, "…": "…"},
 {"id": "openai-compatible", "kind": "http", "protocol": "openai", "baseUrl": "http://localhost:11434/v1", "model": "",
  "agentic": false, "installed": null, "…": "…"},
 {"id": "anthropic-api", "kind": "http", "protocol": "anthropic", "baseUrl": "https://api.anthropic.com", "model": "",
  "agentic": false, "installed": null, "…": "…"},
 {"id": "custom", "kind": "cli", "command": "", "args": [], "modelArgs": [], "agentic": false, "installed": null, "…": "…"}]}
```

`installed` is true when the CLI resolves on PATH (or when an absolute command exists). It is `null` for HTTP providers and for `custom` without a command. Presets are editable: the UI passes the full (edited) object back to `plan propose`. A provider object is merged over the preset with the same `id`, so `{"id": "codex"}` alone is enough; an unknown `id` needs `kind` and the fields below. Accepted keys: `id, title, kind, command, args, modelArgs, model, agentic, baseUrl, protocol (openai|anthropic), apiKey, apiKeyEnv, maxTokens (default 8192), temperature, jsonMode` (OpenAI `response_format: json_object`). Input values for `installed`, `path` and `notes` are ignored.

- **CLI.** Runs with `shell=False`, the source folder as working directory, the inherited environment plus `NO_COLOR=1`, and no console window. A timeout kills the process tree, and stdout is capped at 2 MiB. Placeholders in `args`/`modelArgs`:
  - `{prompt}`: the prompt as one argument (at most 30 000 characters; refused for `.bat`/`.cmd` commands);
  - `{promptFile}`: a temporary UTF-8 file holding the prompt;
  - `{outputFile}`: a temporary file, read as the response when non-empty (otherwise stdout is used);
  - `{source}` and `{model}`.

  Without `{prompt}`/`{promptFile}`, the prompt is written to stdin. `modelArgs` are appended only when `model` is non-empty. Arguments for `.bat`/`.cmd` shims (for example npm's `codex.cmd`) may not contain `% ! ^ & | < > "` or line breaks.
- **HTTP.** `openai`: `POST {baseUrl}/chat/completions`, with `Authorization: Bearer <apiKey>` only when a key is given. `anthropic`: `POST {baseUrl}/v1/messages` with `x-api-key` and `anthropic-version: 2023-06-01`; an apiKey is required. `model` is required for both. Loopback URLs bypass proxies. Responses are capped at 2 MiB.
- **Keys.** `apiKey` (or `apiKeyEnv`, the name of an environment variable) is used only in request headers. It never goes into the prompt, the transcript or the output. The transcript and output are also scrubbed for the key value and for common key shapes (`sk-…`, `sk-ant-…`, `AIza…`, bearer tokens). The plugin keeps keys in its settings, never in vault files.

**8. Agent plan: `plan propose`** — stdin:

```json
{"source": "C:\\...\\sweep", "provider": {"id": "claude-code", "model": ""},
 "instructions": "Also monitor the Abaqus jobs under runs/", "previousPlan": {"profile": {"…": "…"}},
 "privacy": {"exclude": ["private", "*.xlsx"], "excerpts": true}, "timeoutSeconds": 300}
```

These fields are optional:
- `instructions`: at most 8000 characters.
- `previousPlan`: for a revision; the previous plan is sent back with the instructions.
- `privacy`: `exclude` globs and `excerpts`.
- `timeoutSeconds`: 1–3600 per provider call (default 300).

The prompt contains:
- the task;
- the design rules: read-only, declarative profile only, the allowed detection rules and extraction primitives, and `profileId` preferred when a built-in fits;
- the plan format with a valid example;
- `schemas/monitoring-profile.schema.json`;
- the profile library (detect rules, plus extract for small user profiles) and the milestone templates;
- the `plan manifest` output, with excludes applied. With `excerpts: false` only names are sent, and the absolute path is replaced by the folder name.

Agentic providers are told they may browse the working directory read-only and must not open excluded paths.

```json
{"ok": true,
 "plan": {"schemaVersion": "1.0", "profile": {"…monitoring profile…": "…"}, "project": {"title": "…", "type": "simulation", "parentSuggestion": null},
          "milestones": ["…"], "rationale": "…", "warnings": []},
 "validation": {"ok": true, "errors": [], "warnings": []},
 "preview": {"ok": true, "profileId": "solver-runs", "preview": {"runtime": {"…": "…"}, "cases": {"…": "…"}}, "…": "same as plan preview"},
 "repaired": false,
 "provider": {"id": "claude-code", "kind": "cli", "model": null, "durationMs": 37577, "attempts": 1},
 "transcriptPath": "Research Dashboard/System/data/agent-runs/20261010T123545Z-claude-code-d7c638.json",
 "warnings": ["Manifest: 1 file(s) with credential-like names were never opened."], "errors": []}
```

- **Parsing.** The plan is taken from the whole response, a fenced code block or the first embedded JSON object. Known wrappers are removed first: Claude Code `--output-format json` (`result`, `is_error`), Gemini (`response`), OpenAI (`choices[0].message.content`) and Anthropic messages. A bare profile is wrapped as `{"profile": …}`, with a warning. A plan may instead reference a library profile with `"profileId"`.
- **Validation.** `profile` is checked with the `profiles validate` logic. The plan fields are checked too: `project.type` must be a project type, `title`/`parentSuggestion` must be short, `milestones` must be strings, and `rationale` at most 4000 characters. A profile without `origin` gets `{"createdBy": "agent:<provider id>", "approvedAt": null, "version": 1}`. An inline profile that reuses a built-in id with different content is an error. Unknown plan keys give warnings.
- **Repair.** A repair round happens when the response has no JSON, the plan fails validation, or its dry run fails (`preview.ok` false, for example when no file matched). The full prompt is sent once more with the previous answer and the error list (`repaired: true`, `attempts: 2`). There is never a third call. Transport failures (not installed, timeout, HTTP error, Claude `is_error`) are not retried.
- **Result.** `ok` means a valid plan and a successful dry run. `plan` is the last parsed plan, which may be invalid, or `null` when nothing was parsed. `preview` is the `plan preview` response for a valid plan, else `null`. Provider and run problems are in `errors`, plan problems in `validation.errors`, and dry-run problems in `preview.errors`.
- **Exit codes.** Exit 0 whenever the request parsed, including provider failures. Exit 2 (`Type: message` on stderr) for bad input:
  - a missing, relative or nonexistent `source`;
  - a malformed `provider` (unknown `kind`, HTTP without `model`, bad args);
  - a bad `privacy`, `timeoutSeconds` or `instructions` value;
  - a request over 512 KiB.
- **Transcript.** Written to `Research Dashboard/System/data/agent-runs/<UTC stamp>-<provider id>-<random>.json`. It holds the request summary, the provider (key redacted) and, for each attempt: the prompt, the argv (a `{prompt}` argument is not repeated) or URL, capped stdout/stderr, the exit code or HTTP status, the response text, the duration, `timedOut` and any error. It ends with the final validation result. The newest 50 runs are kept.
- **Safety.** Nothing from a plan or response is executed: it is parsed as data, validated, and dry-run with the read-only collectors. The prompt states that only names and short excerpts may be read, and that nothing may be written or run. Each CLI preset adds its tool's own read-only controls:
  - Claude: only Read/Glob/Grep, `dontAsk`, no MCP;
  - Codex: `--sandbox read-only`;
  - Gemini: headless mode refuses tools that need approval.

  Credential-like files are never put in the manifest.

## Sub-project discovery (`discovery.py`)

- **Setting.** A top-level project may have one discovery folder: scanned, never monitored. The absolute path is stored only in `config/local/<host>.json` as alias `discovery:<projectId>` (not a valid source ID, so it never collides). The catalog gets `"discovery": {"rootAlias", "autoAccept", "ignore"}` (schema in `schemas/project-catalog.schema.json`). A sub-project cannot take a folder; a folder inside the vault is refused. A new folder resets that project's dismissals; `--clear` removes alias, catalog key and dismissals. Another computer sees the setting with `available: false` until it sets its own folder.
- **Scan rules.** Only immediate subfolders. Skipped: hidden (`.name`, including `.git`), `tmp`, `build`, `node_modules`, `__pycache__`, the per-project `ignore` globs (case-insensitive `fnmatch` on the name), linked folders, folders containing or inside the vault, folders that equal, contain or sit inside a live source mapped on this host (by resolved path), and dismissed names. Each remaining folder runs `inspect`; the first candidate meeting its threshold is `bestCandidate`. A `subprojectRole: "part"` best match is absorbed (not suggested); no match gives `bestCandidate: null` (the UI offers Ask agent). At most 60 folders per root and 20 s per call (`truncated` + warning).
- **Output.**

```json
{"suggestions": [{"id": "phd:review-2027", "projectId": "phd", "parentId": "phd", "projectTitle": "PhD", "folder": "C:\\...\\papers\\review-2027",
  "relative": "review-2027", "rootName": "papers", "autoAccept": false, "suggestedTitle": "review-2027", "suggestedId": "review-2027",
  "bestCandidate": {"profileId": "latex-paper", "title": "LaTeX paper or thesis", "projectType": "paper", "confidence": 0.9,
                    "subprojectRole": "standalone", "sourceKind": "latex", "engine": "adapter", "origin": "builtin",
                    "milestoneTemplate": "paper-section-draft-complete"}}],
 "dismissed": 1, "projects": [{"projectId": "phd", "available": true, "error": null, "rootName": "papers", "autoAccept": false,
  "scanned": 3, "connected": 1, "dismissed": 1, "ignored": 7, "absorbed": 1, "truncated": false}], "warnings": [], "scannedAt": "…", "elapsedSeconds": 0.4}
```

`parentId` is the project's top-level ancestor (one displayed level), so a project later moved under another files its suggestions there. `suggestedId` is the folder name made unique against catalogs, sources, records and project folders.
- **Accept.** `discover accept` re-scans that one folder, requires a standalone best candidate with confidence ≥ `--min-confidence`, then runs `connect` with that profile (built-ins use the inspected adapter), `--parent <parentId>` and the candidate's milestone template. Dismissed, connected, ignored or absorbed folders are refused (exit 2).

## Verification

```powershell
& $dashboardPython -m unittest discover -s 'Research Dashboard/System/tools/tests' -v
```

Tests use isolated temporary sources and vaults. They verify grouping, rollback, path safety, source preservation, error retention, parsing boundaries and Canvas preservation. View and plugin tests are documented in their own test folders. The plugin build and deployment instructions are in `obsidian-plugin/README.md`.
