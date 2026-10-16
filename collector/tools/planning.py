"""Dry-run plan previews and bounded folder manifests (no writes, nothing executed)."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from adapters import ADAPTERS
from adapters.common import UTC, iso
from adapters.declarative import extract_values
from adapters.scan import BINARY_SUFFIXES, Budget, ScanLimit, normalize_pattern
from dashboard_core import VERSION, validate_state
from onboarding import _validate_adapter, inspect_folder, source_directory
from profiles import detect, engine, find_profile, source_kind, unwrap, validate_profile

MAX_REQUEST_BYTES = 512 * 1024
PREVIEW_ARTIFACTS = 50
PREVIEW_CASES = 20
MANIFEST_IGNORED = {".git", "node_modules", "build", "tmp", "__pycache__", ".venv", "venv", ".svn", ".hg",
                    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".trash", ".obsidian"}
TEXT_SUFFIXES = {".txt", ".md", ".rst", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv",
                 ".log", ".sta", ".msg", ".dat", ".inp", ".tex", ".bib", ".py", ".m", ".sh", ".bat", ".ps1",
                 ".f90", ".f", ".c", ".cpp", ".h", ".r", ".jl", ".xml", ".out", ".err", ".in"}
PRIORITY = [re.compile(pattern, re.IGNORECASE) for pattern in (
    r"^readme", r"\.(json|ya?ml|toml|ini|cfg|conf)$", r"\.(inp|in|dat)$", r"\.(log|sta|msg|out|err)$",
    r"\.tex$", r"\.(csv|tsv)$", r"\.(py|m|sh|bat|ps1|jl|r)$", r"\.(txt|md|rst)$")]
SECRET = re.compile(r"(^\.env|secret|token|credential|password|passwd|\.pem$|\.key$|^id_rsa|\.pfx$|\.p12$)", re.IGNORECASE)


def read_request(stream: Any) -> dict[str, Any]:
    payload = stream.read(MAX_REQUEST_BYTES + 1)
    if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("Request JSON exceeds the 512 KiB input limit")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Request JSON must contain an object")
    return value


def resolve_profile(vault: Path, request: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """Return (profile, errors, warnings) from ``profile`` (inline) or ``profileId``."""
    if "profile" in request:
        profile = unwrap(request["profile"])
        errors, warnings = validate_profile(profile)
        return (None if errors else profile), errors, warnings
    if isinstance(request.get("profileId"), str):
        try:
            profile, _ = find_profile(vault, request["profileId"])
            return profile, [], []
        except ValueError as error:
            return None, [str(error)], []
    return None, ["Provide profileId or an inline profile"], []


def _paper_summary(details: list[dict[str, Any]]) -> dict[str, Any] | None:
    paper = next((item for item in details if item.get("kind") == "paper"), None)
    if not paper:
        return None
    chapters = paper.get("chapters", [])
    parents = {item.get("parentId") for item in chapters if item.get("parentId")}
    return {"count": len(chapters), "topLevel": sum(1 for item in chapters if item.get("parentId") is None),
            "leafCount": sum(1 for item in chapters if item["id"] not in parents),
            "totalWords": paper.get("bodyWords"), "mainFile": paper.get("mainFile"),
            "countingMethod": paper.get("countingMethod"),
            "titles": [item.get("title") for item in chapters if item.get("parentId") is None][:30]}


def preview(vault: Path, request: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    """Read a folder once with a profile and report what would be monitored."""
    now = now or dt.datetime.now(UTC)
    if not isinstance(request.get("source"), str):
        raise ValueError("Request needs source (an absolute folder path)")
    root = source_directory(request["source"])
    profile, errors, warnings = resolve_profile(vault, request)
    response: dict[str, Any] = {"ok": False, "source": str(root), "profileId": profile["id"] if profile else None,
                                "engine": engine(profile) if profile else None,
                                "sourceKind": source_kind(profile) if profile else None,
                                "projectType": profile["projectType"] if profile else None,
                                "adapter": None, "preview": None, "warnings": warnings, "errors": errors}
    if profile is None:
        return response
    started = time.monotonic()
    try:
        if engine(profile) == "adapter":
            kind = profile["extract"]["adapter"]
            adapter = request.get("adapter")
            if adapter is None:
                inspection = inspect_folder(root, vault)
                warnings.extend(inspection["warnings"])
                options = [item for item in inspection["profiles"] if item["kind"] == kind]
                wanted = request.get("inspectedProfileId")
                chosen = next((item for item in options if item["id"] == wanted), None) if wanted else (options[0] if options else None)
                if chosen is None:
                    errors.append(f"The existing {kind} inspector found no connectable entry" +
                                  (f" named {wanted}" if wanted else "") + " in this folder")
                    return response
                adapter = chosen["adapter"]
                response["inspectedProfileId"] = chosen["id"]
                if len(options) > 1:
                    warnings.append(f"{len(options)} {kind} entries were found; previewing {chosen['id']}. "
                                    "Pass inspectedProfileId to choose another.")
            _validate_adapter(root, kind, adapter)
            response["adapter"] = adapter
            result = ADAPTERS[kind](root, adapter, now)
            extra = []
        else:
            result = extract_values(root, profile["extract"], now)
            warnings.extend(result.pop("_warnings"))
            extra = result.pop("_metrics")
            if not result.pop("_matchedFiles"):
                errors.append("No file matched the profile's patterns; collection would report an error")
            response["adapter"] = {"engine": "declarative", "profileId": profile["id"],
                                   "profileVersion": profile.get("origin", {}).get("version", 1),
                                   "extract": profile["extract"]}
    except (OSError, ValueError, KeyError, TypeError, ScanLimit) as error:
        errors.append(f"{type(error).__name__}: {error}")
        return response
    state = {"schemaVersion": "1.0", "projectId": "preview", "kind": source_kind(profile),
             "runtime": result["runtime"], "cases": result["cases"],
             "collection": {"health": "fresh", "attemptedAt": iso(now), "lastSuccessfulAt": iso(now),
                            "sourceModifiedAt": result.get("sourceModifiedAt")}, "details": result["details"]}
    try:
        validate_state(state)
    except (ValueError, KeyError, TypeError) as error:
        errors.append(f"The extracted values do not fit the snapshot schema: {error}")
    details = result["details"]
    cases = [item for item in details if item.get("kind") == "case"]
    response["preview"] = {
        "runtime": result["runtime"], "cases": result["cases"],
        "artifacts": result["artifacts"][:PREVIEW_ARTIFACTS], "artifactCount": len(result["artifacts"]),
        "chapters": _paper_summary(details),
        "metrics": [{key: item.get(key) for key in ("id", "label", "value", "unit")} for item in extra],
        "caseRecords": cases[:PREVIEW_CASES], "caseRecordCount": len(cases),
        "build": next(({"buildOutdated": item.get("buildOutdated"), "message": item.get("message")}
                       for item in details if item.get("kind") == "build"), None),
        "sourceFileCount": len(result.get("sourceFiles", [])), "sourceFiles": result.get("sourceFiles", [])[:30],
        "sourceModifiedAt": result.get("sourceModifiedAt"),
        "elapsedSeconds": round(time.monotonic() - started, 3), "collectorVersion": VERSION}
    response["ok"] = not errors
    return response


# --------------------------------------------------------------------------- manifest

def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


MAX_EXCLUDES = 100


def _segment_regex(segment: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and "]" in segment[index + 2:] or char == "[" and segment[index + 1:index + 2] == "]":
            close = segment.index("]", index + 2)
            body = segment[index + 1:close]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            index = close
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a relative glob (``*``, ``?``, ``[...]``, whole-segment ``**``) to a regex."""
    segments = pattern.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")
        else:
            parts.append(_segment_regex(segment) + ("" if last else "/"))
    return re.compile("^" + "".join(parts) + "$", re.IGNORECASE)


def compile_excludes(patterns: Any) -> list[tuple[str, re.Pattern[str]]]:
    """Validate privacy exclude globs.

    A pattern without ``/`` matches any single path component (file or folder
    name) at any depth, like ``.gitignore``; a pattern with ``/`` matches the
    path relative to the source root. A matching folder excludes everything
    below it.
    """
    if patterns is None:
        return []
    if not isinstance(patterns, list) or len(patterns) > MAX_EXCLUDES:
        raise ValueError(f"exclude must be a list of up to {MAX_EXCLUDES} relative globs")
    compiled = []
    for index, pattern in enumerate(patterns):
        value = normalize_pattern(pattern, f"exclude[{index}]")
        if value.endswith("/**"):
            value = value[:-3]
        compiled.append((value, _glob_regex(value)))
    return compiled


def is_excluded(relative: str, excludes: list[tuple[str, re.Pattern[str]]]) -> bool:
    if not excludes:
        return False
    parts = relative.strip("/").split("/")
    prefixes = ["/".join(parts[:count]) for count in range(1, len(parts) + 1)]
    for value, regex in excludes:
        if "/" in value:
            if any(regex.match(prefix) for prefix in prefixes):
                return True
        elif any(regex.match(part) for part in parts):
            return True
    return False


def _priority(name: str) -> int:
    return next((index for index, pattern in enumerate(PRIORITY) if pattern.search(name)), len(PRIORITY))


def _head(path: Path, limit: int) -> tuple[str, bool] | None:
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if b"\x00" in payload[:limit]:
        return None
    text = payload[:limit].decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return text, len(payload) > limit


def manifest(vault: Path | None, source: str | Path, max_depth: int = 4, max_entries: int = 400,
             excerpts: int = 20, excerpt_bytes: int = 2048, scan_entries: int = 20000, seconds: float = 8.0,
             exclude: list[str] | None = None) -> dict[str, Any]:
    """Bounded folder overview for an agent: tree, extension histogram, heads, profile hits.

    ``exclude`` globs (privacy) remove matching files and folders from every
    part of the manifest; excluded folders are not descended into.
    """
    if not 1 <= max_depth <= 8 or not 20 <= max_entries <= 2000 or not 0 <= excerpts <= 50 or not 128 <= excerpt_bytes <= 8192:
        raise ValueError("Manifest limits: depth 1-8, entries 20-2000, excerpts 0-50, excerpt bytes 128-8192")
    excludes = compile_excludes(exclude)
    root = source_directory(source)
    started = time.monotonic()
    warnings: list[str] = []

    def warn(message: str) -> None:
        if message not in warnings and len(warnings) < 50:
            warnings.append(message)

    tree: list[dict[str, Any]] = []
    histogram: dict[str, dict[str, int]] = {}
    files: list[tuple[str, Path, int]] = []
    totals = {"files": 0, "directories": 0, "bytes": 0, "ignoredDirectories": 0, "excludedEntries": 0}
    pending: list[tuple[Path, int]] = [(root, 0)]
    visited = 0
    stopped = False
    while pending and not stopped:
        folder, depth = pending.pop(0)
        try:
            with os.scandir(folder) as iterator:
                entries = sorted(iterator, key=lambda item: (not item.is_dir(follow_symlinks=False), item.name.lower()))
        except OSError as error:
            warn(f"A folder could not be read: {folder.relative_to(root).as_posix() or '.'} ({type(error).__name__})")
            continue
        children: list[tuple[Path, int]] = []
        for entry in entries:
            visited += 1
            if visited > scan_entries or time.monotonic() - started > seconds:
                warn("The manifest scan limit was reached; counts cover only the scanned part of the folder.")
                stopped = True
                break
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if _linked(path):
                warn("Symbolic links and junctions were skipped.")
                continue
            if is_excluded(relative, excludes):
                totals["excludedEntries"] += 1
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name.lower() in MANIFEST_IGNORED:
                    totals["ignoredDirectories"] += 1
                    if len(tree) < max_entries:
                        tree.append({"path": relative + "/", "type": "dir", "ignored": True})
                    continue
                totals["directories"] += 1
                if depth + 1 < max_depth:
                    children.append((path, depth + 1))
                    if len(tree) < max_entries:
                        tree.append({"path": relative + "/", "type": "dir"})
                elif len(tree) < max_entries:
                    tree.append({"path": relative + "/", "type": "dir", "notExpanded": True})
            elif entry.is_file(follow_symlinks=False):
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                totals["files"] += 1
                totals["bytes"] += stat.st_size
                suffix = path.suffix.lower() or "(none)"
                bucket = histogram.setdefault(suffix, {"count": 0, "bytes": 0})
                bucket["count"] += 1
                bucket["bytes"] += stat.st_size
                files.append((relative, path, stat.st_size))
                if len(tree) < max_entries:
                    tree.append({"path": relative, "type": "file", "size": stat.st_size,
                                 "modifiedAt": iso(dt.datetime.fromtimestamp(stat.st_mtime, UTC))})
        pending.extend(children)
    if len(tree) >= max_entries:
        warn(f"The tree lists the first {max_entries} entries (breadth-first); totals and the histogram cover all scanned entries.")
    if pending and not stopped:
        warn(f"Folders deeper than {max_depth} levels were not expanded.")
    heads: list[dict[str, Any]] = []
    candidates = sorted((item for item in files
                         if item[2] <= 4 * 1024 * 1024 and Path(item[0]).suffix.lower() not in BINARY_SUFFIXES
                         and (Path(item[0]).suffix.lower() in TEXT_SUFFIXES or not Path(item[0]).suffix)
                         and not SECRET.search(Path(item[0]).name)),
                        key=lambda item: (_priority(Path(item[0]).name), item[0].count("/"), item[0].lower()))
    skipped_secret = sum(1 for item in files if SECRET.search(Path(item[0]).name))
    if skipped_secret:
        warn(f"{skipped_secret} file(s) with credential-like names were never opened.")
    seen_suffix: dict[str, int] = {}
    for relative, path, size in candidates:
        if len(heads) >= excerpts:
            break
        suffix = Path(relative).suffix.lower()
        # Prefer variety: at most three excerpts per extension.
        if seen_suffix.get(suffix, 0) >= 3:
            continue
        try:
            head = _head(path, excerpt_bytes)
        except OSError:
            continue
        if head is None:
            continue
        seen_suffix[suffix] = seen_suffix.get(suffix, 0) + 1
        heads.append({"path": relative, "size": size, "head": head[0], "truncated": head[1]})
    if totals["excludedEntries"]:
        warn(f"{totals['excludedEntries']} file(s)/folder(s) matched the privacy exclude list and were left out.")
    detection = detect(root, vault, legacy=None, include_partial=True,
                       budget=Budget(max_bytes=4 * 1024 * 1024, seconds=max(1.0, seconds - (time.monotonic() - started))))
    profile_hits = [item for item in detection["candidates"]
                    # Keep profiles with at least one positive (allOf/anyOf) hit.
                    if any(not text.startswith("noneOf:") for text in item["matched"])]
    histogram_list = sorted(({"extension": key, **value} for key, value in histogram.items()),
                            key=lambda item: (-item["count"], item["extension"]))[:60]
    return {"schemaVersion": "1.0", "sourcePath": str(root), "generatedAt": iso(dt.datetime.now(UTC)),
            "limits": {"maxDepth": max_depth, "maxEntries": max_entries, "excerpts": excerpts,
                       "excerptBytes": excerpt_bytes, "scanEntries": scan_entries, "seconds": seconds,
                       "ignored": sorted(MANIFEST_IGNORED), "exclude": [value for value, _ in excludes]},
            "totals": totals, "extensions": histogram_list, "tree": tree, "excerpts": heads,
            "profiles": [{key: item[key] for key in ("profileId", "title", "origin", "engine", "confidence", "threshold",
                                                    "meetsThreshold", "matched", "missed", "excluded")}
                         for item in profile_hits],
            "libraryErrors": detection["libraryErrors"],
            "warnings": warnings + [item for item in detection["warnings"] if item not in warnings],
            "elapsedSeconds": round(time.monotonic() - started, 3)}
