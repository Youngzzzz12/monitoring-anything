"""Folder onboarding checks use temporary synthetic projects only."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import onboarding
from dashboard_core import atomic_json, read_json, validate_id
from onboarding import connect_folder, inspect_folder, read_adapter_stdin, stable_id


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.source = self.root / "source"
        self.vault.mkdir()
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def file(self, relative: str, text: str) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def latex(self) -> dict:
        self.file("main.tex", "\\documentclass{article}\n\\input{Tex/chapter}\n")
        self.file("Tex/chapter.tex", "Chapter content")
        return inspect_folder(self.source)["profiles"][0]

    def connect(self, profile: dict, pid: str = "trial") -> dict:
        return connect_folder(self.vault, "test-host", self.source, pid,
                              profile["kind"], "Temporary project", profile["adapter"])

    def test_resolves_literal_tex_dependencies_without_unrelated_files(self) -> None:
        self.file("main.tex", "\\documentclass{article}\n% \\input{ignored}\n\\input{Tex/first}\n")
        self.file("Tex/first.tex", "\\include{Tex/second}\n")
        self.file("Tex/second.tex", "\\input{Tex/first}\n")
        self.file("unrelated.tex", "Unselected content")
        self.file("other.log", "Output written on other.pdf (1 page)")
        profile = inspect_folder(self.source)["profiles"][0]
        self.assertEqual(profile["adapter"]["chapters"], ["main.tex", "Tex/first.tex", "Tex/second.tex"])
        self.assertEqual(profile["adapter"]["logPatterns"], ["main.log"])
        self.assertEqual(profile["adapter"]["mainFile"], "main.tex")

    def test_onboarding_rejects_main_file_that_differs_from_selected_entry(self) -> None:
        profile = self.latex()
        profile["adapter"]["mainFile"] = "Tex/chapter.tex"
        with self.assertRaisesRegex(ValueError, "mainFile"):
            self.connect(profile)

    def test_tex_basename_with_period_and_spaces_gets_tex_extension(self) -> None:
        self.file("main.tex", "\\documentclass{article}\n\\input{Tex/0.Abstract}\n\\input{Tex/6. Case study}")
        self.file("Tex/0.Abstract.tex", "Abstract")
        self.file("Tex/6. Case study.tex", "Case study")
        chapters = inspect_folder(self.source)["profiles"][0]["adapter"]["chapters"]
        self.assertEqual(chapters, ["main.tex", "Tex/0.Abstract.tex", "Tex/6. Case study.tex"])

    def test_multiple_tex_entries_are_separate_profiles(self) -> None:
        self.file("main.tex", "\\documentclass{article}")
        self.file("appendix.tex", "\\documentclass{article}")
        result = inspect_folder(self.source)
        self.assertEqual(len(result["profiles"]), 2)
        self.assertEqual(result["profiles"][0]["adapter"]["chapters"], ["main.tex"])
        self.assertEqual(result["profiles"][1]["adapter"]["logPatterns"], ["appendix.log"])
        self.assertNotEqual(result["profiles"][0]["suggestedId"], result["profiles"][1]["suggestedId"])

    def test_suggested_ids_avoid_existing_registry_and_note_paths(self) -> None:
        profile = self.latex()
        expected = profile["suggestedId"]
        config_path = self.vault / f"Research Dashboard/System/config/projects/{expected}.json"
        atomic_json(config_path, {"existing": "content"})
        note_path = self.vault / f"Research Dashboard/System/records/{expected}-2/project.md"
        note_path.parent.mkdir(parents=True)
        note_path.write_text("Existing user note")
        next_profile = inspect_folder(self.source, self.vault)["profiles"][0]
        self.assertEqual(next_profile["suggestedId"], expected + "-3")

    def test_dynamic_and_outside_includes_warn_without_reading_them(self) -> None:
        outside = self.root / "private.tex"
        outside.write_text("\\documentclass{article}")
        self.file("main.tex", "\\documentclass{article}\n\\input{\\chapterfile}\n\\input{../private}\n")
        result = inspect_folder(self.source)
        self.assertEqual(result["profiles"][0]["adapter"]["chapters"], ["main.tex"])
        self.assertTrue(any("dynamic" in warning for warning in result["warnings"]))
        self.assertTrue(any("outside" in warning for warning in result["warnings"]))

    def test_declared_missing_tex_include_is_observed_when_created_later(self) -> None:
        self.file("main.tex", "\\documentclass{article}\n\\input{future/chapter}")
        profile = inspect_folder(self.source)["profiles"][0]
        self.assertEqual(profile["adapter"]["chapters"], ["main.tex", "future/chapter.tex"])
        self.connect(profile)
        state_path = self.vault / "Research Dashboard/System/data/projects/trial.json"
        state = read_json(state_path)
        self.assertFalse(next(item for item in state["details"] if item.get("name") == "future/chapter.tex")["exists"])
        self.file("future/chapter.tex", "New chapter content")
        from dashboard_core import collect_one
        from adapters.common import UTC
        import datetime as dt
        config = read_json(self.vault / "Research Dashboard/System/config/projects/trial.json")
        updated = collect_one(self.vault, config, "test-host", dt.datetime.now(UTC))
        self.assertTrue(next(item for item in updated["details"] if item.get("name") == "future/chapter.tex")["exists"])

    def test_links_are_skipped_when_supported_by_host(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.tex").write_text("\\documentclass{article}")
        try:
            (self.source / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit test symlink creation")
        result = inspect_folder(self.source)
        self.assertEqual(result["profiles"], [])
        self.assertTrue(any("links" in warning for warning in result["warnings"]))

    def test_abaqus_stems_are_separate_and_generic_logs_are_not_claimed(self) -> None:
        self.file("Job-1.sta", "THE ANALYSIS HAS COMPLETED SUCCESSFULLY")
        self.file("Job-1.log", "Abaqus JOB Job-1 COMPLETED")
        self.file("Job-2.msg", "Abaqus message")
        self.file("application.log", "An unrelated application")
        result = inspect_folder(self.source)
        profiles = result["profiles"]
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0]["adapter"]["logPatterns"], ["Job-1.sta", "Job-1.msg", "Job-1.log"])
        self.assertNotIn("jobs", profiles[0]["adapter"])
        self.assertTrue(all("Job-1" in path for path in profiles[0]["adapter"]["logPatterns"]))

    def test_abaqus_input_can_be_connected_before_first_run(self) -> None:
        self.file("future.inp", "*HEADING\nFuture model\n")
        profile = inspect_folder(self.source)["profiles"][0]
        self.assertEqual(profile["kind"], "abaqus")
        self.assertTrue(any("Awaiting source logs" in item for item in profile["summary"]))
        result = self.connect(profile)
        self.assertEqual(result["collectionHealth"], "error")
        self.assertEqual(result["runtimeStatus"], "unknown")
        self.assertTrue((self.vault / result["projectNote"]).exists())
        state = read_json(self.vault / "Research Dashboard/System/data/projects/trial.json")
        self.assertIsNone(state["cases"]["planned"])

    def test_isale_input_extension_does_not_create_false_abaqus_profile(self) -> None:
        self.file("asteroid.inp", "# iSALE input\nGRIDSPC = 0.01\n")
        self.file("material.inp", "MATNAME = basalt\n")
        self.assertEqual(inspect_folder(self.source)["profiles"], [])

    def test_isale_requires_supported_workflow_and_does_not_invent_plan(self) -> None:
        good = {"case_id": 1, "model_name": "case1", "status": "completed", "completed": "2026-10-02T12:00:00Z"}
        self.file("runs/case1/metadata.json", json.dumps(good))
        self.file("other/metadata.json", json.dumps({"status": "completed", "completed": "2026-10-02T12:00:00Z"}))
        self.file("runs/case2/metadata.json", json.dumps({**good, "completed": "invalid"}))
        result = inspect_folder(self.source)
        profile = result["profiles"][0]
        self.assertEqual(profile["adapter"]["metadataPatterns"], ["runs/case1/metadata.json"])
        self.assertEqual(profile["adapter"]["cases"], [])
        self.assertTrue(any("Unsupported" in warning or "Invalid" in warning for warning in result["warnings"]))
        self.connect(profile)
        state = read_json(self.vault / "Research Dashboard/System/data/projects/trial.json")
        self.assertEqual(state["cases"]["completed"], 1)
        self.assertIsNone(state["cases"]["planned"])
        self.assertIsNone(state["runtime"]["progress"])
        self.assertEqual(state["runtime"]["status"], "unknown")

    def test_hidden_results_and_binaries_are_not_inputs(self) -> None:
        self.file(".git/main.tex", "\\documentclass{article}")
        self.file("results/main.tex", "\\documentclass{article}")
        self.file("large.odb", "This is not a status log")
        self.assertEqual(inspect_folder(self.source)["profiles"], [])

    def test_inspection_limit_is_reported(self) -> None:
        for number in range(5):
            self.file(f"file{number}.sta", "Status")
        with patch.object(onboarding, "MAX_FILES", 2):
            result = inspect_folder(self.source)
        self.assertLessEqual(len(result["profiles"]), 2)
        self.assertTrue(any("file limit" in warning for warning in result["warnings"]))

    def test_shared_text_budget_bounds_all_discovery_reads(self) -> None:
        self.file("first.tex", "\\documentclass{article}")
        self.file("second.tex", "\\documentclass{article}")
        with patch.object(onboarding, "MAX_INSPECTION_TEXT_BYTES", 30):
            result = inspect_folder(self.source)
        self.assertEqual(len(result["profiles"]), 1)
        self.assertTrue(any("total text inspection budget" in warning for warning in result["warnings"]))

    def test_stable_ids_handle_reserved_names_and_normalization_collisions(self) -> None:
        for value in ("CON", "A" * 90, "Folder with spaces", "中文"):
            validate_id(stable_id(value))
            self.assertEqual(stable_id(value), stable_id(value))
        self.assertNotEqual(stable_id("Project A"), stable_id("project-a"))

    def test_connection_preserves_aliases_and_source_files(self) -> None:
        profile = self.latex()
        local = self.vault / "Research Dashboard/System/config/local/test-host.json"
        existing = {"schemaVersion": "1.0", "aliases": {"old-project": str(self.root / "old")}, "extra": "preserved"}
        atomic_json(local, existing)
        before = {path.relative_to(self.source).as_posix(): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        result = self.connect(profile)
        self.assertEqual(result["collectionHealth"], "fresh")
        self.assertEqual(result["runtimeStatus"], "unknown")
        current = read_json(local)
        self.assertEqual(current["aliases"]["old-project"], existing["aliases"]["old-project"])
        self.assertEqual(current["extra"], "preserved")
        self.assertEqual(current["aliases"]["trial"], str(self.source))
        after = {path.relative_to(self.source).as_posix(): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_duplicate_ids_and_identical_profile_are_rejected(self) -> None:
        profile = self.latex()
        self.connect(profile)
        with self.assertRaises(FileExistsError):
            self.connect(profile)
        with self.assertRaisesRegex(ValueError, "already connected"):
            self.connect(profile, "second")
        self.file("second.tex", "\\documentclass{article}")
        second = inspect_folder(self.source)["profiles"][1]
        result = self.connect(second, "second")
        self.assertEqual(result["collectionHealth"], "fresh")

    def test_differing_alias_is_rejected_without_overwriting(self) -> None:
        profile = self.latex()
        local = self.vault / "Research Dashboard/System/config/local/test-host.json"
        existing = {"schemaVersion": "1.0", "aliases": {"trial": str(self.root / "other")}}
        atomic_json(local, existing)
        with self.assertRaisesRegex(ValueError, "different folder"):
            self.connect(profile)
        self.assertEqual(read_json(local), existing)
        self.assertFalse((self.vault / "Research Dashboard/System/config/projects/trial.json").exists())

    def test_registration_failure_restores_original_aliases(self) -> None:
        profile = self.latex()
        local = self.vault / "Research Dashboard/System/config/local/test-host.json"
        existing = {"schemaVersion": "1.0", "aliases": {"old": str(self.root)}}
        atomic_json(local, existing)
        with patch.object(onboarding, "register", side_effect=ValueError("Invalid template")):
            with self.assertRaisesRegex(ValueError, "Invalid template"):
                self.connect(profile)
        self.assertEqual(read_json(local), existing)

    def test_invalid_adapter_paths_and_mixed_jobs_are_rejected(self) -> None:
        self.file("job.sta", "Status")
        for adapter in ({"logPatterns": ["../private.sta"]},
                        {"logPatterns": ["*.sta"]},
                        {"logPatterns": ["job.sta", "other.log"]},
                        {"logPatterns": ["job.odb"]}):
            with self.assertRaises(ValueError):
                connect_folder(self.vault, "test-host", self.source, "trial", "abaqus", "Trial", adapter)
        self.assertFalse((self.vault / "Research Dashboard/System/config/projects/trial.json").exists())

    def test_stdin_is_bounded_and_requires_object(self) -> None:
        self.assertEqual(read_adapter_stdin(io.StringIO('{"chapters": []}')), {"chapters": []})
        with self.assertRaises(ValueError):
            read_adapter_stdin(io.StringIO("[]"))
        with self.assertRaisesRegex(ValueError, "limit"):
            read_adapter_stdin(io.StringIO(" " * (onboarding.MAX_ADAPTER_BYTES + 1)))

    def test_cli_contract_uses_stdin_json_and_partial_result_exit_code(self) -> None:
        self.file("future.inp", "*HEADING\n")
        cli = Path(__file__).resolve().parents[1] / "cli.py"
        common = [sys.executable, str(cli), "--vault", str(self.vault), "--host-id", "test-host"]
        inspected = subprocess.run(common + ["inspect", "--source", str(self.source)], capture_output=True, text=True, check=True)
        profile = json.loads(inspected.stdout)["profiles"][0]
        connected = subprocess.run(common + ["connect", "--source", str(self.source), "--id", "trial",
                                             "--kind", profile["kind"], "--title", "Future job", "--adapter-stdin"],
                                   input=json.dumps(profile["adapter"]), capture_output=True, text=True)
        self.assertEqual(connected.returncode, 1)
        result = json.loads(connected.stdout)
        self.assertEqual(result["projectId"], "trial")
        self.assertEqual(result["collectionHealth"], "error")
        self.assertEqual(result["runtimeStatus"], "unknown")
        self.assertTrue(result["collectionError"])
        self.assertEqual(connected.stderr, "")


if __name__ == "__main__":
    unittest.main()
