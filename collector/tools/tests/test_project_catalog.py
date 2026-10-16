"""Logical project grouping and Canvas creation use isolated synthetic sources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli
import onboarding
from dashboard_core import atomic_json, configurations, read_json
from onboarding import connect_folder
from project_catalog import new_catalog, project_catalogs


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.source = self.root / "source"
        self.vault.mkdir()
        self.source.mkdir()
        (self.source / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
        (self.source / "job.sta").write_text("THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def connect(self, source_id: str = "paper", dashboard_project: str | None = None,
                title: str = "Lunar project", host: str = "test-host") -> dict:
        kind = "latex" if source_id == "paper" else "abaqus"
        adapter = {"chapters": ["main.tex"], "logPatterns": ["main.log"]} if kind == "latex" else {"logPatterns": ["job.sta"]}
        return connect_folder(self.vault, host, self.source, source_id, kind, title, adapter,
                              dashboard_project=dashboard_project)

    def files(self) -> dict[str, bytes]:
        return {path.relative_to(self.vault).as_posix(): path.read_bytes()
                for path in self.vault.rglob("*") if path.is_file()}

    def test_first_connection_creates_logical_record_and_dedicated_progress_group(self) -> None:
        result = self.connect()
        self.assertEqual(result["dashboardProjectId"], "paper")
        self.assertEqual(result["projectRecord"], "Research Dashboard/Projects/paper/Project.md")
        self.assertEqual(result["projectDashboard"], "Research Dashboard/Projects/paper/Dashboard.canvas")
        self.assertEqual(result["projectNote"], "Research Dashboard/System/records/paper/project.md")
        records = project_catalogs(self.vault)
        self.assertEqual(records[0]["sourceIds"], ["paper"])
        note = (self.vault / result["projectRecord"]).read_text(encoding="utf-8")
        self.assertIn('type: "research-project"', note)
        self.assertIn("#milestone", note)
        source_note = (self.vault / result["projectNote"]).read_text(encoding="utf-8")
        self.assertIn('type: "research-source"', source_note)
        self.assertNotIn("#milestone", source_note)
        canvas = read_json(self.vault / result["projectDashboard"])
        self.assertEqual(len(canvas["nodes"]), 6)
        self.assertEqual(canvas["edges"], [])
        self.assertEqual(canvas["metadata"], {"version": "1.0-1.0",
                                               "frontmatter": {"cssclasses": ["research-dashboard-canvas"]}})
        self.assertNotIn("cssclasses", canvas)
        group = next(node for node in canvas["nodes"] if node["type"] == "group")
        progress = next(node for node in canvas["nodes"] if node["id"] == "project-progress")
        self.assertEqual(group["label"], "Progress")
        self.assertGreater(progress["x"], group["x"])
        self.assertGreater(progress["y"], group["y"])
        self.assertLess(progress["x"] + progress["width"], group["x"] + group["width"])
        self.assertLess(progress["y"] + progress["height"], group["y"] + group["height"])
        self.assertIn('"mode": "paper-progress"', (self.vault / progress["file"]).read_text(encoding="utf-8"))
        lower_cards = [node for node in canvas["nodes"] if node["id"] in {"project-monitor", "project-results"}]
        self.assertTrue(all(node["y"] > group["y"] + group["height"] for node in lower_cards))
        for node in (node for node in canvas["nodes"] if node["type"] == "file"):
            wrapper = (self.vault / node["file"]).read_text(encoding="utf-8")
            self.assertIn('"projectId": "paper"', wrapper)
            self.assertIn("System/views/research-dashboard", wrapper)

    def test_multiple_sources_produce_one_logical_project_with_source_owned_hosts(self) -> None:
        self.connect()
        result = self.connect("thermal", "paper", "Thermal solver", "another-host")
        self.assertEqual(result["projectId"], "thermal")
        self.assertEqual(result["dashboardProjectId"], "paper")
        projects = project_catalogs(self.vault)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["title"], "Lunar project")
        self.assertEqual(projects[0]["sourceIds"], ["paper", "thermal"])
        self.assertEqual(len(configurations(self.vault)), 2)
        self.assertFalse((self.vault / "Research Dashboard/Projects/thermal").exists())
        configs = {item["projectId"]: item for item in configurations(self.vault)}
        self.assertEqual(configs["thermal"]["collector"]["hostId"], "another-host")

    def test_attach_preserves_custom_project_note_canvas_and_wrapper(self) -> None:
        first = self.connect()
        paths = [self.vault / first["projectRecord"], self.vault / first["projectDashboard"],
                 self.vault / "Research Dashboard/System/cards/projects/paper/overview.md",
                 self.vault / "Research Dashboard/System/cards/projects/paper/progress.md"]
        for index, path in enumerate(paths):
            path.write_text(f"Custom user content {index}", encoding="utf-8")
        self.connect("thermal", "paper")
        self.assertEqual([path.read_text(encoding="utf-8") for path in paths],
                         [f"Custom user content {index}" for index in range(4)])

    def test_later_source_attachment_creates_missing_progress_wrapper_without_rewriting_canvas(self) -> None:
        first = self.connect()
        progress = self.vault / "Research Dashboard/System/cards/projects/paper/progress.md"
        progress.unlink()
        canvas = self.vault / first["projectDashboard"]
        legacy = {"nodes": [{"id": "custom", "type": "text", "text": "User layout", "x": 250, "y": 900,
                             "width": 400, "height": 300}], "edges": []}
        atomic_json(canvas, legacy)
        before = canvas.read_bytes()
        self.connect("thermal", "paper")
        self.assertEqual(canvas.read_bytes(), before)
        self.assertIn('"mode": "paper-progress"', progress.read_text(encoding="utf-8"))

    def test_nonpaper_project_still_has_progress_card_for_future_latex_attachment(self) -> None:
        connected = self.connect("thermal")
        canvas = read_json(self.vault / connected["projectDashboard"])
        progress = next(node for node in canvas["nodes"] if node["id"] == "project-progress")
        self.assertIn('"mode": "paper-progress"', (self.vault / progress["file"]).read_text(encoding="utf-8"))

    def test_missing_or_unsafe_logical_project_writes_nothing(self) -> None:
        for identity in ("missing", "../escape", "con"):
            with self.assertRaises(ValueError):
                self.connect("thermal", identity)
        self.assertEqual(self.files(), {})

    def test_catalog_write_failure_rolls_back_only_own_files_and_alias_changes(self) -> None:
        local = self.vault / "Research Dashboard/System/config/local/test-host.json"
        atomic_json(local, {"schemaVersion": "1.0", "aliases": {"old": str(self.root)}})
        preserved = self.vault / "Research Dashboard/Projects/paper/Project.md"
        preserved.parent.mkdir(parents=True)
        preserved.write_text("Existing research note", encoding="utf-8")
        before = self.files()
        write = onboarding.atomic_json

        def fail_catalog(path: Path, value: dict, **kwargs: object) -> None:
            if path.parent.name == "catalog":
                raise OSError("Catalog write failed")
            write(path, value, **kwargs)

        with patch.object(onboarding, "atomic_json", side_effect=fail_catalog):
            with self.assertRaisesRegex(OSError, "Catalog write failed"):
                self.connect()
        self.assertEqual(self.files(), before)
        self.assertFalse((self.vault / "Research Dashboard/System/records/paper").exists())
        self.assertFalse((self.vault / "Research Dashboard/System/cards/projects/paper").exists())
        self.assertFalse((self.vault / "Research Dashboard/Projects/paper/Dashboard.canvas").exists())
        self.assertEqual(read_json(local)["aliases"], {"old": str(self.root)})

    def test_attach_failure_preserves_prior_project_membership_and_status(self) -> None:
        self.connect()
        before = self.files()
        write = onboarding.atomic_json

        def fail_catalog(path: Path, value: dict, **kwargs: object) -> None:
            if path.parent.name == "catalog":
                raise OSError("Catalog write failed")
            write(path, value, **kwargs)

        with patch.object(onboarding, "atomic_json", side_effect=fail_catalog):
            with self.assertRaises(OSError):
                self.connect("thermal", "paper")
        self.assertEqual(self.files(), before)
        self.assertFalse((self.vault / "Research Dashboard/System/records/thermal").exists())
        self.assertFalse((self.vault / "Research Dashboard/System/config/projects/thermal.json").exists())
        self.assertEqual(project_catalogs(self.vault)[0]["sourceIds"], ["paper"])

    def test_catalog_failure_leaves_no_new_files_or_directories_in_empty_vault(self) -> None:
        write = onboarding.atomic_json

        def fail_catalog(path: Path, value: dict, **kwargs: object) -> None:
            if path.parent.name == "catalog":
                path.parent.mkdir(parents=True, exist_ok=True)
                raise OSError("Catalog write failed")
            write(path, value, **kwargs)

        with patch.object(onboarding, "atomic_json", side_effect=fail_catalog):
            with self.assertRaises(OSError):
                self.connect()
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_catalog_membership_and_canonical_paths_are_validated(self) -> None:
        self.connect()
        path = self.vault / "Research Dashboard/System/config/catalog/paper.json"
        original = read_json(path)
        for field, value in (("projectId", "different"), ("projectNote", "private.md"),
                             ("projectDashboard", "../outside.canvas"), ("sourceIds", ["missing"]),
                             ("sourceIds", ["paper", "paper"])):
            atomic_json(path, {**original, field: value})
            with self.assertRaises(ValueError):
                project_catalogs(self.vault)
        atomic_json(path, original)
        duplicate = new_catalog("second", "Second project", "paper")
        atomic_json(path.parent / "second.json", duplicate)
        second_note = self.vault / duplicate["projectNote"]
        second_note.parent.mkdir(parents=True)
        second_note.write_text("Live project record", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "more than one"):
            project_catalogs(self.vault)

    def test_unicode_titles_and_source_files_are_preserved(self) -> None:
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.source.iterdir()}
        title = 'Lunar habitat · 月球 "α"\nThermal design'
        result = self.connect(title=title)
        self.assertEqual(project_catalogs(self.vault)[0]["title"], title)
        note = (self.vault / result["projectRecord"]).read_text(encoding="utf-8")
        self.assertIn('title: ' + json.dumps(title, ensure_ascii=False), note)
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.source.iterdir()}
        self.assertEqual(before, after)

    def test_partial_collection_still_has_usable_logical_dashboard(self) -> None:
        (self.source / "job.sta").unlink()
        result = self.connect("thermal")
        self.assertEqual(result["collectionHealth"], "error")
        self.assertTrue((self.vault / result["projectDashboard"]).is_file())
        self.assertEqual(project_catalogs(self.vault)[0]["sourceIds"], ["thermal"])

    def test_projects_cli_and_attach_argument_contract(self) -> None:
        self.connect()
        script = Path(__file__).resolve().parents[1] / "cli.py"
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        common = [sys.executable, str(script), "--vault", str(self.vault), "--host-id", "test-host"]
        listed = subprocess.run(common + ["projects"], capture_output=True, text=True, encoding="utf-8", env=environment, check=True)
        self.assertEqual(json.loads(listed.stdout)["projects"][0]["projectId"], "paper")
        connected = subprocess.run(common + ["connect", "--source", str(self.source), "--id", "thermal",
                                             "--kind", "abaqus", "--title", "Thermal · α", "--adapter-stdin",
                                             "--dashboard-project", "paper"], input='{"logPatterns":["job.sta"]}',
                                   capture_output=True, text=True, encoding="utf-8", env=environment, check=True)
        result = json.loads(connected.stdout)
        self.assertEqual(result["dashboardProjectId"], "paper")
        self.assertEqual(result["projectRecord"], "Research Dashboard/Projects/paper/Project.md")
        self.assertEqual(cli.parser().get_default("vault"), script.resolve().parents[3])


if __name__ == "__main__":
    unittest.main()
