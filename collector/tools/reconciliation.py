"""Recoverably archive known internal files for projects whose record was deleted."""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from pathlib import Path
from typing import Any

from adapters.common import UTC, iso
from dashboard_core import atomic_json, output_path, read_json, validate_config, validate_state
from layout import layout_for
from project_catalog import CARDS, card_contents, new_catalog, validate_catalog


def _safe_internal(vault: Path, relative: str) -> Path:
    """Keep moves inside exact vault paths without following links or junctions."""
    target = output_path(vault, relative)
    lexical = vault.resolve() / relative
    for component in (lexical, *lexical.parents):
        if component == vault.resolve():
            break
        if component.is_symlink() or bool(getattr(component, "is_junction", lambda: False)()):
            raise ValueError(f"Linked internal path is not reconciled: {relative}")
    return target


def _field(content: str, key: str) -> Any:
    """Read the simple generated scalar frontmatter without a YAML dependency."""
    match = re.match(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", content, re.DOTALL)
    if not match:
        return None
    scalar = re.search(r"^" + re.escape(key) + r":\s*(.*?)\s*$", match.group(1), re.MULTILINE)
    if not scalar:
        return None
    value = scalar.group(1)
    try:
        return json.loads(value)
    except ValueError:
        return value.strip("'\"")


def _plan(vault: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    layout = layout_for(vault)
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    _safe_internal(vault, layout.catalog_dir())
    for stem, path in layout.catalog_files(vault):
        try:
            _safe_internal(vault, path.relative_to(vault).as_posix())
            record = read_json(path)
            validate_catalog(record, layout)
            if stem != record["projectId"]:
                raise ValueError("Catalog filename does not match project ID")
            _safe_internal(vault, layout.liveness(record["projectId"]))
            records.append(record)
        except (OSError, ValueError, TypeError, KeyError) as error:
            warnings.append(f"Preserved unverified catalog {path.name}: {error}")
    # Explicit source ownership remains evidence if the catalog itself was
    # deleted. Unknown/source-only configs are deliberately left independent.
    known = {record["projectId"] for record in records}
    missing_catalogs: dict[str, dict[str, Any]] = {}
    configs_folder = _safe_internal(vault, layout.source_configs())
    for path in sorted(configs_folder.glob("*.json")) if configs_folder.is_dir() else []:
        try:
            _safe_internal(vault, path.relative_to(vault).as_posix())
            config = read_json(path)
            validate_config(config)
            if path.stem != config["projectId"]:
                raise ValueError("Source configuration filename does not match ID")
            owner = config.get("dashboardProjectId")
            if not owner or owner in known:
                continue
            if _safe_internal(vault, layout.liveness(owner)).is_file():
                warnings.append(f"Preserved source {config['projectId']}: owner {owner} has a record but its catalog is missing; restore the catalog to resume monitoring.")
                continue
            if owner not in missing_catalogs:
                missing_catalogs[owner] = new_catalog(owner, config["title"], config["projectId"], layout)
            else:
                missing_catalogs[owner]["sourceIds"].append(config["projectId"])
        except (OSError, ValueError, TypeError, KeyError) as error:
            warnings.append(f"Preserved unverified source configuration {path.name}: {error}")
    records.extend(missing_catalogs.values())
    orphan = [record for record in records if not output_path(vault, layout.liveness(record["projectId"])).is_file()]
    live_sources = {source for record in records if output_path(vault, layout.liveness(record["projectId"])).is_file()
                    for source in record["sourceIds"]}
    claimed = {source: sum(source in record["sourceIds"] for record in records)
               for record in records for source in record["sourceIds"]}
    files: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    owned_sources: set[str] = set()

    def include(relative: str, project_id: str, source_id: str | None = None,
                verify: Any = None) -> None:
        try:
            path = _safe_internal(vault, relative)
            if not path.exists():
                return
            if not path.is_file():
                raise ValueError("Expected a regular internal file")
            payload = path.read_bytes()
            if verify is not None:
                verify(payload)
            files.append({"path": relative, "projectId": project_id, "sourceId": source_id})
            payloads[relative] = payload
        except (OSError, ValueError, TypeError, KeyError) as error:
            warnings.append(f"Preserved unverified internal file {relative}: {error}")

    for record in orphan:
        project_id = record["projectId"]
        def verify_catalog(payload: bytes, identity: str = project_id) -> None:
            value = json.loads(payload.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("Catalog is not an object")
            validate_catalog(value, layout)
            if value["projectId"] != identity:
                raise ValueError("Catalog identity does not match filename")

        include(layout.catalog(project_id), project_id, verify=verify_catalog)
        for card in (CARDS if layout.cards_dir(project_id) else ()):
            def verify_progress(payload: bytes, identity: str = project_id) -> None:
                content = payload.decode("utf-8-sig").replace("\r\n", "\n")
                if content.strip() != card_contents(identity, "progress").strip():
                    raise ValueError("Progress card no longer matches its generated project binding")

            include(f"{layout.cards_dir(project_id)}/{card}.md", project_id,
                    verify=verify_progress if card == "progress" else None)
        for source_id in record["sourceIds"]:
            if source_id in live_sources or claimed[source_id] > 1:
                warnings.append(f"Preserved shared or ambiguous source {source_id}; another catalog also refers to it.")
                continue
            config_relative = layout.source_config(source_id)
            try:
                config_path = _safe_internal(vault, config_relative)
                if not config_path.is_file():
                    warnings.append(f"Preserved missing or unverified source {source_id}: source configuration is unavailable.")
                    continue
                config = read_json(config_path)
                validate_config(config)
                expected_record = layout.source_note(source_id)
                if config["projectId"] != source_id or config["projectNote"] != expected_record:
                    raise ValueError("Source identity or source record path does not match catalog ownership")
                if config.get("dashboardProjectId", project_id) != project_id:
                    raise ValueError("Source belongs to another logical project")
                owned_sources.add(source_id)
                include(config_relative, project_id, source_id)

                def verify_state(payload: bytes, identity: str = source_id, kind: str = config["kind"]) -> None:
                    state = json.loads(payload.decode("utf-8-sig"))
                    if not isinstance(state, dict):
                        raise ValueError("State is not an object")
                    validate_state(state)
                    if state["projectId"] != identity or state["kind"] != kind:
                        raise ValueError("State identity or kind does not match source ownership")

                def verify_record(payload: bytes, identity: str = source_id) -> None:
                    content = payload.decode("utf-8-sig")
                    if _field(content, "type") != "research-source" or _field(content, "source_id") != identity:
                        raise ValueError("Source record does not contain its generated ownership fields")

                def verify_projection(payload: bytes, identity: str = source_id) -> None:
                    content = payload.decode("utf-8-sig")
                    if _field(content, "type") != "dashboard-status" or _field(content, "project_id") != identity:
                        raise ValueError("Generated summary identity does not match its source")

                def verify_history(payload: bytes, identity: str = source_id, owner: str = project_id) -> None:
                    history = json.loads(payload.decode("utf-8-sig"))
                    if (not isinstance(history, dict) or history.get("schemaVersion") != 1
                            or history.get("sourceId") != identity or history.get("projectId") not in {owner, None}
                            or not isinstance(history.get("snapshots"), list)):
                        raise ValueError("Paper history identity does not match its source ownership")

                include(layout.state(source_id), project_id, source_id, verify_state)
                include(layout.generated(source_id), project_id, source_id, verify_projection)
                include(layout.history(source_id), project_id, source_id, verify_history)
                include(expected_record, project_id, source_id, verify_record)
            except (OSError, ValueError, TypeError, KeyError) as error:
                warnings.append(f"Preserved unverified source {source_id}: {error}")
    result = {"applied": False, "orphanProjectIds": sorted(record["projectId"] for record in orphan),
              "orphanSourceIds": sorted(owned_sources), "archivePath": None,
              "files": files, "warnings": warnings, "aliasesPreserved": True}
    return result, payloads


def reconcile(vault: Path, apply: bool = False) -> dict[str, Any]:
    """Preview by default; explicit apply moves internal files to recoverable trash."""
    vault = vault.resolve()
    layout = layout_for(vault)
    result, payloads = _plan(vault)
    if not apply:
        return result
    result["applied"] = True
    if not result["files"]:
        return result
    changed_projects = set()
    for item in result["files"]:
        identity = item["projectId"]
        record = _safe_internal(vault, layout.liveness(identity))
        current = _safe_internal(vault, item["path"])
        if record.is_file() or not current.is_file() or current.read_bytes() != payloads[item["path"]]:
            changed_projects.add(identity)
    if changed_projects:
        result["warnings"].append("Preserved projects that changed during reconciliation: " + ", ".join(sorted(changed_projects)))
        result["files"] = [item for item in result["files"] if item["projectId"] not in changed_projects]
    if not result["files"]:
        return result
    stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    archive_relative = f"{layout.trash()}/{stamp}"
    archive = _safe_internal(vault, archive_relative)
    archive.mkdir(parents=True, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    try:
        for item in result["files"]:
            original = _safe_internal(vault, item["path"])
            destination = _safe_internal(vault, archive_relative + "/" + item["path"])
            if not destination.is_relative_to(archive):
                raise ValueError("Archive destination escapes reconciliation directory")
            destination.parent.mkdir(parents=True, exist_ok=True)
            original.rename(destination)
            moved.append((original, destination))
        atomic_json(archive / "_reconciliation.json", {
            "schemaVersion": "1.0", "createdAt": iso(dt.datetime.now(UTC)), "originalVault": str(vault),
            "files": result["files"], "orphanProjectIds": result["orphanProjectIds"],
            "orphanSourceIds": result["orphanSourceIds"], "aliasesPreserved": True,
            "note": "Only verified internal Dashboard files were moved. Restore files to their original vault-relative paths to recover them."})
    except Exception:
        for original, destination in reversed(moved):
            if destination.is_file() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                destination.rename(original)
        raise
    result["archivePath"] = archive_relative
    return result
