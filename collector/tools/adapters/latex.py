"""Inspect configured chapters and existing LaTeX logs without compiling."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from .common import add_sources, bounded_text, classify, eligible, empty_result, matching_files, relative_file, latest_run_text, iso, UTC
from .latex_paper import analyze

SUCCESS = r"^Output written on .+\.(?:pdf|dvi)\s*\("
FAILURE = r"^!\s|Fatal error occurred|Emergency stop|No pages of output"


def collect(root: Path, options: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    result = empty_result()
    chapter_names = options.get("chapters", [])
    chapters = []
    for name in chapter_names:
        path = relative_file(root, name)
        if path.is_file():
            chapters.append(path)
        result["details"].append({"kind": "chapter", "name": name, "exists": path.is_file(),
                                  "modifiedAt": iso(dt.datetime.fromtimestamp(path.stat().st_mtime, UTC)) if path.is_file() else None})
    paper, dependencies, manifest = analyze(root, options, now)
    if paper:
        result["details"].append(paper)
    inputs = list(dict.fromkeys(chapters + dependencies))
    logs = eligible(matching_files(root, options.get("logPatterns", ["main.log"])), options)
    for path in logs:
        stamp = path.stat()
        manifest.setdefault(str(path), [stamp.st_mtime_ns, stamp.st_size])
    if logs:
        latest = logs[-1]
        text = latest_run_text(bounded_text(latest), options.get("runStartPattern", r"^This is (?:pdfTeX|XeTeX|LuaHBTeX|LuaTeX)"))
        result["runtime"]["status"] = classify(text, SUCCESS, FAILURE)
        result["runtime"]["runId"] = options.get("runId") or f"{latest.stem}@{latest.stat().st_mtime_ns}"
        if result["runtime"]["status"] == "completed":
            newest_input = max((p.stat().st_mtime for p in inputs), default=None)
            build_outdated = newest_input is not None and newest_input > latest.stat().st_mtime + 2
            result["details"].append({"kind": "build", "buildOutdated": build_outdated,
                                      "message": "A monitored TeX, bibliography or graphics dependency changed after the successful build" if build_outdated else "No newer monitored literal dependency was observed; macro-generated dependencies are not checked"})
            output = list(re.finditer(r"^Output written on (.+\.(?:pdf|dvi))\s*\(", text, re.MULTILINE))
            candidate = relative_file(latest.parent, output[-1].group(1).strip().strip('"')) if output else None
            if candidate and candidate.is_file() and candidate.is_relative_to(root.resolve()):
                # Success evidence belongs to this log; an older PDF is not automatically verified.
                if candidate.stat().st_mtime + 2 >= latest.stat().st_mtime:
                    result["artifacts"].append({"type": candidate.suffix[1:], "path": str(candidate),
                                                "verifiedAt": None if build_outdated else iso(now), "runId": result["runtime"]["runId"]})
    add_sources(result, list(dict.fromkeys(inputs + logs)))
    result["_inputManifest"] = manifest
    if not inputs and not logs:
        raise FileNotFoundError("No configured chapter or existing compilation log was found")
    result["details"].append({"kind": "boundary", "message": "Compilation status and body word counts do not establish paper completion. No compiler was run. Literal includes are resolved dynamically; macro-generated structure and dependencies need review."})
    return result
