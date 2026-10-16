"""Generic declarative collector: bounded extraction primitives, no code execution.

A declarative ``extract`` block (from a monitoring profile) maps files below a
source folder into the existing snapshot fields: ``runtime``, ``cases``,
``artifacts`` and ``details``. Supported primitives are deliberately small so
every rule can be validated and dry-run:

* ``countGlob`` / ``glob``  - number of matching files
* ``regex``                 - first/last/count match over size-capped text
* ``jsonPath``              - dotted/array path subset (``$.a.b[0].length``)
* ``csv``                   - one column with last/first/max/min/sum/mean/count
* ``fromLog`` / ``fromLogs``- success/failure/running patterns -> status
* ``artifacts``             - file globs listed as unverified artifacts

Nothing is executed, written or followed outside the source folder.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from .common import MAX_LOG_BYTES, UTC, classify, empty_result, iso, latest_run_text, progress
from .scan import Budget, Scanner, ScanLimit, normalize_pattern, validate_regex

ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VALUE_FORMS = {
    "countGlob": {"countGlob"},
    "glob": {"glob"},
    "regex": {"regex", "file", "group", "match", "type", "maxBytes", "tail"},
    "jsonPath": {"jsonPath", "file", "type"},
    "csv": {"csv", "column", "aggregate", "delimiter", "files", "type", "maxBytes"},
    "docx": {"docx", "measure", "type"},
}
DOCX_MEASURES = {"words", "headings", "comments", "revisions"}
MAX_DOCX_XML = 32 * 1024 * 1024
LOG_KEYS = {"fromLog", "glob", "success", "failure", "running", "runStart", "activeWindowSeconds", "maxBytes", "maxFiles"}
AGGREGATES = {"last", "first", "max", "min", "sum", "mean", "count"}
TYPES = {"number", "integer", "string"}
HARD_LIMITS = {"maxBytes": 16 * 1024 * 1024, "seconds": 20.0, "maxEntries": 50000}
DEFAULT_LIMITS = {"maxBytes": 8 * 1024 * 1024, "seconds": 10.0, "maxEntries": 20000}
MAX_RULES = 100
MAX_ARTIFACTS = 200
MAX_ARTIFACTS_PER_RULE = 100
MAX_CASE_FILES = 500
MAX_CASE_DETAILS = 200
MAX_METRICS = 50
JSON_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(-?\d+)\]|\[(?:'([^']*)'|\"([^\"]*)\")\]")


# --------------------------------------------------------------------------- validation

def _number_ok(value: Any, minimum: float = 0) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= minimum


def _glob_error(errors: list[str], path: str, value: Any) -> None:
    try:
        normalize_pattern(value, path)
    except ValueError as error:
        errors.append(str(error))


def _regex_error(errors: list[str], path: str, value: Any) -> None:
    try:
        validate_regex(value, path)
    except ValueError as error:
        errors.append(str(error))


def _bytes_error(errors: list[str], path: str, spec: dict[str, Any]) -> None:
    if "maxBytes" in spec and (not isinstance(spec["maxBytes"], int) or isinstance(spec["maxBytes"], bool)
                               or not 1 <= spec["maxBytes"] <= MAX_LOG_BYTES):
        errors.append(f"{path}.maxBytes must be an integer between 1 and {MAX_LOG_BYTES}")


def parse_json_path(expression: str) -> tuple[str | None, list[str | int]]:
    """Split ``file#$.a[0].b`` (file optional) into (file, tokens)."""
    file = None
    if "#" in expression:
        file, expression = expression.split("#", 1)
    expression = expression.strip()
    if not expression.startswith("$") or len(expression) > 200:
        raise ValueError("jsonPath must start with $ and stay under 200 characters, e.g. $.cases.length")
    tokens: list[str | int] = []
    position = 1
    while position < len(expression):
        match = JSON_TOKEN.match(expression, position)
        if not match:
            raise ValueError(f"Unsupported jsonPath syntax near {expression[position:position + 20]!r}; "
                             "use .key, [index], ['key'] and .length only")
        name, index, quoted, double = match.groups()
        tokens.append(int(index) if index is not None else name if name is not None else quoted if quoted is not None else double)
        position = match.end()
    return (file.strip() if file is not None else None), tokens


def validate_value(spec: Any, path: str, errors: list[str], allow_literal: bool = True,
                   default_type: str = "number") -> None:
    if _number_ok(spec, -math.inf) and allow_literal:
        return
    if isinstance(spec, str) and allow_literal and default_type == "string":
        return
    if not isinstance(spec, dict):
        errors.append(f"{path} must be a value rule object" + (" or a number" if allow_literal else ""))
        return
    forms = [form for form in VALUE_FORMS if form in spec]
    if len(forms) != 1:
        errors.append(f"{path} must use exactly one of: {', '.join(VALUE_FORMS)}")
        return
    form = forms[0]
    unknown = set(spec) - VALUE_FORMS[form]
    if unknown:
        errors.append(f"{path} has unsupported keys for {form}: {', '.join(sorted(unknown))}")
    if "type" in spec and spec["type"] not in TYPES:
        errors.append(f"{path}.type must be one of: {', '.join(sorted(TYPES))}")
    if form in {"countGlob", "glob"}:
        _glob_error(errors, f"{path}.{form}", spec[form])
    elif form == "regex":
        _regex_error(errors, f"{path}.regex", spec["regex"])
        if "file" not in spec:
            errors.append(f"{path}.file (a glob; the newest match is read) is required for regex")
        else:
            _glob_error(errors, f"{path}.file", spec["file"])
        if "group" in spec and not (isinstance(spec["group"], str) and spec["group"] or
                                    isinstance(spec["group"], int) and not isinstance(spec["group"], bool) and spec["group"] >= 0):
            errors.append(f"{path}.group must be a group name or a nonnegative index")
        if "match" in spec and spec["match"] not in {"last", "first", "count"}:
            errors.append(f"{path}.match must be last, first or count")
        if "tail" in spec and not isinstance(spec["tail"], bool):
            errors.append(f"{path}.tail must be true or false")
        _bytes_error(errors, path, spec)
        if isinstance(spec.get("regex"), str) and "group" in spec:
            try:
                compiled = re.compile(spec["regex"])
                group = spec["group"]
                if isinstance(group, str) and group not in compiled.groupindex or isinstance(group, int) and group > compiled.groups:
                    errors.append(f"{path}.group {group!r} is not defined in the regex")
            except re.error:
                pass
    elif form == "jsonPath":
        if not isinstance(spec["jsonPath"], str):
            errors.append(f"{path}.jsonPath must be a string such as config.json#$.cases.length")
        else:
            try:
                file, _ = parse_json_path(spec["jsonPath"])
                target = spec.get("file", file)
                if target is None:
                    errors.append(f"{path} needs a file: use 'file.json#$.path' or a separate file glob")
                elif file is not None and "file" in spec:
                    errors.append(f"{path} names its file twice; use either 'file.json#$...' or file")
                else:
                    _glob_error(errors, f"{path}.file", target)
            except ValueError as error:
                errors.append(f"{path}.jsonPath: {error}")
    elif form == "docx":
        _glob_error(errors, f"{path}.docx", spec["docx"])
        if spec.get("measure", "words") not in DOCX_MEASURES:
            errors.append(f"{path}.measure must be one of: {', '.join(sorted(DOCX_MEASURES))}")
    elif form == "csv":
        _glob_error(errors, f"{path}.csv", spec["csv"])
        column = spec.get("column")
        if not (isinstance(column, str) and column or isinstance(column, int) and not isinstance(column, bool) and column >= 0):
            errors.append(f"{path}.column must be a header name or a zero-based index")
        if spec.get("aggregate", "last") not in AGGREGATES:
            errors.append(f"{path}.aggregate must be one of: {', '.join(sorted(AGGREGATES))}")
        if "delimiter" in spec and (not isinstance(spec["delimiter"], str) or len(spec["delimiter"]) != 1):
            errors.append(f"{path}.delimiter must be a single character")
        if spec.get("files", "newest") not in {"newest", "all"}:
            errors.append(f"{path}.files must be newest or all")
        _bytes_error(errors, path, spec)


def _validate_log(spec: Any, path: str, errors: list[str], key: str) -> None:
    if not isinstance(spec, dict):
        errors.append(f"{path} must be an object")
        return
    unknown = set(spec) - LOG_KEYS
    if unknown:
        errors.append(f"{path} has unsupported keys: {', '.join(sorted(unknown))}")
    target = spec.get(key)
    if target is None:
        errors.append(f"{path}.{key} (a glob) is required")
    else:
        _glob_error(errors, f"{path}.{key}", target)
    if "success" not in spec and "failure" not in spec:
        errors.append(f"{path} needs a success and/or failure pattern")
    for field in ("success", "failure", "running", "runStart"):
        if field in spec:
            _regex_error(errors, f"{path}.{field}", spec[field])
    if "activeWindowSeconds" in spec and not _number_ok(spec["activeWindowSeconds"]):
        errors.append(f"{path}.activeWindowSeconds must be a nonnegative number")
    if "maxFiles" in spec and (not isinstance(spec["maxFiles"], int) or isinstance(spec["maxFiles"], bool)
                               or not 1 <= spec["maxFiles"] <= MAX_CASE_FILES):
        errors.append(f"{path}.maxFiles must be an integer between 1 and {MAX_CASE_FILES}")
    _bytes_error(errors, path, spec)


def validate_extract(extract: Any, path: str = "extract") -> list[str]:
    """Return human-readable errors for a declarative extract block (empty = valid)."""
    errors: list[str] = []
    if not isinstance(extract, dict):
        return [f"{path} must be an object"]
    unknown = set(extract) - {"runtime", "cases", "artifacts", "metrics", "limits"}
    if unknown:
        errors.append(f"{path} has unsupported keys: {', '.join(sorted(unknown))}")
    if not any(key in extract for key in ("runtime", "cases", "artifacts", "metrics")):
        errors.append(f"{path} must define at least one of runtime, cases, artifacts or metrics")
    rules = 0
    runtime = extract.get("runtime", {})
    if not isinstance(runtime, dict):
        errors.append(f"{path}.runtime must be an object")
        runtime = {}
    for key in set(runtime) - {"status", "currentValue", "targetValue", "step", "unit"}:
        errors.append(f"{path}.runtime.{key} is not supported")
    if "status" in runtime:
        rules += 1
        _validate_log(runtime["status"], f"{path}.runtime.status", errors, "fromLog")
    for key in ("currentValue", "targetValue", "step"):
        if key in runtime:
            rules += 1
            validate_value(runtime[key], f"{path}.runtime.{key}", errors, default_type="string" if key == "step" else "number")
    if "unit" in runtime and runtime["unit"] is not None and (not isinstance(runtime["unit"], str) or len(runtime["unit"]) > 32):
        errors.append(f"{path}.runtime.unit must be a short string (up to 32 characters) or null")
    cases = extract.get("cases", {})
    if not isinstance(cases, dict):
        errors.append(f"{path}.cases must be an object")
        cases = {}
    for key in set(cases) - {"planned", "completed", "running", "failed", "fromLogs"}:
        errors.append(f"{path}.cases.{key} is not supported")
    for key in ("planned", "completed", "running", "failed"):
        if key in cases:
            rules += 1
            validate_value(cases[key], f"{path}.cases.{key}", errors)
    if "fromLogs" in cases:
        rules += 1
        _validate_log(cases["fromLogs"], f"{path}.cases.fromLogs", errors, "glob")
    artifacts = extract.get("artifacts", [])
    if not isinstance(artifacts, list):
        errors.append(f"{path}.artifacts must be a list")
        artifacts = []
    for index, item in enumerate(artifacts):
        rules += 1
        where = f"{path}.artifacts[{index}]"
        if not isinstance(item, dict) or "glob" not in item:
            errors.append(f"{where} must be an object with a glob")
            continue
        for key in set(item) - {"glob", "type", "max", "label"}:
            errors.append(f"{where}.{key} is not supported")
        _glob_error(errors, f"{where}.glob", item["glob"])
        if "type" in item and (not isinstance(item["type"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", item["type"])):
            errors.append(f"{where}.type must be a short word such as pdf or figure")
        if "label" in item and (not isinstance(item["label"], str) or len(item["label"]) > 120):
            errors.append(f"{where}.label must be a string up to 120 characters")
        if "max" in item and (not isinstance(item["max"], int) or isinstance(item["max"], bool)
                              or not 1 <= item["max"] <= MAX_ARTIFACTS_PER_RULE):
            errors.append(f"{where}.max must be an integer between 1 and {MAX_ARTIFACTS_PER_RULE}")
    metrics = extract.get("metrics", [])
    if not isinstance(metrics, list) or len(metrics) > MAX_METRICS:
        errors.append(f"{path}.metrics must be a list of up to {MAX_METRICS} items")
        metrics = []
    seen: set[str] = set()
    for index, item in enumerate(metrics):
        rules += 1
        where = f"{path}.metrics[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        for key in set(item) - {"id", "label", "value", "unit"}:
            errors.append(f"{where}.{key} is not supported")
        if not isinstance(item.get("id"), str) or not ID.fullmatch(item["id"]) or item["id"] in seen:
            errors.append(f"{where}.id must be a unique safe lowercase ID")
        else:
            seen.add(item["id"])
        if "label" in item and (not isinstance(item["label"], str) or len(item["label"]) > 120):
            errors.append(f"{where}.label must be a string up to 120 characters")
        if "unit" in item and item["unit"] is not None and (not isinstance(item["unit"], str) or len(item["unit"]) > 32):
            errors.append(f"{where}.unit must be a short string")
        if "value" not in item:
            errors.append(f"{where}.value is required")
        else:
            validate_value(item["value"], f"{where}.value", errors, allow_literal=False)
    limits = extract.get("limits", {})
    if not isinstance(limits, dict):
        errors.append(f"{path}.limits must be an object")
    else:
        for key, value in limits.items():
            if key not in HARD_LIMITS:
                errors.append(f"{path}.limits.{key} is not supported")
            elif not _number_ok(value, 1) or value > HARD_LIMITS[key]:
                errors.append(f"{path}.limits.{key} must be between 1 and {HARD_LIMITS[key]:g}")
    if rules > MAX_RULES:
        errors.append(f"{path} defines {rules} rules; the limit is {MAX_RULES}")
    return errors


def ensure_valid_extract(extract: Any) -> None:
    errors = validate_extract(extract)
    if errors:
        raise ValueError("Invalid declarative extract: " + "; ".join(errors[:10]))


# --------------------------------------------------------------------------- evaluation

class _Context:
    def __init__(self, root: Path, extract: dict[str, Any], now: dt.datetime) -> None:
        limits = {**DEFAULT_LIMITS, **extract.get("limits", {})}
        self.budget = Budget(max_entries=int(limits["maxEntries"]), max_bytes=int(limits["maxBytes"]),
                             seconds=float(limits["seconds"]))
        self.scanner = Scanner(root, self.budget)
        self.now = now
        self.sources: set[str] = set()
        self.matched: set[str] = set()

    def warn(self, message: str) -> None:
        self.budget.warn(message)

    def files(self, pattern: str) -> list[str]:
        found = self.scanner.glob(pattern)
        self.matched.update(found)
        return found

    def newest(self, pattern: str, label: str) -> str | None:
        found = self.files(pattern)
        if not found:
            self.warn(f"{label}: no file matches {pattern}")
            return None
        return self.scanner.newest(found)

    def text(self, relative: str, max_bytes: int, tail: bool) -> str:
        text, truncated = self.scanner.read_text(relative, max_bytes, tail)
        self.sources.add(relative)
        if truncated:
            self.warn(f"Only the {'last' if tail else 'first'} {max_bytes} bytes of {relative} were read.")
        return text


def _coerce(value: Any, kind: str, label: str, context: _Context) -> Any:
    if value is None:
        return None
    if kind == "string":
        if isinstance(value, (dict, list)):
            context.warn(f"{label}: expected a scalar, found {type(value).__name__}")
            return None
        return str(value)[:200]
    try:
        number = float(value.strip().replace(",", "")) if isinstance(value, str) else value
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError
    except (ValueError, AttributeError):
        context.warn(f"{label}: {str(value)[:60]!r} is not a finite number")
        return None
    if kind == "integer":
        if float(number) != int(number):
            context.warn(f"{label}: {number} is not an integer")
            return None
        return int(number)
    return int(number) if isinstance(number, int) or float(number).is_integer() and abs(number) < 2 ** 53 else float(number)


def _json_value(document: Any, tokens: list[str | int]) -> Any:
    value = document
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(value, list) or not -len(value) <= token < len(value):
                return None
            value = value[token]
        elif token == "length" and not (isinstance(value, dict) and "length" in value):
            if not isinstance(value, (list, dict, str)):
                return None
            value = len(value)
        else:
            if not isinstance(value, dict) or token not in value:
                return None
            value = value[token]
    return value


def evaluate(spec: Any, label: str, context: _Context, default_type: str = "number") -> Any:
    """Evaluate one value rule; missing evidence yields None plus a warning."""
    if not isinstance(spec, dict):
        return spec
    kind = spec.get("type", default_type)
    if "countGlob" in spec or "glob" in spec:
        return len(context.files(spec.get("countGlob", spec.get("glob"))))
    if "regex" in spec:
        relative = context.newest(spec["file"], label)
        if relative is None:
            return None
        text = context.text(relative, spec.get("maxBytes", MAX_LOG_BYTES), spec.get("tail", True))
        pattern = validate_regex(spec["regex"], label)
        mode = spec.get("match", "last")
        if mode == "count":
            return sum(1 for _ in pattern.finditer(text))
        matches = list(pattern.finditer(text))
        if not matches:
            context.warn(f"{label}: the pattern did not match {relative}")
            return None
        match = matches[-1] if mode == "last" else matches[0]
        group = spec.get("group")
        if group is None:
            group = "value" if "value" in pattern.groupindex else 1 if pattern.groups else 0
        return _coerce(match.group(group), kind, label, context)
    if "jsonPath" in spec:
        file, tokens = parse_json_path(spec["jsonPath"])
        relative = context.newest(spec.get("file", file), label)
        if relative is None:
            return None
        try:
            document = json.loads(context.text(relative, MAX_LOG_BYTES, False))
        except ValueError:
            context.warn(f"{label}: {relative} is not valid JSON (or exceeds the read limit)")
            return None
        value = _json_value(document, tokens)
        if value is None:
            context.warn(f"{label}: {spec['jsonPath']} was not found in {relative}")
        return _coerce(value, kind, label, context)
    if "docx" in spec:
        return _docx_value(spec, label, context)
    if "csv" in spec:
        found = context.files(spec["csv"])
        if not found:
            context.warn(f"{label}: no file matches {spec['csv']}")
            return None
        selected = found if spec.get("files", "newest") == "all" else [context.scanner.newest(found)]
        column, aggregate = spec["column"], spec.get("aggregate", "last")
        cells: list[str] = []
        for relative in selected[:200]:
            text = context.text(relative, spec.get("maxBytes", MAX_LOG_BYTES), False)
            reader = csv.reader(io.StringIO(text), delimiter=spec.get("delimiter", ","))
            rows = [row for row in reader if any(cell.strip() for cell in row)]
            if not rows:
                continue
            index = column
            if isinstance(column, str):
                header = [cell.strip().lower() for cell in rows[0]]
                if column.strip().lower() not in header:
                    context.warn(f"{label}: column {column!r} is missing in {relative}")
                    continue
                index = header.index(column.strip().lower())
                rows = rows[1:]
            cells.extend(row[index].strip() for row in rows if len(row) > index and row[index].strip())
        if aggregate == "count":
            return len(cells)
        if not cells:
            context.warn(f"{label}: no values found in column {column!r}")
            return None
        if aggregate in {"last", "first"}:
            return _coerce(cells[-1] if aggregate == "last" else cells[0], kind, label, context)
        numbers = [value for value in (_coerce(cell, "number", label, context) for cell in cells) if value is not None]
        if not numbers:
            return None
        result = {"max": max, "min": min, "sum": sum, "mean": lambda items: sum(items) / len(items)}[aggregate](numbers)
        return _coerce(result, kind if kind != "string" else "number", label, context)
    return None


DOCX_PARAGRAPH = re.compile(r"<w:p[ >].*?</w:p>", re.DOTALL)
DOCX_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>")
DOCX_HEADING = re.compile(r'<w:pStyle w:val="(?:Heading|heading|Title)[^"]*"')
WORD = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]|[^\W_]+(?:['\u2019-][^\W_]+)*")


def _docx_value(spec: dict[str, Any], label: str, context: _Context) -> Any:
    """One measure of the newest Word document matching a glob (Office lock files ~$* are skipped): body words (each
    CJK character counts as one word), headings, comments or tracked revisions. Read with zipfile, nothing is run."""
    import html
    import zipfile
    found = [item for item in context.files(spec["docx"]) if not item.rsplit("/", 1)[-1].startswith("~$")]
    if not found:
        context.warn(f"{label}: no Word document matches {spec['docx']}")
        return None
    relative = context.scanner.newest(found)
    measure = spec.get("measure", "words")
    try:
        with zipfile.ZipFile(context.scanner.path(relative)) as archive:
            def part(name: str) -> str:
                try:
                    info = archive.getinfo(name)
                except KeyError:
                    return ""
                if info.file_size > MAX_DOCX_XML:
                    raise ValueError(f"{name} is larger than {MAX_DOCX_XML} bytes")
                context.budget.bytes_read += info.compress_size
                return archive.read(name).decode("utf-8", errors="replace")
            context.sources.add(relative)
            if measure == "comments":
                return part("word/comments.xml").count("<w:comment ")
            document = part("word/document.xml")
            if measure == "revisions":
                return document.count("<w:ins ") + document.count("<w:del ")
            if measure == "headings":
                return sum(1 for paragraph in DOCX_PARAGRAPH.findall(document) if DOCX_HEADING.search(paragraph))
            # Words of the body text; deleted runs (w:delText) are not w:t, so tracked deletions are not counted.
            text = " ".join("".join(DOCX_TEXT.findall(paragraph)) for paragraph in DOCX_PARAGRAPH.findall(document))
            return len(WORD.findall(html.unescape(text)))
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        context.warn(f"{label}: {relative} could not be read as a Word document ({error})")
        return None


def _log_status(spec: dict[str, Any], relative: str, context: _Context) -> str:
    text = context.text(relative, spec.get("maxBytes", MAX_LOG_BYTES), True)
    if spec.get("runStart"):
        text = latest_run_text(text, spec["runStart"])
    stat = context.scanner.stat(relative)
    active = stat is not None and (context.now - dt.datetime.fromtimestamp(stat.st_mtime, UTC)).total_seconds() <= spec.get("activeWindowSeconds", 120)
    return classify(text, spec.get("success"), spec.get("failure"), spec.get("running"), active)


def _count(value: Any, label: str, context: _Context) -> int | None:
    if value is None:
        return None
    number = _coerce(value, "number", label, context)
    if number is None or number < 0 or float(number) != int(number):
        if number is not None:
            context.warn(f"{label}: case counts must be nonnegative integers, found {number}")
        return None
    return int(number)


def extract_values(root: Path, extract: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Run the extract block read-only; return a snapshot-shaped result."""
    ensure_valid_extract(extract)
    context = _Context(root, extract, now)
    result = empty_result()
    metrics: list[dict[str, Any]] = []
    try:
        runtime = extract.get("runtime", {})
        status_spec = runtime.get("status")
        if status_spec:
            relative = context.newest(status_spec["fromLog"], "runtime.status")
            if relative is not None:
                result["runtime"]["status"] = _log_status(status_spec, relative, context)
                stat = context.scanner.stat(relative)
                result["runtime"]["runId"] = f"{Path(relative).stem}@{stat.st_mtime_ns}" if stat else None
        current = evaluate(runtime["currentValue"], "runtime.currentValue", context) if "currentValue" in runtime else None
        target = evaluate(runtime["targetValue"], "runtime.targetValue", context) if "targetValue" in runtime else None
        for name, value in (("currentValue", current), ("targetValue", target)):
            if value is not None and (isinstance(value, str) or not math.isfinite(value)):
                context.warn(f"runtime.{name} must be numeric")
        current = current if isinstance(current, (int, float)) else None
        target = target if isinstance(target, (int, float)) else None
        progress(result, current, target, runtime.get("unit"))
        if "step" in runtime:
            step = evaluate(runtime["step"], "runtime.step", context, "string")
            result["runtime"]["step"] = None if step is None else str(step)
        cases_spec = extract.get("cases", {})
        if cases_spec:
            counts: dict[str, int | None] = {key: None for key in ("planned", "completed", "running", "failed", "unknown")}
            logs = cases_spec.get("fromLogs")
            if logs:
                found = context.files(logs["glob"])
                limit = logs.get("maxFiles", MAX_CASE_FILES)
                if len(found) > limit:
                    context.warn(f"cases.fromLogs: only the first {limit} of {len(found)} logs were classified.")
                tallies = {"completed": 0, "running": 0, "failed": 0, "unknown": 0}
                for relative in found[:limit]:
                    try:
                        status = _log_status(logs, relative, context)
                    except ValueError as error:
                        context.warn(f"cases.fromLogs: {error}")
                        status = "unknown"
                    tallies[status if status in tallies else "unknown"] += 1
                    if len(result["details"]) < MAX_CASE_DETAILS:
                        result["details"].append({"kind": "case", "id": relative, "status": status,
                                                  "source": str(context.scanner.path(relative))})
                counts.update(planned=len(found[:limit]), **tallies)
            for key in ("planned", "completed", "running", "failed"):
                if key in cases_spec:
                    counts[key] = _count(evaluate(cases_spec[key], f"cases.{key}", context), f"cases.{key}", context)
            if counts["planned"] is not None and all(counts[key] is not None for key in ("completed", "running", "failed")):
                remainder = counts["planned"] - counts["completed"] - counts["running"] - counts["failed"]
                if remainder < 0:
                    context.warn("Case counts exceed the planned total; the unknown count is left empty.")
                    counts["unknown"] = None
                else:
                    counts["unknown"] = remainder
            elif not logs:
                counts["unknown"] = None
            result["cases"] = counts
            if not status_spec:
                if counts["running"]:
                    result["runtime"]["status"] = "running"
                elif counts["failed"]:
                    result["runtime"]["status"] = "failed"
                elif counts["planned"] and counts["completed"] == counts["planned"]:
                    result["runtime"]["status"] = "completed"
        total = 0
        for index, item in enumerate(extract.get("artifacts", [])):
            found = context.files(item["glob"])
            limit = item.get("max", 50)
            ordered = sorted(found, key=lambda rel: (context.scanner.stat(rel).st_mtime_ns if context.scanner.stat(rel) else 0), reverse=True)
            for relative in ordered[:limit]:
                if total >= MAX_ARTIFACTS:
                    context.warn(f"Only the first {MAX_ARTIFACTS} artifacts are listed.")
                    break
                total += 1
                result["artifacts"].append({"type": item.get("type") or (Path(relative).suffix[1:].lower() or "file"),
                                            "path": str(context.scanner.path(relative)), "verifiedAt": None, "runId": None})
            if len(found) > limit:
                context.warn(f"artifacts[{index}]: {len(found)} files match; the newest {limit} are listed.")
        for item in extract.get("metrics", []):
            value = evaluate(item["value"], f"metrics.{item['id']}", context)
            metrics.append({"kind": "metric", "id": item["id"], "label": item.get("label", item["id"]),
                            "value": value, "unit": item.get("unit")})
    except ScanLimit as error:
        context.warn(str(error))
    result["details"].extend(metrics)
    files = sorted(context.sources | context.matched, key=str.lower)
    paths = [context.scanner.path(relative) for relative in files]
    stamps = [stat.st_mtime for relative in files if (stat := context.scanner.stat(relative)) is not None]
    result["sourceFiles"] = [str(path) for path in paths[:500]]
    result["sourceModifiedAt"] = iso(dt.datetime.fromtimestamp(max(stamps), UTC)) if stamps else None
    result["_warnings"] = list(context.budget.warnings)
    result["_metrics"] = metrics
    result["_matchedFiles"] = len(files)
    return result


def collect(root: Path, options: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Collector entry point used by dashboard_core for ``adapter.engine == "declarative"``."""
    result = extract_values(root, options.get("extract", {}), now)
    warnings = result.pop("_warnings")
    result.pop("_metrics")
    matched = result.pop("_matchedFiles")
    if not matched:
        raise FileNotFoundError("No file matched the declarative profile's patterns" +
                                (f" ({warnings[0]})" if warnings else ""))
    result["details"].append({"kind": "extraction", "profileId": options.get("profileId"),
                              "profileVersion": options.get("profileVersion"), "warnings": warnings[:50]})
    result["details"].append({"kind": "boundary", "message": "Values come from a declarative monitoring profile: "
                              "file patterns, bounded text, JSON and CSV reads only. Nothing was executed. "
                              "Running means recent matching log activity; listed artifacts are not verified results."})
    result["_inputManifest"] = {}
    return result
