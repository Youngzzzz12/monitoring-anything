"""Monitoring profiles, declarative collection, plan previews, milestones and manifests.

All checks use temporary synthetic vaults and source folders only.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import profiles
from adapters.common import UTC
from adapters.declarative import extract_values, parse_json_path, validate_extract
from adapters.scan import Budget, Scanner
from dashboard_core import collect_one, read_json, validate_config
from milestones import apply_template, find_template, inline_template, task_line
from onboarding import connect_folder, inspect_folder
from planning import manifest, preview
from project_catalog import create_logical_project

NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def sweep_profile(**changes: object) -> dict:
    profile = {
        "schemaVersion": "1.0", "id": "matlab-sweep", "title": "MATLAB parameter sweep", "projectType": "simulation",
        "sourceKind": "isale", "origin": {"createdBy": "agent:test", "approvedAt": None, "version": 1},
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
        "milestoneTemplate": "simulation-basic", "subprojectRole": "standalone"}
    profile.update(changes)
    return profile


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.source = self.root / "source"
        self.vault.mkdir()
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def file(self, relative: str, text: str | bytes, base: Path | None = None) -> Path:
        path = (base or self.source) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def sweep(self) -> None:
        self.file("sweep_config.json", json.dumps({"cases": [{"id": i} for i in range(4)]}))
        self.file("results/case_1.mat", b"\x00MATLAB")
        self.file("results/case_2.mat", b"\x00MATLAB")
        self.file("results/summary.csv", "case,stress\n1,12.5\n2,30.25\n3,\n")
        self.file("logs/latest.log", "Phase: setup\nStep 10 of 5000\nPhase: solve\nStep 1240 of 5000\nAll cases finished\n")
        self.file("figures/a.pdf", b"%PDF-1.4")

    def files(self) -> dict[str, bytes]:
        return {path.relative_to(self.vault).as_posix(): path.read_bytes()
                for path in self.vault.rglob("*") if path.is_file()}

    def cli(self, *arguments: str, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        script = Path(__file__).resolve().parents[1] / "cli.py"
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, str(script), "--vault", str(self.vault), "--host-id", "test-host", *arguments],
                              input=stdin, capture_output=True, text=True, encoding="utf-8", env=environment, check=check)

    def save_user(self, profile: dict) -> None:
        profiles.save_profile(self.vault, profile, NOW)

    def paper(self) -> None:
        self.file("main.tex", "\\documentclass{article}\n\\begin{document}\n\\input{Tex/intro}\n\\end{document}\n")
        self.file("Tex/intro.tex", "\\section{Introduction}\nOpening words here.\n\\subsection{Back\\_ground}\n"
                                   "Some background text for the review.\n\\subsection{Aim}\nThe aim is clear.\n"
                                   "\\section{Method}\nMethod text.\n")


class ProfileLibraryTests(Fixture):
    def test_builtin_profiles_load_validate_and_delegate_to_existing_adapters(self) -> None:
        entries, errors = profiles.load_library(self.vault)
        self.assertEqual(errors, [])
        builtins = {entry["profile"]["id"]: entry["profile"] for entry in entries if entry["origin"] == "builtin"}
        self.assertEqual(set(builtins), {"latex-paper", "abaqus-job", "isale-batch", "word-manuscript", "ml-training"})
        # The three original types delegate to their adapters; the app's Word and training types are declarative.
        adapters = {key: value["extract"] for key, value in builtins.items() if "adapter" in value["extract"]}
        self.assertEqual(adapters, {"latex-paper": {"adapter": "latex"}, "abaqus-job": {"adapter": "abaqus"},
                                    "isale-batch": {"adapter": "isale"}})
        for profile in builtins.values():
            self.assertEqual(profiles.validate_profile(profile)[0], [])

    def test_validation_reports_every_problem_with_paths(self) -> None:
        bad = sweep_profile(id="Bad ID", projectType="novel", extra=True)
        bad["detect"] = {"allOf": [{"glob": "../outside/*.txt"}], "anyOf": [{"contains": {"glob": "*.log", "regex": "(a+)+"}}]}
        bad["extract"]["cases"]["completed"] = {"countGlob": "/abs/*.mat"}
        bad["extract"]["runtime"]["currentValue"] = {"regex": "x(", "file": "a.log"}
        errors, _ = profiles.validate_profile(bad)
        text = "\n".join(errors)
        for fragment in ("extra is not a supported", "id must be", "projectType", "parent traversal", "nested quantifier",
                         "relative to the source folder", "not a valid regular expression"):
            self.assertIn(fragment, text)

    def test_declarative_without_source_kind_warns_and_defaults(self) -> None:
        profile = sweep_profile()
        del profile["sourceKind"]
        errors, warnings = profiles.validate_profile(profile)
        self.assertEqual(errors, [])
        self.assertTrue(any("sourceKind" in item for item in warnings))
        self.assertEqual(profiles.source_kind(profile), "abaqus")

    def test_save_versions_user_profiles_and_refuses_builtins(self) -> None:
        first = profiles.save_profile(self.vault, sweep_profile(), NOW)
        self.assertEqual((first["version"], first["previousVersionPath"]), (1, None))
        again = profiles.save_profile(self.vault, read_json(self.vault / first["path"]), NOW)
        self.assertTrue(again["unchanged"])
        second = profiles.save_profile(self.vault, sweep_profile(title="Sweep v2"), NOW + dt.timedelta(seconds=1))
        self.assertEqual(second["version"], 2)
        self.assertTrue((self.vault / second["previousVersionPath"]).is_file())
        self.assertEqual(read_json(self.vault / second["path"])["origin"]["version"], 2)
        with self.assertRaisesRegex(ValueError, "built-in"):
            profiles.save_profile(self.vault, sweep_profile(id="latex-paper"), NOW)
        with self.assertRaisesRegex(ValueError, "built-in"):
            profiles.delete_profile(self.vault, "abaqus-job")
        deleted = profiles.delete_profile(self.vault, "matlab-sweep", NOW)
        self.assertFalse((self.vault / second["path"]).exists())
        self.assertTrue((self.vault / deleted["archivedTo"]).is_file())

    def test_profile_cli_list_validate_save_get_delete(self) -> None:
        listed = json.loads(self.cli("profiles", "list").stdout)
        self.assertEqual({item["id"] for item in listed["profiles"]}, {"latex-paper", "abaqus-job", "isale-batch", "word-manuscript", "ml-training"})
        self.assertIn("paper-section-draft-complete", {item["id"] for item in listed["milestoneTemplates"]})
        invalid = json.loads(self.cli("profiles", "validate", "--stdin", stdin=json.dumps({"id": "x"})).stdout)
        self.assertFalse(invalid["ok"])
        valid = json.loads(self.cli("profiles", "validate", "--stdin", stdin=json.dumps({"profile": sweep_profile()})).stdout)
        self.assertTrue(valid["ok"])
        saved = json.loads(self.cli("profiles", "save", "--stdin", stdin=json.dumps(sweep_profile())).stdout)
        self.assertEqual(saved["path"], "Research Dashboard/System/profiles/user/matlab-sweep.json")
        fetched = json.loads(self.cli("profiles", "get", "--id", "matlab-sweep").stdout)
        self.assertEqual((fetched["origin"], fetched["profile"]["title"]), ("user", "MATLAB parameter sweep"))
        refused = self.cli("profiles", "save", "--stdin", stdin=json.dumps(sweep_profile(id="isale-batch")), check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("built-in", refused.stderr)
        self.assertEqual(json.loads(self.cli("profiles", "delete", "--id", "matlab-sweep").stdout)["deleted"], "matlab-sweep")

    def test_invalid_user_profile_is_reported_not_fatal(self) -> None:
        self.file("Research Dashboard/System/profiles/user/broken.json", "{\"id\": \"broken\"}", base=self.vault)
        entries, errors = profiles.load_library(self.vault)
        self.assertEqual(len(entries), 5)
        self.assertIn("broken.json", errors[0]["path"])
        self.paper()
        result = inspect_folder(self.source, self.vault)
        self.assertTrue(any("broken.json" in warning for warning in result["warnings"]))


class ScannerTests(Fixture):
    def test_glob_semantics_are_bounded_case_insensitive_and_skip_tool_folders(self) -> None:
        self.file("A/Main.TEX", "x")
        self.file("A/b/c/deep.tex", "x")
        self.file(".git/objects/x.tex", "x")
        self.file("node_modules/pkg/y.tex", "x")
        self.file("results/r1.mat", b"\x00")
        scanner = Scanner(self.source)
        self.assertEqual(scanner.glob("**/*.tex"), ["A/b/c/deep.tex", "A/Main.TEX"])
        self.assertEqual(scanner.glob("results/*.MAT"), ["results/r1.mat"])
        self.assertEqual(scanner.glob(".git/**/*.tex"), [".git/objects/x.tex"])
        self.assertEqual(Scanner(self.source, Budget(max_depth=1)).glob("**/*.tex"), ["A/Main.TEX"])
        with self.assertRaises(ValueError):
            scanner.glob("../*.tex")

    def test_text_reads_respect_byte_caps_and_refuse_binaries(self) -> None:
        self.file("big.log", "a" * 5000 + "TAIL")
        self.file("data.bin", b"\x00\x01")
        scanner = Scanner(self.source, Budget(max_bytes=4096))
        text, truncated = scanner.read_text("big.log", 100, tail=True)
        self.assertTrue(truncated)
        self.assertTrue(text.endswith("TAIL"))
        with self.assertRaises(ValueError):
            scanner.read_text("data.bin", 100)


class DetectionTests(Fixture):
    def test_inspect_keeps_existing_fields_and_ranks_builtin_latex_candidate(self) -> None:
        self.paper()
        result = inspect_folder(self.source, self.vault)
        self.assertEqual({"sourcePath", "title", "suggestedId", "profiles", "warnings", "candidates"}, set(result))
        top = result["candidates"][0]
        self.assertEqual(top["profileId"], "latex-paper")
        self.assertGreaterEqual(top["confidence"], 0.9)
        self.assertTrue(top["meetsThreshold"])
        self.assertEqual(top["inspectedProfileIds"], [result["profiles"][0]["id"]])
        self.assertEqual(top["suggestedId"], result["profiles"][0]["suggestedId"])
        self.assertTrue(any("documentclass" in item for item in top["matched"]))
        for key in ("profileId", "title", "projectType", "confidence", "matched", "subprojectRole"):
            self.assertIn(key, top)

    def test_abaqus_and_user_profiles_score_and_none_of_excludes(self) -> None:
        self.file("Job-1.inp", "*HEADING\nmodel\n*NODE\n")
        self.file("Job-1.sta", "THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n")
        self.sweep()
        self.save_user(sweep_profile())
        candidates = {item["profileId"]: item for item in inspect_folder(self.source, self.vault)["candidates"]}
        self.assertTrue(candidates["abaqus-job"]["meetsThreshold"])
        self.assertEqual(candidates["matlab-sweep"]["confidence"], 1.0)
        self.assertEqual(candidates["matlab-sweep"]["origin"], "user")
        self.assertNotIn("latex-paper", candidates)
        self.file("main.tex", "\\documentclass{article}")
        candidates = {item["profileId"]: item for item in inspect_folder(self.source, self.vault)["candidates"]}
        self.assertNotIn("matlab-sweep", candidates)

    def test_partial_profile_scores_below_threshold(self) -> None:
        self.file("sweep_config.json", "{}")
        self.save_user(sweep_profile())
        candidate = next(item for item in inspect_folder(self.source, self.vault)["candidates"] if item["profileId"] == "matlab-sweep")
        self.assertLess(candidate["confidence"], 0.8)
        self.assertFalse(candidate["meetsThreshold"])
        self.assertTrue(candidate["missed"])

    def test_builtin_without_connectable_entry_is_capped(self) -> None:
        self.file("notes.tex", "Just a fragment, no documentclass")
        candidate = next(item for item in inspect_folder(self.source, self.vault)["candidates"] if item["profileId"] == "latex-paper")
        self.assertLessEqual(candidate["confidence"], 0.5)
        self.assertFalse(candidate["meetsThreshold"])


class DeclarativeTests(Fixture):
    def test_extracts_into_existing_snapshot_fields(self) -> None:
        self.sweep()
        result = extract_values(self.source, sweep_profile()["extract"], NOW)
        runtime = result["runtime"]
        self.assertEqual((runtime["status"], runtime["currentValue"], runtime["targetValue"], runtime["unit"], runtime["step"]),
                         ("completed", 1240, 5000, "steps", "solve"))
        self.assertAlmostEqual(runtime["progress"], 1240 / 5000)
        self.assertEqual(result["cases"], {"planned": 4, "completed": 2, "running": None, "failed": None, "unknown": None})
        self.assertEqual([item["type"] for item in result["artifacts"]], ["pdf"])
        self.assertIsNone(result["artifacts"][0]["verifiedAt"])
        self.assertEqual(result["_metrics"][0]["value"], 30.25)

    def test_json_path_subset_and_rejections(self) -> None:
        self.assertEqual(parse_json_path("a.json#$.x[0]['y z'].length"), ("a.json", ["x", 0, "y z", "length"]))
        for bad in ("$..x", "$.x[*]", "x.y", "$.x()"):
            with self.assertRaises(ValueError):
                parse_json_path(bad)
        self.file("c.json", json.dumps({"runs": [{"t": 1.5}, {"t": 2.5}], "name": "n", "length": 7}))
        extract = {"metrics": [{"id": "last", "value": {"jsonPath": "c.json#$.runs[-1].t"}},
                               {"id": "count", "value": {"jsonPath": "$.runs.length", "file": "c.json"}},
                               {"id": "literal-length-key", "value": {"jsonPath": "c.json#$.length"}},
                               {"id": "missing", "value": {"jsonPath": "c.json#$.nope"}}]}
        values = {item["id"]: item["value"] for item in extract_values(self.source, extract, NOW)["_metrics"]}
        self.assertEqual(values, {"last": 2.5, "count": 2, "literal-length-key": 7, "missing": None})

    def test_csv_aggregates_and_regex_modes(self) -> None:
        self.file("a.csv", "t;value\n0;1\n1;4\n2;2\n")
        self.file("run.log", "iter 1\niter 2\niter 3\n")
        metric = lambda ident, aggregate: {"id": ident, "value": {"csv": "a.csv", "column": "VALUE", "delimiter": ";", "aggregate": aggregate}}
        extract = {"metrics": [metric("last", "last"), metric("first", "first"), metric("max", "max"), metric("min", "min"),
                               metric("sum", "sum"), metric("mean", "mean"), metric("count", "count"),
                               {"id": "by-index", "value": {"csv": "a.csv", "column": 0, "delimiter": ";", "aggregate": "max"}},
                               {"id": "iterations", "value": {"regex": r"^iter \d+", "file": "*.log", "match": "count"}},
                               {"id": "first-iter", "value": {"regex": r"iter (\d+)", "file": "*.log", "match": "first"}}]}
        values = {item["id"]: item["value"] for item in extract_values(self.source, extract, NOW)["_metrics"]}
        self.assertEqual(values, {"last": 2, "first": 1, "max": 4, "min": 1, "sum": 7, "mean": 7 / 3, "count": 3,
                                  "by-index": 2, "iterations": 3, "first-iter": 1})

    def test_from_logs_counts_cases_and_derives_runtime(self) -> None:
        self.file("runs/a/log.txt", "done OK\n")
        self.file("runs/b/log.txt", "ERROR: diverged\n")
        self.file("runs/c/log.txt", "step 3\n")
        old = self.file("runs/d/log.txt", "step 1\n")
        stamp = time.time() - 3600
        os.utime(old, (stamp, stamp))
        extract = {"cases": {"fromLogs": {"glob": "runs/*/log.txt", "success": "done OK", "failure": "^ERROR",
                                          "running": r"^step \d+", "activeWindowSeconds": 600}}}
        result = extract_values(self.source, extract, dt.datetime.now(UTC))
        self.assertEqual(result["cases"], {"planned": 4, "completed": 1, "running": 1, "failed": 1, "unknown": 1})
        self.assertEqual(result["runtime"]["status"], "running")
        self.assertEqual(len([item for item in result["details"] if item["kind"] == "case"]), 4)

    def test_missing_evidence_stays_unknown_and_caps_warn(self) -> None:
        self.file("big.log", "x" * 3000 + "\nStep 7\n")
        extract = {"runtime": {"currentValue": {"regex": r"Step (\d+)", "file": "big.log", "maxBytes": 1024},
                               "targetValue": {"regex": r"Total (\d+)", "file": "missing/*.log"}},
                   "cases": {"planned": 3, "completed": {"countGlob": "nothing/*.out"}, "running": 0, "failed": 0}}
        result = extract_values(self.source, extract, NOW)
        self.assertEqual(result["runtime"]["currentValue"], 7)
        self.assertIsNone(result["runtime"]["progress"])
        self.assertEqual(result["cases"], {"planned": 3, "completed": 0, "running": 0, "failed": 0, "unknown": 3})
        self.assertTrue(any("Only the last 1024 bytes" in item for item in result["_warnings"]))
        self.assertTrue(any("missing/*.log" in item for item in result["_warnings"]))

    def test_validate_config_accepts_declarative_and_rejects_invalid_rules(self) -> None:
        config = {"schemaVersion": "1.0", "projectId": "sweep", "title": "Sweep", "kind": "isale", "parentId": None,
                  "projectNote": "Research Dashboard/System/records/sweep/project.md", "profileId": "matlab-sweep",
                  "source": {"rootAlias": None, "path": ""},
                  "collector": {"hostId": "test-host", "intervalSeconds": 60, "staleAfterSeconds": 180},
                  "adapter": {"engine": "declarative", "profileId": "matlab-sweep", "profileVersion": 1,
                              "extract": sweep_profile()["extract"]}}
        validate_config(config)
        config["adapter"]["extract"] = {"runtime": {"currentValue": {"regex": "(", "file": "x"}}}
        with self.assertRaisesRegex(ValueError, "declarative"):
            validate_config(config)
        config["adapter"] = {"engine": "python", "extract": {}}
        with self.assertRaisesRegex(ValueError, "engine"):
            validate_config(config)
        self.assertTrue(validate_extract({"runtime": {"bogus": 1}}))


class PreviewTests(Fixture):
    def test_declarative_preview_reports_values_without_writes(self) -> None:
        self.sweep()
        before = self.files()
        result = preview(self.vault, {"source": str(self.source), "profile": sweep_profile()}, NOW)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(self.files(), before)
        self.assertEqual(result["preview"]["runtime"]["currentValue"], 1240)
        self.assertEqual(result["preview"]["cases"]["planned"], 4)
        self.assertEqual(result["preview"]["metrics"][0], {"id": "peak-stress", "label": "Peak stress", "value": 30.25, "unit": "MPa"})
        self.assertEqual(result["adapter"]["engine"], "declarative")
        self.assertIsNone(result["preview"]["chapters"])

    def test_builtin_latex_preview_uses_existing_adapter_and_reports_chapters(self) -> None:
        self.paper()
        result = preview(self.vault, {"source": str(self.source), "profileId": "latex-paper"}, NOW)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["adapter"]["mainFile"], "main.tex")
        chapters = result["preview"]["chapters"]
        self.assertGreaterEqual(chapters["count"], 4)
        self.assertGreater(chapters["totalWords"], 0)
        self.assertEqual(chapters["leafCount"], 3)

    def test_preview_errors_are_reported_in_json(self) -> None:
        self.file("other.txt", "x")
        invalid = preview(self.vault, {"source": str(self.source), "profile": {"id": "x"}}, NOW)
        self.assertFalse(invalid["ok"])
        self.assertTrue(invalid["errors"])
        nothing = preview(self.vault, {"source": str(self.source), "profile": sweep_profile()}, NOW)
        self.assertFalse(nothing["ok"])
        self.assertTrue(any("No file matched" in item for item in nothing["errors"]))
        builtin = preview(self.vault, {"source": str(self.source), "profileId": "abaqus-job"}, NOW)
        self.assertFalse(builtin["ok"])
        unknown = preview(self.vault, {"source": str(self.source), "profileId": "nope"}, NOW)
        self.assertIn("Unknown monitoring profile", unknown["errors"][0])

    def test_preview_cli_reads_stdin(self) -> None:
        self.sweep()
        result = self.cli("plan", "preview", "--stdin", stdin=json.dumps({"source": str(self.source), "profile": sweep_profile()}))
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        malformed = self.cli("plan", "preview", stdin="not json", check=False)
        self.assertEqual(malformed.returncode, 2)


class ConnectTests(Fixture):
    def test_connect_with_user_declarative_profile_records_profile_and_collects(self) -> None:
        self.sweep()
        self.save_user(sweep_profile())
        create_logical_project(self.vault, "phd", "PhD", "thesis")
        result = json.loads(self.cli("connect", "--source", str(self.source), "--id", "sweep", "--title", "Sweep",
                                     "--profile", "matlab-sweep", "--parent", "phd", "--milestone-template", "profile").stdout)
        self.assertEqual((result["profileId"], result["kind"], result["projectType"], result["parentId"]),
                         ("matlab-sweep", "isale", "simulation", "phd"))
        self.assertEqual(result["collectionHealth"], "fresh")
        self.assertEqual(result["milestones"]["addedCount"], 5)
        config = read_json(self.vault / "Research Dashboard/System/config/projects/sweep.json")
        self.assertEqual(config["profileId"], "matlab-sweep")
        self.assertEqual(config["adapter"]["engine"], "declarative")
        state = read_json(self.vault / "Research Dashboard/System/data/projects/sweep.json")
        self.assertEqual((state["kind"], state["cases"]["planned"], state["runtime"]["currentValue"]), ("isale", 4, 1240))
        self.assertTrue(any(item["kind"] == "metric" for item in state["details"]))
        record = (self.vault / "Research Dashboard/Projects/sweep/Project.md").read_text(encoding="utf-8")
        self.assertIn('project_type: "simulation"', record)
        self.assertIn('parent_id: "phd"', record)
        self.assertIn("- [ ] All planned runs complete #milestone", record)
        again = self.cli("collect", "--project", "sweep")
        self.assertEqual(json.loads(again.stdout)[0]["collection"], "fresh")

    def test_connect_inline_profile_via_stdin_and_builtin_profile_with_adapter(self) -> None:
        self.sweep()
        inline = self.cli("connect", "--source", str(self.source), "--id", "inline", "--title", "Inline", "--profile-stdin",
                          stdin=json.dumps({"profile": sweep_profile(id="inline-sweep")}))
        self.assertEqual(json.loads(inline.stdout)["profileId"], "inline-sweep")
        self.paper()
        adapter = inspect_folder(self.source, self.vault)["profiles"][0]["adapter"]
        result = json.loads(self.cli("connect", "--source", str(self.source), "--id", "paper", "--title", "Paper",
                                     "--profile", "latex-paper", "--adapter-stdin", stdin=json.dumps(adapter)).stdout)
        self.assertEqual((result["kind"], result["projectType"], result["profileId"]), ("latex", "paper", "latex-paper"))
        mismatch = self.cli("connect", "--source", str(self.source), "--id", "paper2", "--title", "Paper", "--kind", "abaqus",
                            "--profile", "latex-paper", "--adapter-stdin", stdin=json.dumps(adapter), check=False)
        self.assertEqual(mismatch.returncode, 2)
        missing = self.cli("connect", "--source", str(self.source), "--id", "paper3", "--title", "Paper", "--profile",
                           "latex-paper", check=False)
        self.assertIn("adapter JSON", missing.stderr)

    def test_legacy_connect_command_is_unchanged(self) -> None:
        self.paper()
        adapter = inspect_folder(self.source, self.vault)["profiles"][0]["adapter"]
        result = json.loads(self.cli("connect", "--source", str(self.source), "--id", "legacy", "--kind", "latex",
                                     "--title", "Legacy", "--adapter-stdin", stdin=json.dumps(adapter)).stdout)
        self.assertIsNone(result["profileId"])
        self.assertNotIn("profileId", read_json(self.vault / "Research Dashboard/System/config/projects/legacy.json"))
        self.assertNotIn("milestones", result)

    def test_parent_and_type_rules_are_checked_before_writing(self) -> None:
        self.sweep()
        create_logical_project(self.vault, "phd", "PhD", "thesis")
        create_logical_project(self.vault, "child", "Child", "paper", "phd")
        before = self.files()
        profile = sweep_profile()
        with self.assertRaisesRegex(ValueError, "already a sub-project"):
            connect_folder(self.vault, "test-host", self.source, "sweep", None, "Sweep", None, profile=profile, parent_id="child")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            connect_folder(self.vault, "test-host", self.source, "sweep", None, "Sweep", None, profile=profile, parent_id="ghost")
        with self.assertRaisesRegex(ValueError, "only when creating"):
            connect_folder(self.vault, "test-host", self.source, "sweep", None, "Sweep", None, profile=profile,
                           dashboard_project="phd", project_type="paper")
        with self.assertRaisesRegex(ValueError, "Project type"):
            connect_folder(self.vault, "test-host", self.source, "sweep", None, "Sweep", None, profile=profile, project_type="novel")
        self.assertEqual(self.files(), before)


class MilestoneTests(Fixture):
    def connect_paper(self) -> dict:
        self.paper()
        adapter = inspect_folder(self.source, self.vault)["profiles"][0]["adapter"]
        return connect_folder(self.vault, "test-host", self.source, "paper", None, "Paper", adapter,
                              profile=profiles.find_profile(self.vault, "latex-paper")[0])

    def test_task_line_matches_connector_format(self) -> None:
        self.assertEqual(task_line("Fix [x] *now* #1 <b>_y_\\"),
                         "- [ ] Fix \\[x\\] \\*now\\* \\#1 \\<b\\>\\_y\\_\\\\ #milestone")
        self.assertEqual(task_line("Intro — draft", {"paper": "p", "chapter": "section-1", "stage": "draft"}),
                         "- [ ] Intro — draft #milestone [paper:: p] [chapter:: section-1] [stage:: draft]")
        with self.assertRaises(ValueError):
            task_line("two\nlines")

    def test_section_template_appends_grouped_bound_tasks_idempotently(self) -> None:
        self.connect_paper()
        note = self.vault / "Research Dashboard/Projects/paper/Project.md"
        template = find_template(self.vault, "paper-section-draft-complete")
        dry = apply_template(self.vault, "paper", template, dry_run=True)
        self.assertEqual(dry["addedCount"], 6)
        before = note.read_text(encoding="utf-8")
        self.assertNotIn("[stage:: complete]", before)
        result = apply_template(self.vault, "paper", template)
        self.assertEqual((result["addedCount"], result["skipped"], result["sourceId"]), (6, 0, "paper"))
        text = note.read_text(encoding="utf-8")
        self.assertIn("### Introduction", text)
        self.assertIn("- [ ] Back\\_ground — draft #milestone [paper:: paper] [chapter:: ", text)
        self.assertIn("- [ ] Method — complete #milestone", text)
        milestones_at, decisions_at = text.index("## Milestones"), text.index("## Decisions and evidence")
        self.assertTrue(milestones_at < text.index("### Introduction") < decisions_at)
        self.assertIn("Define the first deliverable", text)
        again = apply_template(self.vault, "paper", template)
        self.assertEqual((again["addedCount"], again["skipped"]), (0, 6))
        self.assertEqual(note.read_text(encoding="utf-8"), text)

    def test_new_sections_are_inserted_under_their_existing_chapter_heading(self) -> None:
        self.connect_paper()
        note = self.vault / "Research Dashboard/Projects/paper/Project.md"
        template = find_template(self.vault, "paper-section-draft-complete")
        apply_template(self.vault, "paper", template)
        intro = self.source / "Tex/intro.tex"
        intro.write_text(intro.read_text(encoding="utf-8").replace("\\section{Method}", "\\subsection{Scope}\nScope text.\n\\section{Method}"),
                         encoding="utf-8")
        config = read_json(self.vault / "Research Dashboard/System/config/projects/paper.json")
        collect_one(self.vault, config, "test-host", dt.datetime.now(UTC))
        result = apply_template(self.vault, "paper", template)
        self.assertEqual((result["addedCount"], result["skipped"]), (2, 6))
        lines = note.read_text(encoding="utf-8").splitlines()
        scope = lines.index(next(line for line in lines if line.startswith("- [ ] Scope — draft")))
        self.assertLess(lines.index("### Introduction"), scope)
        self.assertLess(scope, lines.index("### Method"))
        self.assertEqual(lines[scope - 1].startswith("- [ ] Aim — complete"), True)

    def test_static_template_cli_and_missing_paper_snapshot(self) -> None:
        create_logical_project(self.vault, "sim", "Sim", "simulation")
        added = json.loads(self.cli("milestones", "apply", "--project", "sim", "--template", "simulation-basic").stdout)
        self.assertEqual(added["addedCount"], 5)
        again = json.loads(self.cli("milestones", "apply", "--project", "sim", "--template", "simulation-basic").stdout)
        self.assertEqual(again["skipped"], 5)
        failed = self.cli("milestones", "apply", "--project", "sim", "--template", "paper-section-draft-complete", check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("paper snapshot", failed.stderr)
        listed = json.loads(self.cli("milestones", "list").stdout)
        self.assertEqual({item["id"] for item in listed["milestoneTemplates"]}, {"paper-section-draft-complete", "simulation-basic"})

    def test_connect_applies_profile_section_template_after_first_collection(self) -> None:
        self.paper()
        adapter = inspect_folder(self.source, self.vault)["profiles"][0]["adapter"]
        result = connect_folder(self.vault, "test-host", self.source, "paper", None, "Paper", adapter,
                                profile=profiles.find_profile(self.vault, "latex-paper")[0], milestone_template="profile")
        self.assertEqual(result["milestones"]["addedCount"], 6)

    def test_inline_template_and_milestones_section_creation(self) -> None:
        create_logical_project(self.vault, "bare", "Bare", "other")
        note = self.vault / "Research Dashboard/Projects/bare/Project.md"
        text = note.read_text(encoding="utf-8")
        note.write_text(text[:text.index("## Milestones")], encoding="utf-8")
        result = apply_template(self.vault, "bare", inline_template([{"title": "Sweep complete", "when": "cases.completed == cases.planned"}]))
        self.assertEqual(result["addedCount"], 1)
        self.assertTrue(note.read_text(encoding="utf-8").rstrip().endswith("## Milestones\n\n- [ ] Sweep complete #milestone"))


class ManifestTests(Fixture):
    def test_manifest_is_bounded_and_skips_binaries_secrets_and_tool_folders(self) -> None:
        self.sweep()
        self.file(".git/config", "[core]")
        self.file("node_modules/x/index.js", "x")
        self.file("build/out.txt", "x")
        self.file(".env", "API_KEY=secret")
        self.file("credentials.json", "{\"key\": 1}")
        for index in range(60):
            self.file(f"many/f{index:02}.txt", f"file {index}\n" * 400)
        self.save_user(sweep_profile())
        result = manifest(self.vault, self.source, max_depth=3, max_entries=30, excerpts=5, excerpt_bytes=128)
        self.assertLessEqual(len(result["tree"]), 30)
        self.assertTrue(any(entry.get("ignored") for entry in result["tree"] if entry["path"] in {".git/", "node_modules/", "build/"}))
        self.assertGreaterEqual(result["totals"]["ignoredDirectories"], 3)
        heads = {item["path"] for item in result["excerpts"]}
        self.assertLessEqual(len(heads), 5)
        self.assertFalse(heads & {".env", "credentials.json", "results/case_1.mat"})
        self.assertTrue(all(len(item["head"]) <= 128 for item in result["excerpts"]))
        extensions = {item["extension"]: item["count"] for item in result["extensions"]}
        self.assertEqual(extensions[".txt"], 60)
        self.assertEqual(extensions[".mat"], 2)
        ranked = {item["profileId"]: item for item in result["profiles"]}
        self.assertEqual(ranked["matlab-sweep"]["confidence"], 1.0)
        self.assertNotIn("latex-paper", ranked)  # no positive rule hit
        self.assertLess(len(json.dumps(result)), 64 * 1024)

    def test_manifest_cli_limits(self) -> None:
        self.file("a.txt", "hello")
        payload = json.loads(self.cli("plan", "manifest", "--source", str(self.source)).stdout)
        self.assertEqual(payload["excerpts"][0]["head"], "hello")
        refused = self.cli("plan", "manifest", "--source", str(self.source), "--max-depth", "20", check=False)
        self.assertEqual(refused.returncode, 2)


if __name__ == "__main__":
    unittest.main()
