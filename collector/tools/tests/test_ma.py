"""ma: the agent command line (report, Next up, milestones, connect handoff). App addition; see VENDORED.md."""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ma
from workspace import init_workspace

SCENARIO = Path(__file__).resolve().parents[3] / "tests" / "shared" / "next-up.json"


def build(root: Path, scenario: dict, today: dt.date) -> None:
    init_workspace(root)
    now = dt.datetime.now(dt.timezone.utc)
    for project in scenario["projects"]:
        folder = root / "projects" / project["id"]
        folder.mkdir(parents=True)
        record = {"schemaVersion": "1.0", "projectId": project["id"], "title": project["title"], "type": project["type"],
                  "parentId": project["parentId"], "health": project["health"], "sourceIds": project["sourceIds"]}
        (folder / "project.json").write_text(json.dumps(record), encoding="utf-8")
        lines = [re.sub(r"\{due:(-?\d+)\}", lambda m: f"[due:: {(today + dt.timedelta(days=int(m.group(1)))).isoformat()}]", line)
                 for line in project["milestones"]]
        (folder / "milestones.md").write_text("# Milestones\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    for source, chapters in scenario["chapters"].items():
        (root / "sources").mkdir(exist_ok=True)
        (root / "data").mkdir(exist_ok=True)
        (root / "sources" / f"{source}.json").write_text(json.dumps({"title": source, "kind": "latex"}), encoding="utf-8")
        details = [{"kind": "paper", "chapters": [
            {"id": c["id"], "title": c["title"], "parentId": c["parentId"], "deltaToday": c["deltaToday"], "deltaWeek": c["deltaWeek"],
             "sourceHash": str(i).rjust(64, "0"),
             "changedAt": None if c["changedMinutesAgo"] is None else (now - dt.timedelta(minutes=c["changedMinutesAgo"])).isoformat().replace("+00:00", "Z")}
            for i, c in enumerate(chapters)]}]
        state = {"collection": {"health": "fresh", "lastSuccessfulAt": now.isoformat().replace("+00:00", "Z"), "staleAfterSeconds": 180}, "details": details}
        (root / "data" / f"{source}.json").write_text(json.dumps(state), encoding="utf-8")


class RdTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "ws"
        self.scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
        self.today = dt.date.today()
        build(self.root, self.scenario, self.today)

    def tearDown(self):
        self.temp.cleanup()

    def ma(self, *argv: str) -> dict:
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ma.main(["--workspace", str(self.root), *argv])
        return json.loads(out.getvalue())

    def test_next_up_matches_the_dashboard_scenario(self):
        everything = ma.report(self.root, None, None, self.today)
        self.assertEqual([item["title"] for item in everything["nextUp"]], self.scenario["expected"]["all"])
        focused = ma.report(self.root, "thesis", None, self.today)
        self.assertEqual([item["title"] for item in focused["nextUp"]], self.scenario["expected"]["focusThesis"])
        self.assertEqual(focused["project"]["subProjects"], ["paper"])
        self.assertEqual(everything["nextUp"][0]["overdueDays"], 3)

    def test_report_counts_families(self):
        result = self.ma("report")
        thesis = next(item for item in result["projects"] if item["id"] == "thesis")
        self.assertEqual(thesis["milestones"], {"completed": 2, "total": 8, "percent": 25})
        self.assertEqual([item["id"] for item in result["subProjects"]], ["paper"])

    def test_milestones_add_done_rename_due_delete(self):
        added = self.ma("milestone", "add", "--project", "thesis", "--title", "Write *discussion*", "--due", "2030-01-02")
        self.assertTrue(added["ok"], added)
        listed = self.ma("milestone", "list", "--project", "thesis")["milestones"]
        self.assertEqual(listed[-1]["title"], "Write *discussion*")
        self.assertEqual(listed[-1]["due"], "2030-01-02")
        self.assertTrue(self.ma("milestone", "done", "--project", "thesis", "--match", "discussion")["completed"])
        renamed = self.ma("milestone", "rename", "--project", "thesis", "--match", "discussion", "--title", "Discussion")
        self.assertEqual(renamed["milestone"], "Discussion")
        self.assertTrue(self.ma("milestone", "due", "--project", "thesis", "--match", "Discussion")["ok"])
        text = (self.root / "projects" / "thesis" / "milestones.md").read_text(encoding="utf-8")
        self.assertIn("- [x] Discussion #milestone", text)
        self.assertTrue(self.ma("milestone", "delete", "--project", "thesis", "--match", "Discussion")["ok"])
        self.assertNotIn("Discussion", (self.root / "projects" / "thesis" / "milestones.md").read_text(encoding="utf-8"))
        ambiguous = self.ma("milestone", "done", "--project", "thesis", "--match", "chapter")
        self.assertFalse(ambiguous["ok"])
        self.assertIn("fits 2 milestones", ambiguous["error"])

    def test_chapter_milestones_record_the_section_hash(self):
        done = self.ma("milestone", "done", "--project", "paper", "--match", "Setup draft")
        self.assertTrue(done["ok"], done)
        text = (self.root / "projects" / "paper" / "milestones.md").read_text(encoding="utf-8")
        self.assertIn("[accepted_hash:: " + "2".rjust(64, "0") + "]", text)
        bound = self.ma("milestone", "add", "--project", "paper", "--title", "Setup figures", "--section", "setup", "--stage", "figures")
        self.assertTrue(bound["ok"], bound)
        self.assertIn("[paper:: paper-tex] [chapter:: setup] [stage:: figures]", (self.root / "projects" / "paper" / "milestones.md").read_text(encoding="utf-8"))
        self.assertFalse(self.ma("milestone", "add", "--project", "paper", "--title", "x", "--section", "nope")["ok"])

    def test_connect_requires_the_app_and_a_valid_plan(self):
        folder = Path(self.temp.name) / "paper"
        folder.mkdir()
        plan = Path(self.temp.name) / "plan.json"
        plan.write_text(json.dumps({"profile": {"id": "bad"}}), encoding="utf-8")
        invalid = self.ma("connect", "--folder", str(folder), "--plan", str(plan))
        self.assertFalse(invalid["ok"])
        self.assertTrue(invalid["errors"])
        self.assertFalse(self.ma("connect", "--folder", "relative/path")["ok"])


if __name__ == "__main__":
    unittest.main()
