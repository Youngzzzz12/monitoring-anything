"""Saved synthetic paper sources and actual observation-history contracts."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters import latex
from adapters.common import UTC, iso
from adapters.latex_paper import MAX_HISTORY, analyze, body_words, observed_history
from dashboard_core import CollectionSuperseded, atomic_json, collect_one, register, validate_state

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def in_london(test):
    """Day boundaries follow the computer's zone; these tests pin Europe/London (skipped without a zone database)."""
    def run(self):
        try:
            ZoneInfo("Europe/London")
        except (ZoneInfoNotFoundError, ValueError, OSError):
            self.skipTest("No IANA time zone database (install tzdata)")
        with patch.dict(os.environ, {"RD_TIMEZONE": "Europe/London"}):
            return test(self)
    run.__name__, run.__doc__ = test.__name__, test.__doc__
    return run


class LatexPaperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.vault = Path(self.temporary.name) / "vault"
        self.vault.mkdir()
        atomic_json(self.vault / "Research Dashboard/System/config/local/test-host.json",
                    {"schemaVersion": "1.0", "aliases": {"research": str(self.root)}})
        self.options = {"mainFile": "main.tex", "wordCountMethod": "heuristic"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def file(self, name: str, content: str, stamp: dt.datetime = NOW) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.utime(path, (stamp.timestamp(), stamp.timestamp()))
        return path

    def config(self) -> dict:
        return {"schemaVersion": "1.0", "projectId": "paper", "title": "Test paper", "kind": "latex", "parentId": None,
                "projectNote": "Research Dashboard/System/records/paper/project.md",
                "source": {"rootAlias": "research", "path": ""},
                "collector": {"hostId": "test-host", "intervalSeconds": 60, "staleAfterSeconds": 180},
                "adapter": dict(self.options)}

    def paper(self) -> dict:
        return analyze(self.root, self.options, NOW)[0]

    def test_dynamic_include_structure_and_nested_aggregate(self) -> None:
        self.file("main.tex", r"\documentclass{book}\title{Research paper}\begin{document}\chapter{Methods}\label{ch:methods}One two.\input{sections/model}\end{document}")
        self.file("sections/model.tex", "\\section{Model}\n\\label{sec:model}\nThree four five.\n\\subsection{Validation}\\label{sec:validation}Six seven.")
        paper = self.paper()
        self.assertEqual(paper["title"], "Research paper")
        self.assertEqual([chapter["title"] for chapter in paper["chapters"]], ["Methods", "Model", "Validation"])
        first, second, third = paper["chapters"]
        self.assertEqual((first["ownBodyWords"], first["bodyWords"]), (2, 7))
        self.assertEqual((second["ownBodyWords"], second["bodyWords"]), (3, 5))
        self.assertEqual(second["parentId"], first["id"])
        self.assertEqual(third["parentId"], second["id"])
        self.assertEqual(second["file"], "sections/model.tex")
        self.assertEqual(second["line"], 1)
        self.assertEqual(paper["bodyWords"], 7)
        self.assertIn("sections/model.tex", paper["dependencies"])
        self.file("sections/new.tex", r"\section{New section}\label{sec:new}Eight nine.")
        self.file("sections/model.tex", r"\section{Model}\label{sec:model}Three four five.\input{new}")
        updated = self.paper()
        self.assertIn("sections/new.tex", updated["dependencies"])
        self.assertEqual([chapter["title"] for chapter in updated["chapters"]], ["Methods", "Model", "New section"])

    def test_labels_survive_rename_and_insertion_unlabelled_rename_is_new_identity(self) -> None:
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two.\section{Discussion}Three.")
        before = self.paper()
        self.file("main.tex", r"\section{Introduction}\label{sec:intro}Start.\section{Approach}\label{sec:methods}One two.\section{Interpretation}Three.")
        after = self.paper()
        self.assertEqual(before["chapters"][0]["id"], after["chapters"][1]["id"])
        self.assertNotEqual(before["chapters"][1]["id"], after["chapters"][2]["id"])

    def test_ambiguous_duplicate_titles_do_not_shift_old_bindings_after_insertion(self) -> None:
        self.file("main.tex", r"\section{Case}First.\section{Case}Second.")
        before = {chapter["id"] for chapter in self.paper()["chapters"]}
        self.file("main.tex", r"\section{Case}New.\section{Case}First.\section{Case}Second.")
        after = {chapter["id"] for chapter in self.paper()["chapters"]}
        self.assertFalse(before.intersection(after))

    def test_preamble_comments_math_caption_refs_do_not_count_as_body(self) -> None:
        self.file("main.tex", "\\documentclass{article}\n\\title{Not body words}\n\\begin{document}\n% \\section{Fake}\n\\section{Methods}\n\\label{sec:methods}\nOne \\textbf{two} three. $x+y$ \\cite{entry}\n\\caption{Not body words}\n\\begin{equation}some math words\\end{equation}\n\\end{document}")
        paper = self.paper()
        self.assertEqual([chapter["title"] for chapter in paper["chapters"]], ["Methods"])
        self.assertEqual(paper["bodyWords"], 3)
        self.assertIn("Heuristic", paper["countingMethod"])
        self.assertEqual(body_words("One \\ref{ignored} \\caption{ignored words} two."), 2)

    def test_formula_edit_changes_source_hash_even_when_body_words_stay_constant(self) -> None:
        self.file("main.tex", r"\section{Methods}\label{sec:methods}Text.\begin{equation}x+y\end{equation}")
        first = self.paper()["chapters"][0]
        self.file("main.tex", r"\section{Methods}\label{sec:methods}Text.\begin{equation}x-y\end{equation}")
        second = self.paper()["chapters"][0]
        self.assertEqual(first["bodyWords"], second["bodyWords"])
        self.assertNotEqual(first["sourceHash"], second["sourceHash"])

    def test_preamble_include_and_bibliography_graphics_affect_build_freshness(self) -> None:
        old = NOW - dt.timedelta(minutes=2)
        self.file("main.tex", r"\documentclass{article}\input{settings}\addbibresource{refs.bib}\begin{document}\section{Methods}\label{sec:methods}Text.\includegraphics{plot}\end{document}", old)
        self.file("settings.tex", r"\newcommand{\name}{Old}", old)
        self.file("refs.bib", "bibliography content", old)
        self.file("plot.png", "graphic", old)
        self.file("main.log", "Output written on main.pdf (1 page).\n", old)
        self.file("main.pdf", "pdf", old)
        before = latex.collect(self.root, self.options, NOW)
        self.assertFalse(next(detail for detail in before["details"] if detail["kind"] == "build")["buildOutdated"])
        self.file("refs.bib", "changed bibliography")
        after = latex.collect(self.root, self.options, NOW)
        self.assertTrue(next(detail for detail in after["details"] if detail["kind"] == "build")["buildOutdated"])
        self.assertIsNone(after["artifacts"][0]["verifiedAt"])
        self.assertEqual(set(next(detail for detail in after["details"] if detail["kind"] == "paper")["dependencies"]),
                         {"main.tex", "settings.tex", "refs.bib", "plot.png"})

    def test_outside_root_and_cycle_are_skipped_without_reads_or_fake_nodes(self) -> None:
        outside = self.root.parent / "outside.tex"
        outside.write_text(r"\section{Private}Never read", encoding="utf-8")
        self.file("main.tex", r"\section{Methods}One.\input{../outside}\input{loop}")
        self.file("loop.tex", r"\input{main}")
        paper, paths, _ = analyze(self.root, self.options, NOW)
        self.assertNotIn(outside, paths)
        self.assertEqual([chapter["title"] for chapter in paper["chapters"]], ["Methods"])
        self.assertTrue(any("outside" in warning for warning in paper["warnings"]))
        self.assertTrue(any("Cyclic" in warning for warning in paper["warnings"]))

    def test_literal_graphics_paths_resolve_punctuated_basenames_and_reject_escape(self) -> None:
        self.file("main.tex", r"\documentclass{article}\graphicspath{{Figures/}{../private/}}\DeclareGraphicsExtensions{.pdf,.png}\begin{document}\section{Methods}Text.\includegraphics{framework.drawio}\includegraphics{plot}\end{document}")
        self.file("Figures/framework.drawio.pdf", "graphic")
        self.file("Figures/plot.png", "graphic")
        paper = self.paper()
        self.assertIn("Figures/framework.drawio.pdf", paper["dependencies"])
        self.assertIn("Figures/plot.png", paper["dependencies"])
        self.assertFalse(any("Declared dependency is missing" in warning for warning in paper["warnings"]))
        self.assertTrue(any("outside the selected folder" in warning for warning in paper["warnings"]))

    def test_missing_explicit_entry_is_an_error_even_with_old_successful_log(self) -> None:
        self.file("main.log", "Output written on main.pdf (1 page).\n")
        self.file("main.pdf", "old build")
        with self.assertRaises(FileNotFoundError):
            latex.collect(self.root, self.options, NOW)

    def test_texcount_is_stdin_only_no_shell_and_validates_all_blocks(self) -> None:
        self.file("main.tex", r"\section{Methods}One two.\section{Results}Three four five.")
        options = {"mainFile": "main.tex"}
        reply = subprocess.CompletedProcess([], 0, "  2+1+0 (1/0/0/0) Section: RD0000\n  3+1+0 (1/0/0/0) Section: RD0001\n", "")
        with patch("adapters.latex_paper.shutil.which", return_value="texcount"), patch("adapters.latex_paper.subprocess.run", return_value=reply) as run, patch("adapters.latex_paper._texcount_version", return_value="3.1.1"):
            paper, _, _ = analyze(self.root, options, NOW)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], "-")
        self.assertIn("-noinc", args[0])
        self.assertIn("-nobib", args[0])
        self.assertNotIn("shell", kwargs)
        self.assertIn("\\section{RD0000}", kwargs["input"])
        self.assertEqual(paper["countingPolicy"], "body-texcount-3.1.1-v1")
        bad = subprocess.CompletedProcess([], 0, "no reliable section subcounts", "")
        with patch("adapters.latex_paper.shutil.which", return_value="texcount"), patch("adapters.latex_paper.subprocess.run", return_value=bad):
            fallback, _, _ = analyze(self.root, options, NOW)
        self.assertEqual(fallback["countingPolicy"], "body-heuristic-v1")
        self.assertEqual(fallback["bodyWords"], 5)

    def test_first_observation_is_a_real_zero_partial_baseline(self) -> None:
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two.")
        paper = self.paper()
        history = observed_history(paper, None, NOW)
        self.assertTrue(paper["activity"]["today"]["available"])
        self.assertEqual(paper["chapters"][0]["deltaToday"], 0)
        self.assertEqual(paper["chapters"][0]["deltaWeek"], 0)
        self.assertEqual(paper["activity"]["today"]["coverage"], "partial")
        self.assertEqual(paper["activity"]["today"]["baselineKind"], "first-observation")
        self.assertEqual(paper["activity"]["week"]["coverage"], "partial")
        self.assertEqual(paper["activity"]["trackingStartedAt"], iso(NOW))
        self.assertEqual(len(history["snapshots"]), 1)
        self.assertEqual(history["dayStarts"]["2026-10-03"]["observedAt"], iso(NOW))

    def test_daytime_opening_baseline_survives_two_collections_in_same_hour(self) -> None:
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two.")
        paper = self.paper()
        history = observed_history(paper, None, NOW)
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two three four.")
        changed = self.paper()
        history = observed_history(changed, history, NOW + dt.timedelta(minutes=10))
        self.assertEqual(changed["chapters"][0]["deltaToday"], 2)
        self.assertEqual(changed["chapters"][0]["deltaWeek"], 2)
        self.assertEqual(changed["activity"]["today"]["baselineAt"], iso(NOW))
        self.assertEqual(history["dayStarts"]["2026-10-03"]["observedAt"], iso(NOW))
        self.assertEqual(history["snapshots"][0]["observedAt"], iso(NOW + dt.timedelta(minutes=10)))
        unchanged = self.paper()
        history = observed_history(unchanged, history, NOW + dt.timedelta(minutes=20))
        self.assertEqual(unchanged["chapters"][0]["deltaToday"], 2)
        self.assertEqual(unchanged["activity"]["trackingStartedAt"], iso(NOW))
        self.assertEqual(len(history["snapshots"]), 1)

    def test_new_day_uses_first_actual_daytime_check_without_overnight_monitoring(self) -> None:
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two.")
        history = observed_history(self.paper(), None, NOW)
        tomorrow = NOW + dt.timedelta(days=1)
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two three.")
        opening = self.paper()
        history = observed_history(opening, history, tomorrow)
        self.assertEqual(opening["chapters"][0]["deltaToday"], 0)
        self.assertEqual(opening["chapters"][0]["deltaWeek"], 1)
        self.assertEqual(opening["activity"]["today"]["baselineAt"], iso(tomorrow))
        self.assertEqual(opening["activity"]["today"]["coverage"], "partial")
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two three four five.")
        edited = self.paper()
        observed_history(edited, history, tomorrow + dt.timedelta(minutes=5))
        self.assertEqual(edited["chapters"][0]["deltaToday"], 2)
        self.assertEqual(edited["chapters"][0]["deltaWeek"], 3)

    @in_london
    def test_london_midnight_and_week_baselines_negative_edits_and_new_ids(self) -> None:
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two.")
        paper = self.paper()
        identity = paper["chapters"][0]["id"]
        # During BST local midnight is 23:00 UTC on the preceding date.
        today_baseline = dt.datetime(2026, 10, 2, 22, 59, tzinfo=UTC)
        history = {"schemaVersion": 1, "mainFile": "main.tex", "countingPolicy": paper["countingPolicy"],
                   "snapshots": [{"observedAt": iso(today_baseline), "revision": "x", "words": {identity: 5}},
                                 {"observedAt": iso(NOW - dt.timedelta(days=7, minutes=1)), "revision": "y", "words": {identity: 1}}]}
        updated = observed_history(paper, history, NOW)
        self.assertTrue(paper["activity"]["today"]["available"])
        self.assertEqual(paper["activity"]["today"]["baselineAt"], iso(today_baseline))
        self.assertEqual(paper["chapters"][0]["deltaToday"], -3)
        self.assertEqual(paper["chapters"][0]["deltaWeek"], 1)
        self.assertEqual(paper["activity"]["today"]["coverage"], "full")
        self.assertEqual(paper["activity"]["today"]["baselineKind"], "boundary")
        self.assertEqual(paper["activity"]["today"]["windowStartAt"], "2026-10-02T23:00:00Z")
        self.assertEqual(paper["activity"]["week"]["coverage"], "full")
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two.\section{New}\label{sec:new}Three.")
        later = self.paper()
        observed_history(later, updated, NOW + dt.timedelta(minutes=1))
        self.assertIsNone(later["chapters"][1]["deltaToday"])

    def test_stale_boundary_falls_back_and_changed_policy_starts_actual_new_baseline(self) -> None:
        self.file("main.tex", r"\section{Methods}One.")
        paper = self.paper()
        identity = paper["chapters"][0]["id"]
        history = {"schemaVersion": 1, "mainFile": "main.tex", "countingPolicy": paper["countingPolicy"],
                   "snapshots": [{"observedAt": iso(NOW - dt.timedelta(days=1)), "words": {identity: 2}}]}
        observed_history(paper, history, NOW)
        self.assertEqual(paper["chapters"][0]["deltaToday"], 0)
        self.assertEqual(paper["activity"]["today"]["coverage"], "partial")
        self.assertEqual(paper["chapters"][0]["deltaWeek"], -1)
        history["countingPolicy"] = "old-policy"
        result = observed_history(paper, history, NOW)
        self.assertEqual(len(result["snapshots"]), 1)
        self.assertEqual(paper["chapters"][0]["deltaWeek"], 0)
        self.assertEqual(result["trackingStartedAt"], iso(NOW))
        self.assertEqual(paper["activity"]["week"]["coverage"], "partial")

    def test_zero_word_baseline_is_valid_and_new_id_does_not_invent_old_zero(self) -> None:
        self.file("main.tex", r"\section{Methods}\label{sec:methods}")
        initial = self.paper()
        history = observed_history(initial, None, NOW)
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two.\section{New}\label{sec:new}Three.")
        changed = self.paper()
        observed_history(changed, history, NOW + dt.timedelta(minutes=5))
        self.assertEqual(changed["chapters"][0]["deltaToday"], 2)
        self.assertIsNone(changed["chapters"][1]["deltaToday"])
        self.assertIsNone(changed["chapters"][1]["deltaWeek"])

    def test_partial_week_uses_earliest_retained_observation_inside_window(self) -> None:
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two three.")
        paper = self.paper()
        identity = paper["chapters"][0]["id"]
        start = NOW - dt.timedelta(days=3)
        history = {"schemaVersion": 1, "mainFile": "main.tex", "countingPolicy": paper["countingPolicy"],
                   "trackingStartedAt": iso(NOW - dt.timedelta(days=20)),
                   "snapshots": [{"observedAt": iso(NOW - dt.timedelta(days=8)), "words": {identity: 100}},
                                 {"observedAt": iso(start), "words": {identity: 1}}]}
        observed_history(paper, history, NOW)
        self.assertEqual(paper["chapters"][0]["deltaWeek"], 2)
        self.assertEqual(paper["activity"]["week"]["baselineAt"], iso(start))
        self.assertEqual(paper["activity"]["week"]["coverage"], "partial")
        self.assertEqual(paper["activity"]["trackingStartedAt"], history["trackingStartedAt"])

    @in_london
    def test_london_dst_repeated_hour_keeps_one_daily_opening_anchor(self) -> None:
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One.")
        first = dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        second = dt.datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
        history = observed_history(self.paper(), None, first)
        self.file("main.tex", r"\section{Methods}\label{sec:methods}One two.")
        changed = self.paper()
        history = observed_history(changed, history, second)
        self.assertEqual(len(history["dayStarts"]), 1)
        self.assertEqual(changed["activity"]["today"]["baselineAt"], iso(first))
        self.assertEqual(changed["activity"]["today"]["windowStartAt"], "2026-10-24T23:00:00Z")
        self.assertEqual(changed["chapters"][0]["deltaToday"], 1)

    def test_history_is_hourly_bounded_and_future_observations_do_not_compare(self) -> None:
        self.file("main.tex", r"\section{Methods}One.")
        paper = self.paper()
        identity = paper["chapters"][0]["id"]
        history = {"schemaVersion": 1, "mainFile": "main.tex", "countingPolicy": paper["countingPolicy"],
                   "snapshots": [{"observedAt": iso(NOW - dt.timedelta(minutes=index)), "words": {identity: index}} for index in range(4000)]}
        history["snapshots"].append({"observedAt": iso(NOW + dt.timedelta(days=1)), "words": {identity: 10000}})
        bounded = observed_history(paper, history, NOW)
        self.assertLessEqual(len(bounded["snapshots"]), MAX_HISTORY)
        self.assertLess(len(bounded["snapshots"]), 70)
        self.assertLessEqual(len(bounded["dayStarts"]), 35)
        self.assertTrue(all(row["observedAt"] <= iso(NOW) for row in bounded["snapshots"]))
        again = observed_history(paper, bounded, NOW + dt.timedelta(seconds=1))
        self.assertEqual(len(again["snapshots"]), len(bounded["snapshots"]))

    def test_core_persists_owned_history_and_keeps_history_on_collection_error(self) -> None:
        source = self.file("main.tex", r"\section{Methods}\label{sec:methods}One two.")
        original = source.read_bytes()
        state = collect_one(self.vault, self.config(), "test-host", NOW)
        history_path = self.vault / "Research Dashboard/System/data/history/paper.json"
        history = json.loads(history_path.read_text())
        self.assertEqual(history["sourceId"], "paper")
        self.assertEqual(history["schemaVersion"], 1)
        self.assertEqual(source.read_bytes(), original)
        validate_state(state)
        source.unlink()
        later = collect_one(self.vault, self.config(), "test-host", NOW + dt.timedelta(minutes=1))
        self.assertEqual(later["collection"]["health"], "error")
        self.assertEqual(json.loads(history_path.read_text()), history)
        self.assertEqual(later["details"], state["details"])

    def test_deleted_registration_during_collection_never_recreates_outputs(self) -> None:
        self.file("main.tex", r"\section{Methods}One.")
        item = self.config()
        register(self.vault, item)
        registration = self.vault / "Research Dashboard/System/config/projects/paper.json"
        real_collect = latex.collect

        def removed(root: Path, options: dict, now: dt.datetime) -> dict:
            result = real_collect(root, options, now)
            registration.unlink()
            return result

        with patch("dashboard_core.ADAPTERS", {"latex": removed}), self.assertRaises(CollectionSuperseded):
            collect_one(self.vault, item, "test-host", NOW)
        self.assertFalse((self.vault / "Research Dashboard/System/data/projects/paper.json").exists())
        self.assertFalse((self.vault / "Research Dashboard/System/data/history/paper.json").exists())

    def test_source_changed_during_collection_discards_counts_and_history(self) -> None:
        source = self.file("main.tex", r"\section{Methods}One.")
        real_collect = latex.collect

        def changed(root: Path, options: dict, now: dt.datetime) -> dict:
            result = real_collect(root, options, now)
            source.write_text(r"\section{Methods}One two three.", encoding="utf-8")
            return result

        with patch("dashboard_core.ADAPTERS", {"latex": changed}), self.assertRaises(CollectionSuperseded):
            collect_one(self.vault, self.config(), "test-host", NOW)
        self.assertFalse((self.vault / "Research Dashboard/System/data/projects/paper.json").exists())
        self.assertFalse((self.vault / "Research Dashboard/System/data/history/paper.json").exists())

    def test_same_stat_source_replacement_is_detected_by_read_hash(self) -> None:
        source = self.file("main.tex", r"\section{Methods}One.")
        stamp = source.stat()
        real_collect = latex.collect

        def changed(root: Path, options: dict, now: dt.datetime) -> dict:
            result = real_collect(root, options, now)
            source.write_text(r"\section{Methods}Two.", encoding="utf-8")
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            return result

        with patch("dashboard_core.ADAPTERS", {"latex": changed}), self.assertRaises(CollectionSuperseded):
            collect_one(self.vault, self.config(), "test-host", NOW)
        self.assertFalse((self.vault / "Research Dashboard/System/data/projects/paper.json").exists())

    def test_local_alias_change_during_collection_never_publishes_wrong_source(self) -> None:
        self.file("main.tex", r"\section{Methods}One.")
        real_collect = latex.collect

        def changed(root: Path, options: dict, now: dt.datetime) -> dict:
            result = real_collect(root, options, now)
            atomic_json(self.vault / "Research Dashboard/System/config/local/test-host.json",
                        {"schemaVersion": "1.0", "aliases": {"research": str(self.root.parent)}})
            return result

        with patch("dashboard_core.ADAPTERS", {"latex": changed}), self.assertRaises(CollectionSuperseded):
            collect_one(self.vault, self.config(), "test-host", NOW)
        self.assertFalse((self.vault / "Research Dashboard/System/data/projects/paper.json").exists())

    def test_chapter_graph_schema_preserves_actual_initial_zero_and_rejects_invalid_counts(self) -> None:
        self.file("main.tex", r"\section{Methods}One.")
        state = collect_one(self.vault, self.config(), "test-host", NOW)
        paper = next(detail for detail in state["details"] if detail["kind"] == "paper")
        self.assertIsNone(state["runtime"]["progress"])
        self.assertEqual(paper["chapters"][0]["deltaToday"], 0)
        for field, value in [("bodyWords", -1), ("deltaToday", False), ("sourceHash", "invalid")]:
            invalid = copy.deepcopy(state)
            next(detail for detail in invalid["details"] if detail["kind"] == "paper")["chapters"][0][field] = value
            with self.assertRaises(ValueError):
                validate_state(invalid)


if __name__ == "__main__":
    unittest.main()
