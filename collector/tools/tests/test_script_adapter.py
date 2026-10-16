"""Custom adapters (Python written for a folder): the static check, the isolated read-only runner, the mapping onto
the snapshot shape, and the profile -> preview -> connect -> collect path. App addition; see VENDORED.md."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters import script
from dashboard_core import read_json
from onboarding import connect_folder
from planning import preview
from profiles import engine, validate_profile
from workspace import init_workspace

ADAPTER = '''
import csv
from pathlib import Path

def collect(folder, options):
    rows = list(csv.DictReader((folder / "log.csv").open("r", encoding="utf8")))
    target = int((folder / "target.txt").read_text().strip())
    return {
        "status": "running" if len(rows) < target else "completed",
        "progress": {"current": len(rows), "target": target, "unit": "cycles"},
        "step": f"cycle {len(rows)}",
        "cases": {"planned": 4, "completed": 1, "running": 1, "failed": 0},
        "metrics": [{"id": "peak", "label": "Peak load", "value": max(float(r["load"]) for r in rows), "unit": "kN"},
                    {"label": "Label only", "value": options.get("note", "-")}],
        "artifacts": [{"path": "plot.png"}, {"path": "../outside.png"}],
        "files": ["log.csv", "target.txt"],
        "warnings": ["calibration pending"],
    }
'''


def profile(code: str = ADAPTER, **script_extra) -> dict:
    return {"schemaVersion": "1.0", "id": "rig-adapter", "title": "Test rig", "projectType": "experiment", "sourceKind": "abaqus",
            "detect": {"allOf": [{"glob": "log.csv"}]},
            "extract": {"script": {"language": "python", "code": code, "options": {"note": "ok"}, **script_extra}}}


class StaticCheckTests(unittest.TestCase):
    def test_reading_code_passes(self) -> None:
        self.assertEqual(script.check_code(ADAPTER), [])
        self.assertEqual(script.check_code("import os, re\ndef collect(folder, options):\n"
                                           "    names = [e.name.replace('-', ' ') for e in os.scandir(folder)]\n"
                                           "    names.remove(names[0]) if names else None\n    return {'step': str(len(names))}\n"), [])

    def test_writing_running_and_escaping_fail(self) -> None:
        cases = {
            "import subprocess\ndef collect(f, o):\n    return {}": "importing subprocess",
            "import os\ndef collect(f, o):\n    os.system('x')\n    return {}": ".system",
            "def collect(f, o):\n    open(f / 'x', 'w')\n    return {}": "only be opened for reading",
            "def collect(f, o):\n    (f / 'x').open(mode='a')\n    return {}": "only be opened for reading",
            "def collect(f, o):\n    (f / 'x').write_text('y')\n    return {}": ".write_text",
            "def collect(f, o):\n    return eval('1')": "eval()",
            "import os\ndef collect(f, o):\n    getattr(os, 'sys' + 'tem')('x')\n    return {}": "getattr()",
            "def collect(f, o):\n    return ().__class__.__bases__": ".__class__",
            "import h5py\ndef collect(f, o):\n    h5py.File(f / 'a.h5', 'w')\n    return {}": "only be opened for reading",
            "import shutil\ndef collect(f, o):\n    return {}": "importing shutil",
            "def other():\n    return {}": "must define collect",
            "def collect(f, o) return": "syntax error",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertTrue(any(expected in error for error in script.check_code(code)), script.check_code(code))


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.folder = self.root / "rig"
        self.folder.mkdir()
        (self.folder / "log.csv").write_text("cycle,load\n1,10.5\n2,12.25\n3,11\n", encoding="utf8")
        (self.folder / "target.txt").write_text("10\n", encoding="utf8")
        (self.folder / "plot.png").write_bytes(b"\x89PNG")
        (self.root / "secret.txt").write_text("private", encoding="utf8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_code(self, code: str, timeout: float = 20) -> dict:
        return script.run(self.folder, {"code": code, "options": {"note": "ok"}, "timeoutSeconds": timeout})

    def test_values_map_onto_the_snapshot_shape(self) -> None:
        before = sorted(path.name for path in self.folder.iterdir())
        result = script.to_snapshot(self.folder.resolve(), self.run_code(ADAPTER))
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), before, "the source folder must stay untouched")
        self.assertEqual(result["runtime"]["status"], "running")
        self.assertEqual((result["runtime"]["currentValue"], result["runtime"]["targetValue"], result["runtime"]["unit"]), (3, 10, "cycles"))
        self.assertAlmostEqual(result["runtime"]["progress"], 0.3)
        self.assertEqual(result["cases"], {"planned": 4, "completed": 1, "running": 1, "failed": 0, "unknown": 2})
        self.assertEqual([(m["label"], m["value"]) for m in result["_metrics"]], [("Peak load", 12.25), ("Label only", "ok")])
        self.assertEqual([Path(a["path"]).name for a in result["artifacts"]], ["plot.png"])
        self.assertIn("An artifact outside the folder was skipped", result["_warnings"])
        self.assertIn("calibration pending", result["_warnings"])
        self.assertEqual(result["_matchedFiles"], 2)
        self.assertIsNotNone(result["sourceModifiedAt"])

    def test_the_guard_refuses_anything_but_reading_inside_the_folder(self) -> None:
        # Each passes or sidesteps the static check; the audit hook in the separate process refuses it.
        attempts = {
            "def collect(folder, options):\n    mode = 'w'\n    open(folder / 'new.txt', mode)\n    return {}": "only read files",
            "def collect(folder, options):\n    return {'step': (folder.parent / 'secret.txt').read_text()}": "only read inside the source folder",
            "import os\ndef collect(folder, options):\n    return {'step': str(os.listdir(folder.parent))}": "only list inside the source folder",
        }
        for code, expected in attempts.items():
            with self.subTest(code=code):
                with self.assertRaises(ValueError) as caught:
                    self.run_code(code)
                self.assertIn(expected, str(caught.exception))
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["log.csv", "plot.png", "target.txt"])

    def test_time_limit_and_failures_are_reported(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not finish within 1 s"):
            self.run_code("def collect(folder, options):\n    while True:\n        pass\n", timeout=1)
        with self.assertRaisesRegex(ValueError, r"KeyError.*line 2"):
            self.run_code("def collect(folder, options):\n    return {}['missing']\n")
        with self.assertRaisesRegex(ValueError, "must return a dict"):
            self.run_code("def collect(folder, options):\n    return [1]\n")


class ProfileFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.folder = self.root / "rig"
        self.folder.mkdir()
        (self.folder / "log.csv").write_text("cycle,load\n1,10.5\n2,12.25\n", encoding="utf8")
        (self.folder / "target.txt").write_text("4\n", encoding="utf8")
        init_workspace(self.workspace, "Test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validation_preview_connect_and_collect(self) -> None:
        errors, _ = validate_profile(profile())
        self.assertEqual(errors, [])
        self.assertEqual(engine(profile()), "script")
        bad, _ = validate_profile(profile("import socket\ndef collect(f, o):\n    return {}"))
        self.assertTrue(any("importing socket" in error for error in bad))
        self.assertTrue(any("timeoutSeconds" in error for error in validate_profile(profile(timeoutSeconds=600))[0]))

        shown = preview(self.workspace, {"source": str(self.folder), "profile": profile()})
        self.assertTrue(shown["ok"], shown)
        self.assertEqual(shown["engine"], "script")
        self.assertEqual(shown["adapter"]["engine"], "script")
        self.assertEqual(shown["preview"]["runtime"]["currentValue"], 2)
        self.assertEqual([m["label"] for m in shown["preview"]["metrics"]], ["Peak load", "Label only"])

        connect_folder(self.workspace, "test-host", self.folder, "rig", None, "Test rig", None, profile=profile())
        config = read_json(self.workspace / "sources" / "rig.json")
        self.assertEqual(config["adapter"]["engine"], "script")
        state = json.loads(next((self.workspace / "data").rglob("rig*.json")).read_text(encoding="utf8"))
        self.assertEqual(state["runtime"]["targetValue"], 4)
        self.assertTrue(any(detail.get("kind") == "boundary" and "custom adapter" in detail["message"] for detail in state["details"]))


class CommandLineTests(unittest.TestCase):
    def test_ma_embeds_the_adapter_file_into_the_plan(self) -> None:
        import ma
        with tempfile.TemporaryDirectory() as temp:
            plan_path, code_path = Path(temp) / "plan.json", Path(temp) / "adapter.py"
            plan_path.write_text(json.dumps({"profile": profile(timeoutSeconds=5)}), encoding="utf8")
            code_path.write_text(ADAPTER, encoding="utf8")
            plan = ma.read_plan(str(plan_path), str(code_path))
        script_part = plan["profile"]["extract"]["script"]
        self.assertEqual((script_part["code"], script_part["timeoutSeconds"], script_part["options"]), (ADAPTER, 5, {"note": "ok"}))
        with self.assertRaises(ma.Problem):
            ma.read_plan(None, str(code_path))


if __name__ == "__main__":
    unittest.main()
