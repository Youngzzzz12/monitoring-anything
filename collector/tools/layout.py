"""Workspace layouts: where the collector reads and writes each file.

The collector originally lived inside an Obsidian vault and used fixed
``Research Dashboard/System/...`` paths. The desktop app owns a plain,
human-readable workspace folder instead. Both layouts are supported so the
upstream behaviour (and its tests) stay intact:

* ``vault`` -- the original Obsidian layout (``Research Dashboard/...``).
* ``app``   -- the Monitoring Anything desktop workspace::

      workspace.json
      projects/<id>/project.json     project record + catalog (sourceIds)
      projects/<id>/milestones.md    GFM task list with [paper::][chapter::][stage::][due::]
      sources/<source id>.json       collector configuration (incl. profile)
      sources/<source id>.md         generated source record
      local/<host id>.json           this computer's folder mappings
      data/<source id>.json          snapshots
      data/history/<source id>.json  paper word-count observations
      data/generated/                Markdown projections and heartbeat
      data/agent-runs/               agent transcripts
      data/discovery/state.json      dismissed sub-project discovery candidates
      profiles/user/                 saved monitoring profiles
      profiles/milestones/user/      saved milestone templates
      .trash/                        recoverable removals

A root containing ``workspace.json`` uses the app layout; anything else uses
the vault layout. ``RD_LAYOUT=app|vault`` overrides the detection.
"""

from __future__ import annotations

import os
from pathlib import Path

WORKSPACE_MARKER = "workspace.json"


class Layout:
    """Relative paths (POSIX strings) for one workspace layout."""

    name = "abstract"
    #: The project record lives inside the catalog JSON (app) rather than a Markdown note (vault).
    record_in_catalog = False
    #: Optional Obsidian artefacts: Canvas dashboards and Dataview cards.
    has_canvas = False

    # ----- sources
    def source_configs(self) -> str: raise NotImplementedError
    def source_config(self, source_id: str) -> str: return f"{self.source_configs()}/{source_id}.json"
    def source_note(self, source_id: str) -> str: raise NotImplementedError
    def records_dir(self) -> str: raise NotImplementedError
    def state_dir(self) -> str: raise NotImplementedError
    def state(self, source_id: str) -> str: return f"{self.state_dir()}/{source_id}.json"
    def history(self, source_id: str) -> str: raise NotImplementedError
    def generated(self, source_id: str) -> str: raise NotImplementedError
    def heartbeat(self, host: str) -> str: raise NotImplementedError
    def local(self, host: str) -> str: raise NotImplementedError

    # ----- logical projects
    def catalog_dir(self) -> str: raise NotImplementedError
    def catalog(self, project_id: str) -> str: raise NotImplementedError
    def catalog_files(self, root: Path) -> list[tuple[str, Path]]: raise NotImplementedError
    def project_dir(self, project_id: str) -> str: raise NotImplementedError
    def project_note(self, project_id: str) -> str: raise NotImplementedError
    def project_dashboard(self, project_id: str) -> str | None: return None
    def cards_dir(self, project_id: str) -> str | None: return None
    def catalog_path_fields(self, project_id: str) -> dict[str, str]: return {}
    def liveness(self, project_id: str) -> str:
        """The file whose presence makes a logical project live."""
        return self.project_note(project_id)

    # ----- libraries and housekeeping
    def profiles_user(self) -> str: raise NotImplementedError
    def profiles_history(self) -> str: return self.profiles_user() + "/.history"
    def milestone_templates_user(self) -> str: raise NotImplementedError
    def agent_runs(self) -> str: raise NotImplementedError
    def trash(self) -> str: raise NotImplementedError
    def discovery_state(self) -> str: raise NotImplementedError
    def projects_root(self) -> str: raise NotImplementedError

    def __repr__(self) -> str:
        return f"<Layout {self.name}>"


class VaultLayout(Layout):
    name = "vault"
    has_canvas = True
    base = "Research Dashboard/System"

    def source_configs(self) -> str: return f"{self.base}/config/projects"
    def source_note(self, source_id: str) -> str: return f"{self.base}/records/{source_id}/project.md"
    def records_dir(self) -> str: return f"{self.base}/records"
    def state_dir(self) -> str: return f"{self.base}/data/projects"
    def history(self, source_id: str) -> str: return f"{self.base}/data/history/{source_id}.json"
    def generated(self, source_id: str) -> str: return f"{self.base}/data/generated/{source_id}.md"
    def heartbeat(self, host: str) -> str: return f"{self.base}/data/generated/collector-health-{host}.md"
    def local(self, host: str) -> str: return f"{self.base}/config/local/{host}.json"

    def catalog_dir(self) -> str: return f"{self.base}/config/catalog"
    def catalog(self, project_id: str) -> str: return f"{self.catalog_dir()}/{project_id}.json"

    def catalog_files(self, root: Path) -> list[tuple[str, Path]]:
        folder = root / self.catalog_dir()
        return [(path.stem, path) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []

    def project_dir(self, project_id: str) -> str: return f"Research Dashboard/Projects/{project_id}"
    def project_note(self, project_id: str) -> str: return f"{self.project_dir(project_id)}/Project.md"
    def project_dashboard(self, project_id: str) -> str: return f"{self.project_dir(project_id)}/Dashboard.canvas"
    def cards_dir(self, project_id: str) -> str: return f"{self.base}/cards/projects/{project_id}"

    def catalog_path_fields(self, project_id: str) -> dict[str, str]:
        return {"projectNote": self.project_note(project_id), "projectDashboard": self.project_dashboard(project_id)}

    def profiles_user(self) -> str: return f"{self.base}/profiles/user"
    def milestone_templates_user(self) -> str: return f"{self.base}/profiles/milestones/user"
    def agent_runs(self) -> str: return f"{self.base}/data/agent-runs"
    def trash(self) -> str: return ".trash/research-dashboard"
    def discovery_state(self) -> str: return f"{self.base}/data/discovery/state.json"
    def projects_root(self) -> str: return "Research Dashboard/Projects"


class AppLayout(Layout):
    name = "app"
    record_in_catalog = True

    def source_configs(self) -> str: return "sources"
    def source_note(self, source_id: str) -> str: return f"sources/{source_id}.md"
    def records_dir(self) -> str: return "sources"
    def state_dir(self) -> str: return "data"
    def history(self, source_id: str) -> str: return f"data/history/{source_id}.json"
    def generated(self, source_id: str) -> str: return f"data/generated/{source_id}.md"
    def heartbeat(self, host: str) -> str: return f"data/generated/collector-health-{host}.md"
    def local(self, host: str) -> str: return f"local/{host}.json"

    def catalog_dir(self) -> str: return "projects"
    def catalog(self, project_id: str) -> str: return f"projects/{project_id}/project.json"

    def catalog_files(self, root: Path) -> list[tuple[str, Path]]:
        folder = root / "projects"
        if not folder.is_dir():
            return []
        return [(path.parent.name, path) for path in sorted(folder.glob("*/project.json"))]

    def project_dir(self, project_id: str) -> str: return f"projects/{project_id}"
    def project_note(self, project_id: str) -> str: return f"projects/{project_id}/milestones.md"
    def liveness(self, project_id: str) -> str: return self.catalog(project_id)

    def profiles_user(self) -> str: return "profiles/user"
    def milestone_templates_user(self) -> str: return "profiles/milestones/user"
    def agent_runs(self) -> str: return "data/agent-runs"
    def trash(self) -> str: return ".trash"
    def discovery_state(self) -> str: return "data/discovery/state.json"
    def projects_root(self) -> str: return "projects"


VAULT = VaultLayout()
APP = AppLayout()
LAYOUTS = {VAULT.name: VAULT, APP.name: APP}


def layout_for(root: Path | None) -> Layout:
    """Layout of a workspace root: RD_LAYOUT override, else workspace.json marker, else the vault layout."""
    override = os.environ.get("RD_LAYOUT", "").strip().lower()
    if override in LAYOUTS:
        return LAYOUTS[override]
    if root is not None and (Path(root) / WORKSPACE_MARKER).is_file():
        return APP
    return VAULT


def source_note_paths(source_id: str) -> set[str]:
    """Every layout's canonical source-record path; configs may come from either layout."""
    return {layout.source_note(source_id) for layout in LAYOUTS.values()}
