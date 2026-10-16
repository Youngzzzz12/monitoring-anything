"""Agent providers for smart onboarding: presets, prompt, parsing, repair, timeouts, HTTP, redaction.

No real model is called: CLI providers are small fake Python scripts and HTTP
providers are a local fake server or an injected transport.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents
from planning import manifest

API_KEY = "sk-test-SECRETSECRETSECRET1234"

FAKE_AGENT = r'''
import json, pathlib, sys, time
state = pathlib.Path(sys.argv[1])
prompt = sys.stdin.read()
count = int(state.read_text()) if state.exists() else 0
state.write_text(str(count + 1))
(state.parent / f"prompt{count}.txt").write_text(prompt, encoding="utf-8")
(state.parent / f"cwd{count}.txt").write_text(str(pathlib.Path.cwd()), encoding="utf-8")
responses = json.loads((state.parent / "responses.json").read_text(encoding="utf-8"))
response = responses[min(count, len(responses) - 1)]
if response == "__SLEEP__":
    time.sleep(60)
elif response == "__FLOOD__":
    sys.stdout.write("x" * (1024 * 1024))
else:
    sys.stdout.write(response)
'''


def sweep_profile(**changes: object) -> dict:
    profile = json.loads(json.dumps(agents.EXAMPLE_PLAN["profile"]))
    profile["id"] = "test-sweep"
    profile.update(changes)
    return profile


def plan(**profile_changes: object) -> dict:
    return {"schemaVersion": "1.0", "profile": sweep_profile(**profile_changes),
            "project": {"title": "Sweep", "type": "simulation", "parentSuggestion": None},
            "milestones": ["Done"], "rationale": "config + results", "warnings": []}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.source = self.root / "source"
        self.work = self.root / "agent"
        for folder in (self.vault, self.source, self.work):
            folder.mkdir()
        self.file("sweep_config.json", json.dumps({"cases": [{"id": i} for i in range(4)]}))
        self.file("results/case_1.mat", b"\x00MATLAB")
        self.file("results/case_2.mat", b"\x00MATLAB")
        self.file("results/summary.csv", "case,stress\n1,12.5\n2,30.25\n")
        self.file("logs/latest.log", "Phase: solve\nStep 1240 of 5000\n")
        self.file("figures/a.pdf", b"%PDF-1.4")
        self.file("private/notes.txt", "PRIVATE-CONTENT-MARKER\n")
        self.script = self.work / "fake_agent.py"
        self.script.write_text(FAKE_AGENT, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def file(self, relative: str, text: str | bytes) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def responses(self, *items: object) -> None:
        values = [item if isinstance(item, str) else json.dumps(item) for item in items]
        (self.work / "responses.json").write_text(json.dumps(values), encoding="utf-8")

    def provider(self, **changes: object) -> dict:
        value = {"id": "custom", "kind": "cli", "command": sys.executable,
                 "args": [str(self.script), str(self.work / "state")], "agentic": True, "apiKey": API_KEY}
        value.update(changes)
        return value

    def prompt(self, index: int = 0) -> str:
        return (self.work / f"prompt{index}.txt").read_text(encoding="utf-8")

    def propose(self, **request: object) -> dict:
        body = {"source": str(self.source), "provider": self.provider(), "timeoutSeconds": 30}
        body.update(request)
        return agents.propose(self.vault, body)

    def cli(self, *arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        script = Path(__file__).resolve().parents[1] / "cli.py"
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, str(script), "--vault", str(self.vault), "--host-id", "test-host", *arguments],
                              input=stdin, capture_output=True, text=True, encoding="utf-8", env=environment)


class ProviderListTests(Fixture):
    def test_presets_and_installed_detection_follow_path(self) -> None:
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        if os.name == "nt":
            (bin_dir / "claude.bat").write_text("@echo off\n", encoding="utf-8")
        else:
            fake = bin_dir / "claude"
            fake.write_text("#!/bin/sh\n", encoding="utf-8")
            fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            providers = {item["id"]: item for item in agents.list_providers()["providers"]}
        self.assertEqual(set(providers), {"claude-code", "codex", "gemini-cli", "openai-compatible", "anthropic-api", "custom"})
        self.assertIs(providers["claude-code"]["installed"], True)
        self.assertIs(providers["codex"]["installed"], False)
        self.assertIs(providers["gemini-cli"]["installed"], False)
        self.assertIsNone(providers["openai-compatible"]["installed"])
        self.assertIsNone(providers["anthropic-api"]["installed"])
        self.assertIsNone(providers["custom"]["installed"])
        self.assertEqual(providers["openai-compatible"]["baseUrl"], "http://localhost:11434/v1")
        self.assertTrue(providers["claude-code"]["agentic"])
        self.assertFalse(providers["openai-compatible"]["agentic"])
        self.assertIn("read-only", providers["codex"]["args"])
        self.assertIn("--print", providers["claude-code"]["args"])

    def test_cli_lists_providers_and_rejects_bad_requests_with_exit_2(self) -> None:
        listed = self.cli("plan", "providers")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(json.loads(listed.stdout)["providers"]), 6)
        missing = self.cli("plan", "propose", stdin=json.dumps({"source": str(self.source)}))
        self.assertEqual(missing.returncode, 2)
        self.assertIn("provider", missing.stderr)
        bad_kind = self.cli("plan", "propose", stdin=json.dumps({"source": str(self.source),
                                                                 "provider": {"id": "x", "kind": "ftp"}}))
        self.assertEqual(bad_kind.returncode, 2)
        no_model = self.cli("plan", "propose", stdin=json.dumps({"source": str(self.source),
                                                                 "provider": {"id": "openai-compatible"}}))
        self.assertEqual(no_model.returncode, 2)
        self.assertIn("model", no_model.stderr)

    def test_preset_is_filled_from_id_and_overrides_apply(self) -> None:
        provider = agents.normalize_provider({"id": "codex", "model": "gpt-x"})
        self.assertEqual(provider["command"], "codex")
        self.assertIn("{outputFile}", provider["args"])
        http = agents.normalize_provider({"id": "openai-compatible", "model": "qwen3", "baseUrl": "http://127.0.0.1:1234/v1/"})
        self.assertEqual(http["baseUrl"], "http://127.0.0.1:1234/v1")
        self.assertEqual(http["protocol"], "openai")
        with self.assertRaises(ValueError):
            agents.normalize_provider({"id": "custom", "kind": "cli", "command": "x", "args": ["{prompt}", "{promptFile}"]})


class PromptTests(Fixture):
    def test_prompt_has_rules_schema_library_and_manifest_but_no_secrets_or_excluded_paths(self) -> None:
        self.responses(plan())
        result = self.propose(privacy={"exclude": ["private"]}, instructions="Also watch the figures.")
        self.assertTrue(result["ok"], result)
        prompt = self.prompt()
        for expected in ("Monitoring Anything monitoring profile", "sweep_config.json", "results/case_1.mat",
                         "Read-only", "countGlob", "jsonPath", "latex-paper", "simulation-basic",
                         "Also watch the figures.", "Folder access", "do NOT list, open or quote", "private"):
            self.assertIn(expected, prompt)
        self.assertNotIn(API_KEY, prompt)
        self.assertNotIn("private/notes.txt", prompt)
        self.assertNotIn("PRIVATE-CONTENT-MARKER", prompt)
        self.assertNotIn(str(self.source), prompt)  # only the folder name is sent
        self.assertEqual(Path((self.work / "cwd0.txt").read_text(encoding="utf-8")).resolve(), self.source.resolve())

    def test_non_agentic_prompt_and_previous_plan_revision(self) -> None:
        self.responses(plan())
        previous = plan(title="Old title")
        result = self.propose(provider=self.provider(agentic=False), previousPlan=previous,
                              privacy={"excerpts": False})
        self.assertTrue(result["ok"], result)
        prompt = self.prompt()
        self.assertIn("You cannot browse the folder", prompt)
        self.assertIn("Previous plan (revise it)", prompt)
        self.assertIn("Old title", prompt)
        self.assertNotIn("Step 1240 of 5000", prompt)  # excerpts disabled

    def test_example_plan_in_prompt_is_itself_valid(self) -> None:
        _, errors, _ = agents.validate_plan(self.vault, agents.EXAMPLE_PLAN, "x")
        self.assertEqual(errors, [])


class ParsingTests(unittest.TestCase):
    def test_raw_fenced_prose_and_wrappers(self) -> None:
        body = plan()
        raw = json.dumps(body)
        fenced = "Here is the plan:\n```json\n" + raw + "\n```\nDone."
        prose = "I looked around. " + raw + " Hope this helps."
        claude = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": fenced,
                             "duration_ms": 5, "total_cost_usd": 0.01})
        claude_list = json.dumps([{"type": "system"}, {"type": "result", "subtype": "success", "result": raw}])
        gemini = json.dumps({"response": fenced, "stats": {}})
        openai = json.dumps({"choices": [{"message": {"role": "assistant", "content": raw}}]})
        for text in (raw, fenced, prose, claude, claude_list, gemini, openai):
            parsed, error = agents.extract_plan(text)
            self.assertIsNone(error, text[:80])
            self.assertEqual(parsed, body)

    def test_errors_and_empty_responses(self) -> None:
        parsed, error = agents.extract_plan(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                                                        "result": "Out of turns"}))
        self.assertIsNone(parsed)
        self.assertIn("Claude Code reported an error", error)
        self.assertEqual(agents.extract_plan("   ")[1], "The provider returned an empty response")
        self.assertIn("No JSON object", agents.extract_plan("no json here")[1])

    def test_plan_validation_variants(self) -> None:
        vault = Path(tempfile.mkdtemp())
        try:
            wrapped, errors, warnings = agents.validate_plan(vault, sweep_profile(), "claude-code")
            self.assertEqual(errors, [])
            self.assertIn("profile", wrapped)
            self.assertTrue(any("bare profile" in item for item in warnings))
            _, errors, _ = agents.validate_plan(vault, {"profileId": "latex-paper"}, "x")
            self.assertEqual(errors, [])
            _, errors, _ = agents.validate_plan(vault, {"profileId": "nope"}, "x")
            self.assertTrue(errors and errors[0].startswith("profileId"))
            _, errors, _ = agents.validate_plan(vault, plan(id="latex-paper"), "x")
            self.assertTrue(any("built-in profile id" in item for item in errors))
            normalized, errors, _ = agents.validate_plan(vault, {"profile": {k: v for k, v in sweep_profile().items()
                                                                              if k != "origin"}}, "codex")
            self.assertEqual(normalized["profile"]["origin"]["createdBy"], "agent:codex")
            _, errors, _ = agents.validate_plan(vault, {**plan(), "project": {"type": "novel"}}, "x")
            self.assertTrue(any("project.type" in item for item in errors))
        finally:
            import shutil
            shutil.rmtree(vault, ignore_errors=True)


class ProposeTests(Fixture):
    def test_invalid_plan_triggers_one_repair_round_then_succeeds(self) -> None:
        self.responses(plan(projectType="novel"), "```json\n" + json.dumps(plan()) + "\n```")
        result = self.propose()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["repaired"])
        self.assertEqual(result["provider"]["attempts"], 2)
        self.assertTrue(result["validation"]["ok"])
        self.assertTrue(result["preview"]["ok"])
        self.assertEqual(result["preview"]["preview"]["cases"]["planned"], 4)
        self.assertEqual(result["preview"]["preview"]["runtime"]["currentValue"], 1240)
        repair = self.prompt(1)
        self.assertIn("Repair request", repair)
        self.assertIn("projectType must be one of", repair)

    def test_repair_failure_is_reported_without_a_third_call(self) -> None:
        self.responses(plan(projectType="novel"))
        result = self.propose()
        self.assertFalse(result["ok"])
        self.assertTrue(result["repaired"])
        self.assertEqual(result["provider"]["attempts"], 2)
        self.assertFalse(result["validation"]["ok"])
        self.assertIsNone(result["preview"])
        self.assertTrue(any("after one repair round" in item for item in result["errors"]))

    def test_dry_run_failure_also_uses_the_repair_round(self) -> None:
        nothing = sweep_profile(extract={"artifacts": [{"glob": "missing/*.xyz"}]})
        self.responses({"profile": nothing}, plan())
        result = self.propose()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["repaired"])
        self.assertIn("dry run:", self.prompt(1))

    def test_timeout_stops_the_provider_and_reports_json(self) -> None:
        self.responses("__SLEEP__")
        started = time.monotonic()
        result = self.propose(timeoutSeconds=1)
        self.assertLess(time.monotonic() - started, 20)
        self.assertFalse(result["ok"])
        self.assertEqual(result["provider"]["attempts"], 1)
        self.assertTrue(any("timed out" in item for item in result["errors"]))
        transcript = json.loads((self.vault / result["transcriptPath"]).read_text(encoding="utf-8"))
        self.assertTrue(transcript["attempts"][0]["timedOut"])

    def test_output_cap_and_missing_command(self) -> None:
        self.responses("__FLOOD__")
        provider = agents.normalize_provider(self.provider())
        record = agents.run_cli(provider, "hi", self.source, timeout=30, max_bytes=4096)
        self.assertIn("exceeded 4096 bytes", record["error"])
        self.assertLessEqual(len(record["stdout"]), 4096)
        missing = agents.run_cli(agents.normalize_provider({"id": "custom", "kind": "cli", "command": "no-such-agent-cli-xyz"}),
                                 "hi", self.source, timeout=5)
        self.assertIn("not found on PATH", missing["error"])
        result = self.propose(provider={"id": "custom", "kind": "cli", "command": "no-such-agent-cli-xyz"})
        self.assertFalse(result["ok"])
        self.assertTrue(any("not found on PATH" in item for item in result["errors"]))

    def test_prompt_file_and_output_file_placeholders(self) -> None:
        script = self.work / "file_agent.py"
        script.write_text("import sys, pathlib, json\nprompt = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
                          "assert 'Folder manifest' in prompt\n"
                          f"pathlib.Path(sys.argv[2]).write_text({json.dumps(json.dumps(plan()))}, encoding='utf-8')\n"
                          "print('progress noise')\n", encoding="utf-8")
        result = self.propose(provider={"id": "custom", "kind": "cli", "command": sys.executable,
                                        "args": [str(script), "{promptFile}", "{outputFile}"]})
        self.assertTrue(result["ok"], result)

    @unittest.skipUnless(os.name == "nt", "batch-file argument rules apply on Windows")
    def test_batch_commands_refuse_prompt_arguments(self) -> None:
        batch = self.work / "agent.cmd"
        batch.write_text("@echo off\r\necho {}\r\n", encoding="utf-8")
        record = agents.run_cli(agents.normalize_provider({"id": "custom", "kind": "cli", "command": str(batch),
                                                           "args": ["{prompt}"]}), "hi", self.source, timeout=5)
        self.assertIn(".bat/.cmd", record["error"])

    def test_transcript_is_redacted_and_runs_are_pruned(self) -> None:
        leaky = "Using key " + API_KEY + " and Bearer abcdefghijklmnop1234\n" + json.dumps(plan())
        self.responses(leaky)
        result = self.propose()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["transcriptPath"].startswith("Research Dashboard/System/data/agent-runs/"))
        text = (self.vault / result["transcriptPath"]).read_text(encoding="utf-8")
        self.assertNotIn(API_KEY, text)
        self.assertNotIn("abcdefghijklmnop1234", text)
        self.assertIn(agents.REDACTED, text)
        transcript = json.loads(text)
        self.assertEqual(transcript["provider"]["apiKey"], agents.REDACTED)
        self.assertIn("Folder manifest", transcript["attempts"][0]["prompt"])
        self.assertNotIn(API_KEY, json.dumps(result))
        runs = self.vault / agents.RUNS_FOLDER
        for index in range(agents.MAX_RUNS + 5):
            (runs / f"00000000T000000Z-old-{index:03d}.json").write_text("{}", encoding="utf-8")
        self.propose()
        self.assertEqual(len(list(runs.glob("*.json"))), agents.MAX_RUNS)

    def test_cli_propose_exit_0_with_errors_in_json(self) -> None:
        self.responses("not a plan at all")
        completed = self.cli("plan", "propose", stdin=json.dumps({"source": str(self.source), "provider": self.provider()}))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertTrue(result["repaired"])
        self.assertTrue(any("No JSON object" in item for item in result["errors"]))


class _FakeChat(http.server.BaseHTTPRequestHandler):
    requests: list[dict] = []
    reply: dict = {}
    status = 200

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).requests.append({"path": self.path, "headers": dict(self.headers),
                                    "body": json.loads(self.rfile.read(length))})
        payload = json.dumps(type(self).reply).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


class HttpProviderTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        _FakeChat.requests = []
        _FakeChat.status = 200
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeChat)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_openai_compatible_server_round_trip(self) -> None:
        _FakeChat.reply = {"choices": [{"message": {"role": "assistant",
                                                    "content": "```json\n" + json.dumps(plan()) + "\n```"},
                                        "finish_reason": "stop"}]}
        result = self.propose(provider={"id": "openai-compatible", "model": "qwen3", "baseUrl": self.base,
                                        "apiKey": API_KEY})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["repaired"])
        sent = _FakeChat.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {API_KEY}")
        self.assertEqual(sent["body"]["model"], "qwen3")
        self.assertNotIn(API_KEY, json.dumps(sent["body"]))
        self.assertIn("You cannot browse the folder", sent["body"]["messages"][1]["content"])
        text = (self.vault / result["transcriptPath"]).read_text(encoding="utf-8")
        self.assertNotIn(API_KEY, text)

    def test_http_error_is_reported_in_json(self) -> None:
        _FakeChat.status = 401
        _FakeChat.reply = {"error": {"message": "invalid key " + API_KEY}}
        result = self.propose(provider={"id": "openai-compatible", "model": "m", "baseUrl": self.base, "apiKey": API_KEY})
        self.assertFalse(result["ok"])
        self.assertTrue(any("HTTP 401" in item for item in result["errors"]))
        self.assertNotIn(API_KEY, json.dumps(result))

    def test_anthropic_protocol_with_injected_transport(self) -> None:
        calls = []

        def transport(url, headers, body, timeout, max_bytes):
            calls.append((url, headers, json.loads(body)))
            return 200, json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(plan())}],
                                    "stop_reason": "end_turn"}).encode("utf-8")

        result = agents.propose(self.vault, {"source": str(self.source), "provider": {
            "id": "anthropic-api", "model": "claude-test", "apiKey": API_KEY}}, transport=transport)
        self.assertTrue(result["ok"], result)
        url, headers, body = calls[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], API_KEY)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["model"], "claude-test")
        self.assertIn("system", body)

    def test_http_timeout(self) -> None:
        def transport(*args, **kwargs):
            raise TimeoutError("timed out")

        result = agents.propose(self.vault, {"source": str(self.source), "timeoutSeconds": 2, "provider": {
            "id": "openai-compatible", "model": "m", "baseUrl": self.base}}, transport=transport)
        self.assertFalse(result["ok"])
        self.assertTrue(any("timed out" in item for item in result["errors"]))


class ManifestPrivacyTests(Fixture):
    def test_exclude_globs_remove_entries_everywhere(self) -> None:
        self.file("results/secret_run.csv", "a\n1\n")
        result = manifest(self.vault, self.source, exclude=["private", "results/*.csv"])
        paths = [item["path"] for item in result["tree"]] + [item["path"] for item in result["excerpts"]]
        self.assertFalse(any(path.startswith("private") or path.endswith(".csv") for path in paths), paths)
        self.assertIn("results/case_1.mat", paths)
        self.assertEqual(result["totals"]["excludedEntries"], 3)
        self.assertEqual(result["limits"]["exclude"], ["private", "results/*.csv"])
        with self.assertRaises(ValueError):
            manifest(self.vault, self.source, exclude=["../outside"])
        completed = self.cli("plan", "manifest", "--source", str(self.source), "--exclude", "private")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        listed = [item["path"] for item in json.loads(completed.stdout)["tree"]]
        self.assertFalse(any(path.startswith("private") for path in listed), listed)


if __name__ == "__main__":
    unittest.main()
