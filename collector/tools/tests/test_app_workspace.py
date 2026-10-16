"""App workspace layout: the desktop app's plain folder instead of an Obsidian vault.

These tests exercise the same collector code paths as the vault tests (create,
connect, collect, milestones, hierarchy, reconciliation) against a workspace with
``workspace.json``, plus the one-time Obsidian vault importer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_core import read_json
from layout import APP, VAULT, layout_for
from milestones import apply_template, find_template
from onboarding import connect_folder, inspect_folder
from project_catalog import active_configurations, create_logical_project, project_catalogs, record_parent_id
from reconciliation import reconcile
from workspace import import_vault, init_workspace, parse_frontmatter, split_milestones

PAPER = ("\\documentclass{article}\n\\begin{document}\n\\section{Introduction}\\label{sec:intro}\n"
         "Lunar habitats need performance targets.\n\\subsection{Background}\nRegolith shielding words here.\n"
         "\\section{Method}\\label{sec:method}\nWe define the framework with several words.\n\\end{document}\n")


class AppWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.source = self.root / "paper"
        self.source.mkdir()
        (self.source / "main.tex").write_text(PAPER, encoding="utf-8")
        init_workspace(self.workspace, "Test workspace")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cli(self, *arguments: str, root: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        script = Path(__file__).resolve().parents[1] / "cli.py"
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        environment.pop("RD_LAYOUT", None)
        return subprocess.run([sys.executable, str(script), "--workspace", str(root or self.workspace), "--host-id", "test-host",
                               *arguments], capture_output=True, text=True, encoding="utf-8", env=environment, check=check)

    def connect(self, source_id: str = "paper", **options) -> dict:
        profile = inspect_folder(self.source, self.workspace)["profiles"][0]
        return connect_folder(self.workspace, "test-host", self.source, source_id, "latex", "Lunar paper",
                              profile["adapter"], **options)

    def test_init_creates_marker_folders_and_detects_layout(self) -> None:
        meta = read_json(self.workspace / "workspace.json")
        self.assertEqual(meta["kind"], "monitoring-anything-workspace")
        self.assertEqual(meta["name"], "Test workspace")
        for folder in ("projects", "sources", "local", "data/history", "profiles/user"):
            self.assertTrue((self.workspace / folder).is_dir(), folder)
        self.assertIs(layout_for(self.workspace), APP)
        self.assertIs(layout_for(self.root), VAULT)
        self.assertFalse(init_workspace(self.workspace)["created"])
        occupied = self.root / "occupied"
        occupied.mkdir()
        (occupied / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            init_workspace(occupied)

    def test_sourceless_project_writes_project_json_and_milestones_only(self) -> None:
        result = create_logical_project(self.workspace, "thesis", "PhD thesis", "thesis")
        self.assertEqual(result["projectRecord"], "projects/thesis/project.json")
        self.assertIsNone(result["projectDashboard"])
        record = read_json(self.workspace / "projects/thesis/project.json")
        self.assertEqual((record["type"], record["parentId"], record["health"], record["sourceIds"]),
                         ("thesis", None, "active", []))
        milestones = (self.workspace / "projects/thesis/milestones.md").read_text(encoding="utf-8")
        self.assertIn("- [ ] Define the first deliverable and its acceptance criteria #milestone", milestones)
        files = sorted(p.relative_to(self.workspace).as_posix() for p in (self.workspace / "projects").rglob("*") if p.is_file())
        self.assertEqual(files, ["projects/thesis/milestones.md", "projects/thesis/project.json"])
        self.assertEqual([item["projectId"] for item in project_catalogs(self.workspace)], ["thesis"])

    def test_hierarchy_reads_parent_from_project_json(self) -> None:
        create_logical_project(self.workspace, "thesis", "PhD thesis", "thesis")
        create_logical_project(self.workspace, "chapter-two", "Chapter two", "paper", "thesis")
        self.assertEqual(record_parent_id(self.workspace, "chapter-two"), "thesis")
        self.assertIsNone(record_parent_id(self.workspace, "thesis"))
        with self.assertRaisesRegex(ValueError, "already a sub-project"):
            create_logical_project(self.workspace, "deep", "Too deep", "other", "chapter-two")

    def test_connect_collect_and_milestone_template(self) -> None:
        result = self.connect(project_type="paper")
        self.assertEqual(result["collectionHealth"], "fresh", result.get("collectionError"))
        self.assertEqual(result["projectRecord"], "projects/paper/project.json")
        config = read_json(self.workspace / "sources/paper.json")
        self.assertEqual(config["projectNote"], "sources/paper.md")
        self.assertEqual(config["dashboardProjectId"], "paper")
        self.assertTrue((self.workspace / "sources/paper.md").is_file())
        self.assertEqual(read_json(self.workspace / "local/test-host.json")["aliases"]["paper"], str(self.source.resolve()))
        state = read_json(self.workspace / "data/paper.json")
        paper = next(detail for detail in state["details"] if detail["kind"] == "paper")
        self.assertGreaterEqual(len(paper["chapters"]), 2)
        self.assertTrue((self.workspace / "data/history/paper.json").is_file())
        record = read_json(self.workspace / "projects/paper/project.json")
        self.assertEqual((record["type"], record["sourceIds"]), ("paper", ["paper"]))
        self.assertFalse(any((self.workspace / "Research Dashboard").rglob("*")) if (self.workspace / "Research Dashboard").exists() else False)

        applied = apply_template(self.workspace, "paper", find_template(self.workspace, "paper-section-draft-complete"))
        self.assertEqual(applied["projectNote"], "projects/paper/milestones.md")
        self.assertGreater(applied["addedCount"], 0)
        text = (self.workspace / "projects/paper/milestones.md").read_text(encoding="utf-8")
        self.assertIn("[paper:: paper]", text)
        self.assertIn("[stage:: draft]", text)
        again = apply_template(self.workspace, "paper", find_template(self.workspace, "paper-section-draft-complete"))
        self.assertEqual(again["addedCount"], 0)

        collected = self.cli("collect")
        self.assertEqual(json.loads(collected.stdout)[0]["collection"], "fresh")
        status = json.loads(self.cli("status").stdout)
        self.assertEqual(status[0]["projectId"], "paper")

    def test_attach_second_source_preserves_record_fields(self) -> None:
        create_logical_project(self.workspace, "thesis", "PhD thesis", "thesis")
        record_path = self.workspace / "projects/thesis/project.json"
        record = read_json(record_path)
        record.update(nextStep="Write chapter 3", health="blocked", due="2026-12-01")
        record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        self.connect("thesis-tex", dashboard_project="thesis")
        updated = read_json(record_path)
        self.assertEqual((updated["nextStep"], updated["health"], updated["due"]), ("Write chapter 3", "blocked", "2026-12-01"))
        self.assertEqual(updated["sourceIds"], ["thesis-tex"])
        self.assertEqual([c["projectId"] for c in active_configurations(self.workspace)], ["thesis-tex"])

    def test_removed_project_folder_is_reconciled_into_workspace_trash(self) -> None:
        self.connect()
        project = self.workspace / "projects/paper"
        trash = self.workspace / ".trash/manual"
        trash.mkdir(parents=True)
        project.rename(trash / "paper")
        self.assertEqual(active_configurations(self.workspace), [])
        preview = reconcile(self.workspace)
        self.assertEqual(preview["orphanSourceIds"], ["paper"])
        applied = reconcile(self.workspace, apply=True)
        self.assertTrue(applied["archivePath"].startswith(".trash/"))
        self.assertFalse((self.workspace / "sources/paper.json").exists())
        self.assertFalse((self.workspace / "data/paper.json").exists())
        self.assertTrue((self.workspace / applied["archivePath"] / "sources/paper.json").is_file())
        self.assertTrue(self.source.joinpath("main.tex").is_file())

    def test_discovery_configures_scans_and_accepts_sub_projects(self) -> None:
        import discovery
        create_logical_project(self.workspace, "thesis", "PhD thesis", "thesis")
        papers = self.root / "papers"
        (papers / "chapter-two").mkdir(parents=True)
        (papers / "chapter-two" / "main.tex").write_text(PAPER, encoding="utf-8")
        discovery.configure(self.workspace, "test-host", "thesis", folder=str(papers))
        record = read_json(self.workspace / "projects/thesis/project.json")
        self.assertEqual(record["discovery"]["rootAlias"], "discovery:thesis")
        self.assertEqual(record["type"], "thesis", "record fields survive the discovery write")
        self.assertEqual(read_json(self.workspace / "local/test-host.json")["aliases"]["discovery:thesis"], str(papers.resolve()))
        found = discovery.public(discovery.scan(self.workspace, "test-host", "thesis"))
        self.assertEqual([item["relative"] for item in found["suggestions"]], ["chapter-two"])
        accepted = discovery.accept(self.workspace, "test-host", "thesis", "chapter-two")
        child = accepted["connect"]["dashboardProjectId"]
        self.assertEqual(read_json(self.workspace / f"projects/{child}/project.json")["parentId"], "thesis")
        discovery.dismiss(self.workspace, "test-host", "thesis", "chapter-two")
        self.assertTrue((self.workspace / "data/discovery/state.json").is_file())

    def test_cli_info_create_and_profiles(self) -> None:
        info = json.loads(self.cli("info").stdout)
        self.assertEqual(info["layout"], "app")
        created = json.loads(self.cli("create-project", "--id", "habitat", "--title", "Habitat study", "--type", "simulation").stdout)
        self.assertEqual(created["projectRecord"], "projects/habitat/project.json")
        profiles = json.loads(self.cli("profiles", "list").stdout)
        self.assertTrue(all(item["path"].startswith("profiles/") for item in profiles["profiles"]))
        fresh = self.root / "fresh"
        made = json.loads(self.cli("init-workspace", "--name", "Fresh", root=fresh).stdout)
        self.assertTrue(made["created"])


class VaultImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.source = self.root / "paper"
        self.source.mkdir()
        (self.source / "main.tex").write_text(PAPER, encoding="utf-8")
        self.vault.mkdir()
        profile = inspect_folder(self.source, self.vault)["profiles"][0]
        connect_folder(self.vault, "test-host", self.source, "paper", "latex", "Lunar paper", profile["adapter"])
        create_logical_project(self.vault, "thesis", "PhD thesis", "thesis")
        note = self.vault / "Research Dashboard/Projects/paper/Project.md"
        text = note.read_text(encoding="utf-8").replace('"parent_id": null', "parent_id: thesis").replace(
            "parent_id: null", "parent_id: thesis")
        text = text.replace("- [ ] Define the first deliverable and its acceptance criteria #milestone",
                            "- [x] Outline [due:: 2026-10-01] #milestone\n\n### Results\n\n- [ ] Figures #milestone")
        note.write_text(text, encoding="utf-8")
        self.workspace = self.root / "workspace"
        init_workspace(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_frontmatter_parser_reads_plain_and_json_scalars(self) -> None:
        fields, body = parse_frontmatter("---\ntitle: \"A: b\"\nparent_id:\ndue: 2026-10-05\ncssclasses:\n  - x\n  - y\n"
                                         "resources: []\narchived: false\n---\n\n# Body\n")
        self.assertEqual(fields["title"], "A: b")
        self.assertIsNone(fields["parent_id"])
        self.assertEqual(fields["due"], "2026-10-05")
        self.assertEqual(fields["cssclasses"], ["x", "y"])
        self.assertEqual(fields["resources"], [])
        self.assertIs(fields["archived"], False)
        self.assertEqual(body.strip(), "# Body")
        section, rest = split_milestones("## A\n\n## Milestones\n\n- [ ] t #milestone\n```\n## not a heading\n```\n\n## B\nx\n")
        self.assertIn("## not a heading", section)
        self.assertIn("## B", rest)

    def test_import_converts_projects_sources_data_and_mappings(self) -> None:
        dry = import_vault(self.vault, self.workspace, dry_run=True)
        self.assertTrue(dry["dryRun"])
        self.assertFalse((self.workspace / "projects/paper").exists())
        result = import_vault(self.vault, self.workspace)
        self.assertEqual(sorted(result["projects"]), ["paper", "thesis"])
        self.assertEqual(result["sources"], ["paper"])
        record = read_json(self.workspace / "projects/paper/project.json")
        self.assertEqual((record["parentId"], record["type"], record["sourceIds"]), ("thesis", "paper", ["paper"]))
        milestones = (self.workspace / "projects/paper/milestones.md").read_text(encoding="utf-8")
        self.assertTrue(milestones.startswith("# Milestones\n"))
        self.assertIn("- [x] Outline [due:: 2026-10-01] #milestone", milestones)
        self.assertIn("### Results", milestones)
        self.assertNotIn("Decisions and evidence", milestones)
        self.assertIn("Decisions and evidence", (self.workspace / "projects/paper/notes.md").read_text(encoding="utf-8"))
        config = read_json(self.workspace / "sources/paper.json")
        self.assertEqual(config["projectNote"], "sources/paper.md")
        self.assertEqual(read_json(self.workspace / "data/paper.json")["projectNote"], "sources/paper.md")
        self.assertIn("paper", read_json(self.workspace / "local/test-host.json")["aliases"])
        self.assertEqual([c["projectId"] for c in active_configurations(self.workspace)], ["paper"])
        self.assertEqual(sorted(p["projectId"] for p in project_catalogs(self.workspace)), ["paper", "thesis"])
        # Re-running never overwrites.
        again = import_vault(self.vault, self.workspace)
        self.assertEqual(again["projects"], [])
        self.assertTrue(all(item["action"] != "create" for item in again["files"]))
        # The vault was only read.
        self.assertTrue((self.vault / "Research Dashboard/Projects/paper/Project.md").is_file())

    def test_import_requires_app_workspace(self) -> None:
        with self.assertRaises(ValueError):
            import_vault(self.vault, self.root / "not-a-workspace")


if __name__ == "__main__":
    unittest.main()


class ActivityZoneTest(unittest.TestCase):
    """Writing-activity day boundaries follow this computer's zone (app change; see VENDORED.md)."""

    def setUp(self):
        self.saved = os.environ.pop("RD_TIMEZONE", None)

    def tearDown(self):
        os.environ.pop("RD_TIMEZONE", None)
        if self.saved is not None:
            os.environ["RD_TIMEZONE"] = self.saved

    def test_local_zone_without_a_name(self):
        from adapters.latex_paper import activity_zone
        name, zone = activity_zone()
        self.assertTrue(name)
        self.assertIsNotNone(zone.utcoffset(None) if hasattr(zone, "utcoffset") else zone)

    def test_named_zone_is_kept_even_without_a_zone_database(self):
        from adapters.latex_paper import activity_zone
        os.environ["RD_TIMEZONE"] = "Europe/London"
        self.assertEqual(activity_zone()[0], "Europe/London")
        os.environ["RD_TIMEZONE"] = "../etc/passwd"
        self.assertNotEqual(activity_zone()[0], "../etc/passwd")


class AgentConnectionTest(unittest.TestCase):
    """plan test / plan detect (app additions; see VENDORED.md)."""

    def test_cli_provider_version_and_prompt(self):
        from agents import test_provider
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "agent.py"
            script.write_text("import sys\nsys.stdin.read()\nprint('{\"ok\": true}')\n", encoding="utf-8")
            provider = {"id": "custom", "kind": "cli", "command": sys.executable, "args": [str(script)]}
            quick = test_provider({"provider": provider})
            self.assertTrue(quick["ok"], quick)
            self.assertIn("Python", quick["version"])
            full = test_provider({"provider": provider, "prompt": True, "timeoutSeconds": 30})
            self.assertTrue(full["ok"], full)
            script.write_text("print('hello')\n", encoding="utf-8")
            wrong = test_provider({"provider": provider, "prompt": True, "timeoutSeconds": 30})
            self.assertFalse(wrong["ok"])
            missing = test_provider({"provider": {"id": "custom", "kind": "cli", "command": "zz-not-a-real-agent", "args": []}})
            self.assertFalse(missing["ok"])
            self.assertIn("not found", missing["error"])

    def test_http_provider_reply_and_key_redaction(self):
        from agents import test_provider
        def transport(url, headers, body, timeout, max_bytes):
            return 200, json.dumps({"content": [{"type": "text", "text": "{\"ok\": true}"}]}).encode()
        provider = {"id": "anthropic-api", "kind": "http", "model": "claude-haiku-5-5", "apiKey": "sk-ant-secret-1234567890"}
        result = test_provider({"provider": provider, "prompt": True}, transport=transport)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("sk-ant-secret", json.dumps(result))
        def failing(url, headers, body, timeout, max_bytes):
            return 401, b'{"error": "invalid x-api-key sk-ant-secret-1234567890"}'
        bad = test_provider({"provider": provider, "prompt": True}, transport=failing)
        self.assertFalse(bad["ok"])
        self.assertNotIn("sk-ant-secret", json.dumps(bad))

    def test_local_servers(self):
        from agents import detect_local_servers
        def fetch(url, timeout):
            if "11434" in url:
                return 200, json.dumps({"data": [{"id": "qwen3"}, {"id": "llama3.2"}]}).encode()
            raise OSError("connection refused")
        self.assertEqual(detect_local_servers(fetch=fetch)["servers"], [{"id": "ollama", "title": "Ollama", "baseUrl": "http://127.0.0.1:11434/v1", "models": ["qwen3", "llama3.2"]}])


class SectionChangeTest(unittest.TestCase):
    """Per-section change tracking by content fingerprint (app addition; see VENDORED.md)."""

    def test_only_the_edited_section_changes(self):
        import datetime as dt
        from adapters.latex_paper import section_changes
        def paper(intro, method):
            return {"mainFile": "main.tex", "chapters": [{"id": "intro", "sourceHash": intro}, {"id": "method", "sourceHash": method}]}
        first = paper("a" * 64, "b" * 64)
        t0 = dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.timezone.utc)
        history = {"mainFile": "main.tex", "sections": section_changes(first, None, t0)}
        self.assertEqual([c["changedAt"] for c in first["chapters"]], [None, None])
        second = paper("a" * 64, "c" * 64)
        t1 = t0 + dt.timedelta(minutes=5)
        history = {"mainFile": "main.tex", "sections": section_changes(second, history, t1)}
        self.assertEqual([c["changedAt"] for c in second["chapters"]], [None, "2026-10-12T08:05:00Z"])
        third = paper("a" * 64, "c" * 64)
        section_changes(third, history, t1 + dt.timedelta(minutes=5))
        self.assertEqual(third["chapters"][1]["changedAt"], "2026-10-12T08:05:00Z")


class NewBuiltinTypesTest(unittest.TestCase):
    """Word manuscript and machine-learning training profiles (app additions; see VENDORED.md)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.workspace = self.base / "ws"
        init_workspace(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def test_builtin_profiles_validate(self):
        from profiles import validate_profile
        root = Path(__file__).resolve().parents[2] / "profiles" / "builtin"
        for name in ("word-manuscript.json", "ml-training.json"):
            errors, _ = validate_profile(json.loads((root / name).read_text(encoding="utf-8")))
            self.assertEqual(errors, [], name)

    def test_word_document_measures(self):
        import zipfile
        from planning import preview
        folder = self.base / "manuscript"
        folder.mkdir()
        body = ('<w:document xmlns:w="w"><w:body>'
                '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Introduction</w:t></w:r></w:p>'
                '<w:p><w:r><w:t xml:space="preserve">Lunar habitats are </w:t></w:r><w:r><w:t>exposed.</w:t></w:r></w:p>'
                '<w:p><w:ins w:id="1"><w:r><w:t>月球基地</w:t></w:r></w:ins><w:del w:id="2"><w:r><w:delText>gone</w:delText></w:r></w:del></w:p>'
                '</w:body></w:document>')
        with zipfile.ZipFile(folder / "paper.docx", "w") as archive:
            archive.writestr("word/document.xml", body)
            archive.writestr("word/comments.xml", '<w:comments><w:comment w:id="0"/><w:comment w:id="1"/></w:comments>')
        (folder / "~$paper.docx").write_bytes(b"lock")
        result = preview(self.workspace, {"source": str(folder), "profileId": "word-manuscript"})
        metrics = {item["id"]: item["value"] for item in result["preview"]["metrics"]}
        self.assertEqual(metrics, {"words": 9, "headings": 1, "comments": 2, "revisions": 2})

    def test_training_run_progress_and_metrics(self):
        from planning import preview
        run = self.base / "run" / "lightning_logs" / "version_0"
        run.mkdir(parents=True)
        (run / "hparams.yaml").write_text("lr: 0.001\nmax_epochs: 20\n", encoding="utf-8")
        (run / "metrics.csv").write_text("epoch,step,train_loss,val_loss,val_acc\n0,10,0.9,,\n0,10,,0.8,0.6\n1,20,0.7,,\n1,20,,0.65,0.7\n", encoding="utf-8")
        result = preview(self.workspace, {"source": str(self.base / "run"), "profileId": "ml-training"})
        runtime = result["preview"]["runtime"]
        self.assertEqual((runtime["currentValue"], runtime["targetValue"]), (1, 20))
        metrics = {item["id"]: item["value"] for item in result["preview"]["metrics"]}
        self.assertEqual((metrics["best-val-loss"], metrics["train-loss"], metrics["val-acc"]), (0.65, 0.7, 0.7))
