"""Detect saved local LaTeX changes using portable, bounded stat scans."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WATCH_SUFFIXES = {".tex", ".bib", ".sty", ".cls", ".bst", ".cfg", ".def", ".bbl",
                  ".png", ".jpg", ".jpeg", ".svg", ".eps", ".pdf", ".dvi", ".log"}
SKIP_DIRECTORIES = {".git", ".svn", "node_modules", "__pycache__", ".venv", "venv"}
MAX_SCAN_FILES = 25000


def configuration_fingerprint(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def latex_fingerprint(root: Path) -> str:
    """Observe creation, atomic saves, renames and deletion without reading bodies.

    A one-second scanner avoids a dependency on platform-specific file watchers.
    It never follows a directory link or opens simulation outputs.
    """
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise FileNotFoundError("LaTeX source folder is unavailable")
    entries: list[tuple[str, int, int]] = []
    visited = 0
    seen_directories: set[Path] = set()

    def failed(error: OSError) -> None:
        raise error

    for directory, folders, files in os.walk(resolved_root, followlinks=False, onerror=failed):
        parent = Path(directory)
        real_parent = parent.resolve()
        if not real_parent.is_relative_to(resolved_root) or real_parent in seen_directories:
            folders[:] = []
            continue
        seen_directories.add(real_parent)
        folders[:] = sorted(name for name in folders
                            if name.lower() not in SKIP_DIRECTORIES and not name.startswith(".")
                            and not (parent / name).is_symlink()
                            and not bool(getattr(parent / name, "is_junction", lambda: False)())
                            and (parent / name).resolve().is_relative_to(resolved_root)
                            and (parent / name).resolve() not in seen_directories)
        for name in files:
            visited += 1
            if visited > MAX_SCAN_FILES:
                raise ValueError("Live scan limit reached; select a narrower LaTeX source folder")
            path = parent / name
            if (path.suffix.lower() not in WATCH_SUFFIXES or path.is_symlink()
                    or not path.resolve().is_relative_to(resolved_root)):
                continue
            try:
                metadata = path.stat()
            except FileNotFoundError:
                # A save may replace a file during enumeration. The next scan
                # will see its final name; this scan cannot establish stability.
                entries.append((path.relative_to(resolved_root).as_posix(), -1, -1))
                continue
            entries.append((path.relative_to(resolved_root).as_posix(), metadata.st_size, metadata.st_mtime_ns))
    entries.sort()
    return hashlib.sha256(json.dumps(entries, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass
class SavedChangeSchedule:
    """Serialize collections and debounce changes using a monotonic clock."""

    signature: str | None = None
    pending_since: float | None = None
    next_periodic: float = 0.0
    debounce_seconds: float = 1.0

    def observe(self, signature: str, now: float) -> None:
        if self.signature is None:
            self.signature = signature
            return
        if signature != self.signature:
            self.signature = signature
            self.pending_since = now

    def ready(self, now: float) -> bool:
        if self.pending_since is not None:
            return now - self.pending_since >= self.debounce_seconds
        return now >= self.next_periodic

    def collected(self, observed_signature: str, now: float, interval: int) -> None:
        self.next_periodic = now + interval
        if self.signature == observed_signature:
            self.pending_since = None
