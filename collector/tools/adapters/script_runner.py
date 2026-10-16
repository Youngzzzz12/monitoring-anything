"""Run one custom adapter (agent- or user-written Python) in this separate, isolated process.

Started by adapters/script.py as ``python -I -B -X utf8 script_runner.py`` with a JSON payload on stdin
({code, folder, options}); prints one JSON object ({ok, result} or {ok: false, error}). Before the adapter's code runs,
an audit hook (PEP 578, which cannot be removed once added) refuses anything but reading: writing, deleting or
renaming files, starting programs, network access, and reading or listing outside the source folder and the Python
installation. The static check in script.py has already rejected the obvious ways around it; this is the second layer.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
BLOCKED_EVENTS = (
    "subprocess.", "_winapi.CreateProcess", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile",
    "os.kill", "os.fork", "os.forkpty", "os.popen", "pty.", "socket.", "urllib.", "http.", "ftplib.", "smtplib.",
    "imaplib.", "poplib.", "nntplib.", "telnetlib.", "webbrowser.", "winreg.", "msvcrt.", "os.remove", "os.rename",
    "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.chflags", "os.truncate", "os.symlink", "os.link", "os.utime",
    "os.putenv", "os.unsetenv", "os.chdir", "shutil.", "tempfile.mkstemp", "tempfile.mkdtemp",
)


class Refused(PermissionError):
    pass


def install_guard(folder: Path) -> None:
    roots = {folder}
    for value in (sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path):
        if value:
            try:
                roots.add(Path(value).resolve())
            except OSError:
                pass
    roots = tuple(sorted(str(root) for root in roots))
    busy = False

    def inside(path) -> bool:
        if isinstance(path, int):
            return True
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        text = os.path.normcase(os.path.abspath(os.fspath(path)))
        return any(text == os.path.normcase(root) or text.startswith(os.path.normcase(root).rstrip("\\/") + os.sep) for root in roots)

    def hook(event: str, args: tuple) -> None:
        nonlocal busy
        if busy:
            return
        busy = True
        try:
            if event.startswith(BLOCKED_EVENTS):
                raise Refused(f"A monitoring adapter may only read files ({event} is not allowed)")
            if event == "open":
                path, mode, flags = (list(args) + [None, None, None])[:3]
                if (isinstance(mode, str) and any(ch in mode for ch in "wax+")) or (mode is None and isinstance(flags, int) and flags & WRITE_FLAGS):
                    raise Refused("A monitoring adapter may only read files (opening for writing is not allowed)")
                if path is not None and not inside(path):
                    raise Refused(f"A monitoring adapter may only read inside the source folder ({path})")
            elif event in ("os.listdir", "os.scandir", "glob.glob", "os.walk") and args and args[0] is not None and not inside(args[0]):
                raise Refused(f"A monitoring adapter may only list inside the source folder ({args[0]})")
            elif event == "ctypes.dlopen" and args and (not isinstance(args[0], str) or not inside(args[0])):
                raise Refused("A monitoring adapter may not load native libraries from outside the Python installation")
        finally:
            busy = False

    sys.addaudithook(hook)


def main() -> None:
    out = sys.stdout
    try:
        payload = json.loads(sys.stdin.read())
        folder = Path(payload["folder"]).resolve()
        code = compile(payload["code"], "adapter.py", "exec")
        options = payload.get("options") or {}
    except Exception as error:  # noqa: BLE001 - reported to the caller
        out.write(json.dumps({"ok": False, "error": f"Invalid runner input: {error}"}))
        return
    install_guard(folder)
    namespace = {"__name__": "monitoring_adapter", "__file__": "adapter.py"}
    try:
        exec(code, namespace)  # the adapter's code, statically checked by script.py
        collect = namespace.get("collect")
        if not callable(collect):
            raise TypeError("The adapter must define collect(folder, options)")
        result = collect(folder, options)
        text = json.dumps({"ok": True, "result": result}, default=str, allow_nan=False)
    except Exception as error:  # noqa: BLE001 - the adapter's failure is the caller's error message
        frames = [frame for frame in traceback.extract_tb(error.__traceback__) if frame.filename == "adapter.py"]
        where = f" (adapter.py line {frames[-1].lineno})" if frames else ""
        text = json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}{where}"[:2000]})
    out.write(text)


if __name__ == "__main__":
    main()
