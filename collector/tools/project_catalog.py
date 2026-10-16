"""Logical research projects group independently collected sources into one Canvas."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from dashboard_core import atomic_json, atomic_text, configurations, output_path, read_json, validate_id
from layout import VAULT, Layout, layout_for

CATALOG_FOLDER = "Research Dashboard/System/config/catalog"
VIEW = "Research Dashboard/System/views/research-dashboard"
CARDS = ("overview", "milestones", "progress", "monitor", "results")
# Logical project types shown in the Dashboard. Folder connections infer the
# first type from the adapter kind; users may change it in the project form.
PROJECT_TYPES = ("thesis", "paper", "simulation", "experiment", "other")
KIND_PROJECT_TYPES = {"latex": "paper", "abaqus": "simulation", "isale": "simulation"}
MAX_TITLE_LENGTH = 200
# App-workspace project records keep their editable fields in project.json.
PROJECT_STAGES = ("planned", "preparation", "modeling", "writing", "computation", "postprocessing",
                  "validation", "review", "completed", "archived")
PROJECT_HEALTH = ("active", "blocked", "paused", "completed")


def project_paths(project_id: str, layout: Layout = VAULT) -> dict[str, str | None]:
    """The editable project note and (vault only) its Canvas dashboard."""
    validate_id(project_id, "dashboardProjectId")
    return {"projectNote": layout.project_note(project_id), "projectDashboard": layout.project_dashboard(project_id)}


def validate_catalog(record: dict[str, Any], layout: Layout = VAULT) -> None:
    if record.get("schemaVersion") != "1.0":
        raise ValueError("Unsupported project catalog schemaVersion")
    identity = validate_id(record.get("projectId"), "dashboardProjectId")
    if not isinstance(record.get("title"), str) or not record["title"].strip():
        raise ValueError("Project catalog title must be a nonempty string")
    for field, expected in layout.catalog_path_fields(identity).items():
        if record.get(field) != expected:
            raise ValueError(f"Project catalog {field} must use its canonical project path")
    if layout.record_in_catalog:
        if record.get("type", "other") not in PROJECT_TYPES:
            raise ValueError("Project type must be one of: " + ", ".join(PROJECT_TYPES))
        if record.get("parentId") is not None:
            validate_id(record["parentId"], "parentId")
    # A logical project may be a sourceless container (for example a thesis
    # grouping sub-projects); an empty list is valid, a missing list is not.
    sources = record.get("sourceIds")
    if not isinstance(sources, list):
        raise ValueError("Project catalog sourceIds must be a list")
    for source in sources:
        validate_id(source, "sourceId")
    if len(sources) != len(set(sources)):
        raise ValueError("Duplicate source membership in project catalog")


def project_catalogs(vault: Path) -> list[dict[str, Any]]:
    """List live projects only; the canonical editable record owns project life."""
    layout = layout_for(vault)
    output_path(vault, layout.catalog_dir())
    configs = {item["projectId"]: item for item in configurations(vault)}
    sources: set[str] = set()
    records = []
    for stem, path in layout.catalog_files(vault):
        identity = validate_id(stem, "dashboardProjectId")
        if not output_path(vault, layout.liveness(identity)).is_file():
            continue
        record = read_json(path)
        validate_catalog(record, layout)
        if record["projectId"] != stem:
            raise ValueError("Project catalog filename does not match dashboardProjectId")
        for relative in project_paths(record["projectId"], layout).values():
            if relative is not None:
                output_path(vault, relative)
        for source in record["sourceIds"]:
            if source not in configs:
                raise ValueError(f"Project catalog refers to an unregistered source: {source}")
            if source in sources:
                raise ValueError(f"Source belongs to more than one dashboard project: {source}")
            if configs[source].get("dashboardProjectId", record["projectId"]) != record["projectId"]:
                raise ValueError(f"Source owner does not match project catalog: {source}")
            sources.add(source)
        records.append(record)
    return records


def inactive_source_ids(vault: Path) -> set[str]:
    """Exclude removed projects without inferring ownership of unknown configs."""
    layout = layout_for(vault)
    output_path(vault, layout.catalog_dir())
    inactive: set[str] = set()
    live: set[str] = set()
    for stem, path in layout.catalog_files(vault):
        try:
            record = read_json(path)
            validate_catalog(record, layout)
            if record["projectId"] != stem:
                continue
            target = live if output_path(vault, layout.liveness(record["projectId"])).is_file() else inactive
            target.update(record["sourceIds"])
        except (OSError, ValueError, TypeError, KeyError):
            # Unknown or damaged registry entries do not establish permission to
            # suppress or archive a source that might belong to another workflow.
            continue
    return inactive - live


def active_configurations(vault: Path, source_id: str | None = None) -> list[dict[str, Any]]:
    """Collection skips sources whose research project record has been removed."""
    inactive = inactive_source_ids(vault)
    live = {record["projectId"]: set(record["sourceIds"]) for record in project_catalogs(vault)}
    selected = []
    for config in configurations(vault, source_id):
        identity, owner = config["projectId"], config.get("dashboardProjectId")
        if identity in inactive:
            continue
        if owner is not None and identity not in live.get(owner, set()):
            continue
        selected.append(config)
    return selected


def new_catalog(project_id: str, title: str, source_id: str | None, layout: Layout = VAULT,
                project_type: str = "other", parent_id: str | None = None) -> dict[str, Any]:
    """A new catalog entry. In the app workspace it is also the editable project record."""
    if layout.record_in_catalog:
        record = {"schemaVersion": "1.0", "projectId": project_id, "title": title,
                  "type": validate_project_type(project_type), "parentId": parent_id, "stage": "planned",
                  "health": "active", "nextStep": "Define the first deliverable and acceptance criteria",
                  "due": None, "archived": False, "resources": [], "knowledge": [],
                  "sourceIds": [] if source_id is None else [source_id]}
    else:
        record = {"schemaVersion": "1.0", "projectId": project_id, "title": title,
                  **layout.catalog_path_fields(project_id), "sourceIds": [] if source_id is None else [source_id]}
    validate_catalog(record, layout)
    return record


def frontmatter_scalar(content: str, key: str) -> Any:
    """Read one simple scalar frontmatter value without a YAML dependency.

    Handles both generated JSON scalars and Obsidian's plain YAML scalars; an
    empty value (``parent_id:``) reads as an empty string.
    """
    match = re.match(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", content, re.DOTALL)
    if not match:
        return None
    scalar = re.search(r"^" + re.escape(key) + r":[ \t]*(.*?)[ \t]*$", match.group(1), re.MULTILINE)
    if not scalar:
        return None
    value = scalar.group(1)
    try:
        return json.loads(value)
    except ValueError:
        return value.strip("'\"")


def record_parent_id(vault: Path, project_id: str) -> str | None:
    """Return a live project's parent_id, or None for a top-level project."""
    layout = layout_for(vault)
    if layout.record_in_catalog:
        value = read_json(output_path(vault, layout.catalog(validate_id(project_id, "dashboardProjectId")))).get("parentId")
        return value if isinstance(value, str) and value.strip() else None
    path = output_path(vault, project_paths(project_id)["projectNote"])
    value = frontmatter_scalar(path.read_text(encoding="utf-8-sig"), "parent_id")
    if value in (None, "", "null", "~"):
        return None
    return value if isinstance(value, str) else str(value)


def validate_project_type(value: Any) -> str:
    if value not in PROJECT_TYPES:
        raise ValueError("Project type must be one of: " + ", ".join(PROJECT_TYPES))
    return value


def render_project(record: dict[str, Any], project_type: str = "other", parent_id: str | None = None) -> str:
    """Create an editable research record; automatic observations stay separate."""
    fields = {"type": "research-project", "project_id": record["projectId"], "title": record["title"],
              "project_kind": "general", "project_type": validate_project_type(project_type),
              "parent_id": parent_id, "stage": "planned", "project_health": "active",
              "next_step": "Define the first deliverable and acceptance criteria", "due": None,
              "archived": False, "cssclasses": ["research-dashboard", "research-project-record"], "resources": [], "knowledge": []}
    frontmatter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    heading = " ".join(record["title"].splitlines())
    return (f"---\n{frontmatter}\n---\n\n# {heading}\n\n"
            f"Open [[{record['projectDashboard']}|Project Dashboard]] · [[Research Dashboard/Overall.canvas|Overall Dashboard]]\n\n"
            "## Objective\n\nDefine the research question, scope, and acceptance criteria.\n\n"
            "## Milestones\n\n- [ ] Define the first deliverable and its acceptance criteria #milestone\n\n"
            "## Decisions and evidence\n\nRecord decisions, blockers, and acceptance evidence here.\n")


def render_milestones_note(record: dict[str, Any]) -> str:
    """App workspace: the project's milestone list (frontmatter-free GFM task list)."""
    heading = " ".join(str(record["title"]).splitlines())
    return (f"# Milestones\n\n<!-- {heading}: one task per line. Inline fields such as [due:: 2026-12-01] "
            "or [paper:: <source>] [chapter:: <section>] [stage:: draft] bind a milestone to a paper chapter. -->\n\n"
            "- [ ] Define the first deliverable and its acceptance criteria #milestone\n")


def card_contents(project_id: str, card: str) -> str:
    mode = {"overview": "project-overview", "milestones": "milestones", "progress": "paper-progress",
            "monitor": "runs", "results": "resources"}[card]
    options = json.dumps({"mode": mode, "projectId": project_id})
    return f"```dataviewjs\nawait dv.view({json.dumps(VIEW)}, {options});\n```\n"


def project_canvas(project_id: str) -> dict[str, Any]:
    geometry = {"overview": (0, 0, 620, 360), "milestones": (660, 0, 620, 360),
                "progress": (20, 460, 1240, 560),
                "monitor": (0, 1080, 780, 500), "results": (820, 1080, 460, 500)}
    nodes = []
    for card in CARDS:
        if card == "progress":
            nodes.append({"id": "project-progress-group", "type": "group", "label": "Progress",
                          "x": 0, "y": 400, "width": 1280, "height": 640})
        x, y, width, height = geometry[card]
        nodes.append({"id": f"project-{card}", "type": "file",
                      "file": f"Research Dashboard/System/cards/projects/{project_id}/{card}.md",
                      "x": x, "y": y, "width": width, "height": height})
    return {"nodes": nodes, "edges": [],
            "metadata": {"version": "1.0-1.0", "frontmatter": {"cssclasses": ["research-dashboard-canvas"]}}}


def create_project_files(vault: Path, record: dict[str, Any], created: list[tuple[Path, bytes]],
                         project_type: str = "other", parent_id: str | None = None) -> None:
    """Create only missing files and track exact payloads for safe rollback."""
    layout = layout_for(vault)
    if layout.record_in_catalog:
        # The record fields live in project.json (written by the caller); only the milestone list is a file.
        targets = [(layout.project_note(record["projectId"]), render_milestones_note(record))]
    else:
        targets = [(record["projectNote"], render_project(record, project_type, parent_id)),
                   (record["projectDashboard"], json.dumps(project_canvas(record["projectId"]), indent=2, ensure_ascii=False) + "\n")]
        targets.extend((f"{layout.cards_dir(record['projectId'])}/{card}.md",
                        card_contents(record["projectId"], card)) for card in CARDS)
    for relative, text in targets:
        path = output_path(vault, relative)
        if path.exists():
            if not path.is_file():
                raise ValueError(f"Project output path is not a file: {relative}")
            continue
        atomic_text(path, text, exclusive=True)
        created.append((path, text.encode("utf-8")))


def rollback_created(created: list[tuple[Path, bytes]]) -> None:
    """Delete only files whose content is still the exact payload we created."""
    for path, payload in reversed(created):
        try:
            if path.is_file() and path.read_bytes() == payload:
                path.unlink()
        except OSError:
            pass


def missing_output_directories(vault: Path, paths: list[Path]) -> set[Path]:
    """Track only missing directories on the known output paths, before writing."""
    missing = set()
    boundary = vault.resolve()
    for path in paths:
        parent = path.parent
        while parent != boundary and parent.is_relative_to(boundary):
            if not parent.exists():
                missing.add(parent)
            parent = parent.parent
    return missing


def rollback_directories(missing: set[Path]) -> None:
    """Remove our newly created empty directories while preserving prior folders."""
    for path in sorted(missing, key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def validate_title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Project title must be a nonempty string")
    title = value.strip()
    if len(title) > MAX_TITLE_LENGTH or re.search(r"[\x00-\x1f\x7f]", title):
        raise ValueError(f"Keep the project title on one line, up to {MAX_TITLE_LENGTH} characters")
    return title


def validate_parent(vault: Path, project_id: str, parent_id: str | None,
                    catalogs: list[dict[str, Any]]) -> str | None:
    """One displayed hierarchy level: a parent must be a live top-level project."""
    if parent_id is None:
        return None
    validate_id(parent_id, "parentId")
    if parent_id == project_id:
        raise ValueError("A project cannot be its own parent")
    if not any(record["projectId"] == parent_id for record in catalogs):
        raise ValueError(f"Parent project does not exist: {parent_id}")
    if record_parent_id(vault, parent_id) is not None:
        raise ValueError(f"Parent project {parent_id} is already a sub-project; choose a top-level project")
    return parent_id


def create_logical_project(vault: Path, project_id: str, title: str, project_type: str = "other",
                           parent_id: str | None = None) -> dict[str, Any]:
    """Create a sourceless logical project: record, Canvas, cards and catalog.

    Nothing is overwritten. Any existing record, Canvas, card folder, catalog or
    source ownership claim for the ID is rejected before writing; a failure
    while writing removes only the exact files and empty folders created here.
    """
    vault = vault.resolve()
    layout = layout_for(vault)
    validate_id(project_id, "dashboardProjectId")
    title = validate_title(title)
    validate_project_type(project_type)
    catalogs = project_catalogs(vault)
    parent_id = validate_parent(vault, project_id, parent_id, catalogs)
    record = new_catalog(project_id, title, None, layout, project_type, parent_id)
    paths = project_paths(project_id, layout)
    catalog_path = output_path(vault, layout.catalog(project_id))
    project_folder = output_path(vault, layout.project_dir(project_id))
    cards_folder = output_path(vault, layout.cards_dir(project_id)) if layout.cards_dir(project_id) else None
    for occupied, label in ((catalog_path, "project catalog"), (project_folder, "project folder"),
                            (cards_folder, "project card folder")):
        if occupied is not None and occupied.exists():
            raise FileExistsError(f"Project ID already exists ({label}); choose a new ID")
    if any(config.get("dashboardProjectId") == project_id for config in configurations(vault)):
        raise FileExistsError("A source configuration already claims this project ID; choose a new ID")
    planned = [catalog_path, *(output_path(vault, value) for value in paths.values() if value is not None)]
    if cards_folder is not None:
        planned.extend(cards_folder / f"{card}.md" for card in CARDS)
    missing_directories = missing_output_directories(vault, planned)
    created: list[tuple[Path, bytes]] = []
    try:
        create_project_files(vault, record, created, project_type, parent_id)
        # Publish the catalog last and exclusively: liveness requires both the
        # record and the catalog, and a concurrent writer must not be replaced.
        atomic_json(catalog_path, record, exclusive=True)
        created.append((catalog_path, catalog_path.read_bytes()))
        project_catalogs(vault)
    except Exception:
        rollback_created(created)
        rollback_directories(missing_directories)
        raise
    result = {"dashboardProjectId": project_id, "title": title, "projectType": project_type, "parentId": parent_id,
              "sourceIds": [], "projectRecord": paths["projectNote"], "projectDashboard": paths["projectDashboard"]}
    if layout.record_in_catalog:
        result.update(projectRecord=layout.catalog(project_id), projectMilestones=paths["projectNote"])
    return result
