"""Bounded, read-only glob matching and text reading for declarative profiles.

Every scan shares one budget (directory entries, files matched, bytes read and a
wall-clock deadline). Links and junctions are never followed, nothing outside
the selected root is opened, and nothing is executed.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

IGNORED_DIRECTORIES = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
                       ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".trash", ".obsidian"}
MAX_PATTERN_LENGTH = 256
MAX_REGEX_LENGTH = 500
BINARY_SUFFIXES = {".odb", ".jdata", ".bin", ".mat", ".h5", ".hdf5", ".npy", ".npz", ".pkl", ".zip", ".gz",
                   ".7z", ".tar", ".exe", ".dll", ".so", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".dvi",
                   ".xlsx", ".docx", ".pptx", ".sim", ".res", ".prt", ".mdl", ".stt", ".cae", ".jnl"}
# A cheap guard against the most common catastrophic-backtracking shape, such
# as (a+)+ or (\w*)*. Python's re cannot be interrupted once matching starts.
NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}]\)\s*[+*{]")


class ScanLimit(RuntimeError):
    """A bounded scan stopped at its configured limit."""


@dataclass
class Budget:
    """Shared caps for one detection, preview or collection pass."""

    max_entries: int = 20000
    max_matches: int = 5000
    max_bytes: int = 16 * 1024 * 1024
    max_depth: int = 8
    seconds: float = 10.0
    entries: int = 0
    bytes_read: int = 0
    started: float = field(default_factory=time.monotonic)
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < 100:
            self.warnings.append(message)

    def check_time(self) -> None:
        if time.monotonic() - self.started > self.seconds:
            raise ScanLimit(f"Scan time limit of {self.seconds:g} s was reached; select a narrower folder or pattern")


def normalize_pattern(pattern: object, label: str = "glob") -> str:
    """Return a safe POSIX relative glob or raise ValueError."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(f"{label} must be a nonempty relative glob string")
    value = pattern.strip().replace("\\", "/")
    if len(value) > MAX_PATTERN_LENGTH:
        raise ValueError(f"{label} is longer than {MAX_PATTERN_LENGTH} characters")
    if value.startswith("/") or PureWindowsPath(pattern).is_absolute() or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{label} must be relative to the source folder")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"{label} cannot contain parent traversal")
    return "/".join(parts)


def validate_regex(pattern: object, label: str = "regex") -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{label} must be a nonempty regular expression string")
    if len(pattern) > MAX_REGEX_LENGTH:
        raise ValueError(f"{label} is longer than {MAX_REGEX_LENGTH} characters")
    if NESTED_QUANTIFIER.search(pattern):
        raise ValueError(f"{label} contains a nested quantifier such as (a+)+; simplify it to keep matching bounded")
    try:
        return re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as error:
        raise ValueError(f"{label} is not a valid regular expression: {error}") from None


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


class Scanner:
    """Cached, bounded directory listing and glob expansion below one root."""

    def __init__(self, root: Path, budget: Budget | None = None) -> None:
        self.root = root.resolve()
        self.budget = budget or Budget()
        self._listing: dict[str, tuple[list[str], list[str]]] = {}
        self._stats: dict[str, os.stat_result] = {}
        self._globs: dict[str, list[str]] = {}

    def listing(self, relative: str) -> tuple[list[str], list[str]]:
        """Return (directories, files) names of one relative folder, sorted."""
        if relative in self._listing:
            return self._listing[relative]
        self.budget.check_time()
        folder = self.root / relative if relative else self.root
        directories: list[str] = []
        files: list[str] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    self.budget.entries += 1
                    if self.budget.entries > self.budget.max_entries:
                        self.budget.warn(f"The directory entry limit ({self.budget.max_entries}) was reached; results may be incomplete.")
                        break
                    path = Path(entry.path)
                    if _linked(path):
                        self.budget.warn("Symbolic links and junctions were skipped.")
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(entry.name)
                        try:
                            self._stats[f"{relative}/{entry.name}" if relative else entry.name] = entry.stat(follow_symlinks=False)
                        except OSError:
                            pass
        except OSError as error:
            if relative:
                self.budget.warn(f"A folder could not be read: {relative} ({type(error).__name__}).")
            else:
                raise
        result = (sorted(directories, key=str.lower), sorted(files, key=str.lower))
        self._listing[relative] = result
        return result

    def stat(self, relative: str) -> os.stat_result | None:
        if relative not in self._stats:
            try:
                path = self.path(relative)
                self._stats[relative] = path.stat()
            except (OSError, ValueError):
                return None
        return self._stats[relative]

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"Path escapes the source folder: {relative}")
        return path

    def glob(self, pattern: str) -> list[str]:
        """Relative POSIX file paths matching a glob, case-insensitively.

        ``*`` and ``?`` match within one path segment, ``[...]`` is a class and a
        whole ``**`` segment matches zero or more folders. Wildcards never enter
        ignored tool folders (such as .git or node_modules); a literal segment may.
        """
        pattern = normalize_pattern(pattern)
        if pattern in self._globs:
            return self._globs[pattern]
        segments = pattern.split("/")
        found: list[str] = []
        seen: set[str] = set()

        def add(relative: str) -> None:
            if relative not in seen:
                if len(found) >= self.budget.max_matches:
                    self.budget.warn(f"The match limit ({self.budget.max_matches}) was reached for {pattern}.")
                    raise ScanLimit("match limit")
                seen.add(relative)
                found.append(relative)

        def walk(relative: str, index: int, depth: int) -> None:
            if index >= len(segments):
                return
            segment = segments[index]
            last = index == len(segments) - 1
            if segment == "**":
                if last:
                    # A trailing ** matches every file below this folder.
                    walk(relative, index + 1, depth)
                    directories, files = self.listing(relative)
                    for name in files:
                        add(f"{relative}/{name}" if relative else name)
                    if depth < self.budget.max_depth:
                        for name in directories:
                            if name.lower() not in IGNORED_DIRECTORIES:
                                walk(f"{relative}/{name}" if relative else name, index, depth + 1)
                    else:
                        self.budget.warn(f"Folders deeper than {self.budget.max_depth} levels were not searched.")
                    return
                walk(relative, index + 1, depth)
                if depth >= self.budget.max_depth:
                    self.budget.warn(f"Folders deeper than {self.budget.max_depth} levels were not searched.")
                    return
                directories, _ = self.listing(relative)
                for name in directories:
                    if name.lower() not in IGNORED_DIRECTORIES:
                        walk(f"{relative}/{name}" if relative else name, index, depth + 1)
                return
            directories, files = self.listing(relative)
            wildcard = any(token in segment for token in "*?[")
            lowered = segment.lower()
            if last:
                for name in files:
                    if (fnmatch.fnmatchcase(name.lower(), lowered) if wildcard else name.lower() == lowered):
                        add(f"{relative}/{name}" if relative else name)
                return
            if depth >= self.budget.max_depth:
                self.budget.warn(f"Folders deeper than {self.budget.max_depth} levels were not searched.")
                return
            for name in directories:
                if wildcard:
                    if name.lower() in IGNORED_DIRECTORIES or not fnmatch.fnmatchcase(name.lower(), lowered):
                        continue
                elif name.lower() != lowered:
                    continue
                walk(f"{relative}/{name}" if relative else name, index + 1, depth + 1)

        try:
            walk("", 0, 0)
        except ScanLimit as error:
            if str(error) != "match limit":
                raise
        found.sort(key=str.lower)
        self._globs[pattern] = found
        return found

    def newest(self, paths: list[str]) -> str | None:
        stamped = [(stat.st_mtime_ns, path) for path in paths if (stat := self.stat(path)) is not None]
        return max(stamped)[1] if stamped else None

    def read_text(self, relative: str, max_bytes: int, tail: bool = False) -> tuple[str, bool]:
        """Read at most ``max_bytes`` (head or tail); return (text, truncated)."""
        self.budget.check_time()
        path = self.path(relative)
        if path.suffix.lower() in BINARY_SUFFIXES:
            raise ValueError(f"Binary file is not a text input: {relative}")
        remaining = self.budget.max_bytes - self.budget.bytes_read
        if remaining <= 0:
            raise ScanLimit("The total read budget was reached; narrow the profile's file patterns")
        limit = max(0, min(max_bytes, remaining))
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if tail and size > limit:
                stream.seek(size - limit)
            payload = stream.read(limit)
        self.budget.bytes_read += len(payload)
        if b"\x00" in payload[:4096]:
            raise ValueError(f"File looks binary and was not read as text: {relative}")
        text = payload.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        return text, size > len(payload)
