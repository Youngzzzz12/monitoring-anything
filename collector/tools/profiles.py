"""Monitoring-profile library: load, validate, score, save and delete profiles.

Built-in profiles ship next to the tools in ``System/profiles/builtin/`` and
delegate extraction to the existing adapters. User profiles live in the vault at
``Research Dashboard/System/profiles/user/<id>.json`` and normally use the
declarative collector. Detection is deterministic and bounded; no AI is used.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any

from adapters.common import UTC, iso
from adapters.declarative import validate_extract
from adapters.script import validate_script_extract
from adapters.scan import Budget, Scanner, ScanLimit, normalize_pattern, validate_regex
from dashboard_core import KINDS, atomic_json, output_path, read_json, validate_id
from layout import layout_for
from project_catalog import PROJECT_TYPES

PROFILE_ROOT = Path(__file__).resolve().parents[1] / "profiles"
BUILTIN_FOLDER = PROFILE_ROOT / "builtin"
USER_FOLDER = "Research Dashboard/System/profiles/user"
HISTORY_FOLDER = USER_FOLDER + "/.history"


def user_folder(vault: Path) -> str:
    """Workspace-relative folder of saved (user) profiles for this layout."""
    return layout_for(vault).profiles_user()


def history_folder(vault: Path) -> str:
    return layout_for(vault).profiles_history()
MAX_PROFILE_BYTES = 256 * 1024
MAX_USER_PROFILES = 200
MAX_DETECT_RULES = 20
DEFAULT_CONTAINS_BYTES = 64 * 1024
MAX_CONTAINS_BYTES = 256 * 1024
MAX_CONTAINS_FILES = 50
DEFAULT_THRESHOLD = 0.7
ROLES = ("standalone", "part")
PROFILE_KEYS = {"schemaVersion", "id", "title", "description", "projectType", "sourceKind", "origin",
                "detect", "extract", "milestoneTemplate", "subprojectRole"}
DEFAULT_SOURCE_KIND = "abaqus"


def _rule_errors(rule: Any, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(rule, dict):
        return [f"{path} must be an object"]
    forms = [key for key in ("glob", "contains") if key in rule]
    if len(forms) != 1:
        return [f"{path} must use exactly one of glob or contains"]
    unknown = set(rule) - {"glob", "contains", "minCount", "description"}
    if unknown:
        errors.append(f"{path} has unsupported keys: {', '.join(sorted(unknown))}")
    if "description" in rule and (not isinstance(rule["description"], str) or len(rule["description"]) > 160):
        errors.append(f"{path}.description must be a string up to 160 characters")
    if "minCount" in rule and (not isinstance(rule["minCount"], int) or isinstance(rule["minCount"], bool)
                               or not 1 <= rule["minCount"] <= 10000):
        errors.append(f"{path}.minCount must be an integer between 1 and 10000")
    try:
        if "glob" in rule:
            normalize_pattern(rule["glob"], f"{path}.glob")
        else:
            contains = rule["contains"]
            if not isinstance(contains, dict):
                return errors + [f"{path}.contains must be an object with glob and regex"]
            for key in set(contains) - {"glob", "regex", "maxBytes"}:
                errors.append(f"{path}.contains.{key} is not supported")
            normalize_pattern(contains.get("glob"), f"{path}.contains.glob")
            validate_regex(contains.get("regex"), f"{path}.contains.regex")
            if "maxBytes" in contains and (not isinstance(contains["maxBytes"], int) or isinstance(contains["maxBytes"], bool)
                                           or not 1 <= contains["maxBytes"] <= MAX_CONTAINS_BYTES):
                errors.append(f"{path}.contains.maxBytes must be between 1 and {MAX_CONTAINS_BYTES}")
    except ValueError as error:
        errors.append(str(error))
    return errors


def validate_profile(profile: Any) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). A profile is usable only when errors is empty."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(profile, dict):
        return ["Profile must be a JSON object"], warnings
    for key in sorted(set(profile) - PROFILE_KEYS):
        errors.append(f"{key} is not a supported profile field")
    if profile.get("schemaVersion") != "1.0":
        errors.append("schemaVersion must be \"1.0\"")
    try:
        validate_id(profile.get("id"), "id")
    except ValueError as error:
        errors.append(str(error))
    title = profile.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or re.search(r"[\x00-\x1f]", title):
        errors.append("title must be a one-line string of 1-200 characters")
    if "description" in profile and (not isinstance(profile["description"], str) or len(profile["description"]) > 1000):
        errors.append("description must be a string up to 1000 characters")
    if profile.get("projectType") not in PROJECT_TYPES:
        errors.append("projectType must be one of: " + ", ".join(PROJECT_TYPES))
    if "sourceKind" in profile and profile["sourceKind"] not in KINDS:
        errors.append("sourceKind must be one of: " + ", ".join(sorted(KINDS)))
    origin = profile.get("origin", {})
    if not isinstance(origin, dict):
        errors.append("origin must be an object")
    else:
        for key in set(origin) - {"createdBy", "approvedAt", "version", "builtin", "savedAt", "basedOn"}:
            errors.append(f"origin.{key} is not supported")
        if "version" in origin and (not isinstance(origin["version"], int) or isinstance(origin["version"], bool) or origin["version"] < 1):
            errors.append("origin.version must be a positive integer")
        for key in ("createdBy", "basedOn"):
            if key in origin and origin[key] is not None and (not isinstance(origin[key], str) or len(origin[key]) > 120):
                errors.append(f"origin.{key} must be a short string")
        for key in ("approvedAt", "savedAt"):
            if origin.get(key) is not None:
                try:
                    value = dt.datetime.fromisoformat(str(origin[key]).replace("Z", "+00:00"))
                    if value.tzinfo is None:
                        raise ValueError
                except ValueError:
                    errors.append(f"origin.{key} must be an ISO timestamp with timezone, or null")
    detect = profile.get("detect")
    if not isinstance(detect, dict):
        errors.append("detect must be an object with allOf/anyOf/noneOf rules")
    else:
        for key in set(detect) - {"allOf", "anyOf", "noneOf", "threshold"}:
            errors.append(f"detect.{key} is not supported")
        count = 0
        for group in ("allOf", "anyOf", "noneOf"):
            rules = detect.get(group, [])
            if not isinstance(rules, list):
                errors.append(f"detect.{group} must be a list")
                continue
            count += len(rules)
            for index, rule in enumerate(rules):
                errors.extend(_rule_errors(rule, f"detect.{group}[{index}]"))
        if not detect.get("allOf") and not detect.get("anyOf"):
            errors.append("detect needs at least one allOf or anyOf rule")
        if count > MAX_DETECT_RULES:
            errors.append(f"detect has {count} rules; the limit is {MAX_DETECT_RULES}")
        threshold = detect.get("threshold", DEFAULT_THRESHOLD)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 < threshold <= 1:
            errors.append("detect.threshold must be a number in (0, 1]")
    extract = profile.get("extract")
    if isinstance(extract, dict) and "adapter" in extract:
        if set(extract) != {"adapter"}:
            errors.append("extract with an adapter reference cannot contain other rules")
        if extract["adapter"] not in KINDS:
            errors.append("extract.adapter must be one of: " + ", ".join(sorted(KINDS)))
        elif profile.get("sourceKind", extract["adapter"]) != extract["adapter"]:
            errors.append("sourceKind must equal extract.adapter for adapter-backed profiles")
    elif isinstance(extract, dict) and "script" in extract:
        errors.extend(validate_script_extract(extract))
    else:
        errors.extend(validate_extract(extract))
    template = profile.get("milestoneTemplate")
    if template is not None:
        if isinstance(template, str):
            try:
                validate_id(template, "milestoneTemplate")
            except ValueError as error:
                errors.append(str(error))
        elif isinstance(template, list):
            if len(template) > 50:
                errors.append("milestoneTemplate may list up to 50 tasks")
            for index, item in enumerate(template):
                if (not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip()
                        or len(item["title"]) > 300 or re.search(r"[\x00-\x1f]", item["title"])
                        or set(item) - {"title", "when"} or "when" in item and not isinstance(item["when"], str)):
                    errors.append(f"milestoneTemplate[{index}] must be {{title, when?}} with a one-line title")
        else:
            errors.append("milestoneTemplate must be a template ID, an inline task list, or null")
    if profile.get("subprojectRole", "standalone") not in ROLES:
        errors.append("subprojectRole must be standalone or part")
    if not errors and "adapter" not in (extract or {}) and "sourceKind" not in profile:
        warnings.append(f"sourceKind is not set; the dashboard will display this source as {DEFAULT_SOURCE_KIND}.")
    return errors, warnings


def ensure_valid(profile: Any) -> dict[str, Any]:
    errors, _ = validate_profile(profile)
    if errors:
        raise ValueError("Invalid monitoring profile: " + "; ".join(errors[:10]))
    return profile


def engine(profile: dict[str, Any]) -> str:
    """adapter (a built-in adapter), script (a custom adapter written for the folder) or declarative (rules)."""
    extract = profile.get("extract", {})
    return "adapter" if "adapter" in extract else "script" if "script" in extract else "declarative"


def source_kind(profile: dict[str, Any]) -> str:
    """Dashboard source kind (display family) recorded in the source config."""
    extract = profile.get("extract", {})
    return extract.get("adapter") or profile.get("sourceKind") or DEFAULT_SOURCE_KIND


def unwrap(value: Any) -> Any:
    """Accept a bare profile or an agent plan ``{"profile": {...}}``."""
    if isinstance(value, dict) and "profile" in value and "detect" not in value:
        return value["profile"]
    return value


def _read_profile(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_PROFILE_BYTES:
        raise ValueError("Profile file exceeds 256 KiB")
    profile = read_json(path)
    ensure_valid(profile)
    if profile["id"] != path.stem:
        raise ValueError("Profile filename must equal its id")
    return profile


def load_library(vault: Path | None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return ([{origin, path, profile}], errors). Built-ins win on ID conflicts."""
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    folders: list[tuple[str, Path]] = [("builtin", BUILTIN_FOLDER)]
    if vault is not None:
        folders.append(("user", output_path(vault, user_folder(vault))))
    for origin, folder in folders:
        paths = sorted(folder.glob("*.json")) if folder.is_dir() else []
        if origin == "user" and len(paths) > MAX_USER_PROFILES:
            errors.append({"path": str(folder), "error": f"Only the first {MAX_USER_PROFILES} user profiles were loaded"})
            paths = paths[:MAX_USER_PROFILES]
        for path in paths:
            try:
                profile = _read_profile(path)
                if profile["id"] in seen:
                    raise ValueError("A built-in profile already uses this id")
                seen.add(profile["id"])
                entries.append({"origin": origin, "path": path, "profile": profile})
            except (OSError, ValueError, TypeError, KeyError) as error:
                errors.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})
    return entries, errors


def find_profile(vault: Path | None, profile_id: str) -> tuple[dict[str, Any], str]:
    validate_id(profile_id, "profileId")
    entries, errors = load_library(vault)
    for entry in entries:
        if entry["profile"]["id"] == profile_id:
            return entry["profile"], entry["origin"]
    broken = next((item["error"] for item in errors if Path(item["path"]).stem == profile_id), None)
    raise ValueError(f"Monitoring profile {profile_id!r} is invalid: {broken}" if broken
                     else f"Unknown monitoring profile: {profile_id}")


def summary(entry: dict[str, Any], vault: Path | None = None) -> dict[str, Any]:
    profile = entry["profile"]
    return {"id": profile["id"], "title": profile["title"], "description": profile.get("description"),
            "projectType": profile["projectType"], "sourceKind": source_kind(profile), "engine": engine(profile),
            "origin": entry["origin"], "version": profile.get("origin", {}).get("version", 1),
            "createdBy": profile.get("origin", {}).get("createdBy"),
            "subprojectRole": profile.get("subprojectRole", "standalone"),
            "milestoneTemplate": profile.get("milestoneTemplate"),
            "path": (f"{user_folder(vault)}/{profile['id']}.json" if vault is not None and entry["origin"] == "user"
                     else f"profiles/builtin/{profile['id']}.json" if vault is not None
                     else f"Research Dashboard/System/profiles/{entry['origin']}/{profile['id']}.json")}


# --------------------------------------------------------------------------- detection

def _describe(group: str, rule: dict[str, Any]) -> str:
    if rule.get("description"):
        return rule["description"]
    if "glob" in rule:
        minimum = rule.get("minCount", 1)
        return f"files matching {rule['glob']}" + (f" (at least {minimum})" if minimum > 1 else "")
    return f"a file matching {rule['contains']['glob']} contains /{rule['contains']['regex']}/"


def _evaluate_rule(rule: dict[str, Any], scanner: Scanner) -> tuple[bool, str]:
    if "glob" in rule:
        found = scanner.glob(rule["glob"])
        return len(found) >= rule.get("minCount", 1), f"{len(found)} file(s)"
    contains = rule["contains"]
    pattern = validate_regex(contains["regex"])
    found = scanner.glob(contains["glob"])
    hits = 0
    for relative in found[:MAX_CONTAINS_FILES]:
        try:
            text, _ = scanner.read_text(relative, contains.get("maxBytes", DEFAULT_CONTAINS_BYTES), tail=False)
        except (OSError, ValueError):
            continue
        if pattern.search(text):
            hits += 1
            if hits >= rule.get("minCount", 1):
                break
    if len(found) > MAX_CONTAINS_FILES and hits < rule.get("minCount", 1):
        scanner.budget.warn(f"Only the first {MAX_CONTAINS_FILES} files matching {contains['glob']} were opened.")
    return hits >= rule.get("minCount", 1), f"{hits} matching file(s) of {len(found)}"


def score(profile: dict[str, Any], scanner: Scanner) -> dict[str, Any]:
    """Deterministic confidence in [0, 1] with the rules that matched or missed.

    All allOf rules and at least one anyOf rule (when listed) are required. A
    profile that satisfies both scores 0.7 plus up to 0.3 for the share of anyOf
    rules matched (1.0 without anyOf). Otherwise the score is at most 0.6, in
    proportion to the satisfied rule groups. Any noneOf match scores 0.
    """
    detect = profile["detect"]
    matched: list[str] = []
    missed: list[str] = []
    excluded: list[str] = []
    results: dict[str, list[bool]] = {}
    for group in ("allOf", "anyOf", "noneOf"):
        results[group] = []
        for rule in detect.get(group, []):
            try:
                ok, evidence = _evaluate_rule(rule, scanner)
            except (OSError, ValueError) as error:
                ok, evidence = False, f"not evaluated: {error}"
            results[group].append(ok)
            text = f"{group}: {_describe(group, rule)} — {evidence}"
            if group == "noneOf":
                (excluded if ok else matched).append(text if ok else f"noneOf: no {_describe(group, rule)}")
            else:
                (matched if ok else missed).append(text)
    all_ok = all(results["allOf"])
    any_ok = not results["anyOf"] or any(results["anyOf"])
    if excluded:
        confidence = 0.0
    elif all_ok and any_ok:
        confidence = 0.7 + 0.3 * (sum(results["anyOf"]) / len(results["anyOf"]) if results["anyOf"] else 1.0)
    else:
        units = [*results["allOf"], *([any(results["anyOf"])] if results["anyOf"] else [])]
        confidence = 0.6 * (sum(units) / len(units)) if units else 0.0
    threshold = detect.get("threshold", DEFAULT_THRESHOLD)
    return {"confidence": round(confidence, 3), "threshold": threshold, "meetsThreshold": confidence >= threshold,
            "matched": matched, "missed": missed, "excluded": excluded}


def detect(root: Path, vault: Path | None, legacy: list[dict[str, Any]] | None = None,
           include_partial: bool = False, budget: Budget | None = None) -> dict[str, Any]:
    """Score every library profile against one folder (read-only)."""
    entries, errors = load_library(vault)
    scanner = Scanner(root, budget or Budget(max_bytes=8 * 1024 * 1024, seconds=8.0))
    candidates = []
    for entry in entries:
        profile = entry["profile"]
        try:
            result = score(profile, scanner)
        except ScanLimit as error:
            scanner.budget.warn(str(error))
            result = {"confidence": 0.0, "threshold": profile["detect"].get("threshold", DEFAULT_THRESHOLD),
                      "meetsThreshold": False, "matched": [], "missed": [f"not evaluated: {error}"], "excluded": []}
        inspected: list[str] = []
        if engine(profile) == "adapter" and legacy is not None:
            kind = profile["extract"]["adapter"]
            inspected = [item["id"] for item in legacy if item.get("kind") == kind]
            # The existing inspector produces the exact adapter options that
            # connect requires, so it confirms or caps adapter-backed profiles.
            if inspected:
                result["confidence"] = max(result["confidence"], 0.9)
                result["matched"].append(f"existing {kind} inspector found {len(inspected)} connectable entr{'y' if len(inspected) == 1 else 'ies'}")
            else:
                result["confidence"] = min(result["confidence"], 0.5)
                result["missed"].append(f"existing {kind} inspector found no connectable entry")
            result["meetsThreshold"] = result["confidence"] >= result["threshold"]
        if result["confidence"] <= 0 and not include_partial:
            continue
        candidates.append({"profileId": profile["id"], "title": profile["title"], "projectType": profile["projectType"],
                           "sourceKind": source_kind(profile), "engine": engine(profile), "origin": entry["origin"],
                           "confidence": result["confidence"], "threshold": result["threshold"],
                           "meetsThreshold": result["meetsThreshold"], "matched": result["matched"],
                           "missed": result["missed"], "excluded": result["excluded"],
                           "subprojectRole": profile.get("subprojectRole", "standalone"),
                           "milestoneTemplate": profile.get("milestoneTemplate"),
                           "inspectedProfileIds": inspected})
    candidates.sort(key=lambda item: (-item["confidence"], item["origin"] != "user", item["profileId"]))
    return {"candidates": candidates, "libraryErrors": errors, "warnings": list(scanner.budget.warnings)}


# --------------------------------------------------------------------------- management

def read_profile_stdin(stream: Any) -> Any:
    payload = stream.read(MAX_PROFILE_BYTES + 1)
    if len(payload.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError("Profile JSON exceeds the 256 KiB input limit")
    return json.loads(payload)


def builtin_ids() -> set[str]:
    return {path.stem for path in BUILTIN_FOLDER.glob("*.json")} if BUILTIN_FOLDER.is_dir() else set()


def save_profile(vault: Path, profile: Any, now: dt.datetime | None = None) -> dict[str, Any]:
    """Write a user profile; overwriting archives the previous version first."""
    profile = unwrap(profile)
    ensure_valid(profile)
    identity = profile["id"]
    if identity in builtin_ids():
        raise ValueError(f"{identity!r} is a built-in profile; save a copy under a new id")
    now = now or dt.datetime.now(UTC)
    target = output_path(vault, f"{user_folder(vault)}/{identity}.json")
    record = json.loads(json.dumps(profile))
    origin = dict(record.get("origin") or {})
    origin.pop("builtin", None)
    archived = None
    previous_version = 0
    if target.exists():
        previous = read_json(target)
        previous_version = int((previous.get("origin") or {}).get("version", 1)) if isinstance(previous.get("origin"), dict) else 1
        if previous == record:
            return {"saved": identity, "path": f"{user_folder(vault)}/{identity}.json", "version": previous_version,
                    "previousVersionPath": None, "unchanged": True}
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        archive = output_path(vault, f"{history_folder(vault)}/{identity}.v{previous_version}.{stamp}.json")
        atomic_json(archive, previous, exclusive=True)
        archived = archive.relative_to(vault.resolve()).as_posix()
    origin["version"] = max(previous_version + 1, int(origin.get("version", 1))) if previous_version else int(origin.get("version", 1))
    origin["savedAt"] = iso(now)
    record["origin"] = origin
    ensure_valid(record)
    atomic_json(target, record)
    return {"saved": identity, "path": f"{user_folder(vault)}/{identity}.json", "version": origin["version"],
            "previousVersionPath": archived, "unchanged": False}


def delete_profile(vault: Path, identity: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """Remove a user profile recoverably (moved into the .history folder)."""
    validate_id(identity, "profileId")
    if identity in builtin_ids():
        raise ValueError(f"{identity!r} is a built-in profile and cannot be deleted")
    target = output_path(vault, f"{user_folder(vault)}/{identity}.json")
    if not target.is_file():
        raise FileNotFoundError(f"No user profile named {identity!r}")
    stamp = (now or dt.datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    archive = output_path(vault, f"{history_folder(vault)}/{identity}.deleted.{stamp}.json")
    archive.parent.mkdir(parents=True, exist_ok=True)
    target.replace(archive)
    return {"deleted": identity, "archivedTo": archive.relative_to(vault.resolve()).as_posix()}

