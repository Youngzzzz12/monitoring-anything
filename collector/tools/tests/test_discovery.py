"""Sub-project discovery: scanner rules, dismissals, ignore globs, already-connected folders and auto-accept."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discovery
from dashboard_core import read_json
from onboarding import connect_folder, inspect_folder
from project_catalog import create_logical_project, project_catalogs

CATALOG = "Research Dashboard/System/config/catalog"
LOCAL = "Research Dashboard/System/config/local/test-host.json"
PART_PROFILE = {
    "schemaVersion": "1.0", "id": "batch-case", "title": "Extra batch case", "projectType": "simulation", "sourceKind": "isale",
    "origin": {"createdBy": "agent:test", "approvedAt": None, "version": 1},
    "detect": {"allOf": [{"glob": "case.json"}], "threshold": 0.8},
    "extract": {"cases": {"completed": {"countGlob": "case.json"}}},
    "subprojectRole": "part",
}


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = base / "vault"
        self.root = base / "papers"
        self.vault.mkdir()
        self.root.mkdir()
        create_logical_project(self.vault, "phd", "PhD", "thesis")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def folder(self, name: str, files: dict[str, str] | None = None) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        for relative, text in (files or {}).items():
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return path

    def paper(self, name: str) -> Path:
        return self.folder(name, {"main.tex": "\\documentclass{article}\n\\begin{document}\n\\section{Aim}\nText\n\\end{document}\n"})

    def configure(self, **options) -> dict:
        return discovery.configure(self.vault, "test-host", "phd", folder=str(self.root), **options)

    def cli(self, *arguments: str) -> subprocess.CompletedProcess:
        script = Path(__file__).resolve().parents[1] / "cli.py"
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, str(script), "--vault", str(self.vault), "--host-id", "test-host", *arguments],
                              capture_output=True, text=True, encoding="utf-8", env=environment)

    def test_configure_keeps_the_absolute_folder_host_local(self) -> None:
        shown = self.configure(auto_accept=True, ignore=["drafts-*, old"])
        catalog = read_json(self.vault / CATALOG / "phd.json")
        self.assertEqual(catalog["discovery"], {"rootAlias": "discovery:phd", "autoAccept": True, "ignore": ["drafts-*", "old"]})
        self.assertNotIn(str(self.root), json.dumps(catalog), "The shared catalog never stores the absolute folder.")
        self.assertEqual(read_json(self.vault / LOCAL)["aliases"]["discovery:phd"], str(self.root.resolve()))
        self.assertEqual(shown["folder"], str(self.root.resolve()))
        self.assertTrue(shown["available"] and shown["topLevel"] and shown["autoAccept"])
        # The project list still validates with the extra key.
        self.assertEqual([item["projectId"] for item in project_catalogs(self.vault)], ["phd"])
        other = discovery.show(self.vault, "other-host", "phd")
        self.assertEqual((other["configured"], other["available"], other["folder"]), (True, False, None),
                         "Another computer sees the setting but has no folder until it sets its own.")

    def test_scan_lists_new_folders_and_skips_ignored_connected_and_absorbed(self) -> None:
        user = self.vault / "Research Dashboard/System/profiles/user"
        user.mkdir(parents=True)
        (user / "batch-case.json").write_text(json.dumps(PART_PROFILE), encoding="utf-8")
        self.paper("review-2027")
        self.folder("notes", {"readme.txt": "Ideas"})
        for name in (".git", ".hidden", "build", "node_modules", "tmp", "__pycache__", "Drafts-old"):
            self.folder(name, {"main.tex": "\\documentclass{article}\n"})
        self.folder("case-07", {"case.json": "{}"})
        connected = self.paper("connected-paper")
        (self.root / "loose-file.tex").write_text("x", encoding="utf-8")
        inspected = inspect_folder(connected)["profiles"][0]
        connect_folder(self.vault, "test-host", connected, "connected-paper", "latex", "Connected", inspected["adapter"])
        self.configure(ignore=["drafts-*"])
        result = discovery.scan(self.vault, "test-host", "phd")
        printed = discovery.public(result)
        self.assertEqual([item["relative"] for item in printed["suggestions"]], ["notes", "review-2027"])
        notes, review = printed["suggestions"]
        self.assertIsNone(notes["bestCandidate"], "An unmatched folder is offered for Ask agent.")
        best = review["bestCandidate"]
        self.assertEqual((best["profileId"], best["projectType"], best["subprojectRole"]), ("latex-paper", "paper", "standalone"))
        self.assertGreaterEqual(best["confidence"], 0.9)
        self.assertEqual(review["id"], "phd:review-2027")
        self.assertEqual((review["projectId"], review["parentId"], review["rootName"]), ("phd", "phd", "papers"))
        self.assertEqual((review["suggestedId"], review["suggestedTitle"]), ("review-2027", "review-2027"))
        self.assertEqual(review["folder"], str((self.root / "review-2027").resolve()))
        self.assertNotIn("_inspection", review)
        info = printed["projects"][0]
        self.assertEqual((info["ignored"], info["connected"], info["absorbed"], info["scanned"]), (7, 1, 1, 3))
        self.assertEqual(printed["dismissed"], 0)

    def test_dismissals_persist_by_relative_name_and_can_be_restored(self) -> None:
        self.paper("review-2027")
        self.paper("other")
        self.configure()
        discovery.dismiss(self.vault, "test-host", "phd", str(self.root / "review-2027"))
        discovery.dismiss(self.vault, "test-host", "phd", "review-2027")
        state = read_json(self.vault / discovery.STATE_PATH)
        self.assertEqual([item["folder"] for item in state["projects"]["phd"]["dismissed"]], ["review-2027"])
        self.assertNotIn(str(self.root), json.dumps(state))
        result = discovery.scan(self.vault, "test-host")
        self.assertEqual([item["relative"] for item in result["suggestions"]], ["other"])
        self.assertEqual(result["dismissed"], 1)
        with self.assertRaises(ValueError):
            discovery.dismiss(self.vault, "test-host", "phd", "../escape")
        with self.assertRaises(ValueError):
            discovery.dismiss(self.vault, "test-host", "phd", str(self.vault))
        with self.assertRaises(ValueError):
            discovery.accept(self.vault, "test-host", "phd", "review-2027")
        discovery.restore(self.vault, "phd", "REVIEW-2027")
        self.assertEqual(len(discovery.scan(self.vault, "test-host")["suggestions"]), 2)

    def test_only_top_level_projects_take_a_folder_and_a_new_folder_resets_dismissals(self) -> None:
        create_logical_project(self.vault, "chapter", "Chapter", "paper", "phd")
        with self.assertRaisesRegex(ValueError, "top-level"):
            discovery.configure(self.vault, "test-host", "chapter", folder=str(self.root))
        with self.assertRaisesRegex(ValueError, "outside the vault"):
            discovery.configure(self.vault, "test-host", "phd", folder=str(self.vault / "Research Dashboard"))
        with self.assertRaisesRegex(ValueError, "Choose a discovery folder first"):
            discovery.configure(self.vault, "test-host", "phd", auto_accept=True)
        self.paper("review-2027")
        self.configure()
        discovery.dismiss(self.vault, "test-host", "phd", "review-2027")
        self.configure(auto_accept=True)
        self.assertEqual(discovery.show(self.vault, "test-host", "phd")["dismissed"], ["review-2027"],
                         "Changing options under the same folder keeps dismissals.")
        moved = self.root.parent / "elsewhere"
        moved.mkdir()
        discovery.configure(self.vault, "test-host", "phd", folder=str(moved))
        self.assertEqual(discovery.show(self.vault, "test-host", "phd")["dismissed"], [])
        cleared = discovery.configure(self.vault, "test-host", "phd", clear=True)
        self.assertFalse(cleared["configured"])
        self.assertNotIn("discovery", read_json(self.vault / CATALOG / "phd.json"))
        self.assertNotIn("discovery:phd", read_json(self.vault / LOCAL)["aliases"])
        self.assertEqual(discovery.scan(self.vault, "test-host")["suggestions"], [])

    def test_a_moved_project_files_suggestions_under_its_top_level_parent(self) -> None:
        self.paper("review-2027")
        self.configure()
        create_logical_project(self.vault, "umbrella", "Umbrella", "other")
        note = self.vault / "Research Dashboard/Projects/phd/Project.md"
        note.write_text(note.read_text(encoding="utf-8").replace("parent_id: null", 'parent_id: "umbrella"'), encoding="utf-8")
        suggestion = discovery.scan(self.vault, "test-host", "phd")["suggestions"][0]
        self.assertEqual((suggestion["projectId"], suggestion["parentId"]), ("phd", "umbrella"))

    def test_accept_connects_the_best_candidate_as_a_sub_project_with_its_milestone_template(self) -> None:
        self.paper("review-2027")
        self.folder("notes", {"readme.txt": "Ideas"})
        self.configure(auto_accept=True)
        suggestion = discovery.scan(self.vault, "test-host", "phd")["suggestions"][1]
        self.assertTrue(suggestion["autoAccept"])
        result = discovery.accept(self.vault, "test-host", "phd", "review-2027")
        connected = result["connect"]
        self.assertEqual((connected["dashboardProjectId"], connected["parentId"], connected["profileId"], connected["projectType"]),
                         ("review-2027", "phd", "latex-paper", "paper"))
        self.assertEqual(connected["milestones"]["templateId"], "paper-section-draft-complete")
        note = (self.vault / "Research Dashboard/Projects/review-2027/Project.md").read_text(encoding="utf-8")
        self.assertIn('parent_id: "phd"', note)
        self.assertEqual([item["relative"] for item in discovery.scan(self.vault, "test-host", "phd")["suggestions"]], ["notes"],
                         "A connected folder is no longer suggested.")
        with self.assertRaisesRegex(ValueError, "confidence"):
            discovery.accept(self.vault, "test-host", "phd", "notes")
        with self.assertRaisesRegex(ValueError, "not a new folder"):
            discovery.accept(self.vault, "test-host", "phd", "review-2027")

    def test_cli_scan_requires_a_scope_and_prints_json(self) -> None:
        self.paper("review-2027")
        self.assertEqual(self.cli("discover").returncode, 2)
        self.assertEqual(json.loads(self.cli("discover", "--all").stdout)["suggestions"], [], "No discovery folder, nothing to scan.")
        configured = self.cli("discover", "configure", "--project", "phd", "--folder", str(self.root), "--auto-accept", "on")
        self.assertEqual(configured.returncode, 0, configured.stderr)
        output = json.loads(self.cli("discover", "--all").stdout)
        self.assertEqual(output["suggestions"][0]["bestCandidate"]["profileId"], "latex-paper")
        self.assertEqual(json.loads(self.cli("discover", "dismiss", "--project", "phd", "--folder", "review-2027").stdout)["dismissed"], True)
        self.assertEqual(json.loads(self.cli("discover", "--project", "phd").stdout)["dismissed"], 1)
        missing = self.cli("discover", "dismiss", "--project", "phd")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--folder", missing.stderr)


if __name__ == "__main__":
    unittest.main()
