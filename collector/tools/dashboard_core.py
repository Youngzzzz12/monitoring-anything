"""Portable, standard-library collection and safe vault output contracts."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import socket
import tempfile
from pathlib import Path
from typing import Any

from adapters import ADAPTERS, ENGINES
from adapters.declarative import ensure_valid_extract
from adapters.script import ensure_valid_script
from adapters.common import UTC, iso, parse_time
from adapters.latex_paper import observed_history
from layout import layout_for, source_note_paths

VERSION = "1.2.0"
KINDS = {"latex", "abaqus", "isale"}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class CollectionSuperseded(RuntimeError):
    """The source, registration or output changed before a collection commit."""


def _file_version(path: Path) -> tuple[int, int, str] | None:
    if not path.is_file():
        return None
    stamp = path.stat()
    return stamp.st_mtime_ns, stamp.st_size, hashlib.sha256(path.read_bytes()).hexdigest()


def host_id() -> str:
    return re.sub(r"[^a-z0-9_-]", "-", socket.gethostname().lower())[:64]


def validate_id(value: Any, label: str = "projectId") -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value) or value in RESERVED:
        raise ValueError(f"{label} must be a safe lowercase ID (1–64 characters)")
    return value


def output_path(vault: Path, relative: str) -> Path:
    """Reject traversal, absolute paths and symlinked output roots outside vault."""
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Output must be a relative vault path")
    target = (vault / relative).resolve()
    if not target.is_relative_to(vault.resolve()):
        raise ValueError(f"Output escapes the vault: {relative}")
    return target


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def atomic_text(path: Path, text: str, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        # Exclusive open establishes ownership before writing; a write failure
        # must not leave a partial file that masquerades as a valid registration.
        stream = path.open("x", encoding="utf-8", newline="\n")
        try:
            with stream:
                stream.write(text)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return
    handle, temporary = tempfile.mkstemp(prefix=".dashboard-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: dict[str, Any], exclusive: bool = False) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", exclusive)


def validate_config(config: dict[str, Any]) -> None:
    if config.get("schemaVersion") != "1.0":
        raise ValueError("Unsupported configuration schemaVersion")
    pid = validate_id(config.get("projectId"))
    if "dashboardProjectId" in config:
        validate_id(config["dashboardProjectId"], "dashboardProjectId")
    if config.get("kind") not in KINDS:
        raise ValueError("kind must be latex, abaqus or isale")
    if not isinstance(config.get("title"), str) or not config["title"].strip():
        raise ValueError("title must be nonempty")
    if config.get("parentId") is not None:
        validate_id(config["parentId"], "parentId")
        if config["parentId"] == pid:
            raise ValueError("Project cannot be its own parent")
    # The source record path is fixed by the workspace layout (vault or app workspace).
    if config.get("projectNote") not in source_note_paths(pid):
        raise ValueError("projectNote must equal the layout's source record path "
                         "(Research Dashboard/System/records/<projectId>/project.md or sources/<projectId>.md)")
    source = config.get("source", {})
    if not isinstance(source, dict) or source.get("rootAlias") is not None and not isinstance(source.get("rootAlias"), str):
        raise ValueError("source.rootAlias must be string or null")
    if not isinstance(source.get("path"), str) or Path(source["path"]).is_absolute() or ".." in Path(source["path"]).parts:
        raise ValueError("source.path must be relative to rootAlias without traversal")
    collector = config.get("collector", {})
    validate_id(collector.get("hostId"), "collector.hostId")
    for field, minimum in [("staleAfterSeconds", 1), ("intervalSeconds", 1)]:
        number = collector.get(field)
        if isinstance(number, bool) or not isinstance(number, int) or number < minimum:
            raise ValueError(f"collector.{field} must be a positive integer")
    if config.get("profileId") is not None:
        validate_id(config["profileId"], "profileId")
    adapter = config.get("adapter")
    if not isinstance(adapter, dict):
        raise ValueError("adapter must be an object")
    if "engine" in adapter:
        # Declarative profiles keep a display kind (latex/abaqus/isale) for the
        # view, while extraction is selected by the adapter engine.
        if adapter["engine"] not in ENGINES:
            raise ValueError("adapter.engine must be: " + ", ".join(sorted(ENGINES)))
        if set(adapter) - {"engine", "profileId", "profileVersion", "extract"}:
            raise ValueError("A declarative or script adapter contains only engine, profileId, profileVersion and extract")
        if adapter.get("profileId") is not None:
            validate_id(adapter["profileId"], "adapter.profileId")
        (ensure_valid_script if adapter["engine"] == "script" else ensure_valid_extract)(adapter.get("extract"))
        return
    for field in ("targetValue", "activeWindowSeconds"):
        if field in adapter and adapter[field] is not None and (isinstance(adapter[field], bool) or not isinstance(adapter[field], (int, float))
                                 or not math.isfinite(adapter[field]) or adapter[field] < 0):
            raise ValueError(f"adapter.{field} must be a finite nonnegative number")
    for field, value in adapter.items():
        if field.endswith("Pattern") and value is not None:
            if not isinstance(value, str):
                raise ValueError(f"adapter.{field} must be a regex string")
            compiled = re.compile(value, re.IGNORECASE | re.MULTILINE)
            if field == "currentValuePattern" and "value" not in compiled.groupindex:
                raise ValueError("currentValuePattern must contain named group (?P<value>...)")
            if field == "stepPattern" and "step" not in compiled.groupindex:
                raise ValueError("stepPattern must contain named group (?P<step>...)")
    if adapter.get("notBefore"):
        parse_time(adapter["notBefore"])
    for field in ("chapters", "logPatterns"):
        if field in adapter and (not isinstance(adapter[field], list) or not all(isinstance(v, str) for v in adapter[field])):
            raise ValueError(f"adapter.{field} must be an array of relative strings")
    if "mainFile" in adapter and (not isinstance(adapter["mainFile"], str) or not adapter["mainFile"] or
                                  Path(adapter["mainFile"]).is_absolute() or ".." in Path(adapter["mainFile"]).parts):
        raise ValueError("adapter.mainFile must be a nonempty relative path")
    if "wordCountMethod" in adapter and adapter["wordCountMethod"] not in {"auto", "heuristic"}:
        raise ValueError("adapter.wordCountMethod must be auto or heuristic")
    jobs = adapter.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError("adapter.jobs must be an array")
    identities = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("Each job must be an object")
        job_id = validate_id(job.get("id"), "job.id")
        if job_id in identities:
            raise ValueError("Duplicate planned job IDs")
        identities.add(job_id)
        if not isinstance(job.get("logPatterns"), list) or not all(isinstance(v, str) for v in job["logPatterns"]):
            raise ValueError("Each job requires logPatterns")
        if job.get("notBefore"):
            parse_time(job["notBefore"])
    cases = adapter.get("cases", [])
    if not isinstance(cases, list):
        raise ValueError("adapter.cases must be an array")
    case_ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each case must be an object")
        case_id = validate_id(case.get("id"), "case.id")
        if case_id in case_ids:
            raise ValueError("Duplicate planned case IDs")
        case_ids.add(case_id)
        if not isinstance(case.get("metadata"), str) or Path(case["metadata"]).is_absolute() or ".." in Path(case["metadata"]).parts:
            raise ValueError("case.metadata must be a relative path")


def adapter_for(config: dict[str, Any]):
    """Collector function: a declarative engine when configured, else the kind's adapter."""
    engine = config["adapter"].get("engine")
    return ENGINES[engine] if engine else ADAPTERS[config["kind"]]


def configurations(vault: Path, project: str | None = None) -> list[dict[str, Any]]:
    folder = output_path(vault, layout_for(vault).source_configs())
    if project:
        paths = [folder / (validate_id(project) + ".json")]
    else:
        paths = sorted(folder.glob("*.json")) if folder.exists() else []
    configs = []
    for path in paths:
        config = read_json(path)
        validate_config(config)
        if path.stem != config["projectId"]:
            raise ValueError(f"Config filename does not match projectId: {path.name}")
        configs.append(config)
    all_configs = {c["projectId"]: c for c in configs}
    # Check the complete registry even when collecting a selected project.
    if project:
        for path in folder.glob("*.json"):
            if path.stem not in all_configs:
                item = read_json(path)
                validate_config(item)
                all_configs[item["projectId"]] = item
    for config in all_configs.values():
        seen = {config["projectId"]}
        parent = config.get("parentId")
        while parent:
            if parent in seen:
                raise ValueError("Project parent cycle detected")
            if parent not in all_configs:
                raise ValueError(f"Unknown parentId: {parent}")
            seen.add(parent)
            parent = all_configs[parent].get("parentId")
    return configs


def source_root(vault: Path, config: dict[str, Any], host: str) -> Path | None:
    alias = config["source"]["rootAlias"]
    if alias is None:
        return None
    local = read_json(output_path(vault, layout_for(vault).local(validate_id(host, 'hostId'))))
    if local.get("schemaVersion") != "1.0" or not isinstance(local.get("aliases"), dict):
        raise ValueError("Local alias configuration requires schemaVersion and aliases")
    if alias not in local["aliases"]:
        raise ValueError(f"No local directory mapping for alias {alias!r}")
    value = local["aliases"][alias]
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("Local alias must map to an absolute path")
    base = Path(value).resolve()
    root = (base / config["source"]["path"]).resolve()
    if not root.is_relative_to(base):
        raise ValueError("Source path escapes root alias")
    if not root.is_dir():
        raise FileNotFoundError(f"Source directory is unavailable: {root}")
    return root


def blank_state(config: dict[str, Any], host: str, now: dt.datetime) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0", "projectId": config["projectId"], "title": config["title"],
        "kind": config["kind"], "parentId": config.get("parentId"), "projectNote": config["projectNote"],
        "runtime": {"status": "unconfigured", "progress": None, "step": None, "currentValue": None,
                    "targetValue": None, "unit": None, "runId": None},
        "cases": {"planned": None, "completed": None, "running": None, "failed": None, "unknown": None},
        "collection": {"health": "unconfigured", "attemptedAt": iso(now), "lastSuccessfulAt": None,
                       "sourceModifiedAt": None, "collectorVersion": VERSION, "hostId": host,
                       "staleAfterSeconds": config["collector"]["staleAfterSeconds"], "error": None},
        "artifacts": [], "details": [],
        "provenance": {"sourceFiles": [], "parserVersion": VERSION, "configId": config["projectId"]},
    }


def validate_state(state: dict[str, Any]) -> None:
    if state.get("schemaVersion") != "1.0":
        raise ValueError("Unsupported state schemaVersion")
    validate_id(state.get("projectId"))
    if state.get("kind") not in KINDS:
        raise ValueError("Invalid state kind")
    runtime = state["runtime"]
    if runtime["status"] not in {"unconfigured", "queued", "running", "completed", "failed", "unknown"}:
        raise ValueError("Invalid runtime status")
    for field in ("progress", "currentValue", "targetValue"):
        value = runtime[field]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)):
            raise ValueError(f"runtime.{field} must be finite or null")
    if runtime["progress"] is not None and not 0 <= runtime["progress"] <= 1:
        raise ValueError("runtime.progress must be 0..1")
    for value in state["cases"].values():
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError("Case counts must be nonnegative integers or null")
    cases = state["cases"]
    if cases["planned"] is not None and all(cases[k] is not None for k in ("completed", "running", "failed", "unknown")):
        if sum(cases[k] for k in ("completed", "running", "failed", "unknown")) != cases["planned"]:
            raise ValueError("Case categories must sum to planned")
    if state["collection"]["health"] not in {"unconfigured", "fresh", "stale", "error"}:
        raise ValueError("Invalid collection health")
    for field in ("attemptedAt", "lastSuccessfulAt", "sourceModifiedAt"):
        parse_time(state["collection"][field])
    for detail in state["details"]:
        if detail.get("kind") != "paper":
            continue
        if detail.get("schemaVersion") != 1 or not isinstance(detail.get("chapters"), list):
            raise ValueError("Invalid paper detail schema")
        identities = set()
        for chapter in detail["chapters"]:
            identity = chapter.get("id")
            if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", identity) or identity in identities:
                raise ValueError("Paper section IDs must be unique safe strings")
            identities.add(identity)
            for field in ("bodyWords", "ownBodyWords"):
                value = chapter.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"paper chapter {field} must be a nonnegative integer")
            for field in ("deltaToday", "deltaWeek"):
                value = chapter.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                    raise ValueError(f"paper chapter {field} must be an integer or null")
            if not isinstance(chapter.get("sourceHash"), str) or not re.fullmatch(r"[a-f0-9]{64}", chapter["sourceHash"]):
                raise ValueError("Invalid paper source hash")
        if any(chapter.get("parentId") is not None and chapter["parentId"] not in identities for chapter in detail["chapters"]):
            raise ValueError("Paper section parent is missing")


def effective_health(state: dict[str, Any], now: dt.datetime) -> str:
    collection = state["collection"]
    if collection["health"] in {"unconfigured", "error"}:
        return collection["health"]
    successful = parse_time(collection["lastSuccessfulAt"])
    return "stale" if successful is None or (now - successful).total_seconds() > collection["staleAfterSeconds"] else "fresh"


def projection(state: dict[str, Any], now: dt.datetime, state_path: str | None = None) -> str:
    fields = {
        "type": "dashboard-status", "project_id": state["projectId"], "project_kind": state["kind"],
        "collection_health": effective_health(state, now), "runtime_status": state["runtime"]["status"],
        "last_successful_at": state["collection"]["lastSuccessfulAt"],
        "stale_after_seconds": state["collection"]["staleAfterSeconds"],
        "runtime_progress": state["runtime"]["progress"], "cases_planned": state["cases"]["planned"],
        "cases_completed": state["cases"]["completed"], "cases_failed": state["cases"]["failed"],
        "project_note": state["projectNote"], "attempted_at": state["collection"]["attemptedAt"],
        "source_modified_at": state["collection"]["sourceModifiedAt"],
    }
    frontmatter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False, allow_nan=False)}" for key, value in fields.items())
    error = state["collection"]["error"]
    return (f"---\n{frontmatter}\n---\n\n# {state['title'].replace(chr(10), ' ')} · Automated status\n\n"
            "> [!info] Generated automatically\n> Edit the source configuration or the project note; this file is rebuilt during collection.\n\n"
            f"Project: [[{state['projectNote']}]]\n\n"
            f"State data: [[{state_path or 'Research Dashboard/System/data/projects/' + state['projectId'] + '.json'}]]\n\n"
            f"Run status: **{state['runtime']['status']}**; collection status: **{effective_health(state, now)}**.\n\n"
            f"Last successful collection: {state['collection']['lastSuccessfulAt'] or '—'}\n\n"
            + (f"Collection error: {str(error).replace(chr(10), ' ')}\n\n" if error else "")
            + "Status reflects the latest observation and does not establish research completion. This file does not update as time passes; the Dashboard calculates staleness.\n")


def collect_one(vault: Path, config: dict[str, Any], host: str, now: dt.datetime) -> dict[str, Any]:
    validate_config(config)
    if config["collector"]["hostId"] != host:
        raise PermissionError(f"Project {config['projectId']} is assigned to host {config['collector']['hostId']}, not {host}")
    layout = layout_for(vault)
    target = output_path(vault, layout.state(config['projectId']))
    config_path = output_path(vault, layout.source_config(config['projectId']))
    config_version = _file_version(config_path)
    target_version = _file_version(target)
    catalog_path = output_path(vault, layout.catalog(config['dashboardProjectId'])) if config.get("dashboardProjectId") else None
    catalog_version = _file_version(catalog_path) if catalog_path else None
    logical_note = output_path(vault, layout.liveness(config['dashboardProjectId'])) if config.get("dashboardProjectId") else None
    logical_note_existed = logical_note.is_file() if logical_note else False
    mapping_path = output_path(vault, layout.local(host))
    mapping_version = _file_version(mapping_path) if config["source"]["rootAlias"] is not None else None
    previous = None
    if target.is_file():
        try:
            previous = read_json(target)
            validate_state(previous)
            if previous["projectId"] != config["projectId"] or previous["kind"] != config["kind"]:
                previous = None
        except (OSError, ValueError, KeyError, TypeError):
            previous = None
    state = blank_state(config, host, now)
    pending_history = None
    history_path = output_path(vault, layout.history(config['projectId']))
    source_manifest: dict[str, Any] = {}
    try:
        root = source_root(vault, config, host)
        if root is not None:
            result = adapter_for(config)(root, config["adapter"], now)
            source_manifest = result.get("_inputManifest", {})
            for field in ("runtime", "cases", "artifacts", "details"):
                state[field] = result[field]
            state["collection"].update(health="fresh", lastSuccessfulAt=iso(now), sourceModifiedAt=result["sourceModifiedAt"])
            state["provenance"]["sourceFiles"] = result["sourceFiles"]
            paper = next((detail for detail in state["details"] if detail.get("kind") == "paper"), None)
            if paper:
                prior_history = None
                if history_path.is_file():
                    try:
                        candidate = read_json(history_path)
                        if candidate.get("sourceId") == config["projectId"]:
                            prior_history = candidate
                    except (OSError, ValueError, TypeError):
                        paper["warnings"].append("Unreadable observation history was reset; earlier deltas are unavailable.")
                pending_history = observed_history(paper, prior_history, now)
                pending_history.update(sourceId=config["projectId"], projectId=config.get("dashboardProjectId"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Never convert an unreadable source into a failed simulation or fake zero.
        pending_history = None
        if previous:
            for field in ("runtime", "cases", "artifacts", "details", "provenance"):
                state[field] = copy.deepcopy(previous[field])
            for field in ("lastSuccessfulAt", "sourceModifiedAt"):
                state["collection"][field] = previous["collection"][field]
        if state["runtime"]["status"] == "unconfigured":
            state["runtime"]["status"] = "unknown"
        state["collection"].update(health="error", error=f"{type(error).__name__}: {error}")
    validate_state(state)
    # Direct calls with an unregistered config remain supported for validation.
    # Registered runs may never recreate a deleted source/project registration.
    if config_version is not None and _file_version(config_path) != config_version:
        raise CollectionSuperseded("Source registration changed or was removed during collection")
    if catalog_version is not None and _file_version(catalog_path) != catalog_version:
        raise CollectionSuperseded("Research project changed or was removed during collection")
    if logical_note_existed and not logical_note.is_file():
        raise CollectionSuperseded("Research project record was removed during collection")
    if mapping_version is not None and _file_version(mapping_path) != mapping_version:
        raise CollectionSuperseded("Local source mapping changed during collection")
    if _file_version(target) != target_version:
        raise CollectionSuperseded("A newer collector committed while this collection was running")
    for filename, expected in source_manifest.items():
        path = Path(filename)
        stamp = path.stat() if path.is_file() else None
        actual = [stamp.st_mtime_ns, stamp.st_size] if stamp else None
        if stamp is not None and expected is not None and len(expected) == 3:
            with path.open("rb") as stream:
                actual.append(hashlib.sha256(stream.read(expected[1] + 1)).hexdigest())
        if actual != expected:
            raise CollectionSuperseded("A monitored source changed during collection; retry after the save settles")
    atomic_json(target, state)
    if pending_history is not None:
        atomic_json(history_path, pending_history)
    atomic_text(output_path(vault, layout.generated(config['projectId'])), projection(state, now, layout.state(config['projectId'])))
    return state


def render_new_project(config: dict[str, Any], vault: Path | None = None) -> str:
    """Source registration is an internal record, separate from a research project."""
    layout = layout_for(vault)
    fields = {"type": "research-source", "source_id": config["projectId"], "title": config["title"],
              "source_kind": config["kind"], "collector_host": config["collector"]["hostId"]}
    if config.get("dashboardProjectId"):
        fields["dashboard_project_id"] = config["dashboardProjectId"]
    frontmatter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    return (f"---\n{frontmatter}\n---\n\n# {config['title'].replace(chr(10), ' ')}\n\n"
            "Internal source registration. Manage research progress from the Project Dashboard.\n\n"
            f"Configuration: [[{layout.source_config(config['projectId'])}]]\n\n"
            f"Latest observation: [[{layout.state(config['projectId'])}]]\n")


def register(vault: Path, config: dict[str, Any]) -> None:
    validate_config(config)
    config_path = output_path(vault, layout_for(vault).source_config(config['projectId']))
    note_path = output_path(vault, config["projectNote"])
    if config_path.exists() or note_path.exists():
        raise FileExistsError("Refusing to overwrite an existing project configuration or note")
    # Validate parent against current registry before creating either file.
    current = configurations(vault)
    if config.get("parentId") not in {None, *(c["projectId"] for c in current)}:
        raise ValueError("Parent project must be registered first")
    # Only two newly created files may be rolled back; no existing content is touched.
    atomic_json(config_path, config, exclusive=True)
    try:
        atomic_text(note_path, render_new_project(config, vault), exclusive=True)
    except Exception:
        config_path.unlink()
        raise
