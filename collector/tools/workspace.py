"""App workspace helpers: create a workspace, describe it, and import an Obsidian vault.

The importer is a one-time conversion from the Obsidian plugin's
``Research Dashboard/`` folders into the desktop app workspace. It reads the
vault with the vault layout and writes the app layout; it never modifies the
vault and never overwrites a file that already exists in the workspace.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from adapters.common import UTC, iso
from dashboard_core import (atomic_json, atomic_text, output_path, read_json, render_new_project, validate_config,
                            validate_id, validate_state)
from layout import APP, VAULT, WORKSPACE_MARKER, layout_for
from project_catalog import PROJECT_TYPES, validate_catalog

WORKSPACE_KIND = "monitoring-anything-workspace"
WORKSPACE_DIRS = ("projects", "sources", "local", "data/history", "profiles/user", "profiles/milestones/user")
STAGES = {"planned", "preparation", "modeling", "writing", "computation", "postprocessing", "validation",
          "review", "completed", "archived"}
HEALTH = {"active", "blocked", "paused", "completed"}
KIND_TYPES = {"latex": "paper", "abaqus": "simulation", "isale": "simulation"}
MAX_NOTE_BYTES = 2 * 1024 * 1024
WORKSPACE_README = """# Monitoring Anything workspace

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
"""


def workspace_info(root: Path, version: str) -> dict[str, Any]:
    layout = layout_for(root)
    marker = root / WORKSPACE_MARKER
    meta: dict[str, Any] | None = None
    if marker.is_file():
        try:
            meta = read_json(marker)
        except (OSError, ValueError):
            meta = None
    return {"collectorVersion": version, "layout": layout.name, "root": str(root), "workspace": meta}


def init_workspace(root: Path, name: str | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """Create an empty app workspace. An existing workspace is left as it is; other content is refused."""
    root = Path(root)
    marker = root / WORKSPACE_MARKER
    if marker.is_file():
        return {"created": False, "root": str(root.resolve()), "workspace": read_json(marker)}
    if root.exists() and not root.is_dir():
        raise NotADirectoryError("The workspace path is a file")
    if root.is_dir() and any(not child.name.startswith(".") for child in root.iterdir()):
        raise FileExistsError("Choose an empty folder for a new workspace (or an existing workspace)")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    meta = {"schemaVersion": "1.0", "kind": WORKSPACE_KIND, "name": (name or root.name or "Research").strip()[:200],
            "layout": APP.name, "createdAt": iso(now or dt.datetime.now(UTC))}
    for folder in WORKSPACE_DIRS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    atomic_text(root / "README.md", WORKSPACE_README)
    atomic_json(marker, meta, exclusive=True)
    return {"created": True, "root": str(root), "workspace": meta}


# --------------------------------------------------------------------------- frontmatter

def _scalar(value: str) -> Any:
    text = value.strip()
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    if text in ("true", "True", "yes"):
        return True
    if text in ("false", "False", "no"):
        return False
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(part) for part in inner.split(",")] if inner else []
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Simple YAML frontmatter (scalars, inline lists, block lists) without a YAML dependency."""
    match = re.match(r"\A﻿?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", content, re.DOTALL)
    if not match:
        return {}, content
    fields: dict[str, Any] = {}
    current: str | None = None
    for line in match.group(1).splitlines():
        item = re.match(r"^\s+-\s*(.*)$", line)
        if item and current is not None:
            if not isinstance(fields.get(current), list):
                fields[current] = []
            fields[current].append(_scalar(item.group(1)))
            continue
        pair = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if pair:
            current = pair.group(1)
            fields[current] = _scalar(pair.group(2))
    return fields, content[match.end():]


def split_milestones(body: str) -> tuple[str, str]:
    """(milestone section without its heading, remaining body). Fenced code is never treated as a heading."""
    lines = body.replace("\r\n", "\n").split("\n")
    fence: str | None = None
    start = end = None
    level = 0
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) and not marker.group(2).strip():
                fence = None
            continue
        if fence is not None:
            continue
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not heading:
            continue
        if start is None and len(heading.group(1)) <= 2 and heading.group(2).strip().lower() == "milestones":
            start, level = index, len(heading.group(1))
        elif start is not None and len(heading.group(1)) <= level:
            end = index
            break
    if start is None:
        return "", body
    end = len(lines) if end is None else end
    section = "\n".join(lines[start + 1:end]).strip("\n")
    rest = "\n".join(lines[:start] + lines[end:])
    return section, rest


def _clean_notes(rest: str, title: str) -> str:
    """Drop the generated navigation line and title heading; keep the user's own sections."""
    lines = [line for line in rest.replace("\r\n", "\n").split("\n")
             if not re.match(r"^Open \[\[.*Dashboard\.canvas\|Project Dashboard\]\]", line)]
    text = "\n".join(lines).strip()
    heading = "# " + " ".join(title.splitlines())
    if text.startswith(heading):
        text = text[len(heading):].strip()
    return text


# --------------------------------------------------------------------------- importer

class _Plan:
    def __init__(self, workspace: Path, dry_run: bool) -> None:
        self.workspace = workspace
        self.dry_run = dry_run
        self.files: list[dict[str, str]] = []
        self.warnings: list[str] = []

    def write(self, relative: str, payload: str | dict[str, Any]) -> bool:
        target = output_path(self.workspace, relative)
        if target.exists():
            self.files.append({"path": relative, "action": "skipped-existing"})
            return False
        self.files.append({"path": relative, "action": "create"})
        if not self.dry_run:
            if isinstance(payload, dict):
                atomic_json(target, payload, exclusive=True)
            else:
                atomic_text(target, payload, exclusive=True)
        return True


def _read_text(path: Path) -> str:
    if path.stat().st_size > MAX_NOTE_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_NOTE_BYTES // (1024 * 1024)} MiB")
    return path.read_text(encoding="utf-8-sig")


def _project_record(catalog: dict[str, Any], fields: dict[str, Any], kinds: list[str]) -> dict[str, Any]:
    declared = str(fields.get("project_type") or "").strip().lower()
    project_type = declared if declared in PROJECT_TYPES else next((KIND_TYPES[k] for k in kinds if k in KIND_TYPES), "other")
    parent = fields.get("parent_id")
    parent = parent.strip() if isinstance(parent, str) and parent.strip() not in ("", "null", "~") else None
    due = fields.get("due")
    due = str(due) if due not in (None, "") else None
    stage = str(fields.get("stage") or "planned")
    health = str(fields.get("project_health") or "active")
    resources = fields.get("resources") if isinstance(fields.get("resources"), list) else []
    knowledge = fields.get("knowledge") if isinstance(fields.get("knowledge"), list) else []
    title = str(fields.get("title") or catalog["title"]).strip() or catalog["projectId"]
    return {"schemaVersion": "1.0", "projectId": catalog["projectId"], "title": title, "type": project_type,
            "parentId": parent, "stage": stage if stage in STAGES else "planned",
            "health": health if health in HEALTH else "active",
            "nextStep": None if fields.get("next_step") in (None, "") else str(fields["next_step"]),
            "due": due, "archived": fields.get("archived") is True,
            "resources": [item for item in resources if item not in (None, "")],
            "knowledge": [item for item in knowledge if item not in (None, "")],
            "sourceIds": list(catalog["sourceIds"]),
            "importedFrom": {"kind": "obsidian-research-dashboard", "projectNote": catalog.get("projectNote")}}


def import_vault(vault: Path, workspace: Path, dry_run: bool = False, now: dt.datetime | None = None) -> dict[str, Any]:
    """Convert a vault's Research Dashboard catalog, records, sources and data into this app workspace."""
    vault, workspace = Path(vault).resolve(), Path(workspace).resolve()
    if layout_for(workspace) is not APP or not (workspace / WORKSPACE_MARKER).is_file():
        raise ValueError("Create or open an app workspace (workspace.json) before importing")
    system = vault / "Research Dashboard" / "System"
    if not system.is_dir():
        raise FileNotFoundError("This folder has no Research Dashboard/System; choose the Obsidian vault root")
    if vault == workspace or workspace.is_relative_to(vault) and (workspace / "Research Dashboard").exists():
        raise ValueError("Import into a separate workspace folder, not into the vault itself")
    plan = _Plan(workspace, dry_run)
    stamp = iso(now or dt.datetime.now(UTC))

    configs: dict[str, dict[str, Any]] = {}
    folder = vault / VAULT.source_configs()
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            config = read_json(path)
            validate_config(config)
            if config["projectId"] != path.stem:
                raise ValueError("filename does not match projectId")
            configs[config["projectId"]] = config
        except (OSError, ValueError, KeyError, TypeError) as error:
            plan.warnings.append(f"Skipped source configuration {path.name}: {error}")

    imported_projects: list[str] = []
    imported_sources: list[str] = []
    claimed: set[str] = set()
    for stem, path in VAULT.catalog_files(vault):
        try:
            catalog = read_json(path)
            validate_catalog(catalog, VAULT)
            if catalog["projectId"] != stem:
                raise ValueError("filename does not match projectId")
            note = vault / VAULT.project_note(stem)
            if not note.is_file():
                plan.warnings.append(f"Skipped project {stem}: its Project.md was removed")
                continue
            fields, body = parse_frontmatter(_read_text(note))
            if fields.get("project_id") not in (None, stem):
                raise ValueError("Project.md project_id does not match its folder")
            kinds = [configs[s]["kind"] for s in catalog["sourceIds"] if s in configs]
            record = _project_record(catalog, fields, kinds)
            missing = [s for s in record["sourceIds"] if s not in configs]
            if missing:
                plan.warnings.append(f"Project {stem}: unregistered sources were dropped: {', '.join(missing)}")
                record["sourceIds"] = [s for s in record["sourceIds"] if s in configs]
            if output_path(workspace, APP.project_dir(stem)).exists():
                plan.warnings.append(f"Skipped project {stem}: projects/{stem} already exists in the workspace")
                continue
            section, rest = split_milestones(body)
            milestones = "# Milestones\n\n" + (section.strip() + "\n" if section.strip() else "")
            plan.write(APP.catalog(stem), record)
            plan.write(APP.project_note(stem), milestones)
            notes = _clean_notes(rest, record["title"])
            if notes:
                plan.write(f"{APP.project_dir(stem)}/notes.md",
                           f"<!-- Imported from Obsidian ({VAULT.project_note(stem)}) on {stamp}. -->\n\n{notes}\n")
            imported_projects.append(stem)
            claimed.update(record["sourceIds"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            plan.warnings.append(f"Skipped project catalog {path.name}: {error}")

    for source_id in sorted(claimed):
        config = copy.deepcopy(configs[source_id])
        config["projectNote"] = APP.source_note(source_id)
        try:
            validate_config(config)
        except ValueError as error:
            plan.warnings.append(f"Skipped source {source_id}: {error}")
            continue
        if plan.write(APP.source_config(source_id), config):
            imported_sources.append(source_id)
        plan.write(APP.source_note(source_id), render_new_project(config, workspace))
        state_path = vault / VAULT.state(source_id)
        if state_path.is_file():
            try:
                state = read_json(state_path)
                validate_state(state)
                state["projectNote"] = config["projectNote"]
                plan.write(APP.state(source_id), state)
            except (OSError, ValueError, KeyError, TypeError) as error:
                plan.warnings.append(f"Snapshot of {source_id} was not imported ({error}); the next collection rebuilds it")
        history_path = vault / VAULT.history(source_id)
        if history_path.is_file():
            try:
                plan.write(APP.history(source_id), read_json(history_path))
            except (OSError, ValueError) as error:
                plan.warnings.append(f"History of {source_id} was not imported: {error}")

    local_folder = vault / "Research Dashboard/System/config/local"
    for path in sorted(local_folder.glob("*.json")) if local_folder.is_dir() else []:
        try:
            host = validate_id(path.stem, "hostId")
            mapping = read_json(path)
            if mapping.get("schemaVersion") != "1.0" or not isinstance(mapping.get("aliases"), dict):
                raise ValueError("not a host mapping")
            aliases = {alias: value for alias, value in mapping["aliases"].items()
                       if alias in claimed or any(configs[s]["source"]["rootAlias"] == alias for s in claimed)}
            if not aliases:
                continue
            target = output_path(workspace, APP.local(host))
            if target.exists():
                existing = read_json(target)
                merged = dict(existing.get("aliases") or {})
                added = {k: v for k, v in aliases.items() if k not in merged}
                if not added:
                    plan.files.append({"path": APP.local(host), "action": "skipped-existing"})
                    continue
                merged.update(added)
                plan.files.append({"path": APP.local(host), "action": "merge"})
                if not dry_run:
                    atomic_json(target, {"schemaVersion": "1.0", "aliases": merged})
            else:
                plan.write(APP.local(host), {"schemaVersion": "1.0", "aliases": aliases})
        except (OSError, ValueError, KeyError, TypeError) as error:
            plan.warnings.append(f"Skipped host mapping {path.name}: {error}")

    for relative_from, relative_to in ((VAULT.profiles_user(), APP.profiles_user()),
                                       (VAULT.milestone_templates_user(), APP.milestone_templates_user())):
        for path in sorted((vault / relative_from).glob("*.json")) if (vault / relative_from).is_dir() else []:
            try:
                plan.write(f"{relative_to}/{path.name}", read_json(path))
            except (OSError, ValueError) as error:
                plan.warnings.append(f"Skipped {path.name}: {error}")

    return {"dryRun": dry_run, "from": str(vault), "workspace": str(workspace),
            "projects": imported_projects, "sources": imported_sources,
            "created": sum(item["action"] in ("create", "merge") for item in plan.files),
            "files": plan.files, "warnings": plan.warnings,
            "note": "The vault was only read. Start live updates (or Collect now) to refresh imported snapshots."}

