"""Sourceless logical projects, project types and one-level parent hierarchy."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import project_catalog
from dashboard_core import read_json
from onboarding import connect_folder
from project_catalog import (active_configurations, create_logical_project, frontmatter_scalar, project_catalogs,
                             record_parent_id, validate_catalog)
from reconciliation import reconcile

CATALOG = "Research Dashboard/System/config/catalog"


class ProjectHierarchyTests(unittest.TestCase):
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

    def files(self) -> dict[str, bytes]:
        return {path.relative_to(self.vault).as_posix(): path.read_bytes()
                for path in self.vault.rglob("*") if path.is_file()}

    def connect(self, source_id: str, kind: str = "latex", dashboard_project: str | None = None) -> dict:
        adapter = {"chapters": ["main.tex"], "logPatterns": ["main.log"]} if kind == "latex" else {"logPatterns": ["job.sta"]}
        return connect_folder(self.vault, "test-host", self.source, source_id, kind, source_id.title(), adapter,
                              dashboard_project=dashboard_project)

    def cli(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
        script = Path(__file__).resolve().parents[1] / "cli.py"
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, str(script), "--vault", str(self.vault), "--host-id", "test-host", *arguments],
                              capture_output=True, text=True, encoding="utf-8", env=environment, check=check)

    def test_sourceless_project_has_record_canvas_cards_and_empty_catalog(self) -> None:
        result = create_logical_project(self.vault, "thesis", "PhD thesis", "thesis")
        self.assertEqual(result, {"dashboardProjectId": "thesis", "title": "PhD thesis", "projectType": "thesis",
                                  "parentId": None, "sourceIds": [],
                                  "projectRecord": "Research Dashboard/Projects/thesis/Project.md",
                                  "projectDashboard": "Research Dashboard/Projects/thesis/Dashboard.canvas"})
        catalog = read_json(self.vault / CATALOG / "thesis.json")
        self.assertEqual(catalog["sourceIds"], [])
        validate_catalog(catalog)
        self.assertEqual([item["projectId"] for item in project_catalogs(self.vault)], ["thesis"])
        note = (self.vault / result["projectRecord"]).read_text(encoding="utf-8")
        self.assertIn('type: "research-project"', note)
        self.assertIn('project_type: "thesis"', note)
        self.assertIn("parent_id: null", note)
        self.assertLess(note.index("project_kind:"), note.index("project_type:"))
        self.assertTrue((self.vault / result["projectDashboard"]).is_file())
        for card in project_catalog.CARDS:
            self.assertTrue((self.vault / f"Research Dashboard/System/cards/projects/thesis/{card}.md").is_file())
        self.assertEqual(active_configurations(self.vault), [])

    def test_child_records_parent_and_hierarchy_is_one_level(self) -> None:
        create_logical_project(self.vault, "thesis", "Thesis", "thesis")
        child = create_logical_project(self.vault, "chapter-paper", "Chapter paper", "paper", "thesis")
        self.assertEqual(child["parentId"], "thesis")
        note = (self.vault / child["projectRecord"]).read_text(encoding="utf-8")
        self.assertIn('parent_id: "thesis"', note)
        self.assertEqual(record_parent_id(self.vault, "chapter-paper"), "thesis")
        self.assertIsNone(record_parent_id(self.vault, "thesis"))
        before = self.files()
        with self.assertRaisesRegex(ValueError, "already a sub-project"):
            create_logical_project(self.vault, "grandchild", "Grandchild", "other", "chapter-paper")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            create_logical_project(self.vault, "orphan", "Orphan", "other", "missing")
        with self.assertRaisesRegex(ValueError, "own parent"):
            create_logical_project(self.vault, "self", "Self", "other", "self")
        with self.assertRaisesRegex(ValueError, "parentId"):
            create_logical_project(self.vault, "unsafe", "Unsafe", "other", "../thesis")
        self.assertEqual(self.files(), before)

    def test_obsidian_yaml_parent_values_are_read_without_capturing_the_next_line(self) -> None:
        self.assertEqual(frontmatter_scalar("---\nparent_id:\nstage: planned\n---\n", "parent_id"), "")
        self.assertEqual(frontmatter_scalar("---\nparent_id: thesis\n---\n", "parent_id"), "thesis")
        self.assertIsNone(frontmatter_scalar("---\nparent_id: null\n---\n", "parent_id"))
        create_logical_project(self.vault, "thesis", "Thesis", "thesis")
        record = self.vault / "Research Dashboard/Projects/thesis/Project.md"
        text = record.read_text(encoding="utf-8").replace("parent_id: null", "parent_id:")
        record.write_text(text, encoding="utf-8")
        self.assertIsNone(record_parent_id(self.vault, "thesis"))
        create_logical_project(self.vault, "child", "Child", "other", "thesis")

    def test_invalid_input_and_occupied_ids_write_nothing(self) -> None:
        self.connect("paper")
        occupied = self.vault / "Research Dashboard/Projects/manual"
        occupied.mkdir(parents=True)
        (occupied / "notes.md").write_text("User folder", encoding="utf-8")
        before = self.files()
        for arguments, error in ((("con", "Reserved"), ValueError), (("Bad ID", "Upper"), ValueError),
                                 (("ok", "   "), ValueError), (("ok", "Two\nlines"), ValueError),
                                 (("ok", "x" * 201), ValueError), (("paper", "Duplicate"), FileExistsError),
                                 (("manual", "Occupied folder"), FileExistsError)):
            with self.assertRaises(error):
                create_logical_project(self.vault, *arguments)
        with self.assertRaisesRegex(ValueError, "Project type"):
            create_logical_project(self.vault, "ok", "Valid title", "poster")
        self.assertEqual(self.files(), before)

    def test_source_ownership_claim_blocks_reusing_a_project_id(self) -> None:
        # A removed project whose source has not been reconciled yet still owns its ID.
        self.connect("thermal", "abaqus")
        for relative in ("Research Dashboard/Projects/thermal", "Research Dashboard/System/cards/projects/thermal"):
            shutil.rmtree(self.vault / relative)
        (self.vault / CATALOG / "thermal.json").unlink()
        before = self.files()
        with self.assertRaisesRegex(FileExistsError, "claims this project ID"):
            create_logical_project(self.vault, "thermal", "Claimed")
        self.assertEqual(self.files(), before)

    def test_catalog_publication_failure_removes_only_new_files_and_folders(self) -> None:
        create_logical_project(self.vault, "thesis", "Thesis", "thesis")
        before = self.files()
        original = project_catalog.atomic_json

        def fail(path: Path, value: dict, **kwargs: object) -> None:
            raise OSError("Catalog write failed")

        with patch.object(project_catalog, "atomic_json", side_effect=fail):
            with self.assertRaisesRegex(OSError, "Catalog write failed"):
                create_logical_project(self.vault, "second", "Second", "paper", "thesis")
        self.assertIs(project_catalog.atomic_json, original)
        self.assertEqual(self.files(), before)
        self.assertFalse((self.vault / "Research Dashboard/Projects/second").exists())
        self.assertFalse((self.vault / "Research Dashboard/System/cards/projects/second").exists())

    def test_reconciliation_never_archives_a_live_sourceless_project(self) -> None:
        create_logical_project(self.vault, "thesis", "Thesis", "thesis")
        create_logical_project(self.vault, "chapter", "Chapter", "paper", "thesis")
        self.connect("paper")
        before = self.files()
        preview = reconcile(self.vault)
        self.assertEqual((preview["orphanProjectIds"], preview["files"], preview["warnings"]), ([], [], []))
        applied = reconcile(self.vault, apply=True)
        self.assertEqual((applied["orphanProjectIds"], applied["files"], applied["archivePath"]), ([], [], None))
        self.assertEqual(self.files(), before)
        self.assertEqual(sorted(item["projectId"] for item in project_catalogs(self.vault)), ["chapter", "paper", "thesis"])

    def test_deleted_sourceless_project_archives_only_its_catalog_and_cards(self) -> None:
        create_logical_project(self.vault, "thesis", "Thesis", "thesis")
        self.connect("paper")
        (self.vault / "Research Dashboard/Projects/thesis/Project.md").unlink()
        applied = reconcile(self.vault, apply=True)
        self.assertEqual(applied["orphanProjectIds"], ["thesis"])
        self.assertEqual(applied["orphanSourceIds"], [])
        self.assertEqual(sorted({item["projectId"] for item in applied["files"]}), ["thesis"])
        self.assertFalse((self.vault / CATALOG / "thesis.json").exists())
        self.assertTrue((self.vault / CATALOG / "paper.json").exists())
        self.assertEqual([item["projectId"] for item in project_catalogs(self.vault)], ["paper"])

    def test_connections_infer_type_and_attaching_preserves_a_sourceless_record(self) -> None:
        paper = self.connect("paper")
        note = (self.vault / paper["projectRecord"]).read_text(encoding="utf-8")
        self.assertIn('project_type: "paper"', note)
        self.assertIn("parent_id: null", note)
        thermal = self.connect("thermal", "abaqus")
        self.assertIn('project_type: "simulation"', (self.vault / thermal["projectRecord"]).read_text(encoding="utf-8"))
        create_logical_project(self.vault, "experiment", "Lab tests", "experiment")
        record = self.vault / "Research Dashboard/Projects/experiment/Project.md"
        record.write_text(record.read_text(encoding="utf-8") + "\nUser notes stay.\n", encoding="utf-8")
        original = record.read_bytes()
        (self.source / "job2.sta").write_text("THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n", encoding="utf-8")
        attached = connect_folder(self.vault, "test-host", self.source, "rig", "abaqus", "Rig", {"logPatterns": ["job2.sta"]},
                                  dashboard_project="experiment")
        self.assertEqual(attached["dashboardProjectId"], "experiment")
        self.assertEqual(record.read_bytes(), original)
        self.assertEqual(read_json(self.vault / CATALOG / "experiment.json")["sourceIds"], ["rig"])
        self.assertEqual(sorted(config["projectId"] for config in active_configurations(self.vault)), ["paper", "rig", "thermal"])

    def test_cli_create_project_contract_and_error_exit(self) -> None:
        created = json.loads(self.cli("create-project", "--id", "thesis", "--title", "PhD thesis · α", "--type", "thesis").stdout)
        self.assertEqual((created["dashboardProjectId"], created["projectType"], created["parentId"]), ("thesis", "thesis", None))
        child = json.loads(self.cli("create-project", "--id", "paper-one", "--title", "Paper one", "--parent", "thesis").stdout)
        self.assertEqual((child["projectType"], child["parentId"]), ("other", "thesis"))
        listed = json.loads(self.cli("projects").stdout)["projects"]
        self.assertEqual({item["projectId"]: item["sourceIds"] for item in listed}, {"paper-one": [], "thesis": []})
        self.assertEqual(json.loads(self.cli("collect").stdout), [])
        failed = self.cli("create-project", "--id", "thesis", "--title", "Again", check=False)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("already exists", failed.stderr)
        nested = self.cli("create-project", "--id", "nested", "--title", "Nested", "--parent", "paper-one", check=False)
        self.assertEqual(nested.returncode, 2)
        self.assertIn("sub-project", nested.stderr)
        invalid = self.cli("create-project", "--id", "x", "--title", "X", "--type", "poster", check=False)
        self.assertEqual(invalid.returncode, 2)


if __name__ == "__main__":
    unittest.main()
