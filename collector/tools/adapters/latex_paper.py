"""Bounded literal TeX structure discovery, body counts and observed history.

This is a source reader, not a TeX engine. Macro-generated includes/structure
are reported as a boundary. Optional TeXcount receives only expanded text on
stdin with includes disabled; no source files or compilation outputs are written.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import UTC, iso, parse_time, relative_file

MAX_FILES = 256
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_HEADINGS = 1000
MAX_HISTORY = 1800
HISTORY_DAYS = 35
COMMAND = re.compile(r"\\([A-Za-z]+)\*?")
HEADINGS = {"chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4}
OMIT_ARGS = {"label", "ref", "eqref", "pageref", "autoref", "cref", "Cref", "cite", "citep", "citet",
             "parencite", "textcite", "bibliography", "bibliographystyle", "addbibresource", "includegraphics",
             "caption", "captionof", "title", "author", "date", "documentclass", "usepackage", "url"}
OMIT_ENVIRONMENTS = {"equation", "equation*", "align", "align*", "gather", "gather*", "displaymath",
                     "verbatim", "Verbatim", "lstlisting", "minted", "comment", "thebibliography", "tabular", "tabular*"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _group(text: str, start: int, left: str = "{", right: str = "}") -> tuple[str | None, int]:
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != left:
        return None, start
    depth, end = 1, start + 1
    while end < len(text):
        if text[end] == "\\":
            end += 2
            continue
        if text[end] == left:
            depth += 1
        elif text[end] == right:
            depth -= 1
            if depth == 0:
                return text[start + 1:end], end + 1
        end += 1
    return None, start


def _arguments(text: str, start: int) -> tuple[str | None, int]:
    _, optional_end = _group(text, start, "[", "]")
    return _group(text, optional_end)


def _blank(match: re.Match[str]) -> str:
    return "".join("\n" if char == "\n" else " " for char in match.group())


def _without_comments(text: str) -> str:
    return re.sub(r"(?<!\\)(?:\\\\)*%[^\n]*", _blank, text)


def _body_text(text: str) -> str:
    """Deterministic fallback; common macro arguments count as prose."""
    text = _without_comments(text)
    for env in OMIT_ENVIRONMENTS:
        text = re.sub(r"\\begin\{" + re.escape(env) + r"\}.*?\\end\{" + re.escape(env) + r"\}", " ", text, flags=re.S)
    text = re.sub(r"\\\[.*?\\\]|\\\(.*?\\\)|\$\$.*?\$\$|(?<!\\)\$.*?(?<!\\)\$", " ", text, flags=re.S)
    pieces, cursor = [], 0
    for match in COMMAND.finditer(text):
        if match.start() < cursor:
            continue
        pieces.append(text[cursor:match.start()])
        cursor = match.end()
        if match.group(1) in OMIT_ARGS:
            _, end = _arguments(text, cursor)
            cursor = end
            if match.group(1) == "captionof":
                _, cursor = _group(text, cursor)
        elif match.group(1) in {"begin", "end"}:
            _, cursor = _group(text, cursor)
    pieces.append(text[cursor:])
    return " ".join(pieces)


def body_words(text: str) -> int:
    value = _body_text(text)
    # English words/numbers; each Han character is counted separately.
    return len(re.findall(r"[\u3400-\u9fff]|[^\W_\u3400-\u9fff]+(?:['’-][^\W_\u3400-\u9fff]+)*", value, re.UNICODE))


def plain_title(text: str) -> str:
    value = re.sub(r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?", "", text)
    return re.sub(r"\s+", " ", value.replace("{", "").replace("}", "")).strip()[:240] or "Untitled section"


@lru_cache(maxsize=4)
def _texcount_version(executable: str) -> str:
    try:
        run = subprocess.run([executable, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        match = re.search(r"TeXcount version ([0-9.]+)", run.stdout)
        return match.group(1) if match else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def analyze(root: Path, options: dict[str, Any], now: dt.datetime) -> tuple[dict[str, Any] | None, list[Path], dict[str, list[Any] | None]]:
    root = root.resolve()
    configured = options.get("chapters", [])
    main_name = options.get("mainFile") or (configured[0] if configured else ("main.tex" if (root / "main.tex").is_file() else None))
    if not main_name:
        return None, [], {}
    main = relative_file(root, main_name)
    if not main.is_file():
        raise FileNotFoundError(f"Configured TeX entry file is missing: {main_name}")
    warnings: list[str] = []
    files: dict[Path, str] = {}
    manifests: dict[str, list[Any] | None] = {}
    dependency_paths: set[Path] = set()
    events: list[dict[str, Any]] = []
    total_bytes = 0
    active: set[Path] = set()
    title = main.stem
    graphic_directories = [root]
    graphic_extensions = [".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg"]

    def warning(message: str) -> None:
        if message not in warnings and len(warnings) < 100:
            warnings.append(message)

    def track(path: Path) -> None:
        if len(dependency_paths) >= 1000 and path not in dependency_paths:
            raise ValueError("Paper analysis exceeded the dependency limit")
        dependency_paths.add(path)
        stamp = path.stat() if path.is_file() else None
        manifests.setdefault(str(path), [stamp.st_mtime_ns, stamp.st_size] if stamp else None)

    def resolve(name: str, owner: Path, extension: str, graphic: bool = False) -> Path | None:
        name = name.strip()
        if not name or any(char in name for char in "\\#$\x00{}") or Path(name).is_absolute() or PureWindowsPath(name).is_absolute():
            warning("Dynamic or absolute dependency was skipped; only literal paths inside the selected folder are monitored.")
            return None
        if extension and not name.lower().endswith(extension):
            name += extension
        candidates = [root / name, owner.parent / name]
        if graphic:
            candidates.extend(folder / name for folder in graphic_directories)
            candidates = list(dict.fromkeys(candidates))
            if Path(name).suffix.lower() not in graphic_extensions:
                candidates = candidates + [Path(str(base) + suffix) for base in candidates for suffix in graphic_extensions]
        path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0]).resolve()
        if not path.is_relative_to(root):
            warning("Dependency outside the selected folder was skipped.")
            return None
        track(path)
        if not path.is_file():
            warning(f"Declared dependency is missing: {path.relative_to(root).as_posix()}.")
        return path

    def visit(path: Path, render: bool = True) -> None:
        nonlocal total_bytes, title
        if path in active:
            warning("Cyclic TeX include was skipped.")
            return
        if not path.is_file():
            return
        if path not in files:
            if len(files) >= MAX_FILES or path.stat().st_size > MAX_FILE_BYTES or total_bytes + path.stat().st_size > MAX_TOTAL_BYTES:
                raise ValueError("Paper analysis exceeded the bounded source-reading limit; narrow the selected folder or entry file")
            track(path)
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("A TeX source exceeded the source-reading limit")
            manifests[str(path)].append(hashlib.sha256(raw).hexdigest())
            total_bytes += len(raw)
            files[path] = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            if "\ufffd" in files[path]:
                warning("A source could not be decoded completely as UTF-8; body counts may be incomplete.")
        active.add(path)
        original = files[path]
        text = _without_comments(original)
        for declaration in re.finditer(r"\\graphicspath\b", text):
            value, _ = _group(text, declaration.end())
            if value is None:
                continue
            position = 0
            while position < len(value):
                directory, end = _group(value, position)
                if directory is None:
                    warning("A dynamic graphics search path was skipped.")
                    break
                position = end
                directory = directory.strip()
                if not directory or any(char in directory for char in "\\#$\x00{}") or Path(directory).is_absolute() or PureWindowsPath(directory).is_absolute():
                    warning("An absolute or dynamic graphics search path was skipped.")
                    continue
                folder = (root / directory).resolve()
                if not folder.is_relative_to(root):
                    warning("A graphics search path outside the selected folder was skipped.")
                    continue
                if folder not in graphic_directories and len(graphic_directories) < 64:
                    graphic_directories.append(folder)
        for declaration in re.finditer(r"\\DeclareGraphicsExtensions\b", text):
            value, _ = _group(text, declaration.end())
            if value:
                extensions = [extension.strip().lower() for extension in value.split(",")]
                if extensions and all(re.fullmatch(r"\.[a-z0-9]{1,8}", extension) for extension in extensions) and len(extensions) <= 16:
                    graphic_extensions[:] = extensions
        # Bibliography/graphics declarations often occur before begin{document}.
        for declaration in re.finditer(r"\\(bibliography|addbibresource|includegraphics)\*?\b", text):
            value, _ = _arguments(text, declaration.end())
            if value:
                for dependency in value.split(","):
                    resolve(dependency, path, ".bib" if declaration.group(1) != "includegraphics" else "", graphic=declaration.group(1) == "includegraphics")
        # Blank ignored environments in-place so headings there cannot create nodes.
        for env in OMIT_ENVIRONMENTS:
            text = re.sub(r"\\begin\{" + re.escape(env) + r"\}.*?\\end\{" + re.escape(env) + r"\}", _blank, text, flags=re.S)
        start = 0
        if path == main:
            document_title = re.search(r"\\title\b", text)
            if document_title:
                value, _ = _arguments(text, document_title.end())
                if value:
                    title = plain_title(value)
            document = re.search(r"\\begin\s*\{document\}", text)
            start = document.end() if document else 0
        if not render or start:
            preamble = text if not render else text[:start]
            for include in re.finditer(r"\\(?:input|include)\b\s*(?:\{([^{}]*)\}|([^\s%{}]+))", preamble):
                dependency = resolve(include.group(1) if include.group(1) is not None else include.group(2), path, ".tex")
                if dependency:
                    visit(dependency, render=False)
        if not render:
            active.remove(path)
            return
        cursor = start
        for match in COMMAND.finditer(text):
            if match.start() < cursor:
                continue
            name = match.group(1)
            if name not in HEADINGS and name not in {"input", "include", "bibliography", "addbibresource", "includegraphics", "begin", "end"}:
                continue
            value, end = _arguments(text, match.end())
            if value is None and name in {"input", "include"}:
                bare = re.match(r"\s*([^\s{}%]+)", text[match.end():])
                if bare:
                    value, end = bare.group(1), match.end() + bare.end()
            if value is None:
                continue
            if name in {"bibliography", "addbibresource", "includegraphics"}:
                for dependency in value.split(","):
                    resolve(dependency, path, ".bib" if name != "includegraphics" else "", graphic=name == "includegraphics")
                continue
            if name in {"begin", "end"} and value not in {"abstract", "document"}:
                continue
            events.append({"kind": "text", "text": text[cursor:match.start()], "source": original[cursor:match.start()], "file": path})
            cursor = end
            if name in HEADINGS or name == "begin" and value == "abstract":
                if len([event for event in events if event["kind"] == "heading"]) >= MAX_HEADINGS:
                    raise ValueError("Paper analysis exceeded the section limit")
                label, label_end = None, end
                following = re.match(r"\s*\\label\s*", text[end:])
                if following:
                    label, label_end = _group(text, end + following.end())
                events.append({"kind": "heading", "title": plain_title(value) if name in HEADINGS else "Abstract",
                               "command": name if name in HEADINGS else "chapter", "label": label,
                               "file": path, "line": text[:match.start()].count("\n") + 1,
                               "source": original[match.start():label_end]})
                cursor = label_end
            elif name in {"input", "include"}:
                dependency = resolve(value, path, ".tex")
                if dependency:
                    visit(dependency)
            elif name == "end" and value == "document":
                cursor = len(text)
                break
        events.append({"kind": "text", "text": text[cursor:], "source": original[cursor:], "file": path})
        active.remove(path)

    visit(main)
    has_chapter = any(event.get("command") == "chapter" and event.get("title") != "Abstract" for event in events)
    labels = Counter(event["label"] for event in events if event.get("label"))
    heading_names = Counter((event["file"], event["command"], event["title"]) for event in events if event["kind"] == "heading")
    identities: Counter[str] = Counter()
    chapters: list[dict[str, Any]] = []
    blocks: list[str] = []
    stack: list[dict[str, Any]] = []
    current = None
    for event in events:
        if event["kind"] == "heading":
            level = HEADINGS[event["command"]] - (0 if has_chapter or event["command"] == "chapter" else 1)
            while stack and stack[-1]["level"] >= level:
                stack.pop()
            relative = event["file"].relative_to(root).as_posix()
            label = event["label"]
            reliable = bool(label and labels[label] == 1)
            anchor = "label:" + label if reliable else f"heading:{relative}:{level}:{event['title']}"
            if not reliable and heading_names[(event["file"], event["command"], event["title"])] > 1:
                # Ambiguous duplicates lose identity after file edits rather than
                # moving an existing milestone onto a newly inserted duplicate.
                anchor += ":ambiguous-file:" + digest(files[event["file"]])
            identities[anchor] += 1
            if not reliable:
                warning("Unlabelled or duplicate-label headings use title-based IDs; renaming them creates a new section identity. Add unique labels for persistent task bindings.")
            identity = "section-" + digest(f"{main.relative_to(root).as_posix()}:{anchor}:{identities[anchor]}")[:20]
            current = {"id": identity, "title": event["title"], "level": level,
                       "parentId": stack[-1]["id"] if stack else None, "file": relative, "line": event["line"],
                       "bodyWords": 0, "ownBodyWords": 0, "sourceHash": "", "modifiedAt": None,
                       "deltaToday": None, "deltaWeek": None, "label": label if reliable else None,
                       "identityMethod": "label" if reliable else "heading-title", "_source": event["source"], "_files": {event["file"]}}
            chapters.append(current)
            blocks.append("")
            stack.append(current)
        elif current:
            blocks[-1] += event["text"]
            current["_source"] += event["source"]
            current["_files"].add(event["file"])
        elif body_words(event["text"]):
            # Keep actual leading prose in an explicit node, rather than losing it.
            relative = event["file"].relative_to(root).as_posix()
            current = {"id": "section-" + digest(f"{main.relative_to(root).as_posix()}:opening-prose")[:20],
                       "title": "Opening text", "level": 1, "parentId": None, "file": relative, "line": 1,
                       "bodyWords": 0, "ownBodyWords": 0, "sourceHash": "", "modifiedAt": None,
                       "deltaToday": None, "deltaWeek": None, "label": None, "identityMethod": "opening-prose",
                       "_source": event["source"], "_files": {event["file"]}}
            chapters.append(current)
            blocks.append(event["text"])
            stack = [current]
    counts = [body_words(block) for block in blocks]
    method, policy = "Heuristic body words (not TeXcount)", "body-heuristic-v1"
    executable = shutil.which("texcount") if options.get("wordCountMethod", "auto") != "heuristic" else None
    if executable and blocks:
        synthetic = "\\documentclass{article}\n\\begin{document}\n" + "\n".join(
            f"\\section{{RD{index:04d}}}\n{block}\n" for index, block in enumerate(blocks)) + "\n\\end{document}\n"
        try:
            process = subprocess.run([executable, "-utf8", "-noinc", "-nobib", "-sub=section", "-v0", "-"],
                                     input=synthetic, text=True, encoding="utf-8", errors="replace", capture_output=True,
                                     timeout=12, cwd=root, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            found = {int(index): int(words) for words, index in re.findall(
                r"^\s*(\d+)\+\d+\+\d+\s+\([^)]*\)\s+Section:\s+RD(\d{4})\b", process.stdout, re.M)}
            if len(blocks) == 1 and not found:
                one_count = re.search(r"^Words in text:\s*(\d+)\s*$", process.stdout, re.M)
                if one_count:
                    found = {0: int(one_count.group(1))}
            if process.returncode == 0 and set(found) == set(range(len(blocks))) and not re.search(r"(?:^|\n)(?:ERROR|Error|!!!)", process.stdout + process.stderr):
                counts = [found[index] for index in range(len(blocks))]
                version = _texcount_version(executable)
                method = f"TeXcount {version} body words (expanded literal sources; captions and headings excluded)"
                policy = f"body-texcount-{version}-v1"
            else:
                warning("TeXcount did not return complete section counts; deterministic heuristic body counts were used.")
        except (OSError, subprocess.TimeoutExpired):
            warning("TeXcount was unavailable or timed out; deterministic heuristic body counts were used.")
    if policy == "body-heuristic-v1":
        warning("Heuristic counts exclude common headings, captions, references, equations and comments; custom macro arguments may be counted as prose. Counts do not establish completion.")
    warning("Only literal includes and dependencies are resolved. Macro-generated paths, conditional branches, dynamic graphics search paths and custom macro semantics need review.")
    by_id = {chapter["id"]: chapter for chapter in chapters}
    for chapter, count in zip(chapters, counts):
        chapter["ownBodyWords"] = chapter["bodyWords"] = count
        chapter["sourceHash"] = digest(chapter.pop("_source"))
        chapter["modifiedAt"] = iso(dt.datetime.fromtimestamp(max(path.stat().st_mtime for path in chapter["_files"]), UTC))
        chapter.pop("_files")
    for chapter in reversed(chapters):
        if chapter["parentId"]:
            parent = by_id[chapter["parentId"]]
            parent["bodyWords"] += chapter["bodyWords"]
            parent["sourceHash"] = digest(parent["sourceHash"] + chapter["sourceHash"])
            parent["modifiedAt"] = max(parent["modifiedAt"], chapter["modifiedAt"])
    if len(chapters) > MAX_HEADINGS:
        raise ValueError("Paper analysis exceeded the section limit")
    for configured_name in configured:
        configured_path = relative_file(root, configured_name)
        track(configured_path)
    dependencies = sorted(path.relative_to(root).as_posix() for path in dependency_paths)
    paper = {"kind": "paper", "schemaVersion": 1, "title": title, "mainFile": main.relative_to(root).as_posix(),
             "countingPolicy": policy, "countingMethod": method, "observedAt": iso(now),
             "revision": digest(policy + "".join(path.relative_to(root).as_posix() + digest(text) for path, text in sorted(files.items()))),
             "chapters": chapters, "bodyWords": sum(chapter["bodyWords"] for chapter in chapters if chapter["parentId"] is None),
             "warnings": warnings, "dependencies": dependencies,
             "activity": {"timezone": activity_zone()[0], "today": {"baselineAt": None, "available": False},
                          "week": {"baselineAt": None, "available": False}}}
    return paper, [path for path in dependency_paths if path.is_file()], manifests


def activity_zone() -> tuple[str, dt.tzinfo]:
    """The day boundary for writing activity: this computer's local time zone, named by $RD_TIMEZONE (an IANA name the
    desktop app passes) when set. A bundled Python has no IANA database; the local zone is then used under that name."""
    local = dt.datetime.now().astimezone().tzinfo or UTC
    name = os.environ.get("RD_TIMEZONE", "").strip()
    if name and re.fullmatch(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*", name):
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return name, local
    return local.tzname(None) or "local", local


def observed_history(paper: dict[str, Any], history: dict[str, Any] | None, now: dt.datetime) -> dict[str, Any]:
    """Keep first daily observations and hourly latest samples, without prehistory.

    A nearby pre-boundary observation covers the requested period approximately.
    Daytime-only monitoring uses a labelled partial period beginning at its first
    actual observation; repeated collections never slide that opening baseline.
    """
    policy = paper["countingPolicy"]
    snapshots: list[dict[str, Any]] = []
    preserved_starts: list[dict[str, Any]] = []
    tracking_started = None
    compatible = bool(history and history.get("schemaVersion") == 1 and history.get("countingPolicy") == policy
                      and history.get("mainFile") == paper["mainFile"])
    oldest = now - dt.timedelta(days=HISTORY_DAYS)

    def valid_row(row: Any) -> bool:
        if not isinstance(row, dict) or not isinstance(row.get("words"), dict) or not isinstance(row.get("observedAt"), str):
            return False
        try:
            stamp = parse_time(row["observedAt"])
        except (ValueError, TypeError):
            return False
        return stamp is not None and oldest <= stamp <= now

    if compatible:
        rows = history.get("snapshots", [])
        if not isinstance(rows, list):
            rows = []
            paper["warnings"].append("Invalid observation history was reset; earlier deltas are unavailable.")
        snapshots = [row for row in rows if valid_row(row)]
        starts = history.get("dayStarts", {})
        if isinstance(starts, dict):
            preserved_starts = [row for row in starts.values() if valid_row(row)]
        recorded_start = history.get("trackingStartedAt")
        if isinstance(recorded_start, str):
            try:
                stamp = parse_time(recorded_start)
                if stamp and stamp <= now:
                    tracking_started = iso(stamp)
            except (ValueError, TypeError):
                pass
    current = {"observedAt": iso(now), "revision": paper["revision"],
               "words": {chapter["id"]: chapter["bodyWords"] for chapter in paper["chapters"]}}
    observations = sorted(snapshots + preserved_starts + [current], key=lambda row: row["observedAt"])
    if tracking_started is None:
        tracking_started = observations[0]["observedAt"]
    zone_name, timezone = activity_zone()
    local = now.astimezone(timezone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    cutoffs = {"today": midnight, "week": now - dt.timedelta(days=7)}
    paper["activity"]["trackingStartedAt"] = tracking_started
    for period, cutoff in cutoffs.items():
        choices = [row for row in observations if cutoff - dt.timedelta(hours=2) <= parse_time(row["observedAt"]) <= cutoff]
        baseline = max(choices, key=lambda row: row["observedAt"]) if choices else None
        boundary = baseline is not None
        if baseline is None:
            partial = [row for row in observations if cutoff <= parse_time(row["observedAt"]) <= now]
            baseline = min(partial, key=lambda row: row["observedAt"]) if partial else None
        paper["activity"][period] = {
            "baselineAt": baseline["observedAt"] if baseline else None, "available": baseline is not None,
            "baselineKind": "boundary" if boundary else "first-observation" if baseline is not None else None,
            "coverage": "full" if boundary else "partial", "windowStartAt": iso(cutoff),
        }
        for chapter in paper["chapters"]:
            prior = baseline["words"].get(chapter["id"]) if baseline else None
            chapter["deltaToday" if period == "today" else "deltaWeek"] = chapter["bodyWords"] - prior if isinstance(prior, int) and not isinstance(prior, bool) and prior >= 0 else None
    # Keep an immutable first observation per London day separately from the
    # replaceable hourly samples. Legacy histories seed this from retained rows.
    day_starts: dict[str, dict[str, Any]] = {}
    for row in observations:
        day = parse_time(row["observedAt"]).astimezone(timezone).date().isoformat()
        day_starts.setdefault(day, row)
    day_starts = {day: day_starts[day] for day in sorted(day_starts)[-HISTORY_DAYS:]}
    hourly = {row["observedAt"][:13]: row for row in sorted(snapshots, key=lambda row: row["observedAt"])}
    hourly[current["observedAt"][:13]] = current
    sections = section_changes(paper, history, now)
    return {"schemaVersion": 1, "mainFile": paper["mainFile"], "countingPolicy": policy,
            "timezone": zone_name, "retentionDays": HISTORY_DAYS, "trackingStartedAt": tracking_started,
            "dayStarts": day_starts, "snapshots": sorted(hourly.values(), key=lambda row: row["observedAt"])[-MAX_HISTORY:],
            "sections": sections}


def section_changes(paper: dict[str, Any], history: dict[str, Any] | None, now: dt.datetime) -> dict[str, Any]:
    """Per-section content fingerprints across collections: `changedAt` is the last collection at which a section's own
    source (sourceHash) differed from the previous one, so an edit is located even when it leaves the word count and
    the file's other sections alone. Unknown (None) until a change is seen; a parent changes with its children."""
    previous = history.get("sections") if isinstance(history, dict) and history.get("mainFile") == paper["mainFile"] else None
    previous = previous if isinstance(previous, dict) else {}
    sections: dict[str, Any] = {}
    for chapter in paper["chapters"]:
        before = previous.get(chapter["id"]) if isinstance(previous.get(chapter["id"]), dict) else None
        changed = before.get("changedAt") if before and isinstance(before.get("changedAt"), str) else None
        if before and isinstance(before.get("hash"), str) and before["hash"] != chapter["sourceHash"]:
            changed = iso(now)
        chapter["changedAt"] = changed
        sections[chapter["id"]] = {"hash": chapter["sourceHash"], "changedAt": changed}
    return sections
