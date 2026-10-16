"""Custom adapters: Python written by an agent (or by hand) for folders the built-in adapters and declarative rules
cannot read, such as binary formats or values that need computing.

A profile carries the code inline: ``extract: {"script": {"language": "python", "code": "...", "options": {...},
"timeoutSeconds": 20}}``. The code defines ``collect(folder: pathlib.Path, options: dict) -> dict`` and returns
plain values (see CONTRACT); it is trusted when it passes two checks:

1. Static (``check_code``): it parses, defines ``collect``, imports only reading and parsing modules, and calls nothing
   that writes, deletes, starts programs, opens connections or evaluates code.
2. At run time (``script_runner.py``): a separate isolated Python process with an audit hook that refuses anything but
   reading inside the source folder, a time limit and a cap on the output size.

The returned values are checked and mapped onto the snapshot shape the other adapters produce.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .common import UTC, empty_result, iso, progress

RUNNER = Path(__file__).with_name("script_runner.py")
MAX_CODE = 64 * 1024
MAX_OUTPUT = 1024 * 1024
DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 60
MAX_METRICS = 50
MAX_ARTIFACTS = 200
MAX_FILES = 500
STATUSES = {"queued", "running", "completed", "failed", "unknown"}

CONTRACT = """def collect(folder: pathlib.Path, options: dict) -> dict:
    # Read files under `folder` (read-only) and return any of:
    return {
        "status": "running",                       # queued | running | completed | failed | unknown
        "progress": {"current": 34, "target": 50, "unit": "epochs"},
        "step": "epoch 34",                        # short text
        "cases": {"planned": 12, "completed": 7, "running": 2, "failed": 0},
        "metrics": [{"id": "best-loss", "label": "Best loss", "value": 0.21, "unit": None}],
        "artifacts": [{"path": "figures/loss.png", "type": "png"}],   # paths inside the folder
        "files": ["logs/train.log"],               # the files the values came from
        "warnings": ["anything the user should know"],
    }"""

# Modules for reading and parsing. Anything else (subprocess, socket, shutil, ctypes, ...) fails the static check.
ALLOWED_MODULES = {
    "__future__", "array", "base64", "binascii", "bisect", "bz2", "calendar", "collections", "colorsys", "configparser",
    "contextlib", "copy", "csv", "dataclasses", "datetime", "decimal", "enum", "fnmatch", "fractions", "functools", "glob",
    "gzip", "hashlib", "heapq", "html", "io", "itertools", "json", "lzma", "math", "numbers", "operator", "os", "pathlib",
    "pickletools", "plistlib", "pprint", "re", "statistics", "string", "struct", "tarfile", "textwrap", "time",
    "tomllib", "typing", "unicodedata", "uuid", "warnings", "xml", "zipfile", "zlib",
    # Optional scientific readers, used when the configured Python has them (the bundled one does not).
    "numpy", "h5py", "scipy", "pandas", "openpyxl", "netCDF4", "tables", "tensorboard", "yaml", "tifffile", "PIL",
}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint", "input", "globals", "vars", "setattr", "delattr",
                   "memoryview"}
# Methods that change files or the system, whatever object they are called on (common names such as str.replace or
# list.remove stay allowed; the run-time guard refuses os.replace, os.remove and the like).
FORBIDDEN_ATTRIBUTES = {
    "system", "popen", "spawnl", "spawnv", "spawnve", "spawnvp", "execv", "execve", "execl", "execvp", "startfile",
    "kill", "fork", "unlink", "rmdir", "removedirs", "rename", "renames", "mkdir", "makedirs", "chmod", "chown",
    "lchown", "truncate", "symlink", "symlink_to", "hardlink_to", "touch", "utime", "write_text", "write_bytes",
    "putenv", "unsetenv", "chdir", "rmtree", "copyfile", "copytree", "extractall", "urlopen", "load_module",
    "exec_module", "__subclasses__", "__globals__", "__builtins__", "__code__", "__closure__", "__getattribute__",
    "__dict__", "__import__", "__loader__", "__spec__", "__class__", "__bases__", "__mro__", "f_globals", "f_locals",
    "f_back", "gi_frame", "tb_frame",
}
# Calls that open files, including readers whose C libraries write without Python's open (HDF5, netCDF).
OPENERS = {"open", "File", "Dataset", "open_file", "NetCDFFile"}
MODE_TEXT = set("rwabxtU+")
WRITE_MODES = set("wax+")


def check_code(code: Any) -> list[str]:
    """Static check: the problems that make the code untrusted (empty when it may run)."""
    if not isinstance(code, str) or not code.strip():
        return ["script.code must be the adapter's Python source"]
    if len(code.encode("utf8")) > MAX_CODE:
        return [f"script.code is larger than {MAX_CODE // 1024} KB"]
    try:
        tree = ast.parse(code, "adapter.py")
    except SyntaxError as error:
        return [f"adapter.py line {error.lineno}: syntax error: {error.msg}"]
    errors: list[str] = []

    def problem(node: ast.AST, message: str) -> None:
        errors.append(f"adapter.py line {getattr(node, 'lineno', '?')}: {message}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    problem(node, f"importing {alias.name} is not allowed (adapters only read files)")
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module or node.module.split(".")[0] not in ALLOWED_MODULES:
                problem(node, f"importing from {node.module or '.'} is not allowed (adapters only read files)")
            elif node.module.split(".")[0] == "os" and any(alias.name in FORBIDDEN_ATTRIBUTES for alias in node.names):
                problem(node, "importing file-changing functions from os is not allowed")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                problem(node, f"{func.id}() is not allowed")
            if isinstance(func, ast.Name) and func.id == "getattr" and (len(node.args) < 2 or not isinstance(node.args[1], ast.Constant)
                                                                    or str(node.args[1].value) in FORBIDDEN_ATTRIBUTES or str(node.args[1].value).startswith("__")):
                problem(node, "getattr() needs a plain attribute name")
            if isinstance(func, (ast.Name, ast.Attribute)) and (func.id if isinstance(func, ast.Name) else func.attr) in OPENERS:
                keyword = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
                modes = [arg.value for arg in node.args[:2] if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                         and 0 < len(arg.value) <= 4 and set(arg.value) <= MODE_TEXT]
                if keyword is not None and not (isinstance(keyword, ast.Constant) and isinstance(keyword.value, str)):
                    problem(node, "the file mode must be written out ('r' or 'rb')")
                elif keyword is not None:
                    modes.append(keyword.value)
                if any(WRITE_MODES & set(mode) for mode in modes):
                    problem(node, "files may only be opened for reading (mode 'r' or 'rb')")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            problem(node, f".{node.attr} is not allowed (adapters only read files)")
        elif isinstance(node, ast.Name) and node.id in {"__builtins__", "__loader__", "__spec__"}:
            problem(node, f"{node.id} is not allowed")
    if not any(isinstance(node, ast.FunctionDef) and node.name == "collect" for node in tree.body):
        errors.append("adapter.py must define collect(folder, options) at the top level")
    return errors[:20]


def validate_script_extract(extract: Any, path: str = "extract") -> list[str]:
    if not isinstance(extract, dict) or set(extract) != {"script"}:
        return [f"{path} with a script contains only the script"]
    script = extract["script"]
    if not isinstance(script, dict):
        return [f"{path}.script must be an object"]
    errors = [f"{path}.script.{key} is not supported" for key in sorted(set(script) - {"language", "code", "options", "timeoutSeconds", "description"})]
    if script.get("language", "python") != "python":
        errors.append(f"{path}.script.language must be python")
    if "options" in script and not isinstance(script["options"], dict):
        errors.append(f"{path}.script.options must be an object")
    timeout = script.get("timeoutSeconds", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= MAX_TIMEOUT:
        errors.append(f"{path}.script.timeoutSeconds must be between 1 and {MAX_TIMEOUT}")
    if "description" in script and (not isinstance(script["description"], str) or len(script["description"]) > 1000):
        errors.append(f"{path}.script.description must be a string up to 1000 characters")
    errors.extend(check_code(script.get("code")))
    return errors


def ensure_valid_script(extract: Any) -> None:
    errors = validate_script_extract(extract)
    if errors:
        raise ValueError("Invalid custom adapter: " + "; ".join(errors[:5]))


def run(root: Path, script: dict[str, Any]) -> dict[str, Any]:
    """Run the adapter in a separate isolated Python process; its returned dict, or ValueError."""
    timeout = float(script.get("timeoutSeconds", DEFAULT_TIMEOUT))
    payload = json.dumps({"code": script["code"], "folder": str(root), "options": script.get("options") or {}})
    # The system variables Windows needs (without SYSTEMDRIVE, Windows components expand "%SystemDrive%" as a relative
    # folder name and create it in the working directory); nothing else from the collector's environment.
    keep = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE",
            "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "ALLUSERSPROFILE", "LANG", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env["PYTHONUTF8"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # A throwaway working directory: nothing the process does by accident lands in the source folder.
    with tempfile.TemporaryDirectory(prefix="ma-adapter-") as scratch:
        try:
            done = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(RUNNER)], input=payload, capture_output=True, text=True,
                                  encoding="utf8", timeout=timeout, cwd=scratch, env=env, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise ValueError(f"The custom adapter did not finish within {timeout:g} s") from None
    if len(done.stdout) > MAX_OUTPUT:
        raise ValueError("The custom adapter returned more than 1 MB")
    try:
        reply = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise ValueError(f"The custom adapter failed: {tail[0][:500]}") from None
    if not reply.get("ok"):
        raise ValueError(f"The custom adapter failed: {reply.get('error') or 'no result'}")
    if not isinstance(reply.get("result"), dict):
        raise ValueError("collect() must return a dict")
    return reply["result"]


def _inside(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve()
    return path if path == root or path.is_relative_to(root) else None


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def to_snapshot(root: Path, raw: dict[str, Any]) -> dict[str, Any]:
    """Map collect()'s plain values onto the snapshot shape (with _warnings, _metrics, _matchedFiles as declarative)."""
    result, warnings = empty_result(), [str(item)[:300] for item in raw.get("warnings", []) if isinstance(item, (str, int, float))][:50]
    status = raw.get("status")
    if status is not None:
        if status in STATUSES:
            result["runtime"]["status"] = status
        else:
            warnings.append(f"status {status!r} is not one of {', '.join(sorted(STATUSES))}")
    span = raw.get("progress")
    if isinstance(span, dict):
        unit = span.get("unit")
        progress(result, _number(span.get("current")), _number(span.get("target")), str(unit)[:32] if unit is not None else None)
    if raw.get("step") is not None:
        result["runtime"]["step"] = str(raw["step"])[:200]
    cases = raw.get("cases")
    if isinstance(cases, dict):
        counts = {key: int(value) if _number(value) is not None and value >= 0 else None for key, value in
                  ((key, cases.get(key)) for key in ("planned", "completed", "running", "failed"))}
        known = [counts[key] for key in ("completed", "running", "failed")]
        unknown = counts["planned"] - sum(known) if counts["planned"] is not None and None not in known else None
        result["cases"] = {**counts, "unknown": unknown if unknown is None or unknown >= 0 else None}
    metrics = []
    for item in raw.get("metrics", [])[:MAX_METRICS] if isinstance(raw.get("metrics"), list) else []:
        if not isinstance(item, dict) or not str(item.get("id") or item.get("label") or "").strip():
            warnings.append("A metric without id or label was skipped")
            continue
        value = item.get("value")
        value = value if value is None or isinstance(value, str) or _number(value) is not None else str(value)
        metric_id = str(item.get("id") or item.get("label"))[:64]
        metrics.append({"kind": "metric", "id": metric_id, "label": str(item.get("label") or metric_id)[:120],
                        "value": value[:200] if isinstance(value, str) else value,
                        "unit": None if item.get("unit") is None else str(item["unit"])[:32]})
    for item in raw.get("artifacts", [])[:MAX_ARTIFACTS] if isinstance(raw.get("artifacts"), list) else []:
        path = _inside(root, item.get("path") if isinstance(item, dict) else item)
        if path is None:
            warnings.append("An artifact outside the folder was skipped")
            continue
        kind = item.get("type") if isinstance(item, dict) else None
        result["artifacts"].append({"type": str(kind or path.suffix[1:].lower() or "file")[:32], "path": str(path), "verifiedAt": None, "runId": None})
    files = [path for value in (raw.get("files", []) if isinstance(raw.get("files"), list) else [])[:MAX_FILES] if (path := _inside(root, value))]
    stamps = [path.stat().st_mtime for path in files if path.exists()]
    result["sourceFiles"] = [str(path) for path in files]
    result["sourceModifiedAt"] = iso(dt.datetime.fromtimestamp(max(stamps), UTC)) if stamps else None
    result["details"].extend(metrics)
    produced = bool(metrics or result["artifacts"] or files or status is not None or isinstance(span, dict) or isinstance(cases, dict))
    result["_warnings"], result["_metrics"], result["_matchedFiles"] = warnings, metrics, len(files) or int(produced)
    return result


def extract_values(root: Path, extract: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Check and run the adapter once; a snapshot-shaped result like declarative.extract_values."""
    ensure_valid_script(extract)
    return to_snapshot(root.resolve(), run(root.resolve(), extract["script"]))


def collect(root: Path, options: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Collector entry point used by dashboard_core for ``adapter.engine == "script"``."""
    result = extract_values(root, options.get("extract", {}), now)
    warnings = result.pop("_warnings")
    result.pop("_metrics")
    if not result.pop("_matchedFiles"):
        raise FileNotFoundError("The custom adapter returned no values" + (f" ({warnings[0]})" if warnings else ""))
    result["details"].append({"kind": "extraction", "profileId": options.get("profileId"),
                              "profileVersion": options.get("profileVersion"), "warnings": warnings[:50]})
    result["details"].append({"kind": "boundary", "message": "Values come from a custom adapter (Python written for this "
                              "folder) that passed the automatic checks and ran read-only in a separate process."})
    result["_inputManifest"] = {}
    return result
