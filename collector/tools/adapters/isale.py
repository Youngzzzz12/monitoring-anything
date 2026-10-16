"""User-configured iSALE text regex adapter, not a universal log parser."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .common import UTC, add_sources, bounded_text, classify, eligible, empty_result, last_match, matching_files, progress, latest_run_text, relative_file


def metadata_value(value: dict[str, Any], field: str | None) -> Any:
    for name in (field or "").split("."):
        if not name or not isinstance(value, dict):
            return None
        value = value.get(name)
    return value


def collect_metadata(root: Path, options: dict[str, Any]) -> dict[str, Any]:
    """Map this workflow's explicit metadata; jdata existence is not evidence."""
    result = empty_result()
    planned = options.get("cases", [])
    if planned:
        specifications = [(item["id"], relative_file(root, item["metadata"])) for item in planned]
        result["cases"] = {"planned": len(planned), "completed": 0, "running": 0, "failed": 0, "unknown": 0}
    else:
        specifications = [(str(p.relative_to(root.resolve())), p) for p in matching_files(root, options.get("metadataPatterns", []))]
        result["cases"] = {"planned": None, "completed": 0, "running": 0, "failed": 0, "unknown": 0}
    if not specifications:
        raise FileNotFoundError("No configured iSALE metadata records were found")
    sources, statuses = [], []
    for identity, path in specifications:
        status, reason = "unknown", None
        if path.is_file() and path in eligible([path], options):
            record = json.loads(bounded_text(path, tail=False))
            if not isinstance(record, dict):
                raise ValueError("iSALE metadata must contain a JSON object")
            raw = metadata_value(record, options.get("statusField", "status"))
            status = options.get("statusMap", {}).get(str(raw), "unknown")
            if status not in {"queued", "running", "completed", "failed", "unknown"}:
                raise ValueError("metadata statusMap has an invalid runtime status")
            if status == "completed":
                completed = metadata_value(record, options.get("completionField"))
                if not isinstance(completed, str) or not completed.strip():
                    status, reason = "unknown", "Completed status lacks configured completion timestamp"
                else:
                    try:
                        dt.datetime.fromisoformat(completed.replace("Z", "+00:00"))
                    except ValueError:
                        status, reason = "unknown", "Invalid completion timestamp"
            run_field = options.get("runIdField")
            if run_field and options.get("runId") and metadata_value(record, run_field) != options["runId"]:
                status, reason = "unknown", "Metadata belongs to another run"
            sources.append(path)
        else:
            reason = "No metadata for the selected run"
        statuses.append(status)
        result["cases"][status if status in {"completed", "running", "failed"} else "unknown"] += 1
        result["details"].append({"kind": "case", "id": identity, "status": status, "source": str(path), "reason": reason})
    result["runtime"]["status"] = ("failed" if "failed" in statuses else "running" if "running" in statuses else
                                   "completed" if planned and all(s == "completed" for s in statuses) else "unknown")
    result["runtime"]["runId"] = options.get("runId")
    add_sources(result, sources)
    result["details"].append({"kind": "boundary", "message": "Metadata mapping is specific to the configured batch workflow. Discovery counts observed records; planned is null without an explicit case list. Prepared does not prove execution."})
    return result


def collect(root: Path, options: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    if options.get("mode") == "metadata-json":
        return collect_metadata(root, options)
    result = empty_result()
    files = eligible(matching_files(root, options.get("logPatterns", [])), options)
    if not files:
        raise FileNotFoundError("No configured iSALE text log was found")
    path = files[-1]
    text = latest_run_text(bounded_text(path), options.get("runStartPattern"))
    active = (now - dt.datetime.fromtimestamp(path.stat().st_mtime, UTC)).total_seconds() <= options.get("activeWindowSeconds", 120)
    result["runtime"]["status"] = classify(text, options.get("successPattern"), options.get("failurePattern"),
                                          options.get("runningPattern"), active)
    result["runtime"]["runId"] = options.get("runId") or f"{path.stem}@{path.stat().st_mtime_ns}"
    match = last_match(text, options.get("currentValuePattern"))
    value = float(match.group("value")) if match else None
    progress(result, value, options.get("targetValue"), options.get("unit"))
    add_sources(result, [path])
    result["details"].append({"kind": "boundary", "message": "Patterns are project-specific and must be verified against actual iSALE version logs. Progress is simulated time, not remaining wall-clock time."})
    return result
