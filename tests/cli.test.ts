import { describe, expect, it } from "vitest";
import { bestOption, connectRequest, parseCliResult, planOptions, previewRows, staleAfter, templatesForType } from "../src/model/cli";

const out = (code: number | null, stdout: string, stderr = "") => ({ code, stdout, stderr, timedOut: false, truncated: false });

describe("parseCliResult", () => {
  it("parses success and keeps connect results whose first collection failed", () => {
    expect(parseCliResult("projects", out(0, '{"projects": []}'))).toEqual({ projects: [] });
    expect(parseCliResult<{ projectId: string }>("connect", out(1, '{"projectId": "p", "collectionHealth": "error"}')).projectId).toBe("p");
    expect(() => parseCliResult("create-project", out(2, "", "ValueError: bad id"))).toThrow("ValueError: bad id");
    expect(() => parseCliResult("collect", { ...out(null, ""), timedOut: true })).toThrow(/timed out/);
    expect(() => parseCliResult("info", out(0, "not json"))).toThrow(/unreadable/);
  });
});

describe("plan card helpers", () => {
  const inspection = {
    sourcePath: "C:/paper",
    profiles: [{ id: "latex-main", kind: "latex", label: "LaTeX · main.tex", adapter: { mainFile: "main.tex", chapters: ["main.tex"], logPatterns: ["main.log"] }, suggestedId: "paper-latex", suggestedTitle: "paper · LaTeX" }],
    candidates: [
      { profileId: "latex-paper", title: "LaTeX paper", projectType: "paper", sourceKind: "latex", engine: "adapter", origin: "builtin", confidence: 0.95, threshold: 0.8, meetsThreshold: true, matched: ["allOf: files matching *.tex — 1 file(s)"], inspectedProfileIds: ["latex-main"], milestoneTemplate: "paper-section-draft-complete", suggestedId: "paper-latex", suggestedTitle: "paper · LaTeX" },
      { profileId: "matlab-sweep", title: "Sweep", projectType: "simulation", engine: "declarative", origin: "user", confidence: 0.4, threshold: 0.7, meetsThreshold: false, inspectedProfileIds: [] },
    ],
  };
  it("builds options and preselects the best match", () => {
    const options = planOptions(inspection);
    expect(options.map(option => option.profileId)).toEqual(["latex-paper", "matlab-sweep"]);
    expect(options[0].entries).toHaveLength(1);
    expect(bestOption(options)?.profileId).toBe("latex-paper");
  });
  it("produces connect argv with adapter stdin, type, parent and template", () => {
    const [option] = planOptions(inspection);
    const request = connectRequest({ source: "C:/paper", id: "paper-latex", title: "Paper", interval: 60, projectType: "paper", parentId: "thesis", milestoneTemplate: "paper-section-draft-complete" }, { option });
    expect(request.args).toEqual(["--source", "C:/paper", "--id", "paper-latex", "--title", "Paper", "--profile", "latex-paper", "--adapter-stdin",
      "--interval", "60", "--stale-after", "180", "--type", "paper", "--parent", "thesis", "--milestone-template", "paper-section-draft-complete"]);
    expect(JSON.parse(request.input!)).toEqual(inspection.profiles[0].adapter);
    const attach = connectRequest({ source: "C:/paper", id: "x", title: "X", interval: 3600, dashboardProjectId: "thesis", projectType: "paper" }, { option });
    expect(attach.args).toContain("--dashboard-project");
    expect(attach.args).not.toContain("--type");
    expect(staleAfter(3600)).toBe(10800);
  });
  it("falls back to legacy options for older inspect output", () => {
    const options = planOptions({ sourcePath: "x", profiles: inspection.profiles });
    expect(options[0].mode).toBe("legacy");
    expect(connectRequest({ source: "x", id: "a", title: "A", interval: 60 }, { option: options[0] }).args).toContain("--kind");
  });
  it("shows only measured preview values and filters templates by type", () => {
    const rows = previewRows({ runtime: { status: "running", currentValue: 1240, targetValue: 5000, unit: "steps", progress: 0.248 }, cases: { planned: null, completed: null }, artifactCount: 0 });
    expect(rows.stats).toEqual([{ label: "Status", value: "Running" }, { label: "Progress", value: "1,240 / 5,000 steps" }, { label: "Artifacts", value: "0" }]);
    expect(rows.progress).toBe(0.248);
    expect(templatesForType([{ id: "a", title: "A", appliesTo: ["paper"] }, { id: "b", title: "B" }], "simulation").map(item => item.id)).toEqual(["b"]);
  });
});
