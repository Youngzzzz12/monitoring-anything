"""Agent providers for smart onboarding: list presets and ask an agent for a monitoring plan.

``plan providers`` lists editable provider presets (CLI agents and HTTP chat
APIs). ``plan propose`` builds a prompt (task, design rules, profile schema,
library, folder manifest), sends it to one provider, extracts and validates the
returned plan JSON, runs one repair round when needed and finishes with a dry
run (``plan preview``).

Safety: nothing from a plan is ever executed; the plan is data that is only
validated and previewed. CLI providers run with ``shell=False``, the source
folder as working directory, a timeout and an output cap. API keys are used
only in request headers, never written to the prompt, transcript or output.
"""

from __future__ import annotations

import copy
import datetime as dt
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from adapters.common import UTC, iso
from dashboard_core import atomic_json, output_path
from milestones import list_templates, template_summary
from onboarding import source_directory
from planning import compile_excludes, manifest, preview
from profiles import builtin_ids, engine as profile_engine, find_profile, load_library, validate_profile
from adapters.script import CONTRACT
from project_catalog import PROJECT_TYPES

RUNS_FOLDER = "Research Dashboard/System/data/agent-runs"


def _runs_folder(vault: Path) -> str:
    """Transcript folder of the workspace layout (vault or app workspace)."""
    from layout import layout_for
    return layout_for(vault).agent_runs()


SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "monitoring-profile.schema.json"
MAX_RUNS = 50
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_ARG_PROMPT = 30000
MAX_ARGS = 64
MAX_ARG_LENGTH = 4096
DEFAULT_MAX_TOKENS = 8192
REDACTED = "[REDACTED]"
PLACEHOLDERS = ("{prompt}", "{promptFile}", "{outputFile}", "{source}", "{model}")
BATCH_UNSAFE = re.compile(r'[%!^&|<>"\r\n]')
SECRET_PATTERNS = [re.compile(pattern) for pattern in (
    r"sk-ant-[A-Za-z0-9_\-]{8,}", r"sk-[A-Za-z0-9_\-]{16,}", r"AIza[0-9A-Za-z_\-]{20,}",
    r"(?i)(?<=bearer )[A-Za-z0-9._\-]{12,}", r"(?i)(?<=x-api-key: )\S{8,}")]
PROVIDER_KEYS = {"id", "title", "kind", "command", "args", "modelArgs", "baseUrl", "model", "agentic", "installed",
                 "notes", "protocol", "apiKey", "apiKeyEnv", "maxTokens", "temperature", "jsonMode", "path", "models",
                 "installUrl"}
PLAN_KEYS = {"schemaVersion", "profile", "profileId", "project", "milestones", "rationale", "warnings"}

PRESETS: list[dict[str, Any]] = [
    {"id": "claude-code", "title": "Claude Code", "kind": "cli", "command": "claude",
     "args": ["--print", "--input-format", "text", "--output-format", "json", "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
              "--permission-mode", "dontAsk", "--no-session-persistence", "--strict-mcp-config"],
     "modelArgs": ["--model", "{model}"], "model": "", "agentic": True,
     "models": ["sonnet", "opus", "haiku"], "installUrl": "https://docs.anthropic.com/en/docs/claude-code/setup",
     "notes": "Prompt on stdin; cwd = source folder. Only the read-only Read/Glob/Grep tools exist and are pre-approved; "
              "dontAsk denies everything else; no MCP servers, no session saved. Uses your Claude Code login."},
    {"id": "codex", "title": "Codex CLI", "kind": "cli", "command": "codex",
     "args": ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--color", "never",
              "--output-last-message", "{outputFile}"],
     "modelArgs": ["-m", "{model}"], "model": "", "agentic": True,
     "models": ["gpt-5.5", "gpt-5.4"], "installUrl": "https://github.com/openai/codex",
     "notes": "Prompt on stdin; cwd = source folder; read-only sandbox, no session files. The final message is read "
              "from --output-last-message. Uses your Codex login."},
    {"id": "gemini-cli", "title": "Gemini CLI", "kind": "cli", "command": "gemini",
     "args": ["--output-format", "json", "--approval-mode", "default"],
     "modelArgs": ["--model", "{model}"], "model": "", "agentic": True,
     "models": [], "installUrl": "https://github.com/google-gemini/gemini-cli",
     "notes": "Best effort (flags not verified on this machine). Prompt on stdin; cwd = source folder; tools that need "
              "approval are refused in headless mode. Uses your Gemini CLI login."},
    {"id": "openai-compatible", "title": "OpenAI-compatible API", "kind": "http", "protocol": "openai",
     "baseUrl": "http://localhost:11434/v1", "model": "", "agentic": False, "models": [], "installUrl": "https://ollama.com",
     "notes": "POST {baseUrl}/chat/completions. Works with OpenAI, Ollama (default URL), LM Studio "
              "(http://localhost:1234/v1), vLLM and similar. Set model; apiKey is optional for local servers. "
              "The model sees only the generated folder manifest."},
    {"id": "anthropic-api", "title": "Anthropic API", "kind": "http", "protocol": "anthropic",
     "baseUrl": "https://api.anthropic.com", "model": "", "agentic": False,
     "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"], "installUrl": "https://console.anthropic.com/settings/keys",
     "notes": "POST {baseUrl}/v1/messages (Messages API). Needs apiKey and a model ID. "
              "The model sees only the generated folder manifest."},
    {"id": "custom", "title": "Custom command", "kind": "cli", "command": "", "args": [], "modelArgs": [],
     "model": "", "agentic": False,
     "notes": "Any command that prints the plan JSON. Placeholders in args: {prompt} (text; not for .bat/.cmd), "
              "{promptFile}, {outputFile}, {source}, {model}. Without {prompt}/{promptFile} the prompt goes to stdin. "
              "Set agentic true if the tool can browse the working directory itself."},
]

SYSTEM_PROMPT = ("You design read-only monitoring profiles for a research dashboard: declarative rules, or a small "
                 "read-only Python adapter when rules cannot express what is needed. You never write, modify, move or "
                 "execute anything in the folder. Reply with exactly one JSON object.")

EXAMPLE_PLAN = {
    "schemaVersion": "1.0",
    "profile": {
        "schemaVersion": "1.0", "id": "matlab-sweep", "title": "MATLAB parameter sweep", "projectType": "simulation",
        "sourceKind": "isale", "origin": {"createdBy": "agent:example", "approvedAt": None, "version": 1},
        "detect": {"allOf": [{"glob": "sweep_config.json"}], "anyOf": [{"glob": "results/*.mat"}],
                   "noneOf": [{"glob": "main.tex"}], "threshold": 0.8},
        "extract": {
            "runtime": {"status": {"fromLog": "logs/*.log", "success": "^All cases finished", "failure": "^Error"},
                        "currentValue": {"regex": r"Step (?P<value>\d+) of \d+", "file": "logs/*.log"},
                        "targetValue": {"regex": r"Step \d+ of (\d+)", "file": "logs/*.log", "match": "first"},
                        "step": {"regex": r"Phase: (\w+)", "file": "logs/*.log", "type": "string"},
                        "unit": "steps"},
            "cases": {"planned": {"jsonPath": "sweep_config.json#$.cases.length"},
                      "completed": {"countGlob": "results/*.mat"}},
            "artifacts": [{"glob": "figures/*.pdf", "type": "pdf"}],
            "metrics": [{"id": "peak-stress", "label": "Peak stress", "unit": "MPa",
                         "value": {"csv": "results/summary.csv", "column": "stress", "aggregate": "max"}}]},
        "milestoneTemplate": "simulation-basic", "subprojectRole": "standalone"},
    "project": {"title": "MATLAB sweep", "type": "simulation", "parentSuggestion": None},
    "milestones": ["All planned cases finished", "Figures produced"],
    "rationale": "sweep_config.json lists the cases; each finished case writes results/*.mat; the log reports the step.",
    "warnings": []}

RULES = """\
Design rules (all mandatory):
1. Read-only. Do not create, edit, move or delete any file, and do not run programs, solvers, scripts or shell commands.
   You may look only at file and folder names and short excerpts of small text files.
2. Choose the simplest thing that works, in this order: an existing profile (top-level "profileId"); a declarative
   profile (rules 3-5); only when those primitives cannot express what is needed (binary or proprietary formats, values
   that need computing across files), a custom adapter (rule 8). Say in the rationale why a custom adapter was needed.
3. Detection (profile.detect) uses only rules {"glob": "...", "minCount"?: n} or
   {"contains": {"glob": "...", "regex": "...", "maxBytes"?: n}} in allOf / anyOf / noneOf (at most 20 rules), plus an
   optional threshold in (0, 1]. All allOf and at least one anyOf rule must match; any noneOf match excludes.
4. Extraction (profile.extract) may use only these primitives:
   - countGlob / glob: count or list files matching a relative glob;
   - regex: {"regex", "file": glob, "match"?: "last"|"first"|"count", "type"?: "number"|"integer"|"string"} - newest
     matching file; named group "value", else group 1, else the whole match;
   - jsonPath: "file.json#$.a.b[0].length" (only .key, [n], ['key'], .length);
   - csv: {"csv": file, "column": header-or-index, "aggregate": "last"|"first"|"max"|"min"|"sum"|"mean"|"count"};
   - fromLog: runtime status from the newest log with "success" / "failure" / optional "running" regexes;
     cases.fromLogs: one log per case;
   - artifacts: [{"glob", "type"?, "label"?, "max"?}]; metrics: [{"id", "label"?, "unit"?, "value": <primitive>}].
   Or, when the folder is a standard LaTeX / Abaqus / iSALE project that a built-in profile handles, reference it with
   top-level "profileId" (e.g. "latex-paper") instead of writing a new profile.
5. Globs are relative to the source folder ("**" spans folders); never use absolute paths or "..".
   Regexes must be simple (no nested quantifiers such as (a+)+).
6. Prefer evidence that changes while work progresses (logs, counters, result files). Never treat a binary file's
   existence as completion unless the folder's conventions clearly say so; leave values unknown rather than guess.
7. profile.id: a new lowercase id (a-z, 0-9, -, _), not equal to a built-in id. projectType is one of
   thesis, paper, simulation, experiment, other. sourceKind (display family) is latex, abaqus or isale.
8. Custom adapter: profile.extract = {"script": {"language": "python", "code": "<source>", "options"?: {...},
   "timeoutSeconds"?: 1-60}} (detect stays declarative). The code defines collect(folder, options) and returns plain
   values; it may only read files under folder (pathlib, os.scandir, open(..., "r"/"rb")), import only standard
   reading/parsing modules (json, csv, re, math, statistics, struct, zipfile, gzip, xml, datetime, pathlib, os, ...)
   or numpy/h5py/scipy/pandas/openpyxl when available, and must not write, delete, start programs, use the network,
   eval/exec or touch dunder internals: such code fails the automatic check and never runs. It runs in a separate
   process that refuses anything but reading inside the folder. Keep it short, bounded (read file tails, cap loops) and
   tolerant: missing files give warnings, not exceptions. Contract:
""" + CONTRACT + """
"""


# --------------------------------------------------------------------------- providers

def _which(command: str) -> str | None:
    if not command:
        return None
    path = Path(command)
    if path.is_absolute():
        return str(path) if path.is_file() else None
    return shutil.which(command)


def list_providers() -> dict[str, Any]:
    """Return the editable presets with PATH detection for CLI providers."""
    providers = []
    for preset in PRESETS:
        entry = copy.deepcopy(preset)
        if entry["kind"] == "cli":
            resolved = _which(entry["command"])
            entry["installed"] = (resolved is not None) if entry["command"] else None
            entry["path"] = resolved
        else:
            entry["installed"] = None
        providers.append(entry)
    return {"providers": providers}


def _preset(identity: str) -> dict[str, Any] | None:
    return next((copy.deepcopy(item) for item in PRESETS if item["id"] == identity), None)


def normalize_provider(value: Any) -> dict[str, Any]:
    """Merge a provider object with its preset (by id) and validate it; raise ValueError on bad input."""
    if not isinstance(value, dict):
        raise ValueError("provider must be an object (see plan providers)")
    identity = value.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", identity):
        raise ValueError("provider.id must be a short identifier")
    unknown = set(value) - PROVIDER_KEYS
    if unknown:
        raise ValueError("provider has unsupported keys: " + ", ".join(sorted(unknown)))
    provider = _preset(identity) or {"id": identity, "title": identity, "agentic": False, "model": ""}
    provider.update({key: item for key, item in value.items() if key not in ("installed", "path", "notes", "models", "installUrl")})
    kind = provider.get("kind")
    if kind not in ("cli", "http"):
        raise ValueError("provider.kind must be cli or http")
    if not isinstance(provider.get("agentic", False), bool):
        raise ValueError("provider.agentic must be true or false")
    model = provider.get("model") or ""
    if not isinstance(model, str) or len(model) > 200 or re.search(r"[\x00-\x1f]", model):
        raise ValueError("provider.model must be a short one-line string")
    provider["model"] = model.strip()
    for key in ("apiKey", "apiKeyEnv"):
        if key in provider and provider[key] is not None and (not isinstance(provider[key], str) or len(provider[key]) > 4096):
            raise ValueError(f"provider.{key} must be a string")
    if kind == "cli":
        command = provider.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 1024 or re.search(r"[\x00-\x1f]", command):
            raise ValueError("provider.command must name an executable (on PATH or an absolute path)")
        provider["command"] = command.strip()
        for key in ("args", "modelArgs"):
            items = provider.get(key, [])
            if (not isinstance(items, list) or len(items) > MAX_ARGS
                    or any(not isinstance(item, str) or len(item) > MAX_ARG_LENGTH or "\x00" in item for item in items)):
                raise ValueError(f"provider.{key} must be a list of up to {MAX_ARGS} strings")
            provider[key] = list(items)
        if sum(arg.count("{prompt}") + arg.count("{promptFile}") for arg in provider["args"]) > 1:
            raise ValueError("provider.args may contain {prompt} or {promptFile} only once")
    else:
        protocol = provider.get("protocol") or ("anthropic" if identity == "anthropic-api" else "openai")
        if protocol not in ("openai", "anthropic"):
            raise ValueError("provider.protocol must be openai or anthropic")
        provider["protocol"] = protocol
        base = provider.get("baseUrl") or ("https://api.anthropic.com" if protocol == "anthropic" else "")
        parsed = urllib.parse.urlparse(base) if isinstance(base, str) else None
        if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("provider.baseUrl must be an http(s) URL without query string")
        provider["baseUrl"] = base.rstrip("/")
        if not provider["model"]:
            raise ValueError("provider.model is required for HTTP providers")
        max_tokens = provider.get("maxTokens", DEFAULT_MAX_TOKENS)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 256 <= max_tokens <= 200000:
            raise ValueError("provider.maxTokens must be an integer between 256 and 200000")
        provider["maxTokens"] = max_tokens
        temperature = provider.get("temperature")
        if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                                        or not 0 <= temperature <= 2):
            raise ValueError("provider.temperature must be a number between 0 and 2, or null")
    return provider


def _api_key(provider: dict[str, Any]) -> str | None:
    if provider.get("apiKey"):
        return provider["apiKey"]
    if provider.get("apiKeyEnv"):
        return os.environ.get(provider["apiKeyEnv"]) or None
    return None


def public_provider(provider: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to log: key values removed."""
    result = {key: value for key, value in provider.items() if key not in ("apiKey",)}
    if provider.get("apiKey"):
        result["apiKey"] = REDACTED
    return result


# --------------------------------------------------------------------------- redaction

class Redactor:
    def __init__(self, secrets_: list[str]) -> None:
        self.secrets = sorted({item for item in secrets_ if isinstance(item, str) and len(item) >= 4}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, REDACTED)
        for pattern in SECRET_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {key: (REDACTED if key.lower() in ("apikey", "api_key", "authorization", "x-api-key") and item
                          else self.value(item)) for key, item in value.items()}
        return value


# --------------------------------------------------------------------------- prompt

def _library_context(vault: Path) -> dict[str, Any]:
    entries, _ = load_library(vault)
    profiles = []
    for entry in entries:
        profile = entry["profile"]
        item = {"id": profile["id"], "origin": entry["origin"], "title": profile["title"],
                "description": profile.get("description"), "projectType": profile["projectType"],
                "engine": profile_engine(profile),
                "detect": profile.get("detect"), "subprojectRole": profile.get("subprojectRole", "standalone"),
                "milestoneTemplate": profile.get("milestoneTemplate")}
        if entry["origin"] == "user" and len(json.dumps(profile.get("extract"))) <= 4000:
            item["extract"] = profile.get("extract")
        profiles.append(item)
    templates, _ = list_templates(vault)
    return {"profiles": profiles[:40],
            "milestoneTemplates": [{key: summary.get(key) for key in ("id", "title", "description", "mode", "appliesTo")}
                                   for summary in (template_summary(entry) for entry in templates)][:40]}


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_prompt(vault: Path, root: Path, folder: dict[str, Any], provider: dict[str, Any],
                 instructions: str | None = None, previous_plan: Any = None, exclude: list[str] | None = None) -> str:
    """Assemble the full single-message prompt. Never contains API keys."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    library = _library_context(vault)
    folder = dict(folder)
    folder["sourcePath"] = root.name  # the folder name is enough; do not send the local absolute path
    exclude = exclude or []
    sections = [
        "# Task",
        "Design a monitoring plan for one research folder so a dashboard can follow its progress. The dashboard "
        "detects the folder type with your profile's detect rules and reads values with its extract rules on every "
        "collection. Your answer is validated and dry-run before a person reviews it; nothing in it is executed.",
        "# " + RULES.strip(),
    ]
    if provider.get("agentic"):
        access = ("# Folder access\nYour working directory is the source folder. You may inspect it with read-only "
                  "tools (list files, glob, grep, read small text files) to confirm patterns. Read only file names and "
                  "short excerpts; do not open large or binary files; write nothing; run nothing.")
        if exclude:
            access += ("\nPrivacy: do NOT list, open or quote any path matching these excluded globs (a pattern "
                       "without '/' matches a file or folder name at any depth): " + ", ".join(exclude))
        sections.append(access)
    else:
        sections.append("# Folder access\nYou cannot browse the folder. Base the plan on the manifest below "
                        "(bounded tree, extension counts and short heads of small text files).")
    sections += [
        "# Output format\nReply with exactly one JSON object and nothing else (no prose, no Markdown fences):\n"
        '{"schemaVersion": "1.0", "profile": {<monitoring profile>} | omit when using "profileId", '
        '"profileId": "<built-in or library id>" (optional, instead of profile), '
        '"project": {"title": str, "type": "thesis|paper|simulation|experiment|other", "parentSuggestion": str|null}, '
        '"milestones": [str], "rationale": "short reason", "warnings": [str]}\n\nExample:\n'
        + json.dumps(EXAMPLE_PLAN, ensure_ascii=False, indent=1),
        "# Monitoring profile JSON schema\n" + schema.strip(),
        "# Existing profiles and milestone templates\nReuse a matching profile via profileId, or base a new one on "
        "them. Set profile.milestoneTemplate to one of these template ids when it fits.\n" + _compact(library),
        "# Folder manifest\n" + _compact(folder),
    ]
    if previous_plan is not None:
        sections.append("# Previous plan (revise it)\n" + json.dumps(previous_plan, ensure_ascii=False, indent=1))
    if instructions:
        sections.append("# User instructions\n" + instructions.strip())
    sections.append("Return the JSON plan now.")
    return "\n\n".join(sections) + "\n"


def repair_prompt(base: str, response: str, errors: list[str]) -> str:
    excerpt = response if len(response) <= 20000 else response[:20000] + "\n…(truncated)"
    return (base + "\n# Repair request\nYour previous answer was:\n" + excerpt +
            "\n\nIt was rejected for these reasons:\n" + "\n".join(f"- {item}" for item in errors[:40]) +
            "\n\nReturn one corrected, complete JSON plan only. Keep the same rules.\n")


# --------------------------------------------------------------------------- response parsing

FENCE = re.compile(r"```[ \t]*(?:json|JSON|jsonc)?[ \t]*\r?\n(.*?)```", re.DOTALL)


def _wrapped_text(value: Any) -> tuple[str | None, str | None]:
    """Return (inner text, error) for known provider wrappers, else (None, None)."""
    if isinstance(value, list):
        result = next((item for item in reversed(value) if isinstance(item, dict) and item.get("type") == "result"), None)
        return _wrapped_text(result) if result else (None, None)
    if not isinstance(value, dict) or "profile" in value or "detect" in value or "profileId" in value:
        return None, None
    if value.get("type") == "result" and ("result" in value or "is_error" in value):  # Claude Code --output-format json
        if value.get("is_error") or value.get("subtype") not in (None, "success"):
            return None, "Claude Code reported an error: " + str(value.get("result") or value.get("subtype"))[:500]
        return (value.get("result") if isinstance(value.get("result"), str) else None), None
    if isinstance(value.get("response"), str):  # Gemini CLI --output-format json
        return value["response"], None
    if isinstance(value.get("error"), (dict, str)) and not value.get("choices"):
        error = value["error"]
        return None, "Provider error: " + (error.get("message") if isinstance(error, dict) else error)[:500]
    choices = value.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):  # OpenAI chat completion
        message = choices[0].get("message") or {}
        return (message.get("content") if isinstance(message.get("content"), str) else None), None
    content = value.get("content")
    if isinstance(content, list) and value.get("type") == "message":  # Anthropic message
        return "".join(block.get("text", "") for block in content if isinstance(block, dict)), None
    return None, None


def _objects(text: str) -> list[Any]:
    """JSON objects found in text: whole document, fenced blocks, then embedded objects."""
    found: list[Any] = []
    stripped = text.strip()
    try:
        found.append(json.loads(stripped))
        return found
    except (json.JSONDecodeError, ValueError):
        pass
    for block in FENCE.findall(text):
        try:
            found.append(json.loads(block))
        except (json.JSONDecodeError, ValueError):
            continue
    decoder = json.JSONDecoder()
    index = text.find("{")
    scanned = 0
    while index != -1 and scanned < 2000:
        scanned += 1
        try:
            value, end = decoder.raw_decode(text, index)
            found.append(value)
            index = text.find("{", end)
        except (json.JSONDecodeError, ValueError):
            index = text.find("{", index + 1)
    return found


def _is_plan_like(value: Any) -> bool:
    return isinstance(value, dict) and any(key in value for key in ("profile", "profileId", "detect"))


def extract_plan(text: str, depth: int = 0) -> tuple[Any, str | None]:
    """Return (plan object or None, error). Tolerates fences, prose and provider wrappers."""
    if not isinstance(text, str) or not text.strip():
        return None, "The provider returned an empty response"
    fallback: Any = None
    for value in _objects(text):
        inner, error = _wrapped_text(value)
        if error:
            return None, error
        if inner is not None and depth < 3:
            plan, inner_error = extract_plan(inner, depth + 1)
            if plan is not None or inner_error:
                return plan, inner_error
            continue
        if _is_plan_like(value):
            return value, None
        if isinstance(value, dict) and fallback is None:
            fallback = value
    if fallback is not None:
        return fallback, None
    return None, "No JSON object was found in the provider response"


# --------------------------------------------------------------------------- validation

def validate_plan(vault: Path, plan: Any, provider_id: str) -> tuple[Any, list[str], list[str]]:
    """Return (normalized plan, errors, warnings)."""
    warnings: list[str] = []
    if not isinstance(plan, dict):
        return plan, ["The plan must be a JSON object"], warnings
    plan = copy.deepcopy(plan)
    if "profile" not in plan and "profileId" not in plan and "detect" in plan:
        plan = {"schemaVersion": "1.0", "profile": plan}
        warnings.append("The agent returned a bare profile; it was wrapped as a plan.")
    errors: list[str] = []
    for key in sorted(set(plan) - PLAN_KEYS):
        warnings.append(f"Plan field {key} is not part of the plan format and is ignored.")
    if plan.get("schemaVersion", "1.0") != "1.0":
        errors.append('plan.schemaVersion must be "1.0"')
    plan.setdefault("schemaVersion", "1.0")
    if "profile" in plan:
        profile = plan["profile"]
        if isinstance(profile, dict) and "origin" not in profile:
            profile["origin"] = {"createdBy": f"agent:{provider_id}"[:120], "approvedAt": None, "version": 1}
        profile_errors, profile_warnings = validate_profile(profile)
        errors += [f"profile: {item}" for item in profile_errors]
        warnings += profile_warnings
        if not profile_errors and profile["id"] in builtin_ids():
            builtin, _ = find_profile(None, profile["id"])
            if profile != builtin:
                errors.append(f"profile.id {profile['id']!r} is a built-in profile id; use \"profileId\": "
                              f"\"{profile['id']}\" to reuse it or choose a new id")
        if "profileId" in plan:
            warnings.append("Both profile and profileId were given; the inline profile is used.")
    elif "profileId" in plan:
        try:
            find_profile(vault, plan["profileId"])
        except (ValueError, TypeError) as error:
            errors.append(f"profileId: {error}")
    else:
        errors.append("The plan needs a profile object (or profileId of an existing profile)")
    project = plan.get("project")
    if project is not None:
        if not isinstance(project, dict):
            errors.append("project must be an object {title, type, parentSuggestion}")
        else:
            title = project.get("title")
            if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 200
                                      or re.search(r"[\x00-\x1f]", title)):
                errors.append("project.title must be a one-line string of 1-200 characters")
            if project.get("type") is not None and project["type"] not in PROJECT_TYPES:
                errors.append("project.type must be one of: " + ", ".join(PROJECT_TYPES))
            parent = project.get("parentSuggestion")
            if parent is not None and (not isinstance(parent, str) or len(parent) > 64):
                errors.append("project.parentSuggestion must be a project id string or null")
    milestones = plan.get("milestones")
    if milestones is not None and (not isinstance(milestones, list) or len(milestones) > 100
                                   or any(not isinstance(item, str) or len(item) > 300 for item in milestones)):
        errors.append("milestones must be a list of up to 100 short strings")
    if plan.get("rationale") is not None and (not isinstance(plan["rationale"], str) or len(plan["rationale"]) > 4000):
        errors.append("rationale must be a string up to 4000 characters")
    agent_warnings = plan.get("warnings")
    if agent_warnings is not None and (not isinstance(agent_warnings, list)
                                       or any(not isinstance(item, str) for item in agent_warnings)):
        errors.append("warnings must be a list of strings")
    return plan, errors, warnings


# --------------------------------------------------------------------------- transports

def _kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def _pump(stream: Any, sink: bytearray, cap: int, overflow: threading.Event) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            room = cap - len(sink)
            if room > 0:
                sink.extend(chunk[:room])
            if len(chunk) > max(room, 0):
                overflow.set()
    except (OSError, ValueError):
        pass


def run_cli(provider: dict[str, Any], prompt: str, root: Path, timeout: float,
            max_bytes: int = MAX_OUTPUT_BYTES) -> dict[str, Any]:
    """Run a CLI provider once. Returns a result record; never raises for provider failures."""
    record: dict[str, Any] = {"argv": None, "stdout": "", "stderr": "", "exitCode": None, "timedOut": False,
                              "responseText": "", "durationMs": 0, "error": None}
    executable = _which(provider["command"])
    if executable is None:
        record["error"] = (f"Provider command {provider['command']!r} was not found on PATH; install it or set an "
                           "absolute command path")
        return record
    batch = executable.lower().endswith((".bat", ".cmd"))
    work = Path(tempfile.mkdtemp(prefix="dashboard-agent-"))
    prompt_file = work / "prompt.txt"
    output_file = work / "output.txt"
    values = {"{promptFile}": str(prompt_file), "{outputFile}": str(output_file), "{source}": str(root),
              "{model}": provider.get("model", "")}
    try:
        uses_arg_prompt = any("{prompt}" in arg for arg in provider["args"])
        uses_prompt_file = any("{promptFile}" in arg for arg in provider["args"])
        if uses_arg_prompt and batch:
            raise ValueError("{prompt} cannot be passed as an argument to a .bat/.cmd command; use stdin or {promptFile}")
        if uses_arg_prompt and len(prompt) > MAX_ARG_PROMPT:
            raise ValueError(f"The prompt has {len(prompt)} characters, too long for a command-line argument; "
                             "use stdin or {promptFile}")
        if uses_prompt_file:
            prompt_file.write_text(prompt, encoding="utf-8")
        templates = list(provider["args"]) + (list(provider.get("modelArgs", [])) if provider.get("model") else [])
        args = []
        for template in templates:
            value = template
            for token, replacement in values.items():
                value = value.replace(token, replacement)
            value = value.replace("{prompt}", prompt)
            args.append(value)
        if batch:
            unsafe = [arg for arg in args if BATCH_UNSAFE.search(arg)]
            if unsafe:
                raise ValueError("Arguments for a .bat/.cmd command cannot contain % ! ^ & | < > \" or line breaks")
        argv = [executable] + args
        record["argv"] = [executable] + [arg if "{prompt}" not in template else "{prompt}"
                                         for arg, template in zip(args, templates)]
        stdin_payload = None if (uses_arg_prompt or uses_prompt_file) else prompt.encode("utf-8")
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            options["start_new_session"] = True
        environment = dict(os.environ, NO_COLOR="1")
        started = time.monotonic()
        process = subprocess.Popen(argv, cwd=str(root), shell=False, env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        stdout, stderr = bytearray(), bytearray()
        overflow, ignored = threading.Event(), threading.Event()
        readers = [threading.Thread(target=_pump, args=(process.stdout, stdout, max_bytes, overflow), daemon=True),
                   threading.Thread(target=_pump, args=(process.stderr, stderr, MAX_STDERR_BYTES, ignored), daemon=True)]
        for reader in readers:
            reader.start()

        def feed() -> None:
            try:
                if stdin_payload:
                    process.stdin.write(stdin_payload)
                process.stdin.close()
            except (OSError, ValueError):
                pass

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        deadline = started + timeout
        while process.poll() is None:
            if overflow.is_set():
                _kill_tree(process)
                record["error"] = f"Provider output exceeded {max_bytes} bytes and was stopped"
                break
            if time.monotonic() > deadline:
                _kill_tree(process)
                record["timedOut"] = True
                record["error"] = f"Provider timed out after {timeout:g} s and was stopped"
                break
            time.sleep(0.05)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
        for thread in readers + [writer]:
            thread.join(timeout=5)
        if overflow.is_set() and record["error"] is None:
            record["error"] = f"Provider output exceeded {max_bytes} bytes and was stopped"
        record["durationMs"] = int((time.monotonic() - started) * 1000)
        record["exitCode"] = process.returncode
        record["stdout"] = stdout.decode("utf-8", errors="replace")
        record["stderr"] = stderr.decode("utf-8", errors="replace")
        response = record["stdout"]
        if "{outputFile}" in " ".join(templates) and output_file.is_file():
            with output_file.open("rb") as stream:
                text = stream.read(max_bytes).decode("utf-8", errors="replace")
            if text.strip():
                response = text
        record["responseText"] = response
        if record["error"] is None and process.returncode not in (0, None) and not response.strip():
            tail = record["stderr"].strip()[-500:]
            record["error"] = f"Provider exited with code {process.returncode}" + (f": {tail}" if tail else "")
    except ValueError as error:
        record["error"] = str(error)
    except OSError as error:
        record["error"] = f"Provider could not be started: {type(error).__name__}: {error}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return record


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def http_post(url: str, headers: dict[str, str], body: bytes, timeout: float, max_bytes: int) -> tuple[int, bytes]:
    """POST and return (status, body). Loopback hosts bypass proxies. Raises OSError on transport failure."""
    handlers: list[Any] = []
    if _loopback(urllib.parse.urlparse(url).hostname):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(max_bytes + 1)
            status = response.status
    except urllib.error.HTTPError as error:
        payload = error.read(max_bytes + 1)
        status = error.code
    if len(payload) > max_bytes:
        raise OSError(f"Response exceeded {max_bytes} bytes")
    return status, payload


def run_http(provider: dict[str, Any], prompt: str, timeout: float, max_bytes: int = MAX_OUTPUT_BYTES,
             transport: Callable[..., tuple[int, bytes]] | None = None) -> dict[str, Any]:
    """Send one chat request. Returns a result record; never raises for provider failures."""
    transport = transport or http_post
    record: dict[str, Any] = {"url": None, "status": None, "stdout": "", "responseText": "", "durationMs": 0,
                              "timedOut": False, "error": None, "warnings": []}
    key = _api_key(provider)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if provider["protocol"] == "anthropic":
        if not key:
            record["error"] = "The Anthropic API needs an apiKey"
            return record
        base = provider["baseUrl"]
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        body: dict[str, Any] = {"model": provider["model"], "max_tokens": provider["maxTokens"], "system": SYSTEM_PROMPT,
                                "messages": [{"role": "user", "content": prompt}]}
    else:
        url = provider["baseUrl"] + "/chat/completions"
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": provider["model"], "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                                         {"role": "user", "content": prompt}],
                "max_tokens": provider["maxTokens"], "stream": False}
        if provider.get("jsonMode"):
            body["response_format"] = {"type": "json_object"}
    if provider.get("temperature") is not None:
        body["temperature"] = provider["temperature"]
    record["url"] = url
    started = time.monotonic()
    try:
        status, payload = transport(url, headers, json.dumps(body).encode("utf-8"), timeout, max_bytes)
    except TimeoutError:
        record["timedOut"] = True
        record["error"] = f"Provider timed out after {timeout:g} s"
        return record
    except (OSError, ValueError) as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, TimeoutError) or "timed out" in str(error).lower():
            record["timedOut"] = True
            record["error"] = f"Provider timed out after {timeout:g} s"
        else:
            record["error"] = f"HTTP request failed: {type(error).__name__}: {error}"
        return record
    finally:
        record["durationMs"] = int((time.monotonic() - started) * 1000)
    record["status"] = status
    text = payload.decode("utf-8", errors="replace")
    record["stdout"] = text
    if status >= 400:
        record["error"] = f"HTTP {status}: {text.strip()[:500]}"
        return record
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        record["error"] = "The provider did not return JSON"
        return record
    if provider["protocol"] == "anthropic":
        blocks = document.get("content") if isinstance(document, dict) else None
        record["responseText"] = "".join(block.get("text", "") for block in blocks or [] if isinstance(block, dict)
                                         and block.get("type") == "text")
        if isinstance(document, dict) and document.get("stop_reason") == "max_tokens":
            record["warnings"].append("The response stopped at maxTokens; the plan may be truncated.")
    else:
        try:
            choice = document["choices"][0]
            record["responseText"] = choice["message"]["content"] or ""
            if choice.get("finish_reason") == "length":
                record["warnings"].append("The response stopped at maxTokens; the plan may be truncated.")
        except (KeyError, IndexError, TypeError):
            record["error"] = "Unexpected chat completion response (no choices[0].message.content)"
    return record


# --------------------------------------------------------------------------- connection test and local servers

TEST_PROMPT = 'Connection test. Reply with exactly this JSON object and nothing else: {"ok": true}'
OK_REPLY = re.compile(r'\\?"ok\\?"\s*:\s*true')
LOCAL_SERVERS = [("ollama", "Ollama", "http://127.0.0.1:11434/v1"), ("lm-studio", "LM Studio", "http://127.0.0.1:1234/v1")]


def _version(executable: str, timeout: float = 20) -> tuple[str | None, str | None]:
    """First line of `<command> --version`, or an error (never raises)."""
    options: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    try:
        done = subprocess.run([executable, "--version"], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              shell=False, **options)
    except subprocess.TimeoutExpired:
        return None, f"`--version` did not answer within {timeout:g} s"
    except OSError as error:
        return None, f"Could not start it: {type(error).__name__}: {error}"
    text = (done.stdout or done.stderr or b"").decode("utf-8", errors="replace").strip()
    line = text.splitlines()[0].strip()[:120] if text else ""
    if done.returncode != 0 and not line:
        return None, f"`--version` exited with code {done.returncode}"
    return line or "installed", None


def test_provider(request: dict[str, Any], transport: Callable[..., tuple[int, bytes]] | None = None) -> dict[str, Any]:
    """Check that a provider answers: the CLI's version, then (prompt: true) one tiny non-writing request that must
    reply {"ok": true}. Raises ValueError only on bad input; provider failures come back as ok: false."""
    provider = normalize_provider(request.get("provider"))
    timeout = _bounded_int(request.get("timeoutSeconds"), 120, 5, 600, "timeoutSeconds")
    redactor = Redactor([_api_key(provider) or ""])
    result: dict[str, Any] = {"ok": False, "provider": public_provider(provider), "version": None, "durationMs": 0,
                              "response": None, "error": None, "prompted": bool(request.get("prompt"))}
    if provider["kind"] == "cli":
        executable = _which(provider["command"])
        if executable is None:
            result["error"] = f"{provider['command']} was not found on this computer. Install it, or set its full path."
            return result
        version, error = _version(executable)
        result["version"] = version
        if error:
            result["error"] = error
            return result
    if not request.get("prompt"):
        result["ok"] = provider["kind"] == "cli"
        if provider["kind"] == "http":
            result["error"] = "HTTP providers are checked with a test request (prompt: true)"
        return result
    if provider["kind"] == "cli":
        work = Path(tempfile.mkdtemp(prefix="dashboard-agent-test-"))
        try:
            record = run_cli(provider, TEST_PROMPT, work, timeout, max_bytes=256 * 1024)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    else:
        record = run_http({**provider, "maxTokens": min(provider["maxTokens"], 256)}, TEST_PROMPT, timeout, max_bytes=256 * 1024,
                          transport=transport)
    result["durationMs"] = record.get("durationMs", 0)
    text = record.get("responseText") or record.get("stdout") or ""
    result["response"] = redactor.text(text.strip()[:300]) or None
    if record.get("error"):
        result["error"] = redactor.text(str(record["error"]))
    elif OK_REPLY.search(text):
        result["ok"] = True
    else:
        result["error"] = "The agent answered, but not with the expected reply. Check the model or the arguments."
    return result


def detect_local_servers(timeout: float = 1.2, fetch: Callable[[str, float], tuple[int, bytes]] | None = None) -> dict[str, Any]:
    """OpenAI-compatible model servers running on this computer (Ollama, LM Studio) with their model ids."""
    def get(url: str, seconds: float) -> tuple[int, bytes]:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=seconds) as response:
            return response.status, response.read(512 * 1024)
    fetch = fetch or get
    servers = []
    for identity, title, base in LOCAL_SERVERS:
        try:
            status, payload = fetch(base + "/models", timeout)
            document = json.loads(payload.decode("utf-8", errors="replace")) if status < 400 else None
        except (OSError, ValueError, TimeoutError):
            continue
        if not isinstance(document, dict):
            continue
        models = [str(item.get("id")) for item in document.get("data", []) if isinstance(item, dict) and item.get("id")][:50]
        servers.append({"id": identity, "title": title, "baseUrl": base, "models": models})
    return {"servers": servers}


# --------------------------------------------------------------------------- transcript

def _write_transcript(vault: Path, run_id: str, transcript: dict[str, Any], redactor: Redactor) -> str | None:
    relative = f"{_runs_folder(vault)}/{run_id}.json"
    try:
        target = output_path(vault, relative)
        atomic_json(target, redactor.value(transcript))
        runs = sorted(target.parent.glob("*.json"), key=lambda path: path.name)
        for old in runs[:-MAX_RUNS]:
            old.unlink(missing_ok=True)
    except (OSError, ValueError):
        return None
    return relative


# --------------------------------------------------------------------------- propose

def _bounded_int(value: Any, default: int, low: int, high: int, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{label} must be a number between {low} and {high}")
    return value


def propose(vault: Path, request: dict[str, Any], transport: Callable[..., tuple[int, bytes]] | None = None,
            now: dt.datetime | None = None) -> dict[str, Any]:
    """Ask a provider for a plan, validate (one repair round), dry-run. Raises ValueError only on bad input."""
    now = now or dt.datetime.now(UTC)
    if not isinstance(request.get("source"), str):
        raise ValueError("Request needs source (an absolute folder path)")
    root = source_directory(request["source"])
    provider = normalize_provider(request.get("provider"))
    instructions = request.get("instructions")
    if instructions is not None and (not isinstance(instructions, str) or len(instructions) > 8000):
        raise ValueError("instructions must be a string up to 8000 characters")
    timeout = _bounded_int(request.get("timeoutSeconds"), DEFAULT_TIMEOUT, 1, MAX_TIMEOUT, "timeoutSeconds")
    privacy = request.get("privacy") or {}
    if not isinstance(privacy, dict) or set(privacy) - {"exclude", "excerpts"}:
        raise ValueError("privacy must be an object {exclude?: [globs], excerpts?: bool}")
    excludes = [value for value, _ in compile_excludes(privacy.get("exclude"))]
    if not isinstance(privacy.get("excerpts", True), bool):
        raise ValueError("privacy.excerpts must be true or false")
    previous_plan = request.get("previousPlan")
    redactor = Redactor([_api_key(provider) or ""])
    warnings: list[str] = []
    errors: list[str] = []
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + f"-{provider['id']}-{secrets.token_hex(3)}"
    transcript: dict[str, Any] = {"schemaVersion": "1.0", "runId": run_id, "startedAt": iso(now),
                                  "source": str(root), "provider": public_provider(provider),
                                  "request": {"instructions": instructions, "hasPreviousPlan": previous_plan is not None,
                                              "privacy": {"exclude": excludes, "excerpts": privacy.get("excerpts", True)},
                                              "timeoutSeconds": timeout},
                                  "attempts": []}
    folder = manifest(vault, root, excerpts=20 if privacy.get("excerpts", True) else 0, exclude=excludes)
    warnings += [f"Manifest: {item}" for item in folder.get("warnings", [])][:10]
    base_prompt = build_prompt(vault, root, folder, provider, instructions, previous_plan, excludes)

    def call(prompt: str, kind: str) -> dict[str, Any]:
        if provider["kind"] == "cli":
            record = run_cli(provider, prompt, root, timeout)
        else:
            record = run_http(provider, prompt, timeout, transport=transport)
        warnings.extend(record.pop("warnings", []))
        transcript["attempts"].append({"kind": kind, "prompt": prompt, **record})
        return record

    plan: Any = None
    validation = {"ok": False, "errors": [], "warnings": []}
    preview_result: dict[str, Any] | None = None
    repaired = False
    total_ms = 0
    prompt = base_prompt
    for attempt in ("initial", "repair"):
        record = call(prompt, attempt)
        total_ms += record["durationMs"]
        problems: list[str] = []
        if record["error"] and not record["responseText"].strip():
            errors.append(record["error"])
            break  # transport failure: a repair round would not help
        if record["error"]:
            warnings.append(record["error"])
        candidate, parse_error = extract_plan(record["responseText"])
        if parse_error:
            problems.append(parse_error)
            if candidate is None and parse_error.startswith(("Claude Code reported", "Provider error")):
                errors.append(parse_error)
                break
        else:
            plan, plan_errors, plan_warnings = validate_plan(vault, candidate, provider["id"])
            validation = {"ok": not plan_errors, "errors": plan_errors, "warnings": plan_warnings}
            problems += plan_errors
            if not plan_errors:
                request_preview = {"source": str(root)}
                if "profile" in plan:
                    request_preview["profile"] = plan["profile"]
                else:
                    request_preview["profileId"] = plan["profileId"]
                try:
                    preview_result = preview(vault, request_preview)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    preview_result = {"ok": False, "preview": None, "warnings": [], "errors": [f"{type(error).__name__}: {error}"]}
                if not preview_result.get("ok"):
                    problems += [f"dry run: {item}" for item in preview_result.get("errors", [])] or ["dry run failed"]
        if not problems:
            break
        if attempt == "initial":
            repaired = True
            prompt = repair_prompt(base_prompt, record["responseText"], problems)
            continue
        if parse_error:
            errors.append(parse_error)
    if plan is not None and not validation["ok"]:
        errors.append("The plan still failed validation after one repair round" if repaired else "The plan failed validation")
    elif preview_result is not None and not preview_result.get("ok"):
        errors.append("The dry run reported errors (see preview.errors)")
    ok = bool(validation["ok"] and preview_result is not None and preview_result.get("ok"))
    result = {"ok": ok, "plan": plan, "validation": validation, "preview": preview_result, "repaired": repaired,
              "provider": {"id": provider["id"], "kind": provider["kind"], "model": provider.get("model") or None,
                           "durationMs": total_ms, "attempts": len(transcript["attempts"])},
              "transcriptPath": None, "warnings": list(dict.fromkeys(warnings)), "errors": list(dict.fromkeys(errors))}
    transcript["finishedAt"] = iso(dt.datetime.now(UTC))
    transcript["result"] = {key: result[key] for key in ("ok", "validation", "repaired", "warnings", "errors")}
    result["transcriptPath"] = _write_transcript(vault, run_id, transcript, redactor)
    if result["transcriptPath"] is None:
        result["warnings"].append("The run transcript could not be written.")
    return redactor.value(result)
