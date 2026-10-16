# Vendored collector: provenance

The Python collector is vendored from the author's Obsidian vault (not a git repository at the time of copying):

- Source: `<vault>/Research Dashboard/System/tools/` and `<vault>/Research Dashboard/System/profiles/`
  (the `obsidian-plugin/` sources and `__pycache__` were not copied; the upstream `tools/README.md` is kept as
  `README-upstream.md`, describing the CLI and JSON contracts).
- Initial snapshot: 2026-10-10 (commit "Vendor the Research Dashboard Python collector (unmodified)").
- Re-synced: 2026-10-11, adding sub-project discovery (`discovery.py`, `tests/test_discovery.py`, the `discover`
  command and the catalog `discovery` field). `cli.py` was merged three-way with the local changes; no conflicts.

## Local changes (app adaptation)

| File | Change |
| --- | --- |
| `tools/layout.py` (new) | Vault and app-workspace layouts; detection by `workspace.json` or `RD_LAYOUT` |
| `tools/workspace.py` (new) | `init-workspace`, `info` and the one-time `import-vault` converter |
| `tools/dashboard_core.py`, `project_catalog.py`, `onboarding.py`, `milestones.py`, `profiles.py`, `agents.py`, `reconciliation.py`, `discovery.py` | Paths resolved through `layout.py` instead of fixed `Research Dashboard/System/...` strings; app-layout project records live in `projects/<id>/project.json` with `milestones.md` beside them |
| `tools/cli.py` | `--workspace` alias of `--vault`; `info`, `init-workspace`, `import-vault`; layout-aware paths |
| `tools/adapters/latex_paper.py` (sections) | Per-section `changedAt`: the history keeps each section's content fingerprint, so the last collection at which a section's own text changed is known (`section_changes`); documented in `schemas/state.schema.json` |
| `tools/adapters/latex_paper.py` | Writing-activity day boundary: this computer's time zone, named by `$RD_TIMEZONE` (set by the app), instead of the fixed `Europe/London`; a bundled Python has no IANA zone database, so the local zone is used under that name |
| `tools/agents.py`, `tools/cli.py` | Agent presets carry suggested model ids (llm-wiki's verified ids) and install links; Claude Code reads stdin as text (`--input-format text`); new `plan test` (version, then one tiny read-only request that must answer `{"ok": true}`) and `plan detect` (Ollama / LM Studio on this computer) |
| `tools/adapters/declarative.py`, `profiles/builtin/word-manuscript.json`, `profiles/builtin/ml-training.json` (new) | A `docx` value rule (newest matching .docx: words, headings, comments, tracked revisions; read with zipfile) and two built-in declarative types: Word manuscript and machine-learning training run (Lightning metrics.csv / Keras history.csv, max_epochs from hparams.yaml, checkpoints) |
| `tools/adapters/script.py`, `tools/adapters/script_runner.py`, `tools/tests/test_script_adapter.py` (new) | Custom adapters: profiles may carry Python (`extract.script`) that passes a static check and runs in an isolated process behind an audit hook (read-only inside the folder); engine `script` in `adapters/__init__.py`, wired through `profiles.py`, `dashboard_core.py`, `onboarding.py`, `planning.py`; agent rule 8 and the schema describe it |
| `tools/ma.py`, `tools/tests/test_ma.py` (new) | `ma`, the agent command line: report (with the dashboard's Next up rule, kept equal to the view by `tests/shared/next-up.json`), milestone list/add/done/undo/rename/due/delete, brief (plan design kit), inspect/preview, and connect (hands the folder and plan to the app's plan card) |
| `tools/tests/test_profiles.py` | The built-in set includes the two new declarative types (only the original three delegate to adapters) |
| `tools/tests/test_app_workspace.py` (new) | App-layout and importer tests; activity time zone tests; agent connection tests |

Upstream behaviour on a vault is unchanged; the upstream tests run unmodified against the vault layout.

## Upstream file hashes at the last re-sync (SHA-256, LF line endings)

| File | SHA-256 (first 16) |
| --- | --- |
| `profiles/builtin/abaqus-job.json` | `639db7da1faf2153` |
| `profiles/builtin/isale-batch.json` | `4d85e85efc925b27` |
| `profiles/builtin/latex-paper.json` | `f2c0307acf7753b1` |
| `profiles/milestones/builtin/paper-section-draft-complete.json` | `4efcc31775a798ea` |
| `profiles/milestones/builtin/simulation-basic.json` | `5e143a51206f9a78` |
| `tools/README.md` | `6d18e34506ebdb4a` |
| `tools/adapters/__init__.py` | `8cdb978445630e8b` |
| `tools/adapters/abaqus.py` | `e28abbe9123ec44b` |
| `tools/adapters/common.py` | `64b40f1bc577d535` |
| `tools/adapters/declarative.py` | `7a9f145793ae0efc` |
| `tools/adapters/isale.py` | `494dd8817771da8c` |
| `tools/adapters/latex.py` | `0d0780857ba268c5` |
| `tools/adapters/latex_paper.py` | `ca8eaf92d28d9ad0` |
| `tools/adapters/scan.py` | `0c380bacef44e43f` |
| `tools/agents.py` | `efd8398906d9abe9` |
| `tools/cli.py` | `261a8ae8131f2ed8` |
| `tools/dashboard_core.py` | `b0b17c9eebd703a8` |
| `tools/discovery.py` | `b903dc8481b15a96` |
| `tools/milestones.py` | `e7dfd78e34112fca` |
| `tools/onboarding.py` | `7e021a23b477f1db` |
| `tools/planning.py` | `6fe36d5de3493797` |
| `tools/profiles.py` | `3f055c015495aad5` |
| `tools/project_catalog.py` | `7dbef5fe72a2df84` |
| `tools/reconciliation.py` | `5acfcb919861cd1b` |
| `tools/schemas/local.schema.json` | `33501f5ed65fc02a` |
| `tools/schemas/milestone-template.schema.json` | `9e859ba3f92cec42` |
| `tools/schemas/monitoring-profile.schema.json` | `20c20cf2ac9fb0c4` |
| `tools/schemas/project-catalog.schema.json` | `c4f9c95f01f45186` |
| `tools/schemas/project-record.schema.json` | `06ada4cb414e8159` |
| `tools/schemas/project.schema.json` | `9ebb6646ad816827` |
| `tools/schemas/state.schema.json` | `4d7f4ec4b7ead32c` |
| `tools/tests/test_agents.py` | `25b2455e49021860` |
| `tools/tests/test_dashboard.py` | `67637ad1fecd400a` |
| `tools/tests/test_discovery.py` | `dcb00b5295562d6d` |
| `tools/tests/test_latex_paper.py` | `df9f4484fc120f2c` |
| `tools/tests/test_onboarding.py` | `2457aaace519703e` |
| `tools/tests/test_profiles.py` | `7595f02fe24f21f4` |
| `tools/tests/test_project_catalog.py` | `301fabce3285cfc2` |
| `tools/tests/test_project_hierarchy.py` | `7815de96fbc7a233` |
| `tools/tests/test_reconciliation.py` | `cd16352652d5e27b` |
| `tools/tests/test_watcher.py` | `3f8ec03ba00866a1` |
| `tools/watcher.py` | `8327dedcbbe2b11e` |
