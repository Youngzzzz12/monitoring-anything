"""Project deletion stops monitoring; owned internal files are archived recoverably."""

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

from dashboard_core import atomic_json, read_json, register
from onboarding import connect_folder
from project_catalog import active_configurations, new_catalog, project_catalogs
from reconciliation import reconcile


class ReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.source = self.root / "source"
        self.vault.mkdir()
        self.source.mkdir()
        (self.source / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
        (self.source / "main.log").write_text("Output written on main.pdf (1 page).\n", encoding="utf-8")
        self.result = connect_folder(self.vault, "test-host", self.source, "paper", "latex", "Lunar project",
                                     {"chapters": ["main.tex"], "logPatterns": ["main.log"]})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def files(self) -> dict[str, bytes]:
        return {path.relative_to(self.vault).as_posix(): path.read_bytes()
                for path in self.vault.rglob("*") if path.is_file()}

    def delete_record(self) -> None:
        (self.vault / self.result["projectRecord"]).unlink()

    def cli(self, arguments: list[str]) -> subprocess.CompletedProcess:
        script = Path(__file__).resolve().parents[1] / "cli.py"
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, str(script), "--vault", str(self.vault), "--host-id", "test-host", *arguments],
                              capture_output=True, text=True, encoding="utf-8", env=environment, check=True)

    def test_missing_record_immediately_disappears_and_source_stops_collecting(self) -> None:
        state = self.vault / "Research Dashboard/System/data/projects/paper.json"
        original = (state.read_bytes(), state.stat().st_mtime_ns)
        self.delete_record()
        self.assertEqual(project_catalogs(self.vault), [])
        self.assertEqual(active_configurations(self.vault), [])
        self.assertEqual(json.loads(self.cli(["projects"]).stdout), {"projects": []})
        self.assertEqual(json.loads(self.cli(["collect"]).stdout), [])
        self.assertEqual(json.loads(self.cli(["status"]).stdout), [])
        self.assertEqual(json.loads(self.cli(["doctor"]).stdout)["checks"], [])
        self.cli(["watch", "--once"])
        self.assertEqual((state.read_bytes(), state.stat().st_mtime_ns), original)

    def test_dry_run_is_default_and_does_not_change_any_file(self) -> None:
        self.delete_record()
        before = self.files()
        result = json.loads(self.cli(["reconcile"]).stdout)
        self.assertFalse(result["applied"])
        self.assertIsNone(result["archivePath"])
        self.assertEqual(result["orphanProjectIds"], ["paper"])
        self.assertEqual(result["orphanSourceIds"], ["paper"])
        history_path = "Research Dashboard/System/data/history/paper.json"
        self.assertIn(history_path, [item["path"] for item in result["files"]])
        self.assertIn("Research Dashboard/System/cards/projects/paper/progress.md", [item["path"] for item in result["files"]])
        self.assertEqual(len(result["files"]), 11)
        self.assertEqual(self.files(), before)
        self.assertFalse((self.vault / ".trash").exists())

    def test_apply_archives_owned_internal_files_preserving_manual_content_aliases_and_sources(self) -> None:
        self.delete_record()
        canvas = self.vault / self.result["projectDashboard"]
        original_canvas = canvas.read_bytes()
        alias = self.vault / "Research Dashboard/System/config/local/test-host.json"
        original_alias = alias.read_bytes()
        external = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.source.iterdir()}
        unknown_card = self.vault / "Research Dashboard/System/cards/projects/paper/keep.txt"
        unknown_card.write_text("Manually maintained", encoding="utf-8")
        unknown_source = self.vault / "Research Dashboard/System/records/paper/keep.md"
        unknown_source.write_text("Keep this research evidence", encoding="utf-8")
        preview = reconcile(self.vault)
        originals = {item["path"]: (self.vault / item["path"]).read_bytes() for item in preview["files"]}
        result = json.loads(self.cli(["reconcile", "--apply"]).stdout)
        self.assertTrue(result["applied"])
        self.assertTrue(result["archivePath"].startswith(".trash/research-dashboard/"))
        archive = self.vault / result["archivePath"]
        for relative, payload in originals.items():
            self.assertFalse((self.vault / relative).exists())
            self.assertEqual((archive / relative).read_bytes(), payload)
        manifest = read_json(archive / "_reconciliation.json")
        self.assertEqual(manifest["files"], result["files"])
        self.assertEqual(canvas.read_bytes(), original_canvas)
        self.assertEqual(alias.read_bytes(), original_alias)
        self.assertEqual(unknown_card.read_text(encoding="utf-8"), "Manually maintained")
        self.assertEqual(unknown_source.read_text(encoding="utf-8"), "Keep this research evidence")
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.source.iterdir()}
        self.assertEqual(after, external)
        again = reconcile(self.vault, apply=True)
        self.assertEqual(again["files"], [])
        self.assertIsNone(again["archivePath"])

    def test_existing_record_remains_live_even_if_canvas_was_deleted(self) -> None:
        (self.vault / self.result["projectDashboard"]).unlink()
        self.assertEqual(len(project_catalogs(self.vault)), 1)
        self.assertEqual(len(active_configurations(self.vault)), 1)
        self.assertEqual(reconcile(self.vault)["files"], [])

    def test_history_with_wrong_source_ownership_is_preserved(self) -> None:
        history = self.vault / "Research Dashboard/System/data/history/paper.json"
        atomic_json(history, {"schemaVersion": 1, "sourceId": "another-source", "projectId": "paper", "snapshots": []})
        before = history.read_bytes()
        self.delete_record()
        result = reconcile(self.vault, apply=True)
        self.assertEqual(history.read_bytes(), before)
        self.assertTrue(any("history" in warning and "identity" in warning for warning in result["warnings"]))

    def test_customized_progress_card_is_preserved_during_owned_cleanup(self) -> None:
        progress = self.vault / "Research Dashboard/System/cards/projects/paper/progress.md"
        progress.write_text("# My manually maintained progress\n\nKeep this evidence.\n", encoding="utf-8")
        before = progress.read_bytes()
        self.delete_record()
        result = reconcile(self.vault, apply=True)
        self.assertEqual(progress.read_bytes(), before)
        self.assertNotIn(progress.relative_to(self.vault).as_posix(), [item["path"] for item in result["files"]])
        self.assertTrue(any("Progress card" in warning for warning in result["warnings"]))

    def test_source_only_and_unowned_configurations_are_preserved(self) -> None:
        other = read_json(self.vault / "Research Dashboard/System/config/projects/paper.json")
        other.pop("dashboardProjectId", None)
        other.update(projectId="independent", title="Independent source", projectNote="Research Dashboard/System/records/independent/project.md")
        register(self.vault, other)
        self.delete_record()
        kept = self.vault / "Research Dashboard/System/config/projects/independent.json"
        original = kept.read_bytes()
        self.assertEqual([item["projectId"] for item in active_configurations(self.vault)], ["independent"])
        reconcile(self.vault, apply=True)
        self.assertEqual(kept.read_bytes(), original)

    def test_live_catalog_membership_protects_shared_source(self) -> None:
        original_source = read_json(self.vault / "Research Dashboard/System/config/projects/paper.json")
        original_source.pop("dashboardProjectId", None)
        atomic_json(self.vault / "Research Dashboard/System/config/projects/paper.json", original_source)
        second = new_catalog("second", "Another project", "paper")
        atomic_json(self.vault / "Research Dashboard/System/config/catalog/second.json", second)
        note = self.vault / second["projectNote"]
        note.parent.mkdir(parents=True)
        note.write_text("Active research record", encoding="utf-8")
        self.delete_record()
        self.assertEqual([item["projectId"] for item in project_catalogs(self.vault)], ["second"])
        self.assertEqual([item["projectId"] for item in active_configurations(self.vault)], ["paper"])
        result = reconcile(self.vault, apply=True)
        self.assertEqual(result["orphanSourceIds"], [])
        self.assertTrue(any("shared or ambiguous" in warning for warning in result["warnings"]))
        self.assertTrue((self.vault / "Research Dashboard/System/config/projects/paper.json").is_file())

    def test_explicit_owner_stops_collection_and_is_reconciled_when_catalog_and_record_removed(self) -> None:
        (self.vault / "Research Dashboard/System/config/catalog/paper.json").unlink()
        self.delete_record()
        self.assertEqual(project_catalogs(self.vault), [])
        self.assertEqual(active_configurations(self.vault), [])
        result = reconcile(self.vault, apply=True)
        self.assertEqual(result["orphanProjectIds"], ["paper"])
        self.assertEqual(result["orphanSourceIds"], ["paper"])
        self.assertFalse((self.vault / "Research Dashboard/System/config/projects/paper.json").exists())

    def test_missing_catalog_with_live_record_stops_collection_but_preserves_its_source(self) -> None:
        (self.vault / "Research Dashboard/System/config/catalog/paper.json").unlink()
        before = self.files()
        self.assertEqual(active_configurations(self.vault), [])
        result = reconcile(self.vault, apply=True)
        self.assertEqual(result["files"], [])
        self.assertIsNone(result["archivePath"])
        self.assertEqual(self.files(), before)

    def test_unverified_source_config_and_state_are_preserved(self) -> None:
        config_path = self.vault / "Research Dashboard/System/config/projects/paper.json"
        config = read_json(config_path)
        config["projectNote"] = "Research Dashboard/other.md"
        atomic_json(config_path, config)
        self.delete_record()
        before = config_path.read_bytes()
        result = reconcile(self.vault, apply=True)
        self.assertEqual(result["orphanSourceIds"], [])
        self.assertEqual(config_path.read_bytes(), before)
        self.assertTrue((self.vault / "Research Dashboard/System/data/projects/paper.json").is_file())
        self.assertTrue(any("unverified source" in warning for warning in result["warnings"]))

    def test_changed_project_is_preserved_during_apply(self) -> None:
        self.delete_record()
        import reconciliation
        original_plan = reconciliation._plan

        def restore_record(vault: Path):
            result, payloads = original_plan(vault)
            (self.vault / self.result["projectRecord"]).write_text("Restored user record", encoding="utf-8")
            return result, payloads

        with patch.object(reconciliation, "_plan", side_effect=restore_record):
            result = reconcile(self.vault, apply=True)
        self.assertEqual(result["files"], [])
        self.assertIsNone(result["archivePath"])
        self.assertTrue((self.vault / "Research Dashboard/System/config/catalog/paper.json").is_file())

    def test_generated_wrappers_have_no_properties_and_new_record_has_scoped_class(self) -> None:
        wrapper = (self.vault / "Research Dashboard/System/cards/projects/paper/overview.md").read_text(encoding="utf-8")
        self.assertTrue(wrapper.startswith("```dataviewjs\n"))
        self.assertNotIn("cssclasses:", wrapper)
        record = (self.vault / self.result["projectRecord"]).read_text(encoding="utf-8")
        self.assertIn('"research-project-record"', record)


if __name__ == "__main__":
    unittest.main()
