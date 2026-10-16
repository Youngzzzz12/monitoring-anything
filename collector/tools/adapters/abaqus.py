"""Read configured Abaqus job logs; never load ODB or submit a job."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .common import UTC, add_sources, bounded_text, classify, eligible, empty_result, last_match, matching_files, progress, latest_run_text

SUCCESS = r"THE ANALYSIS HAS COMPLETED SUCCESSFULLY|Abaqus JOB .+ COMPLETED|Abaqus/Analysis completed"
FAILURE = r"THE ANALYSIS HAS NOT BEEN COMPLETED|Abaqus/Analysis exited with errors|Abaqus JOB .+ ABORTED|THE ANALYSIS HAS BEEN TERMINATED"
RUNNING = r"^\s*\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+[-+.\dEe]+"


def collect(root: Path, options: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    result = empty_result()
    jobs = options.get("jobs", [])
    if jobs:
        result["cases"] = {"planned": len(jobs), "completed": 0, "running": 0, "failed": 0, "unknown": 0}
    job_specs = jobs or [{"id": options.get("runId", "latest-record"),
                         "logPatterns": options.get("logPatterns", ["*.sta", "*.msg", "*.log"])}]
    sources = []
    outcomes = []
    for job in job_specs:
        files = eligible(matching_files(root, job.get("logPatterns", [])), {**options, **job})
        sources.extend(files)
        if not files:
            status = "unknown"
            result["details"].append({"kind": "case", "id": job["id"], "status": status, "reason": "No log for the selected run"})
        else:
            # Older terminal evidence cannot override a newer selected record.
            signals = []
            for path in files:
                text = latest_run_text(bounded_text(path), options.get("runStartPattern"))
                active = (now - dt.datetime.fromtimestamp(path.stat().st_mtime, UTC)).total_seconds() <= options.get("activeWindowSeconds", 120)
                status = classify(text, options.get("successPattern", SUCCESS), options.get("failurePattern", FAILURE),
                                  options.get("runningPattern", RUNNING), active)
                signals.append((path.stat().st_mtime, status, text, path))
            selected = max(signals, key=lambda item: item[0])
            status, text, path = selected[1:]
            result["details"].append({"kind": "case", "id": job["id"], "status": status, "source": str(path)})
            if len(job_specs) == 1:
                result["runtime"]["runId"] = options.get("runId") or f"{path.stem}@{path.stat().st_mtime_ns}"
                match = last_match(text, options.get("currentValuePattern"))
                current = float(match.group("value")) if match else None
                progress(result, current, options.get("targetValue"), options.get("unit"))
                step = last_match(text, options.get("stepPattern"))
                result["runtime"]["step"] = step.group("step") if step else None
        outcomes.append(status)
        if jobs:
            result["cases"][status if status in {"completed", "running", "failed"} else "unknown"] += 1
    if not sources and not jobs:
        raise FileNotFoundError("No Abaqus status log was found")
    result["runtime"]["status"] = ("failed" if "failed" in outcomes else "running" if "running" in outcomes else
                                   "completed" if outcomes and all(s == "completed" for s in outcomes) else "unknown")
    add_sources(result, sources)
    result["details"].append({"kind": "boundary", "message": "Running means recent matching log activity, not an independently verified live process. Configure jobs and notBefore for a new run; no ODB was read."})
    return result
