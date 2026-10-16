// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { buildModel } from "../src/model/load";
import { mountView, type ActionBridge, type Mode } from "../src/view/view";
import { snapshot } from "./fixtures";

function render(mode: Mode, projectId: string | null = null, bridge: Partial<ActionBridge> = {}) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const navigate = vi.fn();
  const handle = mountView(container, { mode, projectId }, {
    load: async () => buildModel(snapshot()),
    bridge: { getStatus: () => ({ running: false, busy: false }), ...bridge },
    navigate, openExternal: vi.fn(), openWorkspaceFile: vi.fn(), subscribe: () => () => {},
  });
  return { container, handle, navigate };
}

describe("ported view", () => {
  it("renders Overall stats, capsules and the per-project average ring", async () => {
    const { container, handle } = render("overview");
    await handle.painted;
    const kpis = Array.from(container.querySelectorAll(".rd-kpi")).map(node => node.querySelector(".rd-kpi-value")!.textContent);
    expect(kpis).toEqual(["1", "1", "1", "3"]);
    /* Two top-level columns: the thesis family (2 own + 3 paper milestones) and the simulation. */
    expect(Array.from(container.querySelectorAll(".rd-capsule-label")).map(node => node.textContent)).toEqual(["PhD thesis", "Impact runs"]);
    expect(Array.from(container.querySelectorAll(".rd-capsule-value")).map(node => node.textContent)).toEqual(["40%", "100%"]);
    /* Average by project, not pooled: (2/5 + 1/1) / 2 = 70%. */
    expect(container.querySelector(".rd-donut-value")!.textContent).toBe("70%");
    handle.dispose();
  });

  it("nests sub-projects under their parent and filters by type", async () => {
    const { container, handle, navigate } = render("projects", null, { setParent: vi.fn() });
    await handle.painted;
    const family = container.querySelector(".rd-project-family")!;
    expect(family.querySelector(".rd-project-row.is-parent")!.getAttribute("data-project-id")).toBe("thesis");
    expect(family.querySelector(".rd-project-row.is-child")!.getAttribute("data-project-id")).toBe("paper");
    expect(Array.from(container.querySelectorAll(".rd-type-tab")).map(tab => tab.getAttribute("data-type"))).toEqual(["all", "thesis", "paper", "simulation"]);
    (container.querySelector('.rd-project-title a[data-href="rd://project/sim"]') as HTMLElement).click();
    expect(navigate).toHaveBeenCalledWith("rd://project/sim", false);
    handle.dispose();
  });

  it("lists alerts: blocked project, overdue milestone", async () => {
    const { container, handle } = render("attention");
    await handle.painted;
    const labels = Array.from(container.querySelectorAll(".rd-alert .rd-status")).map(node => node.textContent);
    expect(labels).toContain("Project blocked");
    expect(labels).toContain("Milestone overdue");
    handle.dispose();
  });

  it("shows the paper workspace and checks a milestone through the bridge", async () => {
    const setMilestone = vi.fn().mockResolvedValue(undefined);
    const { container, handle } = render("project-overview", "paper", { setMilestone });
    await handle.painted;
    expect(container.querySelector("h1.rd-page-title")!.textContent).toBe("Framework paper");
    expect(container.querySelector(".rd-project-breadcrumb a")!.textContent).toBe("PhD thesis");
    expect(Array.from(container.querySelectorAll(".rd-paper-root-branches .rd-paper-node-title")).map(node => node.textContent)).toEqual(["Introduction", "Method"]);
    handle.dispose();
    const milestones = render("milestones", "paper", { setMilestone });
    await milestones.handle.painted;
    const boxes = milestones.container.querySelectorAll<HTMLInputElement>(".rd-paper-task input[type=checkbox]");
    expect(boxes).toHaveLength(3);
    boxes[1].checked = true;
    boxes[1].dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(setMilestone).toHaveBeenCalled());
    expect(setMilestone.mock.calls[0][1]).toMatchObject({ line: 3, completed: true, sourceId: "paper-tex", chapterId: "method", sourceHash: "b".repeat(64) });
    milestones.handle.dispose();
  });

  it("renders the mini widget sections", async () => {
    const { container, handle } = render("mini", null, { openProject: vi.fn(), start: vi.fn() });
    await handle.painted;
    expect(container.querySelector(".rd-mini-ring-value")!.textContent).toBe("70%");
    expect(Array.from(container.querySelectorAll(".rd-mini-section-title")).map(node => node.textContent)).toEqual(["Projects", "Today", "Next up", "Needs attention"]);
    expect(container.querySelector(".rd-mini-writing-value")!.textContent).toBe("+100");
    handle.dispose();
  });
});
