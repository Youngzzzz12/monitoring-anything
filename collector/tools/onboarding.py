"""Bounded folder discovery and one-time project connection for the local UI."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from adapters.common import MAX_LOG_BYTES, UTC
from dashboard_core import (RESERVED, atomic_json, collect_one, configurations,
                            output_path, read_json, register, validate_config, validate_id)
from adapters.declarative import ensure_valid_extract
from adapters.script import ensure_valid_script
from layout import layout_for
from project_catalog import (CARDS, KIND_PROJECT_TYPES, create_project_files, missing_output_directories,
                             new_catalog, project_catalogs, project_paths, rollback_created, rollback_directories,
                             validate_catalog, validate_parent, validate_project_type)
import profiles as profile_library

MAX_DEPTH = 4
MAX_FILES = 1500
MAX_DIRS = 300
MAX_ENTRIES = 3500
MAX_PROFILES = 80
MAX_CHAPTERS = 256
MAX_ADAPTER_BYTES = 256 * 1024
MAX_INSPECTION_TEXT_BYTES = 16 * 1024 * 1024
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "env", "results", "result", "outputs", "output"}
STATUS_MAP = {"prepared": "unknown", "completed": "completed", "failed": "failed", "running": "running"}


def stable_id(value: str) -> str:
    """Keep simple names readable and disambiguate normalization or truncation."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    slug = slug or "project"
    if slug in RESERVED or slug != value.lower() or len(slug) > 64:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        slug = slug[:55].rstrip("-_") + "-" + digest
    return validate_id(slug)


def _warning(warnings: list[str], value: str) -> None:
    if value not in warnings and len(warnings) < 100:
        warnings.append(value)


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def source_directory(source: str | Path) -> Path:
    path = Path(source)
    if not path.is_absolute():
        raise ValueError("Select an absolute project folder path")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise FileNotFoundError("Selected project folder does not exist")
    return resolved


def _discover(root: Path, warnings: list[str]) -> list[Path]:
    """Enumerate a bounded set without following child links or reading binaries."""
    pending = [(root, 0)]
    selected: list[Path] = []
    count_files = count_entries = 0
    count_dirs = 1
    stopped = False
    while pending and not stopped:
        folder, depth = pending.pop(0)
        children: list[Path] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    count_entries += 1
                    if count_entries > MAX_ENTRIES:
                        stopped = True
                        _warning(warnings, "Folder inspection stopped at the entry limit; only discovered files are offered.")
                        break
                    if entry.name.startswith("."):
                        continue
                    path = Path(entry.path)
                    if _linked(path):
                        _warning(warnings, "Symbolic links and junctions were skipped during inspection.")
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() in SKIP_DIRS:
                            continue
                        if depth >= MAX_DEPTH:
                            _warning(warnings, "Folders deeper than four levels were skipped; select a narrower project folder if needed.")
                            continue
                        if count_dirs >= MAX_DIRS:
                            _warning(warnings, "The directory limit was reached; select a narrower project folder if needed.")
                            continue
                        count_dirs += 1
                        children.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        count_files += 1
                        if count_files > MAX_FILES:
                            stopped = True
                            _warning(warnings, "The file limit was reached; only discovered files are offered.")
                            break
                        if path.suffix.lower() in {".tex", ".inp", ".sta", ".msg", ".log"} or path.name.lower() == "metadata.json":
                            if any(token in path.relative_to(root).as_posix() for token in ("*", "?", "[", "]")):
                                _warning(warnings, "File names containing glob syntax require manual adapter configuration and were skipped.")
                            else:
                                selected.append(path)
        except OSError as error:
            _warning(warnings, f"A folder could not be inspected: {folder.relative_to(root).as_posix()} ({type(error).__name__}).")
        pending.extend((path, depth + 1) for path in sorted(children, key=lambda p: p.name.lower()))
    return sorted(selected, key=lambda p: p.relative_to(root).as_posix().lower())


def _text(path: Path, root: Path, warnings: list[str], budget: list[int]) -> str | None:
    try:
        size = path.stat().st_size
        if size > MAX_LOG_BYTES:
            _warning(warnings, f"Oversized text was not inspected: {path.relative_to(root).as_posix()}.")
            return None
        if size > budget[0] or budget[0] <= 0:
            _warning(warnings, "The total text inspection budget was reached; select a narrower folder for complete discovery.")
            return None
        with path.open("rb") as stream:
            payload = stream.read(min(MAX_LOG_BYTES + 1, budget[0] + 1))
        # A concurrently growing file can exceed the pre-read stat. Do not use
        # truncated text to infer a supported profile.
        if len(payload) > budget[0] or len(payload) > MAX_LOG_BYTES:
            budget[0] = max(0, budget[0] - len(payload))
            _warning(warnings, "The text inspection limit was reached while a file changed; select a narrower folder and inspect again.")
            return None
        budget[0] -= len(payload)
        return payload.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    except OSError as error:
        _warning(warnings, f"Text could not be inspected: {path.relative_to(root).as_posix()} ({type(error).__name__}).")
        return None


def _tex_profiles(root: Path, files: list[Path], warnings: list[str], budget: list[int]) -> list[dict[str, Any]]:
    texts: dict[Path, str] = {}
    for path in files:
        if path.suffix.lower() == ".tex":
            text = _text(path, root, warnings, budget)
            if text is not None:
                texts[path] = re.sub(r"(?<!\\)%[^\n]*", "", text)
    mains = [path for path, text in texts.items() if re.search(r"\\documentclass\b", text)]
    if not mains:
        mains = [path for path in texts if path.name.lower() == "main.tex"]
    mains.sort(key=lambda p: (p.name.lower() != "main.tex", p.relative_to(root).as_posix().lower()))
    profiles = []
    include = re.compile(r"\\(?:input|include)\b\s*(?:\{([^{}]*)\}|([^\s%{}]+))")
    for main in mains[:MAX_PROFILES]:
        chapters: list[str] = []
        visited: set[Path] = set()
        stack = [main]
        while stack:
            path = stack.pop()
            if path in visited:
                continue
            if len(visited) >= MAX_CHAPTERS:
                _warning(warnings, "The chapter dependency limit was reached; some included files are not monitored.")
                break
            visited.add(path)
            chapters.append(path.relative_to(root).as_posix())
            dependencies = []
            for match in include.finditer(texts.get(path, "")):
                name = (match.group(1) if match.group(1) is not None else match.group(2)).strip()
                if not name or any(token in name for token in ("\\", "#", "$", "{", "}")):
                    _warning(warnings, f"A dynamic LaTeX include was skipped in {path.relative_to(root).as_posix()}.")
                    continue
                if Path(name).is_absolute() or PureWindowsPath(name).is_absolute():
                    _warning(warnings, "Absolute LaTeX includes were skipped; only files inside the selected folder are monitored.")
                    continue
                # TeX automatically appends .tex even when a chapter basename
                # contains punctuation, such as Tex/0.Abstract.
                if not name.lower().endswith(".tex"):
                    name += ".tex"
                candidates = [root / name, path.parent / name]
                candidate = next((item for item in candidates if item.is_file()), candidates[0])
                resolved = candidate.resolve()
                if not resolved.is_relative_to(root):
                    _warning(warnings, "A LaTeX include outside the selected folder was skipped.")
                    continue
                if any(_linked(root.joinpath(*candidate.relative_to(root).parts[:index]))
                       for index in range(1, len(candidate.relative_to(root).parts) + 1)):
                    _warning(warnings, "A linked LaTeX include was skipped.")
                    continue
                if resolved not in texts:
                    relative_missing = resolved.relative_to(root)
                    parts = relative_missing.parts
                    allowed_missing = (not resolved.exists() and resolved.suffix.lower() == ".tex" and
                                       len(parts) - 1 <= MAX_DEPTH and
                                       not any(part.startswith(".") or part.lower() in SKIP_DIRS for part in parts[:-1]) and
                                       not any(token in relative_missing.as_posix() for token in ("*", "?", "[", "]")))
                    if allowed_missing:
                        _warning(warnings, f"A declared TeX include is missing and will be monitored when created: {relative_missing.as_posix()}.")
                    else:
                        _warning(warnings, f"A LaTeX include is excluded or outside the inspection limits: {relative_missing.as_posix()}.")
                        continue
                dependencies.append(resolved)
            stack.extend(reversed(dependencies))
        relative = main.relative_to(root).as_posix()
        # Select this entry's own expected build log, never an unrelated newer log.
        log = main.with_suffix(".log").relative_to(root).as_posix()
        if not (root / log).is_file():
            _warning(warnings, f"No build log exists for {relative}; edits can be observed, while build status remains unknown until {log} exists.")
        profiles.append({"id": stable_id("latex-" + relative), "label": "LaTeX · " + relative,
                         "kind": "latex", "adapter": {"mainFile": relative, "chapters": chapters, "logPatterns": [log]},
                         "summary": [f"Entry: {relative}", f"Monitored TeX files: {len(chapters)}", f"Build log: {log}",
                                     "Chapter readiness is manual; file edits and build status do not measure paper completion."]})
    if mains:
        _warning(warnings, "Literal TeX dependencies are refreshed during collection. Dynamic macro-generated includes cannot be resolved; review the paper's data warnings for coverage.")
    return profiles


def _abaqus_profiles(root: Path, files: list[Path], warnings: list[str], budget: list[int]) -> list[dict[str, Any]]:
    groups: dict[tuple[Path, str], list[Path]] = {}
    for path in files:
        if path.suffix.lower() in {".inp", ".sta", ".msg", ".log"}:
            groups.setdefault((path.parent, path.stem), []).append(path)
    profiles = []
    for (parent, stem), logs in sorted(groups.items(), key=lambda pair: str(pair[0]).lower()):
        # A generic .log could be LaTeX or another tool. Require status/message
        # extensions, or explicit Abaqus text in a bounded inspected log.
        is_abaqus = any(path.suffix.lower() in {".sta", ".msg"} for path in logs)
        if not is_abaqus:
            for path in logs:
                text = _text(path, root, warnings, budget)
                keyword = (r"^\s*\*(?:HEADING|PART|NODE|ELEMENT|STEP|INCLUDE|ASSEMBLY|MATERIAL)\b"
                           if path.suffix.lower() == ".inp" else r"\bAbaqus\b")
                if text and re.search(keyword, text, re.IGNORECASE | re.MULTILINE):
                    is_abaqus = True
                    break
        if not is_abaqus:
            continue
        identity = (parent / stem).relative_to(root).as_posix()
        patterns = [(parent / (stem + extension)).relative_to(root).as_posix() for extension in (".sta", ".msg", ".log")]
        observed_logs = sum(path.suffix.lower() != ".inp" for path in logs)
        summary = [f"Selected job: {identity}", f"Existing status logs: {observed_logs}",
                   "No total job plan or progress percentage was inferred.",
                   "Recent matching log activity is not independent proof of a live solver."]
        if observed_logs == 0:
            summary.append("Awaiting source logs: the input can be connected now; runtime remains unknown until matching job logs exist.")
        profiles.append({"id": stable_id("abaqus-" + identity), "label": "Abaqus · " + identity,
                         "kind": "abaqus", "adapter": {"logPatterns": patterns},
                         "summary": summary})
    return profiles


def _isale_profile(root: Path, files: list[Path], warnings: list[str], budget: list[int]) -> list[dict[str, Any]]:
    records = []
    for path in files:
        if path.name.lower() != "metadata.json":
            continue
        text = _text(path, root, warnings, budget)
        if text is None:
            continue
        try:
            record = json.loads(text)
            supported = (isinstance(record, dict) and "case_id" in record and
                         isinstance(record.get("model_name"), str) and
                         record.get("status") in STATUS_MAP)
            if supported and record["status"] == "completed":
                stamp = record.get("completed")
                supported = isinstance(stamp, str) and bool(stamp.strip())
                if supported:
                    dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if not supported:
                _warning(warnings, f"Unsupported metadata was skipped: {path.relative_to(root).as_posix()}.")
                continue
            records.append(path.relative_to(root).as_posix())
        except (ValueError, TypeError):
            _warning(warnings, f"Invalid metadata was skipped: {path.relative_to(root).as_posix()}.")
    if not records:
        return []
    _warning(warnings, "iSALE metadata support applies to the detected case_id/model_name/status/completed batch workflow; other versions need a verified adapter.")
    return [{"id": "isale-observed-metadata", "label": "iSALE · observed batch metadata", "kind": "isale",
             "adapter": {"mode": "metadata-json", "metadataPatterns": records, "cases": [],
                         "statusField": "status", "completionField": "completed", "statusMap": STATUS_MAP.copy()},
             "summary": [f"Observed supported metadata records: {len(records)}",
                         "Prepared records remain unknown; completed records require a valid completion timestamp.",
                         "The total planned case count remains unknown; observed cases do not define the project plan.",
                         "Post-processing and research acceptance remain separate manual milestones."]}]


def inspect_folder(source: str | Path, vault: Path | None = None) -> dict[str, Any]:
    root = source_directory(source)
    warnings: list[str] = []
    if Path(source) != root:
        _warning(warnings, "The selected folder was resolved to its canonical local path.")
    files = _discover(root, warnings)
    budget = [MAX_INSPECTION_TEXT_BYTES]
    profiles = (_tex_profiles(root, files, warnings, budget) +
                _abaqus_profiles(root, files, warnings, budget) + _isale_profile(root, files, warnings, budget))
    if len(profiles) > MAX_PROFILES:
        profiles = profiles[:MAX_PROFILES]
        _warning(warnings, "The profile limit was reached; select a narrower folder to inspect more entries.")
    if not profiles:
        _warning(warnings, "No supported TeX entry, Abaqus input/job log, or verified iSALE batch metadata was found. A custom adapter is needed for other formats.")
    occupied: set[str] = set()
    if vault is not None:
        layout = layout_for(vault)
        configs = output_path(vault, layout.source_configs())
        notes = output_path(vault, layout.records_dir())
        occupied.update(path.stem for path in configs.glob("*.json"))
        occupied.update(path.parent.name for path in notes.glob("*/project.md"))
        occupied.update(path.stem for path in notes.glob("*.md"))
    for profile in profiles:
        base = stable_id((root.name or "project") + "-" + profile["id"])
        candidate, counter = base, 2
        while candidate in occupied:
            suffix = f"-{counter}"
            candidate = base[:64 - len(suffix)].rstrip("-_") + suffix
            counter += 1
        occupied.add(candidate)
        profile["suggestedId"] = candidate
        profile["suggestedTitle"] = (root.name or "Research project") + " · " + profile["label"]
    # Score the monitoring-profile library (built-in + user). Built-in candidates
    # point at the connectable entries above through inspectedProfileIds.
    detection = profile_library.detect(root, vault, legacy=profiles)
    for candidate in detection["candidates"]:
        if candidate["inspectedProfileIds"]:
            first = next(item for item in profiles if item["id"] == candidate["inspectedProfileIds"][0])
            candidate["suggestedId"], candidate["suggestedTitle"] = first["suggestedId"], first["suggestedTitle"]
            continue
        base = stable_id((root.name or "project") + "-" + candidate["profileId"])
        suggestion, counter = base, 2
        while suggestion in occupied:
            suffix = f"-{counter}"
            suggestion = base[:64 - len(suffix)].rstrip("-_") + suffix
            counter += 1
        occupied.add(suggestion)
        candidate["suggestedId"] = suggestion
        candidate["suggestedTitle"] = (root.name or "Research project") + " · " + candidate["title"]
    for message in detection["warnings"]:
        _warning(warnings, "Profile detection: " + message)
    for item in detection["libraryErrors"]:
        _warning(warnings, f"A monitoring profile could not be loaded: {Path(item['path']).name} ({item['error']})")
    return {"sourcePath": str(root), "title": root.name or "Research project", "suggestedId": stable_id(root.name or "project"),
            "profiles": profiles, "candidates": detection["candidates"], "warnings": warnings}


def read_adapter_stdin(stream: Any) -> dict[str, Any]:
    payload = stream.read(MAX_ADAPTER_BYTES + 1)
    if len(payload.encode("utf-8")) > MAX_ADAPTER_BYTES:
        raise ValueError("Adapter JSON exceeds the input limit")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Adapter JSON must contain an object")
    return value


def _validate_adapter(root: Path, kind: str, adapter: dict[str, Any]) -> None:
    """Require explicit inspected files, rather than accepting arbitrary glob scans."""
    if not isinstance(adapter, dict):
        raise ValueError("Adapter must be an object")
    allowed = {"latex": {"mainFile", "chapters", "logPatterns"}, "abaqus": {"logPatterns"},
               "isale": {"mode", "metadataPatterns", "cases", "statusField", "completionField", "statusMap"}}[kind]
    if set(adapter) - allowed:
        raise ValueError("Folder onboarding accepts only options from its inspected profile; advanced configuration belongs in the project config")
    for key in ("chapters", "logPatterns", "metadataPatterns"):
        if key in adapter and (not isinstance(adapter[key], list) or not all(isinstance(item, str) for item in adapter[key])):
            raise ValueError(f"Adapter {key} must be a list of relative file paths")
    paths: list[str] = []
    if kind == "latex":
        if not adapter.get("chapters") or not adapter.get("logPatterns"):
            raise ValueError("LaTeX profile requires explicit chapters and its matching build log")
        if adapter.get("mainFile", adapter["chapters"][0]) != adapter["chapters"][0]:
            raise ValueError("LaTeX mainFile must equal its inspected entry file")
        if (any(Path(item).suffix.lower() != ".tex" for item in adapter["chapters"]) or
                adapter["logPatterns"] != [Path(adapter["chapters"][0]).with_suffix(".log").as_posix()]):
            raise ValueError("LaTeX profile must monitor its entry's own matching log and TeX chapter files")
        paths += adapter["chapters"] + adapter["logPatterns"]
    elif kind == "abaqus":
        if not adapter.get("logPatterns"):
            raise ValueError("Abaqus profile requires explicit job log paths")
        selected = [Path(item) for item in adapter["logPatterns"]]
        if (len({(item.parent.as_posix(), item.stem) for item in selected}) != 1 or
                any(item.suffix.lower() not in {".sta", ".msg", ".log"} for item in selected)):
            raise ValueError("Abaqus onboarding selects one exact job stem per profile")
        paths += adapter["logPatterns"]
    else:
        if (adapter.get("mode") != "metadata-json" or adapter.get("cases") != [] or
                adapter.get("statusField") != "status" or adapter.get("completionField") != "completed" or
                adapter.get("statusMap") != STATUS_MAP or not adapter.get("metadataPatterns")):
            raise ValueError("iSALE onboarding requires the supported observed-metadata profile")
        paths += adapter["metadataPatterns"]
        if any(Path(item).name.lower() != "metadata.json" for item in paths):
            raise ValueError("iSALE onboarding expects explicit metadata.json records")
    if len(paths) > MAX_FILES:
        raise ValueError("Adapter contains too many file paths")
    for value in paths:
        if (not isinstance(value, str) or not value or Path(value).is_absolute() or
                PureWindowsPath(value).is_absolute() or ".." in Path(value).parts or
                any(token in value for token in ("*", "?", "[", "]"))):
            raise ValueError("Adapter file paths must be explicit relative paths without traversal or glob patterns")
        path = root / value
        if not path.resolve().is_relative_to(root):
            raise ValueError("Adapter file path escapes the selected folder")
        if any(_linked(root.joinpath(*Path(value).parts[:index])) for index in range(1, len(Path(value).parts) + 1)):
            raise ValueError("Adapter file paths cannot follow symbolic links or junctions")
        suffix = path.suffix.lower()
        if suffix not in {".tex", ".log", ".sta", ".msg", ".json"}:
            raise ValueError("Adapter references an unsupported file type")


def _validate_declarative(root: Path, adapter: dict[str, Any]) -> None:
    """Declarative adapters are validated rule by rule (globs stay inside root); custom adapters pass the static check."""
    if set(adapter) - {"engine", "profileId", "profileVersion", "extract"} or adapter.get("engine") not in {"declarative", "script"}:
        raise ValueError("A declarative or script adapter contains only engine, profileId, profileVersion and extract")
    (ensure_valid_script if adapter["engine"] == "script" else ensure_valid_extract)(adapter.get("extract"))


def resolve_connection(kind: str | None, adapter: dict[str, Any] | None,
                       profile: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Combine --kind, adapter JSON and a monitoring profile into (kind, adapter)."""
    if profile is None:
        if kind is None or adapter is None:
            raise ValueError("Connecting without a profile requires --kind and adapter JSON on stdin")
        return kind, adapter
    profile_library.ensure_valid(profile)
    profile_kind = profile_library.source_kind(profile)
    if kind is not None and kind != profile_kind:
        raise ValueError(f"--kind {kind} does not match profile {profile['id']} (kind {profile_kind})")
    if profile_library.engine(profile) in {"declarative", "script"}:
        if adapter:
            raise ValueError("A declarative or script profile supplies its own extraction; do not pass adapter JSON")
        return profile_kind, {"engine": profile_library.engine(profile), "profileId": profile["id"],
                              "profileVersion": profile.get("origin", {}).get("version", 1),
                              "extract": json.loads(json.dumps(profile["extract"]))}
    if adapter is None:
        raise ValueError(f"Profile {profile['id']} uses the existing {profile_kind} adapter; pass the inspected adapter JSON on stdin")
    return profile_kind, adapter


def connect_folder(vault: Path, host: str, source: str | Path, project_id: str, kind: str | None,
                   title: str, adapter: dict[str, Any] | None, interval: int = 60,
                   stale_after: int = 180, dashboard_project: str | None = None,
                   profile: dict[str, Any] | None = None, project_type: str | None = None,
                   parent_id: str | None = None, milestone_template: str | None = None) -> dict[str, Any]:
    """Register an explicit user selection, then perform one read-only collection.

    ``profile`` (a validated monitoring profile) records ``profileId`` in the
    source configuration; declarative profiles supply their own adapter.
    ``project_type`` and ``parent_id`` apply only to a newly created project.
    ``milestone_template`` (an ID, ``profile`` or ``none``) is applied after the
    first collection; its failure never undoes the connection.
    """
    root = source_directory(source)
    layout = layout_for(vault)
    validate_id(project_id)
    validate_id(host, "hostId")
    kind, adapter = resolve_connection(kind, adapter, profile)
    if kind not in {"latex", "abaqus", "isale"}:
        raise ValueError("Unsupported project kind")
    if adapter.get("engine") is not None:
        _validate_declarative(root, adapter)
    else:
        _validate_adapter(root, kind, adapter)
    catalogs = project_catalogs(vault)
    identity = validate_id(dashboard_project, "dashboardProjectId") if dashboard_project is not None else project_id
    original_catalog = next((item for item in catalogs if item["projectId"] == identity), None)
    if dashboard_project is not None and original_catalog is None:
        raise ValueError("Selected dashboard project does not exist")
    if dashboard_project is None and original_catalog is not None:
        raise FileExistsError("Dashboard project ID already exists; attach to it explicitly or choose a new source ID")
    if dashboard_project is not None and (project_type is not None or parent_id is not None):
        raise ValueError("Project type and parent apply only when creating a new project, not when attaching a source")
    project_type = validate_project_type(project_type or (profile["projectType"] if profile else KIND_PROJECT_TYPES[kind]))
    parent_id = validate_parent(vault, identity, parent_id, catalogs) if dashboard_project is None else None
    if milestone_template not in (None, "none", "profile"):
        validate_id(milestone_template, "milestoneTemplate")
    if milestone_template == "profile" and (profile is None or profile.get("milestoneTemplate") is None):
        raise ValueError("The selected profile has no milestone template")
    if any(project_id in item["sourceIds"] for item in catalogs):
        raise FileExistsError("Source ID already belongs to a dashboard project")
    catalog = (json.loads(json.dumps(original_catalog)) if original_catalog is not None
               else new_catalog(identity, title, project_id, layout, project_type, parent_id))
    if original_catalog is not None:
        catalog["sourceIds"].append(project_id)
    validate_catalog(catalog, layout)
    paths = project_paths(identity, layout)
    catalog_path = output_path(vault, layout.catalog(identity))
    config = {"schemaVersion": "1.0", "projectId": project_id, "dashboardProjectId": identity, "title": title, "kind": kind,
              "parentId": None, "projectNote": layout.source_note(project_id),
              "source": {"rootAlias": project_id, "path": ""},
              "collector": {"hostId": host, "intervalSeconds": interval, "staleAfterSeconds": stale_after},
              "adapter": adapter}
    if profile is not None:
        config["profileId"] = profile["id"]
    validate_config(config)
    config_path = output_path(vault, layout.source_config(project_id))
    note_path = output_path(vault, config["projectNote"])
    if config_path.exists() or note_path.exists():
        raise FileExistsError("Project ID already exists; choose a new ID")
    local_path = output_path(vault, layout.local(host))
    original = read_json(local_path) if local_path.exists() else None
    local = json.loads(json.dumps(original)) if original is not None else {"schemaVersion": "1.0", "aliases": {}}
    if local.get("schemaVersion") != "1.0" or not isinstance(local.get("aliases"), dict):
        raise ValueError("Existing host mappings are invalid")
    previous = local["aliases"].get(project_id)
    if project_id in local["aliases"] and (not isinstance(previous, str) or Path(previous).resolve() != root):
        raise ValueError("Project alias already maps to a different folder")
    # Multiple profiles in one folder are valid, but an identical selection is
    # usually an accidental duplicate. Compare only this host's canonical roots.
    for existing in configurations(vault):
        if existing["kind"] != kind or existing["adapter"] != adapter or existing["collector"]["hostId"] != host:
            continue
        alias = existing["source"]["rootAlias"]
        mapped = local["aliases"].get(alias)
        if isinstance(mapped, str) and (Path(mapped) / existing["source"]["path"]).resolve() == root:
            raise ValueError(f"This folder profile is already connected as {existing['projectId']}")
    local["aliases"][project_id] = str(root)
    changed = original != local
    created: list[tuple[Path, bytes]] = []
    planned = [config_path, note_path, local_path, catalog_path]
    planned.extend(output_path(vault, value) for value in paths.values() if value is not None)
    if layout.cards_dir(identity):
        planned.extend(output_path(vault, f"{layout.cards_dir(identity)}/{card}.md") for card in CARDS)
    missing_directories = missing_output_directories(vault, planned)
    if changed:
        atomic_json(local_path, local)
    try:
        register(vault, config)
        created.extend((path, path.read_bytes()) for path in (config_path, note_path))
        # A new logical project starts with a type inferred from its first source.
        create_project_files(vault, catalog, created, project_type, parent_id)
        # Recheck the exact catalog before publishing membership. A concurrent
        # connector must not have its legitimate membership change overwritten.
        current_catalog = read_json(catalog_path) if catalog_path.exists() else None
        if current_catalog != original_catalog:
            raise ValueError("Dashboard project changed while connecting; retry the connection")
        atomic_json(catalog_path, catalog, exclusive=original_catalog is None)
        # Validate the published ownership against the complete source registry.
        project_catalogs(vault)
    except Exception:
        if catalog_path.exists():
            try:
                if read_json(catalog_path) == catalog:
                    if original_catalog is None:
                        catalog_path.unlink()
                    else:
                        atomic_json(catalog_path, original_catalog)
            except (OSError, ValueError, TypeError):
                pass
        rollback_created(created)
        # Restore only our exact write. Another connector may have legitimately
        # added mappings while registration was in progress.
        if changed and local_path.exists() and read_json(local_path) == local:
            if original is None:
                local_path.unlink(missing_ok=True)
            else:
                atomic_json(local_path, original)
        rollback_directories(missing_directories)
        raise
    try:
        state = collect_one(vault, config, host, dt.datetime.now(UTC))
        health, status, error = state["collection"]["health"], state["runtime"]["status"], state["collection"]["error"]
    except (OSError, ValueError, KeyError, TypeError) as failure:
        # The connection already exists. Report the partial result so the user
        # can open the note and repair collection without repeating registration.
        health, status, error = "error", "unknown", f"{type(failure).__name__}: {failure}"
    result = {"projectId": project_id, "projectNote": config["projectNote"],
              "configPath": config_path.relative_to(vault.resolve()).as_posix(), "collectionHealth": health,
              "runtimeStatus": status, "collectionError": error, "dashboardProjectId": identity,
              "projectDashboard": paths["projectDashboard"],
              "projectRecord": layout.catalog(identity) if layout.record_in_catalog else paths["projectNote"],
              "projectMilestones": paths["projectNote"],
              "profileId": profile["id"] if profile else None, "kind": kind,
              "projectType": project_type if original_catalog is None else None, "parentId": parent_id}
    if milestone_template not in (None, "none"):
        # Imported lazily: milestones depends on the profile library, not on onboarding.
        from milestones import apply_template, find_template, inline_template
        try:
            choice = profile.get("milestoneTemplate") if milestone_template == "profile" else milestone_template
            template = inline_template(choice) if isinstance(choice, list) else find_template(vault, choice)
            result["milestones"] = apply_template(vault, identity, template, source_id=project_id
                                                  if template["mode"] == "per-section" else None)
        except (OSError, ValueError, KeyError, TypeError) as failure:
            result["milestones"] = {"error": f"{type(failure).__name__}: {failure}", "addedCount": 0}
    return result
