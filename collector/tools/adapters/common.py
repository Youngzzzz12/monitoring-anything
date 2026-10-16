"""Bounded, read-only source handling shared by adapters."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc
MAX_LOG_BYTES = 2 * 1024 * 1024


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must contain a timezone")
    return result.astimezone(UTC)


def empty_result() -> dict[str, Any]:
    return {
        "runtime": {"status": "unknown", "progress": None, "step": None,
                    "currentValue": None, "targetValue": None, "unit": None, "runId": None},
        "cases": {"planned": None, "completed": None, "running": None, "failed": None, "unknown": None},
        "artifacts": [], "details": [], "sourceFiles": [], "sourceModifiedAt": None,
    }


def bounded_text(path: Path, tail: bool = True) -> str:
    """Read no more than two MiB; binary result databases are never inputs."""
    if path.suffix.lower() in {".odb", ".jdata", ".bin"}:
        raise ValueError(f"Binary result database is not a status log: {path.name}")
    with path.open("rb") as stream:
        size = path.stat().st_size
        if tail and size > MAX_LOG_BYTES:
            stream.seek(size - MAX_LOG_BYTES)
        return stream.read(MAX_LOG_BYTES).decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def relative_file(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Source path escapes its configured root: {value}")
    return path


def matching_files(root: Path, patterns: list[str]) -> list[Path]:
    found: set[Path] = set()
    for pattern in patterns:
        if not isinstance(pattern, str) or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError(f"Invalid relative source pattern: {pattern!r}")
        for candidate in root.glob(pattern):
            path = candidate.resolve()
            if path.is_file() and path.is_relative_to(root.resolve()):
                found.add(path)
    return sorted(found, key=lambda p: (p.stat().st_mtime, str(p)))


def add_sources(result: dict[str, Any], files: list[Path]) -> None:
    result["sourceFiles"] = [str(p) for p in files]
    if files:
        result["sourceModifiedAt"] = iso(dt.datetime.fromtimestamp(max(p.stat().st_mtime for p in files), UTC))


def last_match(text: str, pattern: str | None) -> re.Match[str] | None:
    if not pattern:
        return None
    matches = list(re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE))
    return matches[-1] if matches else None


def latest_run_text(text: str, pattern: str | None) -> str:
    """Discard earlier run evidence in a reused append-only log when configured."""
    match = last_match(text, pattern)
    return text[match.start():] if match else text


def classify(text: str, success: str | None, failure: str | None,
             running: str | None = None, active: bool = False) -> str:
    """Last explicit terminal evidence wins; new files alone prove nothing."""
    success_match = last_match(text, success)
    failure_match = last_match(text, failure)
    if success_match or failure_match:
        if failure_match and (not success_match or failure_match.start() > success_match.start()):
            return "failed"
        return "completed"
    return "running" if active and last_match(text, running) else "unknown"


def progress(result: dict[str, Any], current: float | None, target: float | None, unit: str | None) -> None:
    result["runtime"].update(currentValue=current, targetValue=target, unit=unit)
    if current is not None and target is not None and target > 0 and current >= 0:
        result["runtime"]["progress"] = min(current / target, 1.0)


def eligible(files: list[Path], options: dict[str, Any]) -> list[Path]:
    not_before = parse_time(options.get("notBefore"))
    return [p for p in files if not not_before or dt.datetime.fromtimestamp(p.stat().st_mtime, UTC) >= not_before]
