"""Live scan and debounce checks on temporary saved-file fixtures."""

from __future__ import annotations

import os
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from watcher import SavedChangeSchedule, configuration_fingerprint, latex_fingerprint
from onboarding import connect_folder
from dashboard_core import read_json


class WatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def file(self, name: str, text: str = "body") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_saved_create_edit_atomic_replace_rename_and_remove_are_observed(self) -> None:
        first = latex_fingerprint(self.root)
        chapter = self.file("chapters/methods.tex")
        created = latex_fingerprint(self.root)
        self.assertNotEqual(first, created)
        chapter.write_text("changed body", encoding="utf-8")
        edited = latex_fingerprint(self.root)
        self.assertNotEqual(created, edited)
        replacement = self.file("replacement.tmp", "a longer replacement body")
        os.replace(replacement, chapter)
        replaced = latex_fingerprint(self.root)
        self.assertNotEqual(edited, replaced)
        renamed = chapter.with_name("results.tex")
        chapter.rename(renamed)
        self.assertNotEqual(replaced, latex_fingerprint(self.root))
        renamed.unlink()
        self.assertEqual(first, latex_fingerprint(self.root))

    def test_bibliography_graphics_and_log_change_but_unrelated_files_do_not(self) -> None:
        previous = latex_fingerprint(self.root)
        for name in ["references.bib", "figures/image.svg", "build/main.log"]:
            self.file(name)
            current = latex_fingerprint(self.root)
            self.assertNotEqual(previous, current)
            previous = current
        self.file("simulation.odb")
        self.file(".git/hidden.tex")
        self.file("node_modules/ignored.tex")
        self.assertEqual(previous, latex_fingerprint(self.root))

    def test_changed_mapping_or_config_has_different_signature(self) -> None:
        self.assertNotEqual(configuration_fingerprint({"source": "a"}),
                            configuration_fingerprint({"source": "b"}))
        self.assertEqual(configuration_fingerprint({"a": 1, "b": 2}),
                         configuration_fingerprint({"b": 2, "a": 1}))

    def test_initial_collection_then_one_debounced_collection_per_save_burst(self) -> None:
        schedule = SavedChangeSchedule()
        schedule.observe("initial", 0)
        self.assertTrue(schedule.ready(0))
        schedule.collected("initial", 0, 60)
        self.assertFalse(schedule.ready(1))
        schedule.observe("save-a", 1)
        self.assertFalse(schedule.ready(1.5))
        schedule.observe("save-b", 1.8)
        self.assertFalse(schedule.ready(2.5))
        self.assertTrue(schedule.ready(2.9))
        schedule.collected("save-b", 3, 60)
        self.assertFalse(schedule.ready(4))
        self.assertTrue(schedule.ready(63))

    def test_changes_during_collection_remain_queued(self) -> None:
        schedule = SavedChangeSchedule()
        schedule.observe("started", 0)
        captured = schedule.signature
        schedule.observe("newer", 1)
        schedule.collected(captured, 2, 60)
        self.assertTrue(schedule.ready(2))

    def test_pending_save_does_not_get_read_before_stable_at_periodic_deadline(self) -> None:
        schedule = SavedChangeSchedule()
        schedule.observe("old", 0)
        schedule.collected("old", 0, 60)
        schedule.observe("saving", 59.9)
        self.assertFalse(schedule.ready(60))
        self.assertTrue(schedule.ready(61))

    def test_running_cli_commits_saved_edit_before_sixty_second_fallback(self) -> None:
        source = self.root / "source"
        vault = self.root / "vault"
        source.mkdir()
        vault.mkdir()
        main = source / "main.tex"
        main.write_text("\\documentclass{article}\n\\begin{document}\n\\section{Methods}\\label{sec:methods}\nInitial body.\n\\end{document}\n", encoding="utf-8")
        connect_folder(vault, "test-host", source, "paper", "latex", "Temporary paper",
                       {"mainFile": "main.tex", "chapters": ["main.tex"], "logPatterns": ["main.log"]})
        script = Path(__file__).resolve().parents[1] / "cli.py"
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        child = subprocess.Popen([sys.executable, "-u", str(script), "--vault", str(vault),
                                  "--host-id", "test-host", "watch"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8", env=environment)
        events: queue.Queue[dict] = queue.Queue()

        def consume() -> None:
            assert child.stdout is not None
            for line in child.stdout:
                item = json.loads(line)
                if item.get("event") == "snapshot-committed":
                    events.put(item)

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        try:
            first = events.get(timeout=20)
            self.assertEqual(first["projectId"], "paper")
            first_state = read_json(vault / "Research Dashboard/System/data/projects/paper.json")
            first_paper = next(item for item in first_state["details"] if item.get("kind") == "paper")
            initial_words = first_paper["chapters"][0]["bodyWords"]
            saved_at = time.monotonic()
            main.write_text("\\documentclass{article}\n\\begin{document}\n\\section{Methods}\\label{sec:methods}\nSaved changes add several verified words to this chapter.\n\\end{document}\n", encoding="utf-8")
            updated = events.get(timeout=20)
            elapsed = time.monotonic() - saved_at
            self.assertNotEqual(first["revision"], updated["revision"])
            self.assertLess(elapsed, 20)
            state = read_json(vault / "Research Dashboard/System/data/projects/paper.json")
            paper = next(item for item in state["details"] if item.get("kind") == "paper")
            self.assertGreater(paper["chapters"][0]["bodyWords"], 2)
            self.assertIsInstance(paper["chapters"][0]["deltaToday"], int)
            self.assertEqual(paper["chapters"][0]["deltaWeek"], paper["chapters"][0]["bodyWords"] - initial_words)
            self.assertEqual(paper["activity"]["week"]["coverage"], "partial")
            self.assertEqual(paper["activity"]["week"]["baselineAt"], first_paper["activity"]["week"]["baselineAt"])
            print(f"\nSaved LaTeX edit committed in {elapsed:.2f} seconds (temporary fixture).")
        finally:
            child.terminate()
            child.wait(timeout=10)
            reader.join(timeout=2)
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()


if __name__ == "__main__":
    unittest.main()
