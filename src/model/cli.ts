/* Collector CLI contracts: result parsing and the plan-card helpers (monitoring options, previews, connect argv).
 * Ported from the Obsidian plugin's helpers.js; see collector/tools/README in the vault for the JSON contracts. */

import { isSafeId, PROJECT_TYPES, slugId } from "./rules";

export interface CliOutput { code: number | null; stdout: string; stderr: string; timedOut: boolean; truncated: boolean }

export function parseCliResult<T = unknown>(command: string, output: CliOutput): T {
  if (output.timedOut) throw new Error("The collector timed out or was stopped. Check the project configuration before retrying.");
  let value: unknown;
  try { value = JSON.parse(String(output.stdout).replace(/^﻿/, "").trim()); }
  catch (_) {
    if (output.code === 0) throw new Error("The collector returned an unreadable response. Check the Python path in Settings.");
  }
  if (output.code === 0) return value as T;
  const record = value as Record<string, unknown> | undefined;
  /* A first collection can fail after a project has been successfully created. */
  if (command === "connect" && output.code === 1 && record && typeof record.projectId === "string" && record.collectionHealth === "error") return value as T;
  /* One unavailable source must not prevent other ready projects from updating. */
  if (command === "doctor" && output.code === 1 && record && Array.isArray(record.checks)
    && (record.checks as Record<string, unknown>[]).some(check => check.assignedToThisHost === true && check.health === "ready")) return value as T;
  if (command === "collect" && output.code === 1 && Array.isArray(value)) return value as T;
  throw new Error(String(output.stderr || output.stdout || `Collector exited with code ${output.code}.`).trim().slice(0, 2000));
}

export const KIND_LABELS: Record<string, string> = { latex: "LaTeX", abaqus: "Abaqus", isale: "iSALE" };
const KIND_PROJECT_TYPES: Record<string, string> = { latex: "paper", abaqus: "simulation", isale: "simulation" };
/** Collection interval choices (seconds); stale-after is three intervals, never below the CLI's 180 s default. */
export const INTERVAL_CHOICES: Record<string, string> = { 30: "Every 30 seconds", 60: "Every minute", 300: "Every 5 minutes", 900: "Every 15 minutes", 3600: "Every hour" };
export function staleAfter(interval: number): number { return Math.max(180, Number(interval) * 3); }

export interface InspectEntry { id: string; label?: string; kind: string; adapter?: Record<string, unknown>; summary?: string[]; suggestedId?: string; suggestedTitle?: string }
export interface Inspection { sourcePath: string; title?: string; suggestedId?: string; profiles?: InspectEntry[]; candidates?: Record<string, unknown>[]; warnings?: string[] }

export type MilestoneTemplateRef = string | unknown[] | null;
export interface Validation { ok: boolean; errors: string[]; warnings?: string[] }
export interface PreviewResult { ok?: boolean; preview?: Record<string, unknown> | null; adapter?: Record<string, unknown> | null; errors?: string[]; warnings?: string[] }

/** A monitoring choice on the plan card: a detected library profile or legacy kind, an agent's plan, or an edited profile. */
export interface PlanOption {
  key: string; mode: "library" | "legacy" | "agent" | "custom"; profileId: string | null; kind?: string; title: string;
  origin: "builtin" | "user" | "agent" | "custom"; engine: Engine; sourceKind: string | null; projectType: string;
  confidence: number; threshold: number; meetsThreshold: boolean; matched: string[]; missed: string[];
  milestoneTemplate: MilestoneTemplateRef; entries: InspectEntry[]; suggestedId?: string; suggestedTitle?: string | null;
  /* Agent and edited plans. */
  profile?: Record<string, unknown>; plan?: Record<string, unknown>; adapter?: Record<string, unknown> | null;
  rationale?: string; warnings?: string[]; validation?: Validation; preview?: PreviewResult | null;
  savedAs?: string | null; savedTitle?: string | null; repaired?: boolean;
}

/** Monitoring options from `inspect`: scored `candidates`, or (older tools) one legacy option per source kind. */
/** How a monitoring type extracts: a built-in adapter, declarative rules, or a custom adapter written for the folder. */
export type Engine = "adapter" | "declarative" | "script";
export function engineOf(value: unknown): Engine { return value === "declarative" || value === "script" ? value : "adapter"; }
/** The custom adapter's source when a profile carries one (extract.script.code). */
export function adapterCode(profile: Record<string, unknown> | null | undefined): string | null {
  const script = ((profile?.extract as Record<string, unknown> | undefined)?.script) as Record<string, unknown> | undefined;
  return script && typeof script.code === "string" ? script.code : null;
}

export function planOptions(inspection: Inspection | null | undefined): PlanOption[] {
  const entries = Array.isArray(inspection?.profiles) ? inspection!.profiles!.filter(item => item && typeof item.id === "string") : [];
  if (Array.isArray(inspection?.candidates)) {
    return inspection!.candidates!.filter(item => item && isSafeId(item.profileId)).map(item => {
      const inspected = (Array.isArray(item.inspectedProfileIds) ? item.inspectedProfileIds as string[] : []).filter(id => entries.some(entry => entry.id === id));
      return {
        key: `profile:${item.profileId}`, mode: "library" as const, profileId: String(item.profileId), title: String(item.title || item.profileId),
        origin: item.origin === "user" ? "user" as const : "builtin" as const, engine: engineOf(item.engine),
        sourceKind: typeof item.sourceKind === "string" ? item.sourceKind : null,
        projectType: Object.hasOwn(PROJECT_TYPES, String(item.projectType)) ? String(item.projectType) : "other",
        confidence: Number.isFinite(Number(item.confidence)) ? Number(item.confidence) : 0, threshold: Number(item.threshold) || 0,
        meetsThreshold: Boolean(item.meetsThreshold), matched: Array.isArray(item.matched) ? item.matched.map(String) : [],
        missed: Array.isArray(item.missed) ? item.missed.map(String) : [],
        milestoneTemplate: typeof item.milestoneTemplate === "string" || Array.isArray(item.milestoneTemplate) ? item.milestoneTemplate as MilestoneTemplateRef : null,
        entries: entries.filter(entry => inspected.includes(entry.id)),
        suggestedId: typeof item.suggestedId === "string" ? item.suggestedId : undefined,
        suggestedTitle: typeof item.suggestedTitle === "string" ? item.suggestedTitle : undefined,
      };
    });
  }
  const kinds = [...new Set(entries.map(entry => entry.kind))];
  return kinds.filter(kind => KIND_LABELS[kind]).map(kind => {
    const own = entries.filter(entry => entry.kind === kind);
    return {
      key: `legacy:${kind}`, mode: "legacy" as const, profileId: null, kind, title: own.length === 1 ? String(own[0].label || KIND_LABELS[kind]) : `${KIND_LABELS[kind]} project`,
      origin: "builtin" as const, engine: "adapter" as const, sourceKind: kind, projectType: KIND_PROJECT_TYPES[kind], confidence: 1, threshold: 0, meetsThreshold: true,
      matched: [], missed: [], milestoneTemplate: null, entries: own, suggestedId: own[0]?.suggestedId, suggestedTitle: own[0]?.suggestedTitle,
    };
  });
}

export function bestOption(options: PlanOption[]): PlanOption | null { return options.find(item => item.meetsThreshold) || null; }

export function confidenceText(value: number): string {
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(Math.max(0, Math.min(1, number)) * 100)}% match` : "Match unknown";
}

export function ruleSummary(lines: string[], limit = 2): { shown: string[]; more: number } {
  const clean = (lines || []).map(line => String(line).replace(/^(allOf|anyOf|noneOf):\s*/, "")).filter(Boolean);
  return { shown: clean.slice(0, limit), more: Math.max(0, clean.length - limit) };
}

export interface TemplateSummary { id: string; title: string; description?: string | null; mode?: string; appliesTo?: string[]; requiresPaperSnapshot?: boolean }

export function templatesForType(templates: TemplateSummary[], projectType: string): TemplateSummary[] {
  return (Array.isArray(templates) ? templates : []).filter(item => item && isSafeId(item.id)
    && (!Array.isArray(item.appliesTo) || !item.appliesTo.length || item.appliesTo.includes(projectType)));
}

const RUNTIME_LABELS: Record<string, string> = { unconfigured: "Not connected", queued: "Queued", running: "Running", completed: "Run completed", failed: "Run failed", unknown: "Unknown" };

function numberText(value: unknown, digits = 3): string | null {
  const number = Number(value);
  if (value == null || value === "" || !Number.isFinite(number)) return null;
  return Number.isInteger(number) ? number.toLocaleString("en") : Number(number.toPrecision(digits)).toLocaleString("en");
}

/** Display rows for a `plan preview` result: only measured values appear; null never becomes zero. */
export function previewRows(preview: Record<string, unknown> | null | undefined): { stats: { label: string; value: string }[]; metrics: { label: string; value: string; unit: string }[]; progress: number | null } {
  if (!preview || typeof preview !== "object") return { stats: [], metrics: [], progress: null };
  const stats: { label: string; value: string }[] = [];
  const runtime = (preview.runtime || {}) as Record<string, unknown>, cases = (preview.cases || {}) as Record<string, unknown>;
  if (runtime.status) stats.push({ label: "Status", value: RUNTIME_LABELS[String(runtime.status)] || String(runtime.status) });
  if (numberText(runtime.currentValue) !== null || numberText(runtime.targetValue) !== null) {
    stats.push({ label: runtime.step ? `Progress · ${runtime.step}` : "Progress", value: `${numberText(runtime.currentValue) ?? "?"} / ${numberText(runtime.targetValue) ?? "?"}${runtime.unit ? ` ${runtime.unit}` : ""}` });
  } else if (runtime.step) stats.push({ label: "Step", value: String(runtime.step) });
  if (numberText(cases.completed) !== null || numberText(cases.planned) !== null) stats.push({ label: "Cases", value: `${numberText(cases.completed) ?? "?"} / ${numberText(cases.planned) ?? "?"}` });
  if (numberText(cases.failed) && Number(cases.failed) > 0) stats.push({ label: "Failed cases", value: numberText(cases.failed)! });
  const chapters = preview.chapters as Record<string, unknown> | null | undefined;
  if (chapters && typeof chapters === "object") {
    stats.push({ label: "Chapters", value: numberText(chapters.topLevel ?? chapters.count) ?? "?" });
    if (numberText(chapters.totalWords) !== null) stats.push({ label: "Words", value: numberText(chapters.totalWords)! });
  }
  const build = preview.build as Record<string, unknown> | null | undefined;
  if (build && typeof build === "object" && build.buildOutdated != null) stats.push({ label: "Build", value: build.buildOutdated ? "Outdated" : "Up to date" });
  const artifacts = Number.isFinite(Number(preview.artifactCount)) ? Number(preview.artifactCount) : Array.isArray(preview.artifacts) ? preview.artifacts.length : null;
  if (artifacts !== null) stats.push({ label: "Artifacts", value: artifacts.toLocaleString("en") });
  if (Number.isFinite(Number(preview.sourceFileCount))) stats.push({ label: "Files read", value: Number(preview.sourceFileCount).toLocaleString("en") });
  const metrics = (Array.isArray(preview.metrics) ? preview.metrics as Record<string, unknown>[] : []).filter(item => item && (item.label || item.id))
    .map(item => ({ label: String(item.label || item.id), value: numberText(item.value) ?? (item.value == null ? "—" : String(item.value)), unit: item.unit ? String(item.unit) : "" }));
  const ratio = Number(runtime.progress);
  return { stats, metrics, progress: runtime.progress != null && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1 ? ratio : null };
}

export interface ConnectValues {
  source: string; id: string; title: string; interval: number;
  dashboardProjectId?: string | null; projectType?: string | null; parentId?: string | null; milestoneTemplate?: string | null;
}

/** Collector argv and stdin for connecting the plan card's selection. Library profiles connect by ID (adapter-backed
 * built-ins also pass the inspected adapter on stdin); agent and edited plans pass the profile inline; legacy options
 * keep --kind/--adapter-stdin. */
export function connectRequest(values: ConnectValues, choice: { option: PlanOption; entryId?: string | null; profile?: Record<string, unknown> | null; adapter?: Record<string, unknown> | null }): { args: string[]; input: string | null } {
  const { option } = choice;
  const args = ["--source", values.source, "--id", values.id, "--title", values.title];
  let input: string | null = null;
  if (option.mode === "legacy") {
    const entry = option.entries.find(item => item.id === choice.entryId) || option.entries[0];
    if (!entry) throw new Error("Choose a detected entry to monitor.");
    args.push("--kind", String(option.kind), "--adapter-stdin");
    input = JSON.stringify(entry.adapter || {});
  } else if (option.mode === "library") {
    args.push("--profile", String(option.profileId));
    if (option.engine === "adapter") {
      const entry = option.entries.find(item => item.id === choice.entryId) || option.entries[0];
      if (!entry) throw new Error("The existing inspector found no connectable entry for this monitoring type.");
      args.push("--adapter-stdin");
      input = JSON.stringify(entry.adapter || {});
    }
  } else {
    if (!choice.profile || typeof choice.profile !== "object") throw new Error("This plan has no valid monitoring profile yet.");
    args.push("--profile-stdin");
    input = JSON.stringify(choice.adapter ? { profile: choice.profile, adapter: choice.adapter } : { profile: choice.profile });
  }
  args.push("--interval", String(values.interval), "--stale-after", String(staleAfter(values.interval)));
  if (values.dashboardProjectId) {
    if (!isSafeId(values.dashboardProjectId)) throw new Error("Choose a valid existing project.");
    args.push("--dashboard-project", values.dashboardProjectId);
  } else if (option.mode !== "legacy") {
    if (values.projectType) {
      if (!Object.hasOwn(PROJECT_TYPES, values.projectType)) throw new Error("Choose a supported project type.");
      args.push("--type", values.projectType);
    }
    if (values.parentId) args.push("--parent", values.parentId);
  }
  if (option.mode !== "legacy" && values.milestoneTemplate) args.push("--milestone-template", values.milestoneTemplate);
  return { args, input };
}

/* ---------- Agents ---------- */

export interface ProviderPreset {
  id: string; title: string; kind: "cli" | "http"; command?: string; args?: string[]; baseUrl?: string; model?: string;
  agentic: boolean; installed: boolean | null; notes?: string; protocol?: string;
  /** Suggested model ids (llm-wiki's verified ids for the CLIs). */
  models?: string[]; installUrl?: string; path?: string | null;
}
export interface ProviderOverrides { command?: string; args?: string[]; baseUrl?: string; model?: string }
export interface AgentSettings {
  providerId: string; overrides: Record<string, ProviderOverrides>; exclude: string[]; timeoutMinutes: number;
  /** The user picked the provider (otherwise the first agent found on this computer is used). */
  chosen: boolean;
  /** The first-run agent setup was completed or skipped. */
  setupDone: boolean;
}
/** A model server running on this computer (`plan detect`). */
export interface LocalServer { id: string; title: string; baseUrl: string; models: string[] }
/** CLI agents in the order the app prefers them when nothing was chosen. */
export const AUTO_ORDER = ["claude-code", "codex", "gemini-cli"];
export const AGENT_TIMEOUT = { min: 2, max: 30, fallback: 10 };

/** Presets offered when the collector cannot list providers; `installed: null` means unknown. */
export const FALLBACK_PROVIDERS: ProviderPreset[] = [
  { id: "claude-code", title: "Claude Code", kind: "cli", command: "claude", args: ["--print", "--output-format", "json"], agentic: true, installed: null, notes: "" },
  { id: "codex", title: "Codex", kind: "cli", command: "codex", args: ["exec", "--sandbox", "read-only", "--json"], agentic: true, installed: null, notes: "" },
  { id: "gemini-cli", title: "Gemini CLI", kind: "cli", command: "gemini", args: [], agentic: true, installed: null, notes: "" },
  { id: "openai-compatible", title: "OpenAI-compatible (Ollama, LM Studio)", kind: "http", baseUrl: "http://localhost:11434/v1", model: "", agentic: false, installed: null, notes: "" },
  { id: "anthropic-api", title: "Anthropic API", kind: "http", baseUrl: "https://api.anthropic.com", model: "", agentic: false, installed: null, notes: "" },
  { id: "custom", title: "Custom command", kind: "cli", command: "", args: [], agentic: false, installed: null, notes: "" },
];

export function normalizeAgentSettings(value: unknown): AgentSettings {
  const agent = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
  const minutes = Math.round(Number(agent.timeoutMinutes));
  return {
    providerId: typeof agent.providerId === "string" && agent.providerId ? agent.providerId : "claude-code",
    overrides: agent.overrides && typeof agent.overrides === "object" ? JSON.parse(JSON.stringify(agent.overrides)) : {},
    exclude: Array.isArray(agent.exclude) ? agent.exclude.map(String).filter(Boolean) : [],
    timeoutMinutes: Number.isFinite(minutes) ? Math.min(AGENT_TIMEOUT.max, Math.max(AGENT_TIMEOUT.min, minutes)) : AGENT_TIMEOUT.fallback,
    chosen: agent.chosen === true,
    setupDone: agent.setupDone === true,
  };
}

/** The provider to use: the chosen one, else the first CLI agent found on this computer, else the chosen id anyway
 * (so its problem can be explained). */
export function effectivePreset(providers: ProviderPreset[], settings: AgentSettings): ProviderPreset | null {
  const chosen = providers.find(item => item.id === settings.providerId) || null;
  if (settings.chosen && chosen) return chosen;
  const found = AUTO_ORDER.map(id => providers.find(item => item.id === id)).find(item => item?.installed === true);
  return found || chosen;
}

/** Why the provider cannot run yet ("" when it can): not installed, no command, no endpoint or model, no key. */
export function providerProblem(preset: ProviderPreset | null, overrides: ProviderOverrides = {}, hasKey = false): string {
  if (!preset) return "No agent is set up yet.";
  if (preset.kind === "cli") {
    const command = String(overrides.command ?? preset.command ?? "").trim();
    if (!command) return `Set the ${preset.title} command.`;
    if (preset.installed === false && command === preset.command) return `${preset.title} was not found on this computer.`;
    return "";
  }
  if (!String(overrides.baseUrl ?? preset.baseUrl ?? "").trim()) return `Set the ${preset.title} address.`;
  if (!String(overrides.model ?? preset.model ?? "").trim()) return `Choose a model for ${preset.title}.`;
  if ((preset.protocol === "anthropic" || preset.id === "anthropic-api") && !hasKey) return "The Anthropic API needs an API key.";
  return "";
}

/** The provider object for `plan propose`, without a key: the app's Rust side adds it from the OS keychain. */
export function providerRequest(preset: ProviderPreset | null | undefined, overrides: ProviderOverrides = {}): Record<string, unknown> {
  if (!preset || typeof preset.id !== "string") throw new Error("Choose an agent provider in Settings → Agent.");
  const kind = preset.kind === "http" ? "http" : "cli";
  const result: Record<string, unknown> = { id: preset.id, title: preset.title || preset.id, kind, agentic: Boolean(preset.agentic) };
  if (kind === "cli") {
    result.command = String(overrides.command ?? preset.command ?? "").trim();
    result.args = Array.isArray(overrides.args) ? overrides.args.map(String) : Array.isArray(preset.args) ? preset.args.map(String) : [];
    const model = String(overrides.model ?? preset.model ?? "").trim();
    if (model) result.model = model;
    if (!result.command) throw new Error(`Set the ${result.title} command in Settings → Agent.`);
  } else {
    result.baseUrl = String(overrides.baseUrl ?? preset.baseUrl ?? "").trim();
    result.model = String(overrides.model ?? preset.model ?? "").trim();
    if (!result.baseUrl) throw new Error(`Set the ${result.title} base URL in Settings → Agent.`);
  }
  return result;
}

/** Whether a provider keeps everything on this computer (CLI agents may still call their own service). */
export function providerIsLocal(provider: { kind?: string; baseUrl?: string } | null | undefined): boolean {
  if (provider?.kind !== "http") return false;
  try { return ["localhost", "127.0.0.1", "[::1]", "::1"].includes(new URL(String(provider.baseUrl)).hostname); }
  catch (_) { return false; }
}

export function elapsedText(ms: number): string {
  const seconds = Math.max(0, Math.floor(Number(ms) / 1000) || 0);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

export function linesList(value: string): string[] { return String(value || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean); }

/** Glob patterns one per line; commas outside {a,b} braces also separate them. Trimmed and de-duplicated in order. */
export function patternList(value: string | string[]): string[] {
  const parts: string[] = [];
  for (const item of Array.isArray(value) ? value : String(value || "").split(/\r?\n/)) {
    let depth = 0, current = "";
    for (const char of String(item)) {
      if (char === "{") depth++;
      else if (char === "}") depth = Math.max(0, depth - 1);
      if (char === "," && !depth) { parts.push(current); current = ""; } else current += char;
    }
    parts.push(current);
  }
  return [...new Set(parts.map(item => item.trim()).filter(Boolean))];
}

/** A folder name as a readable title: underscores become spaces ("Regolith_Shielding_Paper" → "Regolith Shielding Paper"). */
export function folderTitle(value: string): string {
  const name = String(value || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || String(value || "");
  return name.replace(/_+/g, " ").replace(/\s+/g, " ").trim() || name;
}

/* ---------- Sub-project discovery ---------- */

export interface DiscoveryCandidate { profileId: string; title: string; projectType: string; confidence: number; subprojectRole: "standalone" | "part"; sourceKind: string | null; origin: "builtin" | "user"; engine: Engine }
export interface Suggestion {
  id: string; projectId: string; parentId: string; projectTitle: string; folder: string; relative: string; rootName: string;
  autoAccept: boolean; suggestedTitle: string; suggestedId: string; bestCandidate: DiscoveryCandidate | null;
}

/** Folder-name globs, one per line or comma-separated, trimmed and de-duplicated (the CLI validates them again). */
export function ignoreGlobs(value: string | string[]): string[] {
  return [...new Set(patternList(value).map(item => item.replace(/^[\\/]+|[\\/]+$/g, "")).filter(Boolean))];
}

/** A `discover` suggestion as the app keeps it, or null when it is malformed (it is then not shown). */
export function normalizeSuggestion(item: unknown): Suggestion | null {
  const value = item as Record<string, unknown> | null;
  if (!value || typeof value !== "object" || typeof value.id !== "string" || !isSafeId(value.projectId)) return null;
  const folder = typeof value.folder === "string" ? value.folder : "";
  const relative = typeof value.relative === "string" ? value.relative : "";
  if (!/^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(folder) || !relative || /[\\/]/.test(relative) || relative === "..") return null;
  const raw = value.bestCandidate as Record<string, unknown> | null;
  const best = raw && typeof raw === "object" && isSafeId(raw.profileId) ? raw : null;
  const confidence = Number(best?.confidence);
  return {
    id: value.id, projectId: value.projectId, parentId: isSafeId(value.parentId) ? value.parentId : value.projectId,
    projectTitle: typeof value.projectTitle === "string" ? value.projectTitle : value.projectId, folder, relative,
    rootName: typeof value.rootName === "string" ? value.rootName : "", autoAccept: value.autoAccept === true,
    suggestedTitle: typeof value.suggestedTitle === "string" && value.suggestedTitle.trim() ? value.suggestedTitle.trim() : relative,
    suggestedId: isSafeId(value.suggestedId) ? value.suggestedId : slugId(relative),
    bestCandidate: best ? {
      profileId: String(best.profileId), title: String(best.title || best.profileId),
      projectType: Object.hasOwn(PROJECT_TYPES, String(best.projectType)) ? String(best.projectType) : "other",
      confidence: Number.isFinite(confidence) ? Math.max(0, Math.min(1, confidence)) : 0,
      subprojectRole: best.subprojectRole === "part" ? "part" : "standalone", sourceKind: typeof best.sourceKind === "string" ? best.sourceKind : null,
      origin: best.origin === "user" ? "user" : "builtin", engine: engineOf(best.engine),
    } : null,
  };
}
