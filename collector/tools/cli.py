#!/usr/bin/env python3
"""Register and observe research projects without modifying source projects."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from adapters.common import UTC, iso
from dashboard_core import (VERSION, CollectionSuperseded, atomic_text, blank_state, collect_one, configurations,
                            effective_health, host_id, output_path, read_json, register,
                            source_root, validate_id, validate_state)
from onboarding import connect_folder, inspect_folder, read_adapter_stdin
from milestones import apply_template, find_template, list_templates, template_summary
from planning import manifest, preview, read_request
from agents import detect_local_servers, list_providers, propose, test_provider
from profiles import (delete_profile, find_profile, load_library, read_profile_stdin, save_profile, summary,
                      unwrap, validate_profile)
from project_catalog import PROJECT_TYPES, active_configurations, create_logical_project, project_catalogs
from reconciliation import reconcile
import discovery
from watcher import SavedChangeSchedule, configuration_fingerprint, latex_fingerprint
from layout import layout_for
from workspace import import_vault, init_workspace, workspace_info


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--vault", "--workspace", dest="vault", type=Path, default=Path(__file__).resolve().parents[3],
                        help="Workspace root: an app workspace (with workspace.json) or an Obsidian vault")
    result.add_argument("--host-id", default=host_id(), help="Collection owner / local alias filename")
    result.add_argument("--version", action="version", version=VERSION)
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="Report the collector version and the workspace layout")
    init = sub.add_parser("init-workspace", help="Create an empty app workspace (workspace.json and folders) at --workspace")
    init.add_argument("--name", default=None, help="Display name stored in workspace.json")
    importer = sub.add_parser("import-vault", help="One-time import from an Obsidian vault's Research Dashboard folder")
    importer.add_argument("--from", dest="source_vault", required=True, type=Path, help="Obsidian vault root")
    importer.add_argument("--dry-run", action="store_true", help="Report what would be imported without writing")
    new = sub.add_parser("register", help="Create an unconfigured project; never overwrite")
    new.add_argument("--id", required=True)
    new.add_argument("--kind", required=True, choices=["latex", "abaqus", "isale"])
    new.add_argument("--title", required=True)
    new.add_argument("--parent-id")
    new.add_argument("--root-alias")
    new.add_argument("--source-path", default="")
    new.add_argument("--adapter-json", type=Path, help="Adapter options JSON object")
    new.add_argument("--interval", type=int, default=60)
    new.add_argument("--stale-after", type=int, default=180)
    inspect = sub.add_parser("inspect", help="Inspect a project folder without writing or running software")
    inspect.add_argument("--source", required=True, type=Path)
    connect = sub.add_parser("connect", help="Connect an inspected folder profile and collect once")
    connect.add_argument("--source", required=True, type=Path)
    connect.add_argument("--id", required=True)
    connect.add_argument("--kind", choices=["latex", "abaqus", "isale"],
                         help="Source kind; required without --profile, otherwise derived from the profile")
    connect.add_argument("--title", required=True)
    stdin = connect.add_mutually_exclusive_group()
    stdin.add_argument("--adapter-stdin", action="store_true", help="Read inspected adapter JSON from stdin")
    stdin.add_argument("--profile-stdin", action="store_true",
                       help="Read an inline monitoring profile from stdin (or {profile, adapter?})")
    connect.add_argument("--profile", help="Monitoring profile ID from the library (built-in or user)")
    connect.add_argument("--type", dest="project_type", choices=list(PROJECT_TYPES),
                         help="Project type of a new project (default: profile projectType or kind inference)")
    connect.add_argument("--parent", help="Top-level project ID to nest a new project under")
    connect.add_argument("--milestone-template", help="Template ID, 'profile' (the profile's template) or 'none'")
    connect.add_argument("--interval", type=int, default=60)
    connect.add_argument("--stale-after", type=int, default=180)
    connect.add_argument("--dashboard-project", help="Attach the new source to an existing logical research project")
    plan = sub.add_parser("plan", help="Dry-run plan previews and folder manifests (no writes)")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    plan_preview = plan_sub.add_parser("preview", help="Read {source, profileId|profile, adapter?, inspectedProfileId?} "
                                                       "from stdin and report what would be monitored")
    plan_preview.add_argument("--stdin", action="store_true", default=True, help="Request JSON on stdin (default)")
    plan_manifest = plan_sub.add_parser("manifest", help="Bounded folder overview for an agent")
    plan_manifest.add_argument("--source", required=True, type=Path)
    plan_manifest.add_argument("--max-depth", type=int, default=4, help="Tree depth, 1-8 (default 4)")
    plan_manifest.add_argument("--max-entries", type=int, default=400, help="Listed tree entries, 20-2000 (default 400)")
    plan_manifest.add_argument("--excerpts", type=int, default=20, help="Text file heads, 0-50 (default 20)")
    plan_manifest.add_argument("--excerpt-bytes", type=int, default=2048, help="Bytes per head, 128-8192 (default 2048)")
    plan_manifest.add_argument("--exclude", action="append", default=None, metavar="GLOB",
                               help="Privacy exclude glob (repeatable); matching files/folders are left out")
    plan_sub.add_parser("providers", help="List agent provider presets with PATH detection")
    plan_sub.add_parser("test", help="Read {provider, prompt?, timeoutSeconds?} on stdin; check the provider answers")
    plan_sub.add_parser("detect", help="List model servers running on this computer (Ollama, LM Studio)")
    plan_propose = plan_sub.add_parser("propose", help="Read {source, provider, instructions?, previousPlan?, privacy?, "
                                                       "timeoutSeconds?} from stdin; ask the agent for a plan")
    plan_propose.add_argument("--stdin", action="store_true", default=True, help="Request JSON on stdin (default)")
    library = sub.add_parser("profiles", help="Manage the monitoring-profile library")
    library_sub = library.add_subparsers(dest="profiles_command", required=True)
    library_sub.add_parser("list", help="List built-in and user profiles plus milestone templates")
    library_get = library_sub.add_parser("get", help="Print one profile's full JSON")
    library_get.add_argument("--id", required=True)
    library_validate = library_sub.add_parser("validate", help="Validate a profile from stdin; always JSON {ok, errors, warnings}")
    library_validate.add_argument("--stdin", action="store_true", default=True)
    library_save = library_sub.add_parser("save", help="Save a user profile from stdin; overwrites keep a version")
    library_save.add_argument("--stdin", action="store_true", default=True)
    library_delete = library_sub.add_parser("delete", help="Delete a user profile (archived recoverably)")
    library_delete.add_argument("--id", required=True)
    tasks = sub.add_parser("milestones", help="Milestone templates")
    tasks_sub = tasks.add_subparsers(dest="milestones_command", required=True)
    tasks_sub.add_parser("list", help="List milestone templates")
    tasks_apply = tasks_sub.add_parser("apply", help="Append a template's tasks to Projects/<id>/Project.md idempotently")
    tasks_apply.add_argument("--project", required=True, help="Logical project ID")
    tasks_apply.add_argument("--template", required=True, help="Milestone template ID")
    tasks_apply.add_argument("--source", help="Paper source ID when the project has several")
    tasks_apply.add_argument("--dry-run", action="store_true", help="Report the tasks without writing")
    sub.add_parser("projects", help="List logical projects available for source attachment")
    create = sub.add_parser("create-project", help="Create a sourceless logical project record, Canvas and catalog")
    create.add_argument("--id", required=True, help="New logical project ID")
    create.add_argument("--title", required=True)
    create.add_argument("--type", default="other", choices=list(PROJECT_TYPES), help="Project type (default: other)")
    create.add_argument("--parent", help="Existing top-level logical project ID")
    discover = sub.add_parser("discover", help="Sub-project discovery: scan discovery folders for new candidate folders")
    discover.add_argument("action", nargs="?", default="scan", choices=["scan", "dismiss", "restore", "configure", "show", "accept"],
                          help="scan (default; read-only), dismiss/restore a folder, configure/show a project's discovery "
                               "folder, or accept (connect) one folder as a sub-project")
    discover.add_argument("--project", help="Logical project ID")
    discover.add_argument("--all", action="store_true", help="Scan every project with a discovery folder")
    discover.add_argument("--folder", help="configure: absolute folder; dismiss/restore/accept: subfolder name or absolute path")
    discover.add_argument("--clear", action="store_true", help="configure: remove the discovery folder and its dismissals")
    discover.add_argument("--auto-accept", choices=["on", "off"], help="configure: connect confident matches automatically")
    discover.add_argument("--ignore", action="append", default=None, metavar="GLOB",
                          help="configure: folder-name glob to skip (repeatable; replaces the list)")
    discover.add_argument("--clear-ignore", action="store_true", help="configure: remove every ignore glob")
    discover.add_argument("--min-confidence", type=float, default=0.8, help="accept: minimum best-candidate confidence (default 0.8)")
    cleanup = sub.add_parser("reconcile",help="Preview orphaned internal files; --apply archives them recoverably")
    cleanup.add_argument("--apply", action="store_true", help="Move verified orphan files into the vault trash")
    for name in ("collect", "status", "doctor", "watch"):
        command = sub.add_parser(name)
        command.add_argument("--project", help="A single project ID")
        if name == "watch":
            command.add_argument("--once", action="store_true", help="One foreground cycle (useful for checks)")
    return result


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def collect(vault: Path, configs: list[dict], host: str) -> tuple[list[dict], bool]:
    results = []
    failed = False
    for config in configs:
        try:
            state = collect_one(vault, config, host, dt.datetime.now(UTC))
            results.append({"projectId": state["projectId"], "runtime": state["runtime"]["status"],
                            "collection": state["collection"]["health"],
                            "revision": next((detail.get("revision") for detail in state["details"]
                                              if detail.get("kind") == "paper"), None)
                                        or state["collection"]["attemptedAt"]})
            failed |= state["collection"]["health"] == "error"
        except CollectionSuperseded as error:
            failed = True
            results.append({"projectId": config["projectId"], "superseded": True, "error": str(error)})
        except (OSError, ValueError, KeyError, TypeError) as error:
            failed = True
            results.append({"projectId": config["projectId"], "error": str(error)})
    return results, failed


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    vault = args.vault.resolve()
    try:
        validate_id(args.host_id, "hostId")
        if args.command == "init-workspace":
            print_json(init_workspace(vault, args.name))
            return 0
        if not vault.is_dir():
            raise FileNotFoundError("Vault directory does not exist")
        if args.command == "info":
            print_json(workspace_info(vault, VERSION))
            return 0
        if args.command == "import-vault":
            print_json(import_vault(args.source_vault.resolve(), vault, dry_run=args.dry_run))
            return 0
        if args.command == "inspect":
            print_json(inspect_folder(args.source, vault))
            return 0
        if args.command == "connect":
            profile, adapter = None, None
            if args.profile_stdin:
                if args.profile:
                    raise ValueError("Use either --profile or --profile-stdin")
                payload = read_profile_stdin(sys.stdin)
                profile = unwrap(payload)
                adapter = payload.get("adapter") if isinstance(payload, dict) and "profile" in payload else None
                errors, _ = validate_profile(profile)
                if errors:
                    raise ValueError("Invalid monitoring profile: " + "; ".join(errors[:10]))
            elif args.profile:
                profile, _ = find_profile(vault, args.profile)
            if args.adapter_stdin:
                adapter = read_adapter_stdin(sys.stdin)
            elif profile is None:
                raise ValueError("connect needs --adapter-stdin (with --kind), --profile or --profile-stdin")
            result = connect_folder(vault, args.host_id, args.source, args.id, args.kind,
                                    args.title, adapter, args.interval, args.stale_after,
                                    dashboard_project=args.dashboard_project, profile=profile,
                                    project_type=args.project_type, parent_id=args.parent,
                                    milestone_template=args.milestone_template)
            print_json(result)
            return 1 if result["collectionHealth"] == "error" else 0
        if args.command == "plan":
            if args.plan_command == "preview":
                print_json(preview(vault, read_request(sys.stdin)))
            elif args.plan_command == "providers":
                print_json(list_providers())
            elif args.plan_command == "test":
                print_json(test_provider(read_request(sys.stdin)))
            elif args.plan_command == "detect":
                print_json(detect_local_servers())
            elif args.plan_command == "propose":
                print_json(propose(vault, read_request(sys.stdin)))
            else:
                print_json(manifest(vault, args.source, args.max_depth, args.max_entries, args.excerpts, args.excerpt_bytes,
                                    exclude=args.exclude))
            return 0
        if args.command == "profiles":
            if args.profiles_command == "list":
                entries, errors = load_library(vault)
                templates, template_errors = list_templates(vault)
                print_json({"profiles": [summary(entry, vault) for entry in entries],
                            "milestoneTemplates": [template_summary(entry) for entry in templates],
                            "errors": errors + template_errors})
            elif args.profiles_command == "get":
                profile, origin = find_profile(vault, args.id)
                print_json({"origin": origin, "profile": profile})
            elif args.profiles_command == "validate":
                profile = unwrap(read_profile_stdin(sys.stdin))
                errors, warnings = validate_profile(profile)
                print_json({"ok": not errors, "profileId": profile.get("id") if isinstance(profile, dict) else None,
                            "errors": errors, "warnings": warnings})
            elif args.profiles_command == "save":
                print_json(save_profile(vault, read_profile_stdin(sys.stdin)))
            else:
                print_json(delete_profile(vault, args.id))
            return 0
        if args.command == "milestones":
            if args.milestones_command == "list":
                templates, errors = list_templates(vault)
                print_json({"milestoneTemplates": [template_summary(entry) for entry in templates], "errors": errors})
            else:
                print_json(apply_template(vault, args.project, find_template(vault, args.template),
                                          source_id=args.source, dry_run=args.dry_run))
            return 0
        if args.command == "create-project":
            print_json(create_logical_project(vault, args.id, args.title, args.type, args.parent))
            return 0
        if args.command == "discover":
            action = args.action
            if action == "scan":
                if bool(args.project) == bool(args.all):
                    raise ValueError("discover needs exactly one of --project <ID> or --all")
                print_json(discovery.public(discovery.scan(vault, args.host_id, args.project)))
                return 0
            if not args.project:
                raise ValueError(f"discover {action} needs --project <ID>")
            if action == "show":
                print_json(discovery.show(vault, args.host_id, args.project))
            elif action == "configure":
                print_json(discovery.configure(vault, args.host_id, args.project, folder=args.folder, clear=args.clear,
                                               auto_accept=None if args.auto_accept is None else args.auto_accept == "on",
                                               ignore=args.ignore, clear_ignore=args.clear_ignore))
            elif action == "restore":
                print_json(discovery.restore(vault, args.project, args.folder))
            else:
                if not args.folder:
                    raise ValueError(f"discover {action} needs --folder <subfolder>")
                if action == "dismiss":
                    print_json(discovery.dismiss(vault, args.host_id, args.project, args.folder))
                else:
                    if not 0 < args.min_confidence <= 1:
                        raise ValueError("--min-confidence must be within (0, 1]")
                    result = discovery.accept(vault, args.host_id, args.project, args.folder, args.min_confidence)
                    print_json(result)
            return 0
        if args.command == "projects":
            print_json({"projects": project_catalogs(vault)})
            return 0
        if args.command == "reconcile":
            print_json(reconcile(vault, apply=args.apply))
            return 0
        if args.command == "register":
            config = {"schemaVersion": "1.0", "projectId": args.id, "title": args.title, "kind": args.kind,
                      "parentId": args.parent_id, "projectNote": layout_for(vault).source_note(args.id),
                      "source": {"rootAlias": args.root_alias, "path": args.source_path},
                      "collector": {"hostId": args.host_id, "staleAfterSeconds": args.stale_after,
                                    "intervalSeconds": args.interval},
                      "adapter": read_json(args.adapter_json) if args.adapter_json else {}}
            register(vault, config)
            print_json({"registered": args.id, "hostId": args.host_id, "configured": args.root_alias is not None})
            return 0
        configs = active_configurations(vault, args.project)
        if args.command == "status":
            states = []
            for config in configs:
                path = output_path(vault, layout_for(vault).state(config['projectId']))
                state = read_json(path) if path.exists() else blank_state(config, args.host_id, dt.datetime.now(UTC))
                validate_state(state)
                state["collection"]["health"] = effective_health(state, dt.datetime.now(UTC))
                states.append(state)
            print_json(states)
            return 0
        if args.command == "doctor":
            checks = []
            healthy = True
            for config in configs:
                try:
                    assigned = config["collector"]["hostId"] == args.host_id
                    root = source_root(vault, config, args.host_id) if assigned else None
                    checks.append({"projectId": config["projectId"], "assignedToThisHost": assigned,
                                   "source": str(root) if root else None,
                                   "health": "ready" if assigned and root else "unconfigured" if assigned else "other-host"})
                except (OSError, ValueError, KeyError, TypeError) as error:
                    healthy = False
                    checks.append({"projectId": config["projectId"], "health": "error", "error": str(error)})
            print_json({"version": VERSION, "hostId": args.host_id, "checks": checks,
                        "note": "No compiler, solver, background task, or external write was executed."})
            return 0 if healthy else 1
        if args.command == "collect":
            results, failed = collect(vault, configs, args.host_id)
            print_json(results)
            return 1 if failed else 0
        # One owned foreground process serializes collection. Saved LaTeX changes
        # bypass the periodic deadline after a short stability debounce.
        schedules: dict[str, SavedChangeSchedule] = {}
        next_heartbeat = 0.0
        print(f"Foreground watch on {args.host_id}; Ctrl-C stops collection.", file=sys.stderr)
        while True:
            configs = active_configurations(vault, args.project)
            owned = [config for config in configs if config["collector"]["hostId"] == args.host_id]
            active_ids = {config["projectId"] for config in owned}
            schedules = {pid: value for pid, value in schedules.items() if pid in active_ids}
            selected: list[dict] = []
            captured: dict[str, str] = {}
            for config in owned:
                pid = config["projectId"]
                schedule = schedules.setdefault(pid, SavedChangeSchedule())
                signature = configuration_fingerprint(config)
                if config["kind"] == "latex":
                    try:
                        root = source_root(vault, config, args.host_id)
                        if root is not None:
                            signature += ":" + str(root) + ":" + latex_fingerprint(root)
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        signature += f":unavailable:{type(error).__name__}:{error}"
                        if schedule.signature != signature:
                            print(f"Live scan for {pid}: {error}; periodic collection remains available.", file=sys.stderr)
                observed = time.monotonic()
                schedule.observe(signature, observed)
                if schedule.ready(observed):
                    selected.append(config)
                    captured[pid] = signature
            if selected:
                results, _ = collect(vault, selected, args.host_id)
                for result in results:
                    event = ("snapshot-committed" if "revision" in result else
                             "collection-superseded" if result.get("superseded") else "collection-error")
                    print(json.dumps({"event": event, **result}, ensure_ascii=False,
                                     allow_nan=False), flush=True)
                for config in selected:
                    schedules[config["projectId"]].collected(captured[config["projectId"]], time.monotonic(),
                                                           config["collector"]["intervalSeconds"])
            if time.monotonic() >= next_heartbeat:
                now = iso(dt.datetime.now(UTC))
                text = (f"---\ntype: dashboard-collector-health\nhost_id: {json.dumps(args.host_id)}\nlast_seen_at: {json.dumps(now)}\n---\n\n"
                        "# Collector heartbeat\n\nThe foreground watch is running. When this record stops updating, mark it stale based on its timestamp; an old file does not prove the collector is still online.\n")
                atomic_text(output_path(vault, layout_for(vault).heartbeat(args.host_id)), text)
                next_heartbeat = time.monotonic() + 15
            if args.once:
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        print("Collection stopped; no background task remains.", file=sys.stderr)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
