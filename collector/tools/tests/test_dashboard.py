"""Behavioral checks use temporary synthetic projects, never real source files."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters import abaqus, isale, latex
from adapters.common import UTC, MAX_LOG_BYTES, bounded_text, iso
from dashboard_core import (atomic_json, blank_state, collect_one, configurations, effective_health,
                            output_path, projection, register, validate_config, validate_state)

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def config(kind: str = "abaqus", pid: str = "trial") -> dict:
    return {"schemaVersion": "1.0", "projectId": pid, "title": "Temporary test project", "kind": kind,
            "parentId": None, "projectNote": f"Research Dashboard/System/records/{pid}/project.md",
            "source": {"rootAlias": "research", "path": ""},
            "collector": {"hostId": "test-host", "intervalSeconds": 60, "staleAfterSeconds": 180},
            "adapter": {}}


class DashboardTests(unittest.TestCase):
    def test_optional_unknown_target_is_null(self) -> None:
        item = config()
        item["adapter"]["targetValue"] = None
        validate_config(item)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.source = self.root / "source"
        self.vault.mkdir()
        self.source.mkdir()
        atomic_json(self.vault / "Research Dashboard/System/config/local/test-host.json",
                    {"schemaVersion": "1.0", "aliases": {"research": str(self.source)}})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def file(self, relative: str, text: str, age: int = 0) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stamp = NOW.timestamp() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_unconfigured_is_not_zero(self) -> None:
        item = config()
        item["source"]["rootAlias"] = None
        state = collect_one(self.vault, item, "test-host", NOW)
        self.assertEqual(state["collection"]["health"], "unconfigured")
        self.assertIsNone(state["runtime"]["progress"])
        self.assertIsNone(state["cases"]["planned"])
        self.assertIn("runtime_progress: null", projection(state, NOW))

    def test_register_preserves_existing_user_note_and_config(self) -> None:
        item = config()
        register(self.vault, item)
        note = self.vault / item["projectNote"]
        note.write_text("My research judgement", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            register(self.vault, item)
        self.assertEqual(note.read_text(), "My research judgement")

    def test_source_record_is_separate_from_research_project_templates(self) -> None:
        template = self.vault / "Research Dashboard/System/templates/latex.md"
        template.parent.mkdir(parents=True)
        template.write_text('---\ntype: research-project-template\nproject_id: "{{project_id}}"\nparent_id: "{{parent_id}}"\ntitle: "{{title}}"\nproject_kind: "{{project_kind}}"\n---\n', encoding="utf-8")
        register(self.vault, config("latex"))
        note = (self.vault / "Research Dashboard/System/records/trial/project.md").read_text(encoding="utf-8")
        self.assertIn('type: "research-source"\n', note)
        self.assertIn('source_id: "trial"', note)
        self.assertNotIn('type: research-project', note)
        self.assertNotIn("{{", note)

    def test_traversal_and_unsafe_ids_are_rejected(self) -> None:
        for identity in ["../escape", "CON", "con", "bad/name"]:
            item = config(pid=identity)
            with self.assertRaises(ValueError):
                validate_config(item)
        with self.assertRaises(ValueError):
            output_path(self.vault, "../external.json")

    def test_another_host_cannot_publish(self) -> None:
        with self.assertRaises(PermissionError):
            collect_one(self.vault, config(), "other-host", NOW)
        self.assertFalse((self.vault / "Research Dashboard/System/data").exists())

    def test_parent_cycle_rejected(self) -> None:
        a, b = config(pid="first"), config(pid="second")
        a["parentId"], b["parentId"] = "second", "first"
        folder = self.vault / "Research Dashboard/System/config/projects"
        atomic_json(folder / "first.json", a)
        atomic_json(folder / "second.json", b)
        with self.assertRaisesRegex(ValueError, "cycle"):
            configurations(self.vault)

    def test_source_is_read_only_and_note_is_not_touched(self) -> None:
        source = self.file("Job-17.sta", "THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n")
        item = config()
        item["adapter"] = {"logPatterns": ["Job-17.sta"]}
        note = self.vault / "Research Dashboard/System/records/trial/project.md"
        note.parent.mkdir(parents=True)
        note.write_text("Do not change me", encoding="utf-8")
        original = source.read_bytes()
        state = collect_one(self.vault, item, "test-host", NOW)
        self.assertEqual(state["runtime"]["status"], "completed")
        self.assertEqual(note.read_text(), "Do not change me")
        self.assertEqual(source.read_bytes(), original)

    def test_failure_to_read_keeps_last_valid_result(self) -> None:
        source = self.file("job.sta", "THE ANALYSIS HAS COMPLETED SUCCESSFULLY")
        item = config()
        item["adapter"] = {"logPatterns": ["job.sta"]}
        prior = collect_one(self.vault, item, "test-host", NOW)
        source.unlink()
        later = collect_one(self.vault, item, "test-host", NOW + dt.timedelta(seconds=300))
        self.assertEqual(later["runtime"], prior["runtime"])
        self.assertEqual(later["collection"]["health"], "error")
        self.assertEqual(later["collection"]["lastSuccessfulAt"], iso(NOW))

    def test_stale_calculated_without_source_change(self) -> None:
        state = blank_state(config(), "test-host", NOW)
        state["collection"].update(health="fresh", lastSuccessfulAt=iso(NOW))
        self.assertEqual(effective_health(state, NOW + dt.timedelta(seconds=181)), "stale")
        self.assertEqual(effective_health(state, NOW + dt.timedelta(seconds=179)), "fresh")

    def test_invalid_progress_and_nan_rejected(self) -> None:
        for value in [float("nan"), float("inf"), -0.1, 1.1, True]:
            state = blank_state(config(), "test-host", NOW)
            state["runtime"]["progress"] = value
            with self.assertRaises(ValueError):
                validate_state(state)

    def test_latex_pdf_alone_does_not_prove_success(self) -> None:
        self.file("main.pdf", "old pdf")
        self.file("chapter.tex", "Text")
        result = latex.collect(self.source, {"chapters": ["chapter.tex"], "logPatterns": ["main.log"]}, NOW)
        self.assertEqual(result["runtime"]["status"], "unknown")
        self.assertEqual(result["artifacts"], [])

    def test_latex_explicit_log_not_latest_machine_copy(self) -> None:
        self.file("main.log", "! Emergency stop\n", age=30)
        self.file("main-Mac.log", "Output written on main.pdf (4 pages).\n")
        result = latex.collect(self.source, {"logPatterns": ["main.log"]}, NOW)
        self.assertEqual(result["runtime"]["status"], "failed")

    def test_latex_restart_discards_previous_success(self) -> None:
        self.file("main.log", "This is pdfTeX\nOutput written on main.pdf (4 pages).\nThis is pdfTeX\nWorking...\n")
        result = latex.collect(self.source, {}, NOW)
        self.assertEqual(result["runtime"]["status"], "unknown")

    def test_latex_modified_chapter_marks_pdf_outdated(self) -> None:
        self.file("main.log", "Output written on main.pdf (4 pages).\n", age=60)
        self.file("main.pdf", "old build", age=59)
        self.file("chapter.tex", "new chapter")
        result = latex.collect(self.source, {"chapters": ["chapter.tex"], "logPatterns": ["main.log"]}, NOW)
        self.assertEqual(result["runtime"]["status"], "completed")
        self.assertIsNone(result["artifacts"][0]["verifiedAt"])
        self.assertTrue(next(d for d in result["details"] if d["kind"] == "build")["buildOutdated"])

    def test_abaqus_latest_terminal_signal_wins(self) -> None:
        self.file("job.sta", "THE ANALYSIS HAS COMPLETED SUCCESSFULLY\nTHE ANALYSIS HAS NOT BEEN COMPLETED\n")
        result = abaqus.collect(self.source, {"logPatterns": ["job.sta"]}, NOW)
        self.assertEqual(result["runtime"]["status"], "failed")

    def test_new_unknown_abaqus_log_does_not_mean_running(self) -> None:
        self.file("job.log", "Abaqus license manager checked out licenses\n")
        result = abaqus.collect(self.source, {"logPatterns": ["job.log"]}, NOW)
        self.assertEqual(result["runtime"]["status"], "unknown")

    def test_old_success_does_not_override_new_activity(self) -> None:
        self.file("job.log", "Abaqus JOB job COMPLETED\n", age=600)
        self.file("job.sta", " 1 2 1 0 5 1.0 1.0 1.0\n")
        result = abaqus.collect(self.source, {"logPatterns": ["job.log", "job.sta"]}, NOW)
        self.assertEqual(result["runtime"]["status"], "running")

    def test_old_activity_and_not_before_are_unknown(self) -> None:
        self.file("job.sta", " 1 2 1 0 5 1.0 1.0 1.0\n", age=600)
        result = abaqus.collect(self.source, {"jobs": [{"id": "job", "logPatterns": ["job.sta"]}],
                                              "notBefore": iso(NOW)}, NOW)
        self.assertEqual(result["cases"]["planned"], 1)
        self.assertEqual(result["cases"]["unknown"], 1)
        self.assertEqual(result["runtime"]["status"], "unknown")

    def test_planned_missing_cases_are_not_omitted(self) -> None:
        self.file("one.sta", "THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n")
        result = abaqus.collect(self.source, {"jobs": [{"id": "one", "logPatterns": ["one.sta"]},
                                                       {"id": "two", "logPatterns": ["two.sta"]}]}, NOW)
        self.assertEqual(result["cases"], {"planned": 2, "completed": 1, "running": 0, "failed": 0, "unknown": 1})
        self.assertEqual(result["runtime"]["status"], "unknown")

    def test_isale_regex_requires_explicit_evidence(self) -> None:
        self.file("run.log", "time=0.0\nSimulation finished successfully\n")
        options = {"logPatterns": ["run.log"], "successPattern": r"^Simulation finished successfully$",
                   "currentValuePattern": r"time=(?P<value>\d+(?:\.\d+)?)", "targetValue": 1.0, "unit": "s"}
        result = isale.collect(self.source, options, NOW)
        self.assertEqual(result["runtime"]["status"], "completed")
        self.assertEqual(result["runtime"]["progress"], 0.0)
        self.assertEqual(isale.collect(self.source, {"logPatterns": ["run.log"]}, NOW)["runtime"]["status"], "unknown")

    def test_zero_target_does_not_produce_percentage(self) -> None:
        self.file("run.log", "time=1.0")
        result = isale.collect(self.source, {"logPatterns": ["run.log"], "currentValuePattern": r"time=(?P<value>\d+\.\d+)",
                                           "targetValue": 0, "unit": "s"}, NOW)
        self.assertIsNone(result["runtime"]["progress"])

    def test_isale_metadata_plan_completion_and_missing(self) -> None:
        self.file("runs/level/case_0001/metadata.json", json.dumps({"status": "completed", "completed": "2026-10-01T12:00:00"}))
        self.file("runs/level/case_0002/metadata.json", json.dumps({"status": "prepared"}))
        options = {"mode": "metadata-json", "statusField": "status", "completionField": "completed",
                   "statusMap": {"completed": "completed", "prepared": "unknown"},
                   "cases": [{"id": "first", "metadata": "runs/level/case_0001/metadata.json"},
                             {"id": "second", "metadata": "runs/level/case_0002/metadata.json"},
                             {"id": "third", "metadata": "runs/level/case_0003/metadata.json"}]}
        result = isale.collect(self.source, options, NOW)
        self.assertEqual(result["cases"]["planned"], 3)
        self.assertEqual(result["cases"]["completed"], 1)
        self.assertEqual(result["cases"]["unknown"], 2)

    def test_isale_discovery_and_jdata_are_not_a_plan_or_success(self) -> None:
        self.file("runs/level/case_0001/metadata.json", json.dumps({"status": "completed"}))
        self.file("runs/level/case_0001/jdata.dat", "binary placeholder")
        result = isale.collect(self.source, {"mode": "metadata-json", "metadataPatterns": ["runs/*/case_*/metadata.json"],
                                           "statusMap": {"completed": "completed"}, "completionField": "completed"}, NOW)
        self.assertIsNone(result["cases"]["planned"])
        self.assertEqual(result["cases"]["completed"], 0)
        self.assertEqual(result["runtime"]["status"], "unknown")

    def test_large_log_read_is_bounded_and_odb_rejected(self) -> None:
        path = self.file("large.log", "a" * (MAX_LOG_BYTES + 100) + "THE ANALYSIS HAS COMPLETED SUCCESSFULLY")
        text = bounded_text(path)
        self.assertLessEqual(len(text.encode()), MAX_LOG_BYTES)
        self.assertIn("COMPLETED SUCCESSFULLY", text)
        with self.assertRaises(ValueError):
            bounded_text(self.file("model.odb", "never read"))


if __name__ == "__main__":
    unittest.main()
