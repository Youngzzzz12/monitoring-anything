"""ma: the Monitoring Anything command line for agents (Claude Code, Codex) and scripts.

Every command prints one JSON object. The workspace is the one the desktop app has open (its settings file), or
--workspace / $RD_WORKSPACE. Research folders are only read. Writes go to the workspace: milestones directly (each
edit verifies the exact line first), connections through the app, which shows its plan card for the user to confirm.

  ma where                                   paths of the app, the bundled Python and the workspace
  ma report [--project ID] [--limit N]       progress: completion, Next up, overdue, sources (as the dashboard shows)
  ma brief                                   how to design a monitoring plan (rules, schema, example, saved types)
  ma inspect --folder PATH                   detected monitoring types for a folder
  ma preview --folder PATH (--profile-id ID | --plan FILE [--script FILE.py])   what a plan would read now (dry run;
                                             a custom adapter is checked and run read-only)
  ma connect --folder PATH [--plan FILE [--script FILE.py] | --profile-id ID] [--title T] [--parent ID]
                                             open the plan card in the app, prefilled, for the user to confirm
  ma milestone list|add|done|undo|rename|due|delete --project ID ...
  ma collector ARGS...                       any collector command (advanced)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

APP_ID = "io.github.monitoring-anything"
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
TASK = re.compile(r"^(\s*[-*+]\s+)\[([ xX])\]\s+(.*)$")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
TAG = re.compile(r"(?:^|\s)#milestone(?:\s|$)")
STAGES = ("draft", "figures", "references", "review", "complete")
DUE_SOON_DAYS = 14


class Problem(Exception):
    """A user-facing error: printed as {"ok": false, "error": ...}."""


# --------------------------------------------------------------------------- locations

def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_ID
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_ID
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_ID


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def app_paths() -> dict[str, Any]:
    """What the app recorded at its last launch (cli.json) plus the settings' workspace."""
    folder = config_dir()
    paths: dict[str, Any] = {"configDir": str(folder)}
    for name in ("cli.json", "settings.json"):
        try:
            paths[name] = read_json(folder / name)
        except (OSError, ValueError):
            paths[name] = None
    return paths


def workspace_root(explicit: str | None) -> Path:
    value = explicit or os.environ.get("RD_WORKSPACE") or ((app_paths().get("settings.json") or {}).get("workspace"))
    if not value:
        raise Problem("No workspace: open or create one in Monitoring Anything, or pass --workspace.")
    root = Path(value)
    if not (root / "workspace.json").is_file():
        raise Problem(f"Not a Monitoring Anything workspace: {root}")
    return root


def atomic_write(path: Path, text: str) -> None:
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- workspace model

def field(text: str, name: str) -> str | None:
    match = re.search(r"\[" + name + r"::\s*([^\]]*)\]", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def label(text: str) -> str:
    text = re.sub(r"(^|\s)#milestone\b", " ", text)
    text = re.sub(r"\[(?:due|paper|chapter|stage|accepted_hash)::[^\]]*\]", "", text, flags=re.IGNORECASE)
    return " ".join(re.sub(r"\\([\\`*_\[\]#<>])", r"\1", text).split())


def parse_tasks(markdown: str) -> list[dict[str, Any]]:
    """Milestone tasks outside frontmatter and fenced code (mirrors the app's parseMilestones)."""
    tasks, fence, heading = [], None, None
    lines = markdown.split("\n")
    start = 0
    if lines and lines[0].lstrip("﻿").strip() == "---":
        start = next((i + 1 for i in range(1, len(lines)) if re.match(r"^(---|\.\.\.)\s*$", lines[i])), len(lines))
    for index in range(start, len(lines)):
        line = lines[index].rstrip("\r")
        marker = FENCE.match(line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) and not marker.group(2).strip():
                fence = None
            continue
        if fence is not None:
            continue
        head = HEADING.match(line)
        if head:
            heading = None if head.group(2).lower() == "milestones" else head.group(2)
            continue
        task = TASK.match(line)
        if task and TAG.search(task.group(3)):
            text = task.group(3)
            tasks.append({"line": index, "text": text, "title": label(text), "completed": task.group(2).lower() == "x",
                          "due": field(text, "due"), "paper": field(text, "paper"), "chapter": field(text, "chapter"),
                          "stage": field(text, "stage"), "heading": heading})
    return tasks


def load(root: Path) -> list[dict[str, Any]]:
    projects = []
    for path in sorted((root / "projects").glob("*/project.json")):
        try:
            record = read_json(path)
        except (OSError, ValueError):
            continue
        pid = path.parent.name
        if not isinstance(record, dict) or record.get("projectId") != pid:
            continue
        notes = path.parent / "milestones.md"
        text = notes.read_text(encoding="utf-8") if notes.is_file() else ""
        sources = []
        for sid in record.get("sourceIds") or []:
            config, state = {}, None
            try:
                config = read_json(root / "sources" / f"{sid}.json")
            except (OSError, ValueError):
                pass
            try:
                state = read_json(root / "data" / f"{sid}.json")
            except (OSError, ValueError):
                pass
            sources.append({"id": sid, "title": config.get("title") or sid, "kind": config.get("kind"), "state": state})
        parent = record.get("parentId")
        projects.append({"id": pid, "title": record.get("title") or pid, "type": record.get("type") or "other",
                         "parentId": parent if isinstance(parent, str) and parent and parent != pid else None,
                         "health": record.get("health") or "active", "stage": record.get("stage") or "planned",
                         "nextStep": record.get("nextStep"), "due": record.get("due"), "tasks": parse_tasks(text),
                         "sources": sources, "children": []})
    by_id = {project["id"]: project for project in projects}
    for project in projects:
        # One displayed level: hang under the top-most live ancestor; a loop leaves the project top-level.
        seen, cursor, top = {project["id"]}, by_id.get(project["parentId"] or ""), None
        while cursor is not None and cursor["id"] not in seen:
            top = cursor
            seen.add(cursor["id"])
            cursor = by_id.get(cursor["parentId"] or "")
        if cursor is not None:
            top = None
        project["top"] = top["id"] if top else None
        if top:
            top["children"].append(project)
    return projects


def paper_of(source: dict[str, Any]) -> dict[str, Any] | None:
    for detail in (source.get("state") or {}).get("details") or []:
        if isinstance(detail, dict) and detail.get("kind") == "paper" and isinstance(detail.get("chapters"), list):
            return detail
    return None


def chapter_of(project: dict[str, Any], task: dict[str, Any]) -> dict[str, Any] | None:
    if not task["paper"] or not task["chapter"]:
        return None
    source = next((item for item in project["sources"] if item["id"] == task["paper"]), None)
    paper = paper_of(source) if source else None
    return next((item for item in (paper or {}).get("chapters", []) if item.get("id") == task["chapter"]), None)


def stamp(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return dt.datetime.fromisoformat(value[:10]).timestamp()
        except ValueError:
            return 0.0


def due_days(due: str | None, today: dt.date) -> int | None:
    try:
        return (dt.date.fromisoformat(str(due)[:10]) - today).days if due else None
    except ValueError:
        return None


def next_up(pool_projects: list[dict[str, Any]], limit: int, today: dt.date) -> list[dict[str, Any]]:
    """The dashboard's Next up rule (mirrors nextMilestones in src/view/view.ts): overdue (longest first), due within 14
    days, current writing (section changed most recently, at most two), then each project's next in turn. Completed
    projects are left out; paused and blocked ones follow active ones."""
    pool = [project for project in pool_projects if project["health"] != "completed"]
    late = lambda project: 1 if project["health"] in ("paused", "blocked") else 0
    open_items = [(project, task) for project in pool for task in project["tasks"] if not task["completed"]]
    picked: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def take(project: dict[str, Any], task: dict[str, Any], reason: str) -> bool:
        key = (project["id"], task["line"])
        if len(picked) >= limit or key in seen:
            return False
        seen.add(key)
        days = due_days(task["due"], today)
        picked.append({"project": project["id"], "projectTitle": project["title"], "line": task["line"], "title": task["title"],
                       "due": task["due"], "overdueDays": -days if days is not None and days < 0 else None, "reason": reason,
                       "chapter": task["chapter"], "stage": task["stage"]})
        return True

    dated = [(p, t) for p, t in open_items if due_days(t["due"], today) is not None]
    for p, t in sorted([item for item in dated if due_days(item[1]["due"], today) < 0], key=lambda i: (late(i[0]), due_days(i[1]["due"], today))):
        take(p, t, "overdue")
    for p, t in sorted([item for item in dated if 0 <= due_days(item[1]["due"], today) <= DUE_SOON_DAYS], key=lambda i: (late(i[0]), due_days(i[1]["due"], today))):
        take(p, t, "due")
    edited = []
    for project in pool:
        for source in project["sources"]:
            paper = paper_of(source)
            if not paper:
                continue
            chapters = {item.get("id"): item for item in paper["chapters"] if isinstance(item, dict)}
            def depth(chapter: dict[str, Any]) -> int:
                level, node = 0, chapter
                while node.get("parentId") in chapters and level < 8:
                    node, level = chapters[node["parentId"]], level + 1
                return level
            for chapter in chapters.values():
                changed = stamp(chapter.get("changedAt"))
                today_words, week = abs(chapter.get("deltaToday") or 0), abs(chapter.get("deltaWeek") or 0)
                modified = stamp(chapter.get("modifiedAt"))
                if changed or today_words or week or modified:
                    edited.append(((0 if late(project) else 1, changed, depth(chapter) if changed else 0, today_words, week, modified),
                                   project, source, chapter, chapters))
    edited.sort(key=lambda item: item[0], reverse=True)
    writing = 0
    for _, project, source, chapter, chapters in edited:
        if writing >= 2 or len(picked) >= limit:
            break
        node, visited = chapter, set()
        while node and node.get("id") not in visited:
            visited.add(node.get("id"))
            task = next((t for t in project["tasks"] if not t["completed"] and t["paper"] == source["id"] and t["chapter"] == node.get("id")), None)
            if task:
                writing += take(project, task, "writing")
                break
            node = chapters.get(node.get("parentId"))
    queues = [[t for p, t in open_items if p is project] for project in sorted(pool, key=late)]
    owners = sorted(pool, key=late)
    while len(picked) < limit and any(queues):
        for project, queue in zip(owners, queues):
            while queue and (project["id"], queue[0]["line"]) in seen:
                queue.pop(0)
            if queue:
                take(project, queue.pop(0), "next")
            if len(picked) >= limit:
                break
    return picked


def report(root: Path, project_id: str | None, limit: int | None, today: dt.date) -> dict[str, Any]:
    projects = load(root)
    by_id = {project["id"]: project for project in projects}
    if project_id and project_id not in by_id:
        raise Problem(f"No project {project_id!r}. Projects: {', '.join(sorted(by_id))}")

    def counts(project: dict[str, Any]) -> tuple[int, int]:
        family = [project, *project["children"]]
        tasks = [task for member in family for task in member["tasks"]]
        return sum(task["completed"] for task in tasks), len(tasks)

    def summary(project: dict[str, Any]) -> dict[str, Any]:
        done, total = counts(project)
        overdue = [{"title": t["title"], "due": t["due"], "days": -due_days(t["due"], today)} for t in project["tasks"]
                   if not t["completed"] and (due_days(t["due"], today) or 0) < 0]
        sources = []
        for source in project["sources"]:
            collection = (source.get("state") or {}).get("collection") or {}
            paper = paper_of(source)
            after = collection.get("staleAfterSeconds")
            stale = bool(collection.get("lastSuccessfulAt") and isinstance(after, (int, float)) and after > 0
                         and dt.datetime.now(dt.timezone.utc).timestamp() - stamp(collection["lastSuccessfulAt"]) > after)
            sources.append({"id": source["id"], "title": source["title"], "kind": source["kind"],
                            "health": "stale" if stale and collection.get("health") == "fresh" else collection.get("health"),
                            "lastCollected": collection.get("lastSuccessfulAt"), "error": collection.get("error"),
                            "wordsToday": sum((c.get("deltaToday") or 0) for c in paper["chapters"] if not c.get("parentId")) if paper else None,
                            "runtime": (source.get("state") or {}).get("runtime")})
        return {"id": project["id"], "title": project["title"], "type": project["type"], "status": project["health"],
                "stage": project["stage"], "parent": project["top"], "nextStep": project["nextStep"], "due": project["due"],
                "milestones": {"completed": done, "total": total, "percent": round(done / total * 100) if total else None},
                "subProjects": [child["id"] for child in project["children"]], "overdue": overdue, "sources": sources}

    if project_id:
        focus = by_id[project_id]
        family = [focus, *focus["children"]]
        return {"ok": True, "today": today.isoformat(), "project": summary(focus),
                "subProjects": [summary(child) for child in focus["children"]], "nextUp": next_up(family, limit or 5, today)}
    tops = [project for project in projects if not project["top"]]
    rated = [counts(project) for project in tops if counts(project)[1]]
    average = round(sum(done / total for done, total in rated) / len(rated) * 100) if rated else None
    return {"ok": True, "today": today.isoformat(), "averageCompletion": average,
            "projects": [summary(project) for project in tops],
            "subProjects": [summary(project) for project in projects if project["top"]],
            "nextUp": next_up(projects, limit or 3, today)}


# --------------------------------------------------------------------------- milestones

def notes_path(root: Path, project_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project_id or "") or not (root / "projects" / project_id / "project.json").is_file():
        raise Problem(f"No project {project_id!r}")
    return root / "projects" / project_id / "milestones.md"


def find_task(tasks: list[dict[str, Any]], line: int | None, match: str | None) -> dict[str, Any]:
    if line is not None:
        found = next((task for task in tasks if task["line"] == line), None)
        if not found:
            raise Problem(f"No milestone on line {line}")
        return found
    needle = (match or "").strip().lower()
    if not needle:
        raise Problem("Give --line N or --match TEXT")
    exact = [task for task in tasks if task["title"].lower() == needle]
    hits = exact or [task for task in tasks if needle in task["title"].lower()]
    if len(hits) != 1:
        raise Problem(f"--match {match!r} fits {len(hits)} milestones" + (": " + "; ".join(f"line {t['line']}: {t['title']}" for t in hits[:8]) if hits else ""))
    return hits[0]


def escape(title: str) -> str:
    text = str(title or "").strip()
    if not text or len(text) > 300 or re.search(r"[\r\n\x00-\x1f]", text):
        raise Problem("Milestone titles are one line of 1-300 characters")
    return re.sub(r"([\\`*_\[\]#<>])", r"\\\1", text)


def check_due(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise Problem("Use a due date as YYYY-MM-DD") from error


def milestone(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    path = notes_path(root, args.project)
    original = path.read_text(encoding="utf-8") if path.is_file() else "# Milestones\n"
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.split(newline)
    tasks = parse_tasks(original.replace("\r\n", "\n"))
    if args.action == "list":
        return {"ok": True, "project": args.project, "milestones": tasks}
    project = next(item for item in load(root) if item["id"] == args.project)
    if args.action == "add":
        fields = ""
        if args.section:
            source = args.source or next((s["id"] for s in project["sources"] if paper_of(s)), None)
            paper = paper_of(next((s for s in project["sources"] if s["id"] == source), {})) if source else None
            if not paper or not any(c.get("id") == args.section for c in paper["chapters"]):
                raise Problem(f"No section {args.section!r} in this project's paper (ma report shows section ids via milestones; ma collector inspect lists them)")
            stage = args.stage or "draft"
            if stage not in STAGES:
                raise Problem("Stage is one of: " + ", ".join(STAGES))
            fields = f" [paper:: {source}] [chapter:: {args.section}] [stage:: {stage}]"
        due = check_due(args.due)
        line = f"- [ ] {escape(args.title)}{f' [due:: {due}]' if due else ''} #milestone{fields}"
        # Append at the end of the Milestones section (as the app does), else at the end of the file.
        body = original.rstrip("\r\n")
        updated = body + (newline if TASK.match(body.split(newline)[-1] or "") else newline + newline) + line + newline
        atomic_write(path, updated)
        return {"ok": True, "added": label(line[6:]), "project": args.project}
    task = find_task(tasks, args.line, args.match)
    current = TASK.match(lines[task["line"]].rstrip("\r"))
    if not current or current.group(3) != task["text"]:
        raise Problem("The milestone changed while it was being edited; run the command again")
    text = current.group(3)
    if args.action == "delete":
        del lines[task["line"]]
        atomic_write(path, newline.join(lines))
        return {"ok": True, "deleted": task["title"], "project": args.project}
    box = current.group(2)
    if args.action in ("done", "undo"):
        text = re.sub(r"\s*\[accepted_hash::\s*[^\]]+\]", "", text)
        if args.action == "done" and task["chapter"]:
            chapter = chapter_of(project, task)
            if not chapter or not re.fullmatch(r"[0-9a-f]{16,128}", str(chapter.get("sourceHash") or "")):
                raise Problem("This milestone's section is not in the latest collection; collect again first")
            text += f" [accepted_hash:: {chapter['sourceHash']}]"
        box = "x" if args.action == "done" else " "
    elif args.action in ("rename", "due"):
        bound = " ".join(re.findall(r"\[(?:paper|chapter|stage|accepted_hash)::[^\]]*\]", text, re.IGNORECASE))
        title = escape(args.title) if args.action == "rename" else re.sub(r"\[(?:due|paper|chapter|stage|accepted_hash)::[^\]]*\]", "", re.sub(r"(^|\s)#milestone\b", " ", text), flags=re.IGNORECASE).strip()
        title = " ".join(title.split())
        due = check_due(args.due) if args.action == "due" else task["due"]
        text = f"{title}{f' [due:: {due}]' if due else ''} #milestone{(' ' + bound) if bound else ''}"
    lines[task["line"]] = f"{current.group(1)}[{box}] {text}"
    atomic_write(path, newline.join(lines))
    return {"ok": True, "action": args.action, "milestone": label(text), "completed": box == "x", "project": args.project}


# --------------------------------------------------------------------------- plans and connections

def collector(root: Path, argv: list[str], stdin: str | None = None) -> Any:
    """Run one collector command in-process and return its JSON."""
    import contextlib
    import io
    import cli
    out, previous = io.StringIO(), sys.stdin
    try:
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        with contextlib.redirect_stdout(out):
            code = cli.main(["--workspace", str(root), *argv])
    finally:
        sys.stdin = previous
    text = out.getvalue().strip()
    try:
        value = json.loads(text) if text else None
    except ValueError:
        value = text
    if code not in (0, None) and not (isinstance(value, dict) and value.get("collectionHealth") == "error"):
        raise Problem(f"collector {' '.join(argv[:2])} failed: {value}")
    return value


def read_plan(path: str | None, script: str | None = None) -> dict[str, Any] | None:
    """The plan JSON; with --script, the adapter file's code becomes profile.extract.script.code (keeping its options)."""
    if not path:
        if script:
            raise Problem("--script needs --plan (the plan supplies detect, title and type)")
        return None
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Problem(f"Could not read the plan JSON: {error}") from error
    if not isinstance(plan, dict):
        raise Problem("The plan must be a JSON object ({profile, project?, milestones?, rationale?})")
    if script:
        try:
            code = Path(script).read_text(encoding="utf-8")
        except OSError as error:
            raise Problem(f"Could not read the adapter: {error}") from error
        profile = plan.get("profile") if isinstance(plan.get("profile"), dict) else plan
        current = (profile.get("extract") or {}).get("script") if isinstance(profile.get("extract"), dict) else None
        profile["extract"] = {"script": {**(current if isinstance(current, dict) else {}), "language": "python", "code": code}}
    return plan


def brief(root: Path) -> dict[str, Any]:
    import agents
    from adapters.script import CONTRACT
    from milestones import list_templates, template_summary
    library = collector(root, ["profiles", "list"])
    templates, _ = list_templates(root)
    return {"ok": True,
            "howTo": ["Read the folder yourself (names and short excerpts only; never run or modify anything in it).",
                      "Design a plan: {schemaVersion: '1.0', profile: {...}, project: {title, type, parentSuggestion?}, rationale}.",
                      "Write it to a temporary .json file; check it with `ma preview --folder F --plan FILE` until preview.ok is true and the values look right.",
                      "Then `ma connect --folder F --plan FILE`: the app opens its plan card prefilled and the user confirms.",
                      "Prefer an existing type when `ma inspect` already matches well (use --profile-id instead of --plan).",
                      "Only when declarative rules cannot read what is needed: write a custom adapter (rules, rule 8) to a temporary "
                      "adapter.py and add `--script adapter.py` to preview and connect (the plan keeps detect, title and type; its "
                      "extract becomes the script). Iterate until preview.ok is true: errors name the failing check or the adapter's "
                      "exception and line. Write nothing into the folder itself."],
            "adapterContract": CONTRACT,
            "rules": agents.RULES, "example": agents.EXAMPLE_PLAN,
            "schema": json.loads(agents.SCHEMA_PATH.read_text(encoding="utf-8")),
            "savedTypes": [{"id": item.get("id"), "title": item.get("title"), "origin": item.get("origin")} for item in (library or {}).get("profiles", [])],
            "milestoneTemplates": [template_summary(item) for item in templates]}


def connect(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    folder = Path(args.folder)
    if not folder.is_absolute() or not folder.is_dir():
        raise Problem("--folder must be an existing absolute folder")
    plan = read_plan(args.plan, args.script)
    if plan is not None:
        profile = plan.get("profile") if isinstance(plan.get("profile"), dict) else plan
        checked = collector(root, ["profiles", "validate"], json.dumps({"profile": profile}))
        if not checked.get("ok"):
            return {"ok": False, "error": "The plan does not validate; fix it (see errors) and check with ma preview.", "errors": checked.get("errors"), "warnings": checked.get("warnings")}
    exe = ((app_paths().get("cli.json") or {}).get("appExe"))
    if not exe or not Path(exe).is_file():
        raise Problem("Monitoring Anything was not found. Start the app once (it records its location), then retry.")
    request = {"schemaVersion": 1, "action": "connect", "source": str(folder), "plan": plan, "profileId": args.profile_id,
               "title": args.title, "parentId": args.parent}
    handle, temp = tempfile.mkstemp(prefix="ma-request-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(request, stream)
    flags = {"creationflags": 0x00000008 | 0x00000200} if sys.platform == "win32" else {"start_new_session": True}
    subprocess.Popen([exe, "--ma-request", temp], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **flags)
    return {"ok": True, "handedTo": "Monitoring Anything",
            "next": "The plan card is open in the app with this folder" + (" and your plan" if plan else "") + ". Ask the user to review and press Connect there."}


# --------------------------------------------------------------------------- command line

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ma", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root.add_argument("--workspace", help="Workspace folder (default: the one open in the app)")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("where")
    rep = sub.add_parser("report")
    rep.add_argument("--project")
    rep.add_argument("--limit", type=int)
    sub.add_parser("brief")
    ins = sub.add_parser("inspect")
    ins.add_argument("--folder", required=True)
    pre = sub.add_parser("preview")
    pre.add_argument("--folder", required=True)
    pre.add_argument("--profile-id")
    pre.add_argument("--plan")
    pre.add_argument("--script", help="A custom adapter (.py) to embed in the plan")
    con = sub.add_parser("connect")
    con.add_argument("--folder", required=True)
    con.add_argument("--plan")
    con.add_argument("--script", help="A custom adapter (.py) to embed in the plan")
    con.add_argument("--profile-id")
    con.add_argument("--title")
    con.add_argument("--parent")
    mil = sub.add_parser("milestone")
    mil.add_argument("action", choices=["list", "add", "done", "undo", "rename", "due", "delete"])
    mil.add_argument("--project", required=True)
    mil.add_argument("--title")
    mil.add_argument("--due", help="YYYY-MM-DD (empty clears with `due`)")
    mil.add_argument("--section", help="add: bind to this paper section id")
    mil.add_argument("--source", help="add: the paper source id (default: the project's paper)")
    mil.add_argument("--stage", choices=STAGES)
    mil.add_argument("--line", type=int, help="The milestone's line (from list)")
    mil.add_argument("--match", help="Part of the milestone's title (must fit exactly one)")
    raw = sub.add_parser("collector")
    raw.add_argument("rest", nargs=argparse.REMAINDER)
    return root


def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = parser().parse_args(argv)
    if args.command == "where":
        paths = app_paths()
        return {"ok": True, "configDir": paths["configDir"], "app": paths.get("cli.json"),
                "workspace": (paths.get("settings.json") or {}).get("workspace")}
    root = workspace_root(args.workspace)
    if args.command == "report":
        return report(root, args.project, args.limit, dt.date.today())
    if args.command == "brief":
        return brief(root)
    if args.command == "inspect":
        return {"ok": True, **collector(root, ["inspect", "--source", args.folder])}
    if args.command == "preview":
        plan = read_plan(args.plan, args.script)
        if not plan and not args.profile_id:
            raise Problem("Give --profile-id ID or --plan FILE")
        request = {"source": args.folder, "profileId": args.profile_id} if args.profile_id else {"source": args.folder, "profile": plan.get("profile") if isinstance(plan.get("profile"), dict) else plan}
        return {"ok": True, **collector(root, ["plan", "preview"], json.dumps(request))}
    if args.command == "connect":
        return connect(root, args)
    if args.command == "milestone":
        if args.action in ("add", "rename") and not args.title:
            raise Problem(f"milestone {args.action} needs --title")
        return milestone(root, args)
    rest = [item for item in args.rest if item != "--"]
    return {"ok": True, "result": collector(root, rest)}


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(argv)
    except Problem as error:
        result = {"ok": False, "error": str(error)}
    except (ValueError, OSError) as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
