"""Sub-project discovery: scan a top-level project's discovery folder for new candidate sub-projects.

A discovery folder is scanned, never monitored. Its absolute path lives only in the host-local alias file
(``System/config/local/<host>.json`` under the alias ``discovery:<projectId>``); the project catalog stores the alias,
an ``autoAccept`` flag and per-project ignore globs. Dismissals are stored by folder name relative to the root in
``System/data/discovery/state.json``, so the shared file never holds an absolute path.

Scanning is bounded and read-only: immediate subfolders only, existing sources (by resolved path) and dismissed
folders are skipped, and each remaining folder is scored with the monitoring-profile library through ``inspect``.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import time
from pathlib import Path
from typing import Any

from adapters.common import UTC, iso
from dashboard_core import atomic_json, output_path, read_json, validate_id
from onboarding import _linked, connect_folder, inspect_folder, source_directory, stable_id
from profiles import find_profile
from layout import layout_for
from project_catalog import active_configurations, project_catalogs, record_parent_id, validate_catalog

ALIAS_PREFIX = "discovery:"
STATE_PATH = "Research Dashboard/System/data/discovery/state.json"
LOCAL_FOLDER = "Research Dashboard/System/config/local"
# Folder names that are never candidates (case-insensitive); hidden folders (".name") are skipped too.
DEFAULT_IGNORE = ("tmp", "build", "node_modules", "__pycache__")
MAX_FOLDERS = 60            # subfolders scored per root; the rest wait for a narrower root or ignore globs
MAX_IGNORE = 50
MAX_GLOB_LENGTH = 200
SCAN_SECONDS = 20.0         # total budget for one discover call
AUTO_ACCEPT_CONFIDENCE = 0.8


def alias_for(project_id: str) -> str:
    """The host-local alias of a project's discovery folder (not a valid source ID, so it never collides)."""
    return ALIAS_PREFIX + validate_id(project_id, "projectId")


def _local_path(vault: Path, host: str) -> Path:
    return output_path(vault, layout_for(vault).local(validate_id(host, 'hostId')))


def _read_local(vault: Path, host: str) -> dict[str, Any]:
    path = _local_path(vault, host)
    local = read_json(path) if path.exists() else {"schemaVersion": "1.0", "aliases": {}}
    if local.get("schemaVersion") != "1.0" or not isinstance(local.get("aliases"), dict):
        raise ValueError("Local alias configuration requires schemaVersion and aliases")
    return local


def settings(catalog: dict[str, Any]) -> dict[str, Any] | None:
    """Validated discovery settings of a catalog entry, or None when the project has no discovery folder."""
    value = catalog.get("discovery")
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("rootAlias"), str):
        raise ValueError(f"Invalid discovery settings for {catalog.get('projectId')}")
    ignore = value.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(item, str) for item in ignore):
        raise ValueError(f"Discovery ignore globs must be strings for {catalog.get('projectId')}")
    return {"rootAlias": value["rootAlias"], "autoAccept": value.get("autoAccept") is True, "ignore": ignore}


def normalize_ignore(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            glob = part.strip().strip("/\\")
            if not glob:
                continue
            if len(glob) > MAX_GLOB_LENGTH or any(ord(char) < 32 for char in glob):
                raise ValueError("Keep each ignore glob on one line, up to 200 characters")
            if glob not in result:
                result.append(glob)
    if len(result) > MAX_IGNORE:
        raise ValueError(f"Use at most {MAX_IGNORE} ignore globs")
    return result


def resolve_root(vault: Path, host: str, value: dict[str, Any] | None, local: dict[str, Any] | None = None) -> tuple[Path | None, str | None]:
    """(root, problem): the absolute discovery folder on this host, or why it is unavailable here."""
    if value is None:
        return None, "No discovery folder is set."
    local = local if local is not None else _read_local(vault, host)
    mapped = local["aliases"].get(value["rootAlias"])
    if mapped is None:
        return None, "The discovery folder is not set on this computer."
    if not isinstance(mapped, str) or not Path(mapped).is_absolute():
        return None, "The discovery folder mapping must be an absolute path."
    root = Path(mapped).resolve()
    if not root.is_dir():
        return None, f"The discovery folder is unavailable: {root}"
    return root, None


def _ignored(name: str, globs: list[str]) -> bool:
    lowered = name.lower()
    if name.startswith(".") or lowered in DEFAULT_IGNORE:
        return True
    return any(fnmatch.fnmatchcase(lowered, glob.lower()) for glob in globs)


def connected_roots(vault: Path, host: str, local: dict[str, Any]) -> list[tuple[str, Path]]:
    """Resolved folders of live sources mapped on this host."""
    roots = []
    for config in active_configurations(vault):
        alias = config.get("source", {}).get("rootAlias")
        mapped = local["aliases"].get(alias) if isinstance(alias, str) else None
        if not isinstance(mapped, str) or not Path(mapped).is_absolute():
            continue
        try:
            roots.append((config["projectId"], (Path(mapped) / config["source"].get("path", "")).resolve()))
        except (OSError, ValueError):
            continue
    return roots


def _overlaps(folder: Path, roots: list[tuple[str, Path]]) -> str | None:
    """A source at, above or below the folder means it is already connected (the parent source absorbs it)."""
    for source_id, root in roots:
        if folder == root or root.is_relative_to(folder) or folder.is_relative_to(root):
            return source_id
    return None


def load_state(vault: Path) -> dict[str, Any]:
    path = output_path(vault, layout_for(vault).discovery_state())
    if not path.exists():
        return {"schemaVersion": "1.0", "projects": {}}
    state = read_json(path)
    if state.get("schemaVersion") != "1.0" or not isinstance(state.get("projects"), dict):
        raise ValueError("Discovery state requires schemaVersion 1.0 and projects")
    return state


def dismissed_names(state: dict[str, Any], project_id: str) -> dict[str, str]:
    """casefolded name -> stored name."""
    entries = state["projects"].get(project_id, {}).get("dismissed", [])
    return {str(item.get("folder", "")).casefold(): str(item.get("folder", "")) for item in entries
            if isinstance(item, dict) and isinstance(item.get("folder"), str)}


def _top_level(vault: Path, project_id: str, live: set[str]) -> str:
    parent = record_parent_id(vault, project_id)
    return parent if parent and parent in live else project_id


def _best(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in candidates if item.get("meetsThreshold")), None)


def _occupied(vault: Path, catalogs: list[dict[str, Any]]) -> set[str]:
    """IDs a new project must not take: catalogs, source configurations, source records and project folders."""
    occupied = {item["projectId"] for item in catalogs}
    layout = layout_for(vault)
    configs = output_path(vault, layout.source_configs())
    records = output_path(vault, layout.records_dir())
    folders = output_path(vault, layout.projects_root())
    if configs.is_dir():
        occupied.update(path.stem for path in configs.glob("*.json"))
    if records.is_dir():
        occupied.update(path.parent.name for path in records.glob("*/project.md"))
        occupied.update(path.stem for path in records.glob("*.md"))
    if folders.is_dir():
        occupied.update(path.name for path in folders.iterdir() if path.is_dir())
    return occupied


def _unique_id(name: str, occupied: set[str]) -> str:
    base = stable_id(name or "project")
    candidate, counter = base, 2
    while candidate in occupied:
        suffix = f"-{counter}"
        candidate = base[:64 - len(suffix)].rstrip("-_") + suffix
        counter += 1
    occupied.add(candidate)
    return candidate


def _candidate_summary(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {key: candidate.get(key) for key in ("profileId", "title", "projectType", "confidence", "subprojectRole",
                                                  "sourceKind", "engine", "origin", "milestoneTemplate")}


def _project_entry(vault: Path, host: str, catalog: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    value = settings(catalog)
    root, problem = resolve_root(vault, host, value, local)
    return {"projectId": catalog["projectId"], "title": catalog["title"], "settings": value, "root": root, "problem": problem}


def scan(vault: Path, host: str, project_id: str | None = None, folder_name: str | None = None,
         seconds: float = SCAN_SECONDS) -> dict[str, Any]:
    """List new candidate sub-project folders under the discovery folders (read-only).

    ``project_id`` limits the scan to one project; ``folder_name`` (with a project) scores just that subfolder.
    """
    started = time.monotonic()
    deadline = started + seconds
    vault = vault.resolve()
    catalogs = project_catalogs(vault)
    live = {item["projectId"] for item in catalogs}
    if project_id is not None:
        validate_id(project_id, "projectId")
        if project_id not in live:
            raise ValueError(f"Project does not exist: {project_id}")
    local = _read_local(vault, host)
    state = load_state(vault)
    roots = connected_roots(vault, host, local)
    occupied = _occupied(vault, catalogs)
    suggestions: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    warnings: list[str] = []
    dismissed_total = 0
    for catalog in sorted(catalogs, key=lambda item: (item["title"].lower(), item["projectId"])):
        if project_id is not None and catalog["projectId"] != project_id:
            continue
        try:
            entry = _project_entry(vault, host, catalog, local)
        except ValueError as error:
            projects.append({"projectId": catalog["projectId"], "available": False, "error": str(error)})
            continue
        if entry["settings"] is None:
            if project_id is not None:
                projects.append({"projectId": catalog["projectId"], "available": False, "error": entry["problem"]})
            continue
        info = {"projectId": catalog["projectId"], "available": entry["root"] is not None, "error": entry["problem"],
                "rootName": entry["root"].name if entry["root"] else None, "autoAccept": entry["settings"]["autoAccept"],
                "scanned": 0, "connected": 0, "dismissed": 0, "ignored": 0, "absorbed": 0, "truncated": False}
        projects.append(info)
        root = entry["root"]
        if root is None:
            continue
        parent_id = _top_level(vault, catalog["projectId"], live)
        dismissed = dismissed_names(state, catalog["projectId"])
        try:
            children = sorted((path for path in root.iterdir()), key=lambda item: item.name.lower())
        except OSError as error:
            info.update(available=False, error=f"The discovery folder could not be listed ({type(error).__name__}).")
            continue
        for child in children:
            name = child.name
            if folder_name is not None and name.casefold() != folder_name.casefold():
                continue
            try:
                if not child.is_dir():
                    continue
                if _linked(child):
                    info["ignored"] += 1
                    continue
            except OSError:
                continue
            if _ignored(name, entry["settings"]["ignore"]):
                info["ignored"] += 1
                continue
            resolved = child.resolve()
            if vault.is_relative_to(resolved) or resolved.is_relative_to(vault):
                info["ignored"] += 1
                continue
            if _overlaps(resolved, roots):
                info["connected"] += 1
                continue
            if name.casefold() in dismissed:
                info["dismissed"] += 1
                dismissed_total += 1
                continue
            if info["scanned"] >= MAX_FOLDERS or time.monotonic() > deadline:
                info["truncated"] = True
                continue
            info["scanned"] += 1
            try:
                inspection = inspect_folder(resolved, vault)
            except (OSError, ValueError) as error:
                warnings.append(f"{catalog['projectId']}/{name}: could not be inspected ({type(error).__name__}).")
                continue
            best = _best(inspection.get("candidates", []))
            if best is not None and best.get("subprojectRole") == "part":
                info["absorbed"] += 1
                continue
            suggestions.append({
                "id": f"{catalog['projectId']}:{name}", "projectId": catalog["projectId"], "parentId": parent_id,
                "projectTitle": catalog["title"], "folder": str(resolved), "relative": name, "rootName": root.name,
                "bestCandidate": _candidate_summary(best), "autoAccept": entry["settings"]["autoAccept"],
                "suggestedTitle": name.replace("_", " ").strip() or name, "suggestedId": _unique_id(name, occupied),
                "_inspection": inspection,
            })
        if info["truncated"]:
            warnings.append(f"{catalog['projectId']}: only the first {info['scanned']} new folders were scored; "
                            "add ignore globs or choose a narrower discovery folder.")
    return {"suggestions": suggestions, "dismissed": dismissed_total, "projects": projects, "warnings": warnings,
            "scannedAt": iso(dt.datetime.now(UTC)), "elapsedSeconds": round(time.monotonic() - started, 3)}


def public(result: dict[str, Any]) -> dict[str, Any]:
    """Drop the internal inspection payloads before printing."""
    return {**result, "suggestions": [{key: value for key, value in item.items() if not key.startswith("_")}
                                      for item in result["suggestions"]]}


def _catalog_path(vault: Path, project_id: str) -> Path:
    return output_path(vault, layout_for(vault).catalog(validate_id(project_id, 'projectId')))


def _live_catalog(vault: Path, project_id: str) -> dict[str, Any]:
    validate_id(project_id, "projectId")
    catalog = next((item for item in project_catalogs(vault) if item["projectId"] == project_id), None)
    if catalog is None:
        raise ValueError(f"Project does not exist: {project_id}")
    return catalog


def _folder_name(vault: Path, host: str, catalog: dict[str, Any], folder: str) -> tuple[str, Path | None]:
    """A direct subfolder name from a name or an absolute path inside the discovery folder."""
    value = settings(catalog)
    if value is None:
        raise ValueError(f"Project {catalog['projectId']} has no discovery folder")
    root, _ = resolve_root(vault, host, value)
    path = Path(folder)
    if path.is_absolute():
        if root is None or path.resolve().parent != root:
            raise ValueError("The folder must be a direct subfolder of the discovery folder")
        return path.resolve().name, root
    if not folder or folder in (".", "..") or any(sep in folder for sep in ("/", "\\")) or any(ord(char) < 32 for char in folder):
        raise ValueError("Give a direct subfolder name of the discovery folder")
    return folder, root


def _write_state(vault: Path, state: dict[str, Any]) -> None:
    atomic_json(output_path(vault, layout_for(vault).discovery_state()), state)


def dismiss(vault: Path, host: str, project_id: str, folder: str) -> dict[str, Any]:
    vault = vault.resolve()
    catalog = _live_catalog(vault, project_id)
    name, _ = _folder_name(vault, host, catalog, folder)
    state = load_state(vault)
    entry = state["projects"].setdefault(project_id, {"dismissed": []})
    entries = entry.setdefault("dismissed", [])
    if name.casefold() not in dismissed_names(state, project_id):
        entries.append({"folder": name, "dismissedAt": iso(dt.datetime.now(UTC))})
        _write_state(vault, state)
    return {"projectId": project_id, "folder": name, "dismissed": True, "dismissedCount": len(entries)}


def restore(vault: Path, project_id: str, folder: str | None = None) -> dict[str, Any]:
    """Undo one dismissal, or all of a project's dismissals without a folder."""
    vault = vault.resolve()
    _live_catalog(vault, project_id)
    state = load_state(vault)
    entry = state["projects"].get(project_id, {"dismissed": []})
    before = list(entry.get("dismissed", []))
    kept = [item for item in before if folder is not None and str(item.get("folder", "")).casefold() != folder.casefold()]
    if len(kept) != len(before):
        if kept:
            state["projects"][project_id] = {**entry, "dismissed": kept}
        else:
            state["projects"].pop(project_id, None)
        _write_state(vault, state)
    return {"projectId": project_id, "restored": [item.get("folder") for item in before if item not in kept],
            "dismissedCount": len(kept)}


def show(vault: Path, host: str, project_id: str) -> dict[str, Any]:
    vault = vault.resolve()
    catalog = _live_catalog(vault, project_id)
    value = settings(catalog)
    root, problem = resolve_root(vault, host, value)
    local = _read_local(vault, host)
    mapped = local["aliases"].get(value["rootAlias"]) if value else None
    return {"projectId": project_id, "configured": value is not None, "topLevel": record_parent_id(vault, project_id) is None,
            "folder": str(root) if root else (mapped if isinstance(mapped, str) else None), "available": root is not None,
            "problem": problem if value else None, "rootAlias": value["rootAlias"] if value else None,
            "autoAccept": value["autoAccept"] if value else False, "ignore": value["ignore"] if value else [],
            "dismissed": sorted(dismissed_names(load_state(vault), project_id).values(), key=str.lower)}


def configure(vault: Path, host: str, project_id: str, folder: str | Path | None = None, clear: bool = False,
              auto_accept: bool | None = None, ignore: list[str] | None = None, clear_ignore: bool = False) -> dict[str, Any]:
    """Set or clear a top-level project's discovery folder and options.

    The absolute folder goes only into this host's alias file; the catalog keeps the alias and the options. A new
    folder resets the project's dismissals (they are relative to the old folder). Clearing removes both.
    """
    vault = vault.resolve()
    validate_id(host, "hostId")
    if clear and (folder is not None or auto_accept is not None or ignore or clear_ignore):
        raise ValueError("--clear cannot be combined with other discovery options")
    catalog = _live_catalog(vault, project_id)
    current = settings(catalog)
    alias = alias_for(project_id)
    catalog_path = _catalog_path(vault, project_id)
    original_catalog = read_json(catalog_path)
    local_path = _local_path(vault, host)
    original_local = read_json(local_path) if local_path.exists() else None
    local = json.loads(json.dumps(original_local)) if original_local is not None else {"schemaVersion": "1.0", "aliases": {}}
    if local.get("schemaVersion") != "1.0" or not isinstance(local.get("aliases"), dict):
        raise ValueError("Existing host mappings are invalid")
    state = load_state(vault)
    reset_dismissals = False
    updated = json.loads(json.dumps(original_catalog))
    if clear:
        updated.pop("discovery", None)
        local["aliases"].pop(alias, None)
        reset_dismissals = True
    else:
        if folder is None and current is None:
            raise ValueError("Choose a discovery folder first")
        value = dict(current or {"rootAlias": alias, "autoAccept": False, "ignore": []})
        if folder is not None:
            if record_parent_id(vault, project_id) is not None:
                raise ValueError("Only a top-level project can have a discovery folder; sub-projects are found under their parent")
            root = source_directory(folder)
            if root.is_relative_to(vault):
                raise ValueError("Choose a discovery folder outside the vault")
            previous = local["aliases"].get(alias)
            if not (isinstance(previous, str) and Path(previous).resolve() == root):
                reset_dismissals = True
            local["aliases"][alias] = str(root)
            value["rootAlias"] = alias
        if auto_accept is not None:
            value["autoAccept"] = bool(auto_accept)
        if clear_ignore:
            value["ignore"] = []
        if ignore:
            value["ignore"] = normalize_ignore(ignore)
        updated["discovery"] = {"rootAlias": value["rootAlias"], "autoAccept": value["autoAccept"], "ignore": value["ignore"]}
    validate_catalog(updated, layout_for(vault))
    if original_local != local:
        atomic_json(local_path, local)
    if read_json(catalog_path) != original_catalog:
        raise ValueError("The project changed while saving; retry")
    if updated != original_catalog:
        atomic_json(catalog_path, updated)
    if reset_dismissals and project_id in state["projects"]:
        state["projects"].pop(project_id)
        _write_state(vault, state)
    return show(vault, host, project_id)


def _milestone_choice(candidate: dict[str, Any]) -> str | None:
    template = candidate.get("milestoneTemplate")
    if isinstance(template, list) and template:
        return "profile"
    if isinstance(template, str) and template:
        return template
    return None


def accept(vault: Path, host: str, project_id: str, folder: str,
           min_confidence: float = AUTO_ACCEPT_CONFIDENCE) -> dict[str, Any]:
    """Connect one discovered folder as a sub-project with its best standalone candidate and milestone template."""
    vault = vault.resolve()
    catalog = _live_catalog(vault, project_id)
    name, _ = _folder_name(vault, host, catalog, folder)
    if name.casefold() in dismissed_names(load_state(vault), project_id):
        raise ValueError(f"{name} was dismissed; restore it before adding it")
    result = scan(vault, host, project_id, folder_name=name)
    suggestion = next(iter(result["suggestions"]), None)
    if suggestion is None:
        raise ValueError(f"{name} is not a new folder in the discovery folder (already connected, ignored or absorbed)")
    best = suggestion["bestCandidate"]
    if best is None or not isinstance(best.get("confidence"), (int, float)) or best["confidence"] < min_confidence:
        raise ValueError(f"No monitoring type matches {name} with at least {round(min_confidence * 100)}% confidence")
    inspection = suggestion["_inspection"]
    candidate = next(item for item in inspection["candidates"] if item["profileId"] == best["profileId"])
    profile, _ = find_profile(vault, best["profileId"])
    adapter = None
    if best.get("engine") == "adapter":
        inspected = candidate.get("inspectedProfileIds") or []
        entry = next((item for item in inspection["profiles"] if inspected and item["id"] == inspected[0]), None)
        if entry is None:
            raise ValueError(f"No connectable entry was found for {best['profileId']} in {name}")
        adapter = entry["adapter"]
    connected = connect_folder(vault, host, suggestion["folder"], suggestion["suggestedId"], None, suggestion["suggestedTitle"],
                               adapter, profile=profile, parent_id=suggestion["parentId"],
                               milestone_template=_milestone_choice(candidate))
    return {"accepted": True, "suggestion": public({"suggestions": [suggestion]})["suggestions"][0], "connect": connected}
