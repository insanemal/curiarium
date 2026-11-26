// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";

// The panel imports the domain module directly, so the mock lives there. The
// original is spread back so every other export keeps working.
const arrangeApi = vi.hoisted(() => ({
  getArrangePlan: vi.fn(),
  arrangeBundles: vi.fn(),
  undoArrange: vi.fn(),
}));
vi.mock("../../lib/api/bundles", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/bundles")>()),
  getArrangePlan: arrangeApi.getArrangePlan,
  arrangeBundles: arrangeApi.arrangeBundles,
  undoArrange: arrangeApi.undoArrange,
}));

import {
  arrangeBundles,
  getArrangePlan,
  undoArrange,
  type BundleArrangeAdoptions,
  type BundleArrangeApplied,
  type BundleArrangePlan,
  type BundleArrangeUndo,
} from "../../lib/api/bundles";
import { getToasts, resetToasts } from "../../lib/toast";
import { UnauthorizedError } from "../../lib/api/http";
import Toaster from "../Toaster";
import ArrangePlanPanel from "./ArrangePlanPanel";

const getArrangePlanMock = vi.mocked(getArrangePlan);
const arrangeBundlesMock = vi.mocked(arrangeBundles);
const undoArrangeMock = vi.mocked(undoArrange);

const STALE_SENTENCE = "the tree changed since this plan was previewed; preview it again";

function makePlan(overrides: Partial<BundleArrangePlan> = {}): BundleArrangePlan {
  return {
    key: "Container",
    shape: "split-top-folders",
    plan_id: "plan-1",
    moves: [
      { from: "Container/Alpha", to: "Alpha", kind: "dir", action: "hoist" },
      { from: "Container/Beta", to: "Beta", kind: "dir", action: "move" },
    ],
    text: ['move "Container/Alpha" -> "Alpha"', 'move "Container/Beta" -> "Beta"'],
    creates: [{ path: "Alpha", exists: false }],
    refusals: [],
    report: { creatable: false, blocked_reason: "needs_arrange" },
    ...overrides,
  };
}

function makeApplied(overrides: Partial<BundleArrangeApplied> = {}): BundleArrangeApplied {
  return {
    key: "Container",
    shape: "split-top-folders",
    plan_id: "plan-1",
    arrange_id: "arrange-7",
    project_id: "p1",
    applied: true,
    moves: 2,
    already_applied: false,
    unseeable: [],
    stale: [],
    ...overrides,
  };
}

function makeUndone(overrides: Partial<BundleArrangeUndo> = {}): BundleArrangeUndo {
  return {
    arrange_id: "arrange-7",
    key: "Container",
    undone: true,
    moves: 2,
    project_removed: true,
    unseeable: [],
    stale: [],
    ...overrides,
  };
}

function renderPanel(
  props: Partial<ComponentProps<typeof ArrangePlanPanel>> = {},
): ReturnType<typeof render> {
  return render(
    <>
      <ArrangePlanPanel
        bundleKey="Container"
        shape="split-top-folders"
        canContribute
        {...props}
      />
      <Toaster />
    </>,
  );
}

function previewButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Preview the moves" }) as HTMLButtonElement;
}

function applyButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Apply this arrangement" }) as HTMLButtonElement;
}

function undoButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Undo this arrange" }) as HTMLButtonElement;
}

function planPanel(): HTMLElement {
  return screen.getByRole("group", { name: "Arrangement preview" });
}

/** The adoptions block a zero-move plan carries: 171 memberships in 9 folders,
 *  two of them new. This is the whole outcome of the grow-door's plan. */
function makeAdoptions(overrides: Partial<BundleArrangeAdoptions> = {}): BundleArrangeAdoptions {
  return {
    target: { root: "import/Stagetop", project_id: "p1", creates_project: false },
    total: 171,
    by_role: { model: 102, image: 27, text: 18, archive: 12, other: 12 },
    groups: Array.from({ length: 9 }, (_unused, index) => ({
      name: index === 0 ? "Feet 5414731" : `part ${index}`,
      root: "import/Stagetop",
      files: index === 0 ? 23 : 17,
      new: index === 0,
      foreign: index === 0,
      separable: index === 0,
      sample_names: index === 0 ? ["part.stl", "bracket.stl"] : ["part.stl"],
    })),
    sample_names: ["part.stl", "bracket.stl"],
    unregistered: 0,
    ...overrides,
  };
}

/** The zero-move adopt itself: no moves, no creates, no objection, and a preview
 *  whose only facts are the memberships. */
function adoptPlan(overrides: Partial<BundleArrangePlan> = {}): BundleArrangePlan {
  return makePlan({
    moves: [],
    text: [],
    creates: [],
    adoptions: makeAdoptions(),
    ...overrides,
  });
}

/** Preview and settle, the first two steps every write test needs. */
async function preview(): Promise<void> {
  fireEvent.click(previewButton());
  await waitFor(() => expect(planPanel()).toBeTruthy());
}

beforeEach(() => {
  vi.clearAllMocks();
  resetToasts();
});

afterEach(() => {
  cleanup();
});

describe("ArrangePlanPanel", () => {
  it("renders nothing and asks for nothing for a viewer", () => {
    // Catches a viewer gate that renders an empty wrapper (which still claims a
    // layout track) or that fires the preview on mount: a READ-only role must
    // cause neither a pixel nor a disk walk.
    const { container } = renderPanel({ canContribute: false });

    expect(container.firstChild).toBeNull();
    expect(getArrangePlanMock).not.toHaveBeenCalled();
  });

  it("asks for the plan only on the click, for its own key and shape", async () => {
    // Catches a mount-time preview: the GET walks the disk, so the click is the
    // one trigger - and the request must carry the props, not a guessed default.
    getArrangePlanMock.mockResolvedValue(makePlan());
    renderPanel();

    expect(getArrangePlanMock).not.toHaveBeenCalled();
    await preview();
    expect(getArrangePlanMock).toHaveBeenCalledWith("Container", "split-top-folders");
  });

  it("renders the move rows from the wire array, never from the prose", async () => {
    // G1: the engine's `text` and its `moves` can disagree. A pane that echoed
    // the prose would show the wrong file list - and a move the array does not
    // name would be approved without ever being rendered. The rows must come
    // from `moves`; the prose is only a second, verbatim record.
    getArrangePlanMock.mockResolvedValue(
      makePlan({ text: ['the prose insists: move "Container/Gamma" -> "Gamma"'] }),
    );
    renderPanel();
    await preview();

    const panel = planPanel();
    const rows = Array.from(panel.querySelectorAll("[data-arrange-move]"));
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('move "Container/Alpha" -> "Alpha"');
    expect(rows[0].textContent).toContain("hoist");
    expect(rows[1].textContent).toContain('move "Container/Beta" -> "Beta"');
    expect(rows[1].textContent).toContain("move");
    expect(rows[0].getAttribute("data-arrange-action")).toBe("hoist");
    expect(rows[1].getAttribute("data-arrange-action")).toBe("move");
    // Gamma exists only in the prose, so it must never gain a move row.
    expect(rows.some(row => row.textContent?.includes("Gamma"))).toBe(false);

    const text = Array.from(panel.querySelectorAll("[data-arrange-text]")).map(
      node => node.textContent,
    );
    expect(text).toEqual(['the prose insists: move "Container/Gamma" -> "Gamma"']);
  });

  it("counts through the one formatter, with an en dash for an absent list", async () => {
    // Catches a `moves.length` read asserted as an array (`?? 0` would fabricate
    // a zero, and `undefined.length` would throw): an older payload's absent list
    // is unknown, not empty.
    const plan = makePlan();
    delete (plan as Partial<BundleArrangePlan>).moves;
    getArrangePlanMock.mockResolvedValue(plan);
    renderPanel();
    await preview();

    expect(planPanel().querySelector("[data-arrange-moves]")?.textContent).toBe(
      "Arrangement: \u2013 moves",
    );
    expect(planPanel().querySelectorAll("[data-arrange-move]")).toHaveLength(0);
  });

  it("applies the served plan's id and shape, and refuses while an objection stands", async () => {
    // Catches an Apply that sends the row key alone, or the prop shape instead of
    // the plan's own: the server echoes `plan_id` against the tree it measured.
    getArrangePlanMock.mockResolvedValue(
      makePlan({ key: "ServedKey", shape: "manual", plan_id: "plan-42" }),
    );
    arrangeBundlesMock.mockResolvedValue(makeApplied({ moves: 2 }));
    renderPanel();
    await preview();

    fireEvent.click(applyButton());

    await waitFor(() =>
      expect(arrangeBundlesMock).toHaveBeenCalledWith({
        key: "Container",
        shape: "manual",
        planId: "plan-42",
      }),
    );
  });

  it("keeps Apply enabled for a grown project's zero-move plan with no refusal", async () => {
    // The old `arrangeAppliesNothing` bug: a plan that legitimately has nothing
    // to move (the folder already matches its shape) is still applicable - its
    // work is the memberships it would add - and a disabled button would strand
    // the operator.
    getArrangePlanMock.mockResolvedValue(adoptPlan());
    renderPanel();
    await preview();

    expect(applyButton().disabled).toBe(false);
  });

  it("names the memberships a zero-move adoption plan would add, and keeps Apply workable", async () => {
    // The whole point of the grow door: 171 files join the project with no move
    // at all. A `0 moves` badge beside a live Apply is the operator approving a
    // blank plan - and a disabled Apply would strand the one case this door was
    // built for.
    getArrangePlanMock.mockResolvedValue(adoptPlan());
    renderPanel();
    await preview();

    const badge = planPanel().querySelector("[data-arrange-moves]");
    expect(badge?.textContent).toBe("Adds 171 files in 9 folders");
    expect(badge?.getAttribute("data-arrange-applies-nothing")).toBeNull();
    expect(applyButton().disabled).toBe(false);

    // The role split is the panel's own sentence: the badge already said the
    // total, so this one says what the files ARE.
    expect(planPanel().querySelector("[data-arrange-adopt-roles]")?.textContent).toBe(
      "Models 102 \u00b7 Images 27 \u00b7 Documents 18 \u00b7 Archives 12 \u00b7 Other 12",
    );

    const groups = Array.from(planPanel().querySelectorAll("[data-arrange-adopt-group]"));
    expect(groups).toHaveLength(9);
    expect(groups[0].textContent).toContain("Feet 5414731");
    expect(groups[0].textContent).toContain("\u2014 23 files");
    expect(groups[0].textContent).toContain("new folder");
    expect(groups[0].textContent).toContain("part.stl, bracket.stl");
    // Only the group the apply has to create wears that word: the other eight
    // already exist, and "new folder" there would promise a directory nothing
    // creates.
    expect(groups[0].getAttribute("data-arrange-adopt-new")).toBe("true");
    expect(groups.filter(group => group.textContent?.includes("new folder"))).toHaveLength(1);

    expect(planPanel().querySelector("[data-arrange-adopt-sample]")?.textContent).toBe(
      "e.g. part.stl, bracket.stl",
    );
    // A zero is not a warning: an unregistered count of 0 renders nothing.
    expect(planPanel().querySelector("[data-arrange-adopt-unregistered]")).toBeNull();
  });

  it("names the files on disk the library cannot adopt, and asks for the rescan", async () => {
    // Those files cannot join the project by this apply, so the operator has to
    // hear about them before pressing it - otherwise the result is a project
    // smaller than the folder it came from.
    getArrangePlanMock.mockResolvedValue(adoptPlan({ adoptions: makeAdoptions({ unregistered: 6 }) }));
    renderPanel();
    await preview();

    const warning = planPanel().querySelector("[data-arrange-adopt-unregistered]");
    expect(warning?.textContent).toBe(
      "6 files in this folder are on disk but not in the library; rescan before applying",
    );
    expect(warning?.className).toContain("text-red-500");
  });

  it("says a plan that changes nothing applies nothing, and disables Apply for it", async () => {
    // One predicate, two readers: the badge's verdict and the button's state must
    // come from the same answer, or the sentence and the button disagree. The
    // plan here has no move, no create and no adoptions block at all - the shape
    // the server refuses with `nothing_to_arrange`.
    getArrangePlanMock.mockResolvedValue(makePlan({ moves: [], creates: [], text: [] }));
    renderPanel();
    await preview();

    const badge = planPanel().querySelector("[data-arrange-moves]");
    expect(badge?.textContent).toBe("Arrangement: applies nothing");
    expect(badge?.getAttribute("data-arrange-applies-nothing")).toBe("true");
    expect(applyButton().disabled).toBe(true);
  });

  it("disables Apply while the server holds an objection", async () => {
    // Catches an Apply that ignores `refusals`: pressing it would answer 409 and
    // write nothing, so it must not look like a working button.
    getArrangePlanMock.mockResolvedValue(
      makePlan({ refusals: [{ status: 409, reason: "cross_device", detail: "cross-device" }] }),
    );
    renderPanel();
    await preview();

    expect(applyButton().disabled).toBe(true);
  });

  it("shows a stale-plan 409 verbatim and offers the preview again, with no Apply", async () => {
    // G3: the server refuses a plan whose tree moved. The pane must render the
    // server's own sentence (it already says to preview again), drop the dead
    // plan, and offer the only action that can work. A paraphrased sentence or a
    // lingering Apply would both invite the same 409 again.
    getArrangePlanMock.mockResolvedValue(makePlan());
    arrangeBundlesMock.mockRejectedValue(new Error(STALE_SENTENCE));
    renderPanel();
    await preview();

    fireEvent.click(applyButton());

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(STALE_SENTENCE);
    expect(alert.getAttribute("data-arrange-plan-failure")).toBe("true");
    expect(screen.queryByRole("group", { name: "Arrangement preview" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Apply this arrangement" })).toBeNull();
    expect(previewButton()).toBeTruthy();
  });

  it("raises one toast naming the moves that landed, and tells the host", async () => {
    // Catches a toast that lists the previewed moves when the landed count did
    // not match, and a completion signal that never fires: the host would keep a
    // stale report.
    const onChanged = vi.fn();
    getArrangePlanMock.mockResolvedValue(makePlan());
    arrangeBundlesMock.mockResolvedValue(makeApplied());
    renderPanel({ onChanged });
    await preview();

    fireEvent.click(applyButton());

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    const toasts = getToasts();
    expect(toasts).toHaveLength(1);
    expect(toasts[0].message).toBe("Arranged: 2 moves");
    expect(toasts[0].details).toEqual([
      'move "Container/Alpha" -> "Alpha"',
      'move "Container/Beta" -> "Beta"',
    ]);
    expect(screen.queryByRole("group", { name: "Arrangement preview" })).toBeNull();
    expect(document.querySelector("[data-arrange-applied]")?.textContent).toBe("Arranged: 2 moves");
  });

  it("does not list the moves when the landed count disagrees with the preview", async () => {
    // Catches a toast that prints every previewed line under a smaller landed
    // count: it would claim moves that did not happen.
    getArrangePlanMock.mockResolvedValue(makePlan());
    arrangeBundlesMock.mockResolvedValue(makeApplied({ moves: 1 }));
    renderPanel();
    await preview();

    fireEvent.click(applyButton());

    await waitFor(() => expect(getToasts()).toHaveLength(1));
    expect(getToasts()[0].message).toBe("Arranged: 1 move");
    expect(getToasts()[0].details).toBeUndefined();
  });

  it("reports what an apply ADDED, beside the moves it made", async () => {
    // The post-apply half of the grow door: `Arranged: 0 moves` alone reads as if
    // nothing happened, so the result also names the memberships. They count
    // different things - bytes moved and rows joined - and both are true.
    getArrangePlanMock.mockResolvedValue(adoptPlan());
    arrangeBundlesMock.mockResolvedValue(makeApplied({ moves: 0 }));
    renderPanel();
    await preview();

    fireEvent.click(applyButton());

    await waitFor(() => expect(document.querySelector("[data-arrange-added]")).not.toBeNull());
    expect(document.querySelector("[data-arrange-added]")?.textContent).toBe(
      "Added 171 files in 9 folders",
    );
    expect(document.querySelector("[data-arrange-applied]")?.textContent).toBe("Arranged: 0 moves");
  });

  it("undoes by the id the server issued, and shows a second undo's refusal instead of the button", async () => {
    // G4: the ledger is found by `arrange_id`, so a second undo answers "already
    // undone". That sentence must replace the button; leaving the button there
    // would swallow the server's answer and invite the same 409 forever.
    getArrangePlanMock.mockResolvedValue(makePlan());
    arrangeBundlesMock.mockResolvedValue(makeApplied({ arrange_id: "arrange-9" }));
    undoArrangeMock.mockRejectedValue(new Error("this arrangement was already undone"));
    renderPanel();
    await preview();
    fireEvent.click(applyButton());
    await waitFor(() => expect(undoButton()).toBeTruthy());

    fireEvent.click(undoButton());

    const alert = await screen.findByRole("alert");
    expect(alert.getAttribute("data-arrange-undo-failure")).toBe("true");
    expect(alert.textContent).toBe("this arrangement was already undone");
    expect(undoArrangeMock).toHaveBeenCalledWith("arrange-9");
    expect(screen.queryByRole("button", { name: "Undo this arrange" })).toBeNull();
  });

  it("puts the arrangement back and announces the count", async () => {
    // Catches an undo that leaves the applied group on screen: those moves no
    // longer happened, so neither the count nor the undo may survive a success.
    getArrangePlanMock.mockResolvedValue(makePlan());
    arrangeBundlesMock.mockResolvedValue(makeApplied({ arrange_id: "arrange-7" }));
    undoArrangeMock.mockResolvedValue(makeUndone({ moves: 2 }));
    const onChanged = vi.fn();
    renderPanel({ onChanged });
    await preview();
    fireEvent.click(applyButton());
    await waitFor(() => expect(undoButton()).toBeTruthy());

    fireEvent.click(undoButton());

    await waitFor(() => expect(undoArrangeMock).toHaveBeenCalledWith("arrange-7"));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Undo this arrange" })).toBeNull(),
    );
    expect(document.querySelector("[data-arrange-applied]")).toBeNull();
    expect(getToasts().some(toast => toast.message === "Put back: 2 moves")).toBe(true);
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it("hands a 401 to the host and writes no sentence of its own", async () => {
    // Catches the regression this door replaced a hook to avoid: a local catch
    // that turns a 401 into "Failed to preview the arrangement." shows a
    // signed-out operator a generic failure and leaves them on an expired
    // session with no way back. The host's sign-out is the whole answer, so the
    // panel must render neither a failure sentence nor a plan.
    const onUnauthorized = vi.fn();
    getArrangePlanMock.mockRejectedValue(new UnauthorizedError());
    renderPanel({ onUnauthorized });

    fireEvent.click(previewButton());

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("group", { name: "Arrangement preview" })).toBeNull();
    // The click is not left disabled: the panel dies with the session, not
    // stuck mid-flight.
    expect(previewButton().disabled).toBe(false);
  });

  it("renders a refusal as the server's detail, verbatim", async () => {
    // Catches a sentence rebuilt from `status`/`reason`: the detail is the
    // server's own measurement of the tree it walked, and a second explanation
    // would let the two drift.
    getArrangePlanMock.mockResolvedValue(
      makePlan({
        refusals: [
          {
            status: 409,
            reason: "destination_exists",
            detail: "destination exists: Alpha",
          },
        ],
      }),
    );
    renderPanel();
    await preview();

    const refusals = Array.from(planPanel().querySelectorAll("[data-arrange-refusal]"));
    expect(refusals.map(node => node.textContent)).toEqual(["destination exists: Alpha"]);
    expect(refusals[0].className).toContain("review-detail-reason");
  });
});
