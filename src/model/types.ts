/* Data contracts: the workspace snapshot read by the Rust side, collector snapshots (state.schema.json) and the
 * in-memory model the dashboard renders. Collector fields stay loosely typed where the collector owns them. */

import type { Task } from "./milestones";

export interface JsonEntry { id: string; value: unknown; error: string | null }
export interface ProjectEntry { id: string; record: unknown; recordError: string | null; milestones: string | null; milestonesError: string | null }
export interface ProfileTitle { id: string; title: string; origin: string }

export interface WorkspaceSnapshot {
  root: string;
  name: string;
  projects: ProjectEntry[];
  sources: JsonEntry[];
  states: JsonEntry[];
  local: JsonEntry[];
  profiles: ProfileTitle[];
  errors: string[];
  loadedAt: string;
}

export interface WorkspaceInfo { root: string; name: string; created: boolean; meta: Record<string, unknown> }

/** projects/<id>/project.json */
export interface ProjectRecord {
  schemaVersion: string;
  projectId: string;
  title: string;
  type?: string;
  parentId?: string | null;
  stage?: string;
  health?: string;
  nextStep?: string | null;
  due?: string | null;
  archived?: boolean;
  resources?: unknown[];
  knowledge?: unknown[];
  sourceIds: string[];
  [key: string]: unknown;
}

/** sources/<id>.json (collector configuration). */
export interface SourceConfig {
  schemaVersion: string;
  projectId: string;
  dashboardProjectId?: string;
  title: string;
  kind: string;
  profileId?: string | null;
  source?: { rootAlias: string | null; path: string };
  collector?: { hostId: string; intervalSeconds?: number; staleAfterSeconds?: number };
  adapter?: Record<string, unknown>;
  projectNote?: string;
}

export interface Runtime { status: string; progress: number | null; step?: string | null; currentValue?: number | null; targetValue?: number | null; unit?: string | null; runId?: string | null }
export interface Cases { planned?: number | null; completed?: number | null; running?: number | null; failed?: number | null; unknown?: number | null }
export interface Collection { health: string; attemptedAt?: string | null; lastSuccessfulAt?: string | null; sourceModifiedAt?: string | null; collectorVersion?: string; hostId?: string; staleAfterSeconds?: number | null; error?: string | null }

export interface Chapter {
  id: string; title: string; parentId?: string | null; bodyWords?: number; ownBodyWords?: number;
  deltaToday?: number | null; deltaWeek?: number | null; sourceHash?: string; file?: string; line?: number;
  modifiedAt?: string | number | null; identityMethod?: string;
  /** Last collection at which the section's own source changed (content fingerprint), or null until seen. */
  changedAt?: string | null;
}

export interface ActivityPeriod { baselineAt?: string | null; available?: boolean; coverage?: string; baselineKind?: string }
export interface PaperDetail {
  kind: "paper"; schemaVersion: 1; chapters: Chapter[]; title?: string; mainFile?: string; bodyWords?: number;
  observedAt?: string; countingMethod?: unknown; warnings?: unknown[];
  activity?: { timezone?: string; trackingStartedAt?: string | null; today?: ActivityPeriod; week?: ActivityPeriod };
}

export interface Detail { kind?: string; [key: string]: unknown }
export interface Artifact { type?: string; path?: string; verifiedAt?: string | null; runId?: string | null }

export interface SourceState {
  schemaVersion: string; projectId: string; title?: string; kind?: string; projectNote?: string;
  runtime: Runtime; cases: Cases; collection: Collection; artifacts?: Artifact[]; details?: Detail[];
  provenance?: { sourceFiles?: unknown[]; parserVersion?: string; configId?: string };
}

export interface Health { state: "fresh" | "stale" | "error" | "unconfigured"; isStale: boolean }

export interface Source {
  id: string;
  config: SourceConfig | null;
  state: SourceState | null;
  readError: string | null;
  health: Health;
  title: string;
  kind: string;
  display: { label: string; title: string; profileId: string | null } | null;
  directory: string | null;
}

/** Record fields under the names the reference view uses (frontmatter keys of the Obsidian Project.md). */
export interface ProjectPage {
  title: string;
  project_type: string;
  parent_id: string | null;
  stage: string;
  project_health: string;
  next_step: string | null;
  due: string | null;
  archived: boolean;
  resources: unknown[];
  knowledge: unknown[];
}

export interface Project {
  id: string;
  catalog: ProjectRecord;
  page: ProjectPage;
  sources: Source[];
  title: string;
  /** Workspace-relative milestones file. */
  note: string;
  /** In-app link to the project page. */
  dashboard: string;
  dashboardMissing?: boolean;
  kinds: string[];
  parentId: string;
  type: string;
  parent: Project | null;
  children: Project[];
  rawMilestones: Task[];
  milestoneText: string | null;
  milestoneReadError?: string;
}

export interface ModelIssue { path: string; message: string }
export interface Model { projects: Project[]; errors: ModelIssue[] }

export const OVERALL_LINK = "rd://overall";
export const projectLink = (id: string) => `rd://project/${id}`;
