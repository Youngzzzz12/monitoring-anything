/* Typed wrappers around the Rust commands and events (src-tauri/src/lib.rs). */

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { parseCliResult, type CliOutput } from "../model/cli";
import type { WorkspaceInfo, WorkspaceSnapshot } from "../model/types";

export interface WidgetSettings { visible: boolean; keepOnTop: boolean; fadeWhenIdle: boolean; idleOpacity: number; focusProjectId: string | null; magnification: number; bounds: { x: number; y: number; width: number; height: number } | null }
export interface AppSettings {
  schemaVersion: number; workspace: string | null; recentWorkspaces: string[]; pythonPath: string | null;
  motion: "full" | "subtle" | "off"; theme: "system" | "light" | "dark"; plainBackdrop: boolean;
  closeToTray: boolean; startLiveOnLaunch: boolean; widget: WidgetSettings;
  /** Provider choice, overrides, never-send patterns and time limit (keys live in the OS credential store). */
  agent: Record<string, unknown>;
  discoveryScan: boolean;
}
/** A request from the `ma` command line (an agent): open the plan card for a folder, optionally with its plan. */
export interface AgentRequest { action: "connect"; source: string; plan?: Record<string, unknown> | null; profileId?: string | null; title?: string | null; parentId?: string | null }
/** The monitoring-anything skill for one agent (state: missing, installed, outdated, modified, foreign). */
export interface SkillTarget { agent: string; title: string; folder: string; agentPresent: boolean; state: "missing" | "installed" | "outdated" | "modified" | "foreign" }
export interface PythonStatus { source: "custom" | "bundled" | "system"; path: string; version: string | null; ok: boolean; error: string | null }
export interface WatchStatus { running: boolean; stopping: boolean; error: string | null; startedAt: string | null }
export interface PythonCandidate { path: string; version: string | null; ok: boolean; error: string | null }
export interface AppInfo { version: string; collectorDir: string | null; platform: string; settingsPath: string; launchedByAutostart: boolean }
export interface NavigateTarget { projectId?: string | null; sourceId?: string | null; chapterId?: string | null }

export const api = {
  settings: () => invoke<AppSettings>("get_settings"),
  updateSettings: (patch: Record<string, unknown>) => invoke<AppSettings>("update_settings", { patch }),
  appInfo: () => invoke<AppInfo>("app_info"),
  workspaceCurrent: () => invoke<WorkspaceInfo | null>("workspace_current"),
  workspaceCreate: (path: string, name?: string | null) => invoke<WorkspaceInfo>("workspace_create", { path, name: name || null }),
  workspaceOpen: (path: string) => invoke<WorkspaceInfo>("workspace_open", { path }),
  snapshot: () => invoke<WorkspaceSnapshot>("workspace_snapshot"),
  projectUpdate: (id: string, patch: Record<string, unknown>) => invoke<Record<string, unknown>>("project_update", { id, patch }),
  milestonesWrite: (id: string, content: string, expected: string | null) => invoke<void>("milestones_write", { id, content, expected }),
  projectRemove: (id: string) => invoke<string>("project_remove", { id }),
  sourceRemap: (sourceId: string, folder: string) => invoke<{ folder: string; previous: string | null; historyTrash: string | null; warnings: string[] }>("source_remap", { sourceId, folder }),
  collectorRaw: (command: string, args: string[] = [], input: string | null = null, timeout?: number) =>
    invoke<CliOutput>("collector_run", { command, args, input, timeout: timeout ?? null }),
  async collector<T = unknown>(command: string, args: string[] = [], input: string | null = null, timeout?: number): Promise<T> {
    return parseCliResult<T>(command, await api.collectorRaw(command, args, input, timeout));
  },
  watchStart: () => invoke<WatchStatus>("watch_start"),
  watchStop: () => invoke<WatchStatus>("watch_stop"),
  watchStatus: () => invoke<WatchStatus>("watch_status"),
  pythonDetect: () => invoke<PythonCandidate[]>("python_detect"),
  pythonCheck: (path: string) => invoke<PythonCandidate>("python_check", { path }),
  pythonStatus: () => invoke<PythonStatus>("python_status"),
  agentKeyStatus: (providerId: string) => invoke<boolean>("agent_key_status", { providerId }),
  agentKeySet: (providerId: string, value: string | null) => invoke<boolean>("agent_key_set", { providerId, value }),
  agentProposeRaw: (runId: string, request: Record<string, unknown>) => invoke<CliOutput>("agent_propose", { runId, request }),
  agentTestRaw: (runId: string, request: Record<string, unknown>) => invoke<CliOutput>("agent_test", { runId, request }),
  agentCancel: (runId: string) => invoke<boolean>("agent_cancel", { runId }),
  openPath: (path: string) => invoke<void>("open_path", { path }),
  revealPath: (path: string) => invoke<void>("reveal_path", { path }),
  pathKind: (path: string) => invoke<"dir" | "file" | "missing">("path_kind", { path }),
  openUrl: (url: string) => invoke<void>("open_url", { url }),
  openSource: (sourceId: string, relative: string | null) => invoke<void>("open_source", { sourceId, relative }),
  widgetSetVisible: (visible: boolean) => invoke<boolean>("widget_set_visible", { visible }),
  widgetPointer: () => invoke<boolean | null>("widget_pointer"),
  connectInMain: (source: string | null) => invoke<void>("connect_in_main", { source }),
  takeRequests: () => invoke<AgentRequest[]>("take_requests"),
  skillStatus: () => invoke<SkillTarget[]>("skill_status"),
  skillInstall: (agents: string[], force = false) => invoke<SkillTarget[]>("skill_install", { agents, force }),
  skillUninstall: (agent: string) => invoke<SkillTarget[]>("skill_uninstall", { agent }),
  navigateMain: (target: NavigateTarget) => invoke<void>("navigate_main", { target }),
  quit: () => invoke<void>("quit_app"),
};

export const events = {
  workspaceChanged: (handler: (paths: string[]) => void): Promise<UnlistenFn> => listen<string[]>("workspace://changed", e => handler(e.payload)),
  workspaceOpened: (handler: (info: WorkspaceInfo) => void) => listen<WorkspaceInfo>("workspace://opened", e => handler(e.payload)),
  collectorEvent: (handler: (event: Record<string, unknown>) => void) => listen<Record<string, unknown>>("collector://event", e => handler(e.payload)),
  collectorStatus: (handler: (status: WatchStatus) => void) => listen<WatchStatus>("collector://status", e => handler(e.payload)),
  settingsChanged: (handler: (settings: AppSettings) => void) => listen<AppSettings>("settings://changed", e => handler(e.payload)),
  navigate: (handler: (target: NavigateTarget) => void) => listen<NavigateTarget>("rd://navigate", e => handler(e.payload)),
  connect: (handler: (request: { source: string | null }) => void) => listen<{ source: string | null }>("rd://connect", e => handler(e.payload)),
  request: (handler: (request: AgentRequest) => void) => listen<AgentRequest>("rd://request", e => handler(e.payload)),
  error: (handler: (message: string) => void) => listen<string>("app://error", e => handler(e.payload)),
};
