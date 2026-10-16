"""Regenerate dev/demo-connect.json: what the collector says about a made-up LaTeX paper (inspect, plan preview,
milestone templates), for the plan card on the demo page. The folder is built in a temporary directory and its path is
replaced with C:\\Demo\\Paper2, so no local path reaches the repository.

    python scripts/demo-connect.py
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "collector" / "tools" / "cli.py"
FAKE_SOURCE = "C:\\Demo\\Paper2"
FAKE_WORKSPACE = "C:\\Demo\\Workspace"
WORDS = ("the model results show that method data analysis load response design framework case study approach "
         "performance test values compared with previous work under different conditions").split()


def sentence(seed: int, count: int) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(WORDS) for _ in range(count)) + "."


def write_paper(folder: Path) -> None:
    sections = ["introduction", "method", "results", "discussion", "conclusion"]
    (folder / "sections").mkdir(parents=True)
    (folder / "figures").mkdir()
    body = "\n".join(f"\\input{{sections/{name}}}" for name in sections)
    (folder / "main.tex").write_text("\\documentclass{article}\n\\title{Project A: Paper 2}\n\\begin{document}\n\\maketitle\n"
                                     f"{body}\n\\end{{document}}\n", encoding="utf8")
    for i, name in enumerate(sections, 1):
        text = f"\\section{{{name.capitalize()}}}\n{sentence(i, 260)}\n\n\\subsection{{Overview}}\n{sentence(i + 10, 140)}\n"
        (folder / "sections" / f"{name}.tex").write_text(text, encoding="utf8")


def collector(workspace: Path, *args: str, stdin: str | None = None) -> dict:
    out = subprocess.run([sys.executable, str(CLI), "--workspace", str(workspace), *args], input=stdin, capture_output=True,
                         text=True, check=True)
    return json.loads(out.stdout)


def scrub(value, replacements: dict[str, str]):
    if isinstance(value, dict):
        return {key: scrub(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, replacements) for item in value]
    if isinstance(value, str):
        for real, fake in replacements.items():
            value = value.replace(real, fake)
    return value


def main() -> None:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    with tempfile.TemporaryDirectory() as temp:
        source, workspace = Path(temp) / "Paper2", Path(temp) / "workspace"
        write_paper(source)
        workspace.mkdir()
        collector(workspace, "init-workspace")
        inspect = collector(workspace, "inspect", "--source", str(source))
        inspected = inspect["profiles"][0]["id"]
        preview = collector(workspace, "plan", "preview", stdin=json.dumps({"source": str(source), "profileId": "latex-paper", "inspectedProfileId": inspected}))
        templates = collector(workspace, "milestones", "list")
        replacements = {}
        for real, fake in ((source, FAKE_SOURCE), (workspace, FAKE_WORKSPACE), (Path(temp), "C:\\Demo")):
            replacements[str(real)] = fake
            replacements[str(real).replace("\\", "/")] = fake
        data = scrub({"inspect": inspect, "preview": preview, "templates": templates}, replacements)
    text = json.dumps(data, indent=1)
    leaks = re.findall(r".{0,30}(?:AppData|Users|tmp[a-z0-9_]{6,}).{0,30}", text)
    if leaks:
        raise SystemExit(f"A local path is still in the data: {leaks[:3]}")
    target = ROOT / "dev" / "demo-connect.json"
    target.write_text(text + "\n", encoding="utf8", newline="\n")
    print(f"{target.relative_to(ROOT)}: preview ok={preview.get('ok')}")


if __name__ == "__main__":
    main()
