"""Data-driven milestone templates appended idempotently to a project's Project.md.

Templates live next to the profile library:
``System/profiles/milestones/builtin/<id>.json`` (shipped) and
``Research Dashboard/System/profiles/milestones/user/<id>.json`` (vault).

* ``per-section`` templates need a collected paper snapshot; they add one task
  per section and stage with ``[paper::][chapter::][stage::]`` bindings.
* ``static`` templates add plain ``#milestone`` tasks.

Task lines match the Connector's ``milestoneTask`` format and escaping. Existing
tasks with the same binding (per-section) or the same title (static) are kept
and skipped, so applying a template twice changes nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from dashboard_core import atomic_text, output_path, read_json, validate_id
from project_catalog import PROJECT_TYPES, project_catalogs, project_paths
from layout import layout_for
from profiles import PROFILE_ROOT

BUILTIN_TEMPLATES = PROFILE_ROOT / "milestones" / "builtin"
USER_TEMPLATES = "Research Dashboard/System/profiles/milestones/user"
PAPER_STAGES = {"draft": "Draft", "figures": "Figures", "references": "References", "review": "Review", "complete": "Complete"}
MAX_TASKS = 400
MAX_TITLE = 300
TASK = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


def validate_template(template: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(template, dict):
        return ["Milestone template must be an object"]
    allowed = {"schemaVersion", "id", "title", "description", "mode", "appliesTo", "sections", "stages",
               "titleFormat", "groupBy", "tasks"}
    errors.extend(f"{key} is not a supported template field" for key in sorted(set(template) - allowed))
    if template.get("schemaVersion") != "1.0":
        errors.append("schemaVersion must be \"1.0\"")
    try:
        validate_id(template.get("id"), "id")
    except ValueError as error:
        errors.append(str(error))
    if not isinstance(template.get("title"), str) or not template["title"].strip():
        errors.append("title must be a nonempty string")
    applies = template.get("appliesTo", [])
    if not isinstance(applies, list) or any(item not in PROJECT_TYPES for item in applies):
        errors.append("appliesTo must list project types: " + ", ".join(PROJECT_TYPES))
    mode = template.get("mode")
    if mode == "per-section":
        stages = template.get("stages")
        if not isinstance(stages, list) or not stages or any(stage not in PAPER_STAGES for stage in stages) or len(set(stages)) != len(stages):
            errors.append("stages must be a nonempty list of: " + ", ".join(PAPER_STAGES))
        if template.get("sections", "leaf") not in {"leaf", "all", "top"}:
            errors.append("sections must be leaf, all or top")
        if template.get("groupBy", "root") not in {"root", "none"}:
            errors.append("groupBy must be root or none")
        title_format = template.get("titleFormat", "{section} — {stage}")
        if not isinstance(title_format, str) or "{section}" not in title_format:
            errors.append("titleFormat must contain {section}")
        else:
            try:
                title_format.format(section="s", stage="draft", Stage="Draft")
            except (KeyError, IndexError, ValueError):
                errors.append("titleFormat may use only {section}, {stage} and {Stage}")
    elif mode == "static":
        tasks = template.get("tasks")
        if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
            errors.append(f"tasks must be a nonempty list of up to {MAX_TASKS} items")
        else:
            for index, task in enumerate(tasks):
                title = task.get("title") if isinstance(task, dict) else None
                if (not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE
                        or re.search(r"[\x00-\x1f]", title) or set(task) - {"title", "when"}):
                    errors.append(f"tasks[{index}] must be {{title, when?}} with a one-line title")
    else:
        errors.append("mode must be per-section or static")
    return errors


def list_templates(vault: Path | None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    folders = [("builtin", BUILTIN_TEMPLATES)]
    if vault is not None:
        folders.append(("user", output_path(vault, layout_for(vault).milestone_templates_user())))
    for origin, folder in folders:
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                template = read_json(path)
                problems = validate_template(template)
                if problems:
                    raise ValueError("; ".join(problems))
                if template["id"] != path.stem or template["id"] in seen:
                    raise ValueError("Template filename must equal a unique id")
                seen.add(template["id"])
                entries.append({"origin": origin, "template": template})
            except (OSError, ValueError, TypeError) as error:
                errors.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})
    return entries, errors


def template_summary(entry: dict[str, Any]) -> dict[str, Any]:
    template = entry["template"]
    return {"id": template["id"], "title": template["title"], "description": template.get("description"),
            "mode": template["mode"], "appliesTo": template.get("appliesTo", []), "origin": entry["origin"],
            "requiresPaperSnapshot": template["mode"] == "per-section",
            "stages": template.get("stages"), "taskCount": len(template.get("tasks", [])) or None}


def find_template(vault: Path | None, template_id: str) -> dict[str, Any]:
    validate_id(template_id, "milestoneTemplate")
    entries, errors = list_templates(vault)
    for entry in entries:
        if entry["template"]["id"] == template_id:
            return entry["template"]
    broken = next((item["error"] for item in errors if Path(item["path"]).stem == template_id), None)
    raise ValueError(f"Milestone template {template_id!r} is invalid: {broken}" if broken
                     else f"Unknown milestone template: {template_id}")


def inline_template(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """A profile's inline ``milestoneTemplate`` list as a static template."""
    template = {"schemaVersion": "1.0", "id": "inline", "title": "Profile milestones", "mode": "static",
                "tasks": [{"title": item["title"]} for item in tasks]}
    problems = validate_template(template)
    if problems:
        raise ValueError("Invalid inline milestone template: " + "; ".join(problems))
    return template


def escape_title(title: str) -> str:
    """Mirror helpers.js milestoneTask: one line, 1-300 characters, Markdown escaped."""
    text = str(title or "").strip()
    if not text or len(text) > MAX_TITLE or re.search(r"[\r\n\x00-\x1f]", text):
        raise ValueError("Milestone titles must be one line of 1-300 characters")
    return re.sub(r"([\\`*_\[\]#<>])", r"\\\1", text)


def task_line(title: str, binding: dict[str, str] | None = None) -> str:
    fields = ""
    if binding:
        fields = f" [paper:: {binding['paper']}] [chapter:: {binding['chapter']}] [stage:: {binding['stage']}]"
    return f"- [ ] {escape_title(title)} #milestone{fields}"


def _field(text: str, name: str) -> str | None:
    match = re.search(r"\[" + name + r"::\s*([^\]]+)\]", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _label(text: str) -> str:
    text = re.sub(r"(^|\s)#milestone\b", " ", text)
    text = re.sub(r"\[(?:due|paper|chapter|stage|accepted_hash)::[^\]]*\]", "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _unescape(text: str) -> str:
    return re.sub(r"\\([\\`*_\[\]#<>])", r"\1", text)


def _plain_tex(title: str) -> str:
    """Section titles are literal TeX; drop escapes such as \\_ and ties (~)."""
    text = re.sub(r"\\([_&%#${}])", r"\1", str(title)).replace("~", " ")
    return " ".join(text.split())


def _plain(text: str) -> str:
    """Comparable title of an existing task: label without fields, unescaped."""
    return " ".join(_unescape(_label(text)).split())


def parse_note(lines: list[str]) -> dict[str, Any]:
    """Headings and milestone tasks outside frontmatter and fenced code."""
    headings: list[tuple[int, int, str]] = []
    tasks: list[tuple[int, str]] = []
    fence: str | None = None
    start = 0
    if lines and lines[0].lstrip("﻿").strip() == "---":
        for index in range(1, len(lines)):
            if re.match(r"^(---|\.\.\.)\s*$", lines[index]):
                start = index + 1
                break
    for index in range(start, len(lines)):
        line = lines[index]
        marker = FENCE.match(line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) and not marker.group(2).strip():
                fence = None
            continue
        if fence is not None:
            continue
        heading = HEADING.match(line)
        if heading:
            headings.append((index, len(heading.group(1)), heading.group(2).strip()))
            continue
        task = TASK.match(line)
        if task and re.search(r"(?:^|\s)#milestone(?:\s|$)", task.group(3)):
            tasks.append((index, task.group(3)))
    if fence is not None:
        raise ValueError("Close the unfinished code block in Project.md before applying a milestone template")
    return {"headings": headings, "tasks": tasks, "bodyStart": start}


def _paper_source(vault: Path, catalog: dict[str, Any], source_id: str | None) -> tuple[str, dict[str, Any]]:
    candidates = [source_id] if source_id else list(catalog["sourceIds"])
    if source_id and source_id not in catalog["sourceIds"]:
        raise ValueError(f"Source {source_id} does not belong to project {catalog['projectId']}")
    found: list[tuple[str, dict[str, Any]]] = []
    for identity in candidates:
        path = output_path(vault, layout_for(vault).state(validate_id(identity, 'sourceId')))
        if not path.is_file():
            continue
        state = read_json(path)
        paper = next((detail for detail in state.get("details", []) if detail.get("kind") == "paper"
                      and detail.get("schemaVersion") == 1), None)
        if paper:
            found.append((identity, paper))
    if not found:
        raise ValueError("This template needs a collected paper snapshot; connect a LaTeX source and collect once first")
    if len(found) > 1:
        raise ValueError("Several paper sources belong to this project; choose one with --source")
    return found[0]


def planned_tasks(vault: Path, catalog: dict[str, Any], template: dict[str, Any],
                  source_id: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """Return [{line, group, key}] in document order and the bound source ID."""
    if template["mode"] == "static":
        return [{"line": task_line(item["title"]), "group": None,
                 "key": ("label", " ".join(item["title"].split()))} for item in template["tasks"]], None
    identity, paper = _paper_source(vault, catalog, source_id)
    chapters = [item for item in paper["chapters"] if item.get("identityMethod") != "opening-prose"]
    by_id = {item["id"]: item for item in paper["chapters"]}
    parents = {item.get("parentId") for item in paper["chapters"] if item.get("parentId")}
    selection = template.get("sections", "leaf")
    if selection == "leaf":
        chapters = [item for item in chapters if item["id"] not in parents]
    elif selection == "top":
        chapters = [item for item in chapters if item.get("parentId") is None]
    title_format = template.get("titleFormat", "{section} — {stage}")
    planned: list[dict[str, Any]] = []
    for chapter in chapters:
        root = chapter
        guard = 0
        while root.get("parentId") in by_id and guard < 16:
            root, guard = by_id[root["parentId"]], guard + 1
        # A top-level leaf gets its own heading so it never reads as part of
        # the preceding chapter's group.
        group = _plain_tex(root.get("title") or "Untitled")[:200] if template.get("groupBy", "root") == "root" else None
        section = _plain_tex(chapter.get("title") or "Untitled section")
        for stage in template["stages"]:
            sample = title_format.format(section="", stage=stage, Stage=PAPER_STAGES[stage])
            room = MAX_TITLE - len(sample)
            label = section if len(section) <= room else section[:max(1, room - 1)].rstrip() + "…"
            title = title_format.format(section=label, stage=stage, Stage=PAPER_STAGES[stage])
            binding = {"paper": identity, "chapter": chapter["id"], "stage": stage}
            planned.append({"line": task_line(title, binding), "group": group,
                            "key": ("binding", identity, chapter["id"], stage)})
        if len(planned) > MAX_TASKS:
            raise ValueError(f"The template would add more than {MAX_TASKS} tasks; choose sections: top")
    return planned, identity


def _insert(lines: list[str], parsed: dict[str, Any], additions: list[dict[str, Any]]) -> list[str]:
    headings = parsed["headings"]
    section = next(((index, level) for index, level, text in headings if level <= 2 and text.lower() == "milestones"), None)
    if section is None:
        result = list(lines)
        while result and not result[-1].strip():
            result.pop()
        result += ["", "## Milestones"]
        lines, section_level = result, 2
        section_start, section_end = len(lines) - 1, len(lines)
    else:
        section_start, section_level = section
        section_end = next((index for index, level, _ in headings if index > section_start and level <= section_level), len(lines))
    inner = [(index, level, text) for index, level, text in headings if section_start < index < section_end]

    def last_content(begin: int, finish: int) -> int:
        position = begin
        for index in range(begin + 1, finish):
            if lines[index].strip():
                position = index
        return position

    operations: dict[int, list[str]] = {}
    appended: list[str] = []
    order: list[str | None] = []
    groups: dict[str | None, list[str]] = {}
    for item in additions:
        if item["group"] not in groups:
            groups[item["group"]] = []
            order.append(item["group"])
        groups[item["group"]].append(item["line"])
    for group in order:
        block = groups[group]
        heading = None
        if group is not None:
            heading = next(((index, level) for index, level, text in inner
                            if level > section_level and _unescape(text).lower() == group.lower()), None)
        if heading is not None:
            index, level = heading
            finish = next((other for other, other_level, _ in inner if other > index and other_level <= level), section_end)
            position = last_content(index, finish) + 1
            prefix = [""] if position == index + 1 else []
            operations.setdefault(position, []).extend(prefix + block)
        elif group is not None:
            appended += ["", "#" * min(section_level + 1, 6) + " " + group, ""] + block
        else:
            appended += block
    if appended:
        position = last_content(section_start, section_end) + 1
        previous = lines[position - 1] if position - 1 >= 0 else ""
        if appended[0] != "" and not TASK.match(previous):
            appended = [""] + appended
        operations.setdefault(position, []).extend(appended)
    result = list(lines)
    for position in sorted(operations, reverse=True):
        block = operations[position]
        if position < len(result) and result[position].strip():
            block = block + [""]
        result[position:position] = block
    return result


def apply_template(vault: Path, project_id: str, template: dict[str, Any], source_id: str | None = None,
                   dry_run: bool = False) -> dict[str, Any]:
    """Append missing template tasks to Projects/<id>/Project.md (idempotent)."""
    vault = vault.resolve()
    validate_id(project_id, "dashboardProjectId")
    problems = validate_template(template)
    if problems:
        raise ValueError("Invalid milestone template: " + "; ".join(problems))
    catalog = next((item for item in project_catalogs(vault) if item["projectId"] == project_id), None)
    if catalog is None:
        raise ValueError(f"Project does not exist: {project_id}")
    layout = layout_for(vault)
    note_relative = project_paths(project_id, layout)["projectNote"]
    note = output_path(vault, note_relative)
    original = note.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.replace("\r\n", "\n").split("\n")
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines.pop()
    parsed = parse_note(lines)
    existing_bindings = set()
    existing_labels = set()
    for _, text in parsed["tasks"]:
        paper, chapter, stage = _field(text, "paper"), _field(text, "chapter"), _field(text, "stage")
        if paper and chapter:
            existing_bindings.add(("binding", paper, chapter, stage or "draft"))
        existing_labels.add(("label", _plain(text)))
    planned, bound = planned_tasks(vault, catalog, template, source_id)
    additions = []
    skipped = 0
    for item in planned:
        if item["key"] in existing_bindings or item["key"] in existing_labels:
            skipped += 1
            continue
        existing_bindings.add(item["key"])
        existing_labels.add(item["key"])
        additions.append(item)
    result = {"projectId": project_id, "templateId": template["id"], "sourceId": bound,
              "projectNote": note_relative, "added": [item["line"] for item in additions],
              "addedCount": len(additions), "skipped": skipped, "dryRun": dry_run}
    if not additions or dry_run:
        return result
    updated = _insert(lines, parsed, additions)
    text = newline.join(updated) + newline
    # Optimistic concurrency: the Connector may edit tasks at the same time.
    if note.read_text(encoding="utf-8-sig") != original:
        raise ValueError(f"{Path(note_relative).name} changed while applying the template; retry")
    atomic_text(note, text)
    return result
