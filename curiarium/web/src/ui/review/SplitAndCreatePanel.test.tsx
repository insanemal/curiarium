// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";

// The panel imports the domain module directly, so the mock lives there. The
// original is spread back so every other export keeps working.
const operationsApi = vi.hoisted(() => ({
  planOperation: vi.fn(),
  submitOperation: vi.fn(),
  getOperation: vi.fn(),
  undoOperation: vi.fn(),
  listOperations: vi.fn(),
}));
vi.mock("../../lib/api/operations", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/operations")>()),
  planOperation: operationsApi.planOperation,
  submitOperation: operationsApi.submitOperation,
  getOperation: operationsApi.getOperation,
  undoOperation: operationsApi.undoOperation,
  listOperations: operationsApi.listOperations,
}));

import {
  getOperation,
  listOperations,
  planOperation,
  submitOperation,
  undoOperation,
  type Operation,
  type OperationPlan,
  type OperationReview,
  type OperationStep,
  type OperationUndo,
} from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";
import { getToasts, resetToasts } from "../../lib/toast";
import Toaster from "../Toaster";
import SplitAndCreatePanel from "./SplitAndCreatePanel";

const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);
const undoOperationMock = vi.mocked(undoOperation);
const listOperationsMock = vi.mocked(listOperations);

/** The server's refusal, whole: a sentence naming the shape and the folder, so a
 *  test that paraphrased it would be caught by the byte comparison. */
const REFUSAL =
  "this shape would change nothing: 'import/X' has no top folder holding models to hoist; pick another shape or create the project by hand";

/** A split of one container into three projects: three hoists, three projects,
 *  and one file the server leaves out with its own reason. */
function makeReview(overrides: Partial<OperationReview> = {}): OperationReview {
  return {
    workflow: "split-and-create",
    target: "Container",
    moves: 3,
    creates: 3,
    adopts: 0,
    deletes: 0,
    members: 27,
    groups: [],
    cover: null,
    excluded: [{ path: "Container/notes.psd", reason: "does not hold a model" }],
    existing_project_id: null,
    existing_project_name: null,
    move_list: [
      { source: "Container/Alpha", target: "Alpha", kind: "dir", action: "hoist" },
      { source: "Container/Beta", target: "Beta", kind: "dir", action: "hoist" },
      { source: "Container/Gamma", target: "Gamma", kind: "dir", action: "hoist" },
    ],
    projects: [
      { name: "Alpha", root: "Alpha", members: 9 },
      { name: "Beta", root: "Beta", members: 9 },
      { name: "Gamma", root: "Gamma", members: 9 },
    ],
    ...overrides,
  };
}

function makePlan(overrides: Partial<OperationPlan> = {}): OperationPlan {
  return {
    plan_id: "plan-1",
    workflow: "split-and-create",
    target: "Container",
    steps: [],
    review: makeReview(),
    ...overrides,
  };
}

function makeStep(overrides: Partial<OperationStep> = {}): OperationStep {
  return {
    index: 0,
    op: "move",
    status: "pending",
    result: null,
    error: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function makeOperation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: "op-7",
    workflow: "split-and-create",
    target: "Container",
    status: "queued",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
    error: null,
    undo_state: "unavailable",
    superseded_by: null,
    steps: [],
    ...overrides,
  };
}

/** The seven steps a split of three projects runs: one move, three creates and
 *  a membership check per project. */
const SEVEN_STEPS: OperationStep[] = [
  makeStep({ index: 0, op: "move", status: "done" }),
  makeStep({ index: 1, op: "create-project", status: "done", result: { created: true } }),
  makeStep({ index: 2, op: "create-project", status: "done", result: { created: true } }),
  makeStep({ index: 3, op: "create-project", status: "done", result: { created: true } }),
  makeStep({ index: 4, op: "verify-members", status: "done" }),
  makeStep({ index: 5, op: "verify-members", status: "done" }),
  makeStep({ index: 6, op: "verify-members", status: "running" }),
];

function makeUndone(overrides: Partial<OperationUndo> = {}): OperationUndo {
  return {
    operation_id: "op-7",
    undone: true,
    project_id: null,
    project_name: null,
    moves: 3,
    projects_removed: ["Alpha", "Beta", "Gamma"],
    ...overrides,
  };
}

function renderPanel(
  props: Partial<ComponentProps<typeof SplitAndCreatePanel>> = {},
): ReturnType<typeof render> {
  return render(
    <>
      <SplitAndCreatePanel bundleKey="Container" shape="split-top-folders" canContribute {...props} />
      <Toaster />
    </>,
  );
}

/** The shared review surface, named by the ONE vocabulary: the group's name is
 *  the workflow's label plus "review", not a door-private "preview". */
function planPanel(): HTMLElement {
  return screen.getByRole("group", { name: "Split into projects review" });
}

function actionButton(): HTMLButtonElement {
  return document.querySelector("[data-split-action-button]") as HTMLButtonElement;
}

function undoButton(): HTMLButtonElement | null {
  return document.querySelector("[data-split-undo]");
}

function stepLines(): HTMLElement[] {
  return Array.from(document.querySelectorAll("[data-split-step]"));
}

/** Settle the plan the panel asks for on mount. */
async function settlePlan(): Promise<void> {
  await waitFor(() => expect(planPanel()).toBeTruthy());
}

beforeEach(() => {
  vi.clearAllMocks();
  resetToasts();
  // The panel's in-flight lookup is a READ on mount; nothing is running for this
  // target unless a test says so.
  listOperationsMock.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("SplitAndCreatePanel", () => {
  it("renders nothing and asks for nothing for a viewer", async () => {
    // Catches a viewer gate that renders an empty wrapper (which still claims a
    // layout track) or that plans anyway: a READ-only role must cause neither a
    // pixel nor a request.
    const { container } = renderPanel({ canContribute: false });

    expect(container.firstChild).toBeNull();
    await act(async () => {});
    expect(planOperationMock).not.toHaveBeenCalled();
  });

  it("plans on mount and renders 3 moves inline, the projects, and what it leaves out", async () => {
    // Catches a plan rendered as a ladder of steps to take, and a pane that
    // rebuilt the moves instead of reading the wire array: the operator must see
    // exactly the three hoists the server named, in order, with the server's own
    // exclusion reason beside its path.
    planOperationMock.mockResolvedValue(makePlan());
    renderPanel();
    await settlePlan();

    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "split-and-create",
      target: "Container",
      params: { shape: "split-top-folders" },
    });

    const moves = Array.from(planPanel().querySelectorAll("[data-split-move]"));
    expect(moves).toHaveLength(3);
    expect(moves.map(node => node.textContent)).toEqual([
      'move "Container/Alpha" -> "Alpha" hoist',
      'move "Container/Beta" -> "Beta" hoist',
      'move "Container/Gamma" -> "Gamma" hoist',
    ]);
    expect(moves.map(node => node.getAttribute("data-split-action"))).toEqual([
      "hoist",
      "hoist",
      "hoist",
    ]);

    const projects = Array.from(planPanel().querySelectorAll("[data-split-project]"));
    expect(projects).toHaveLength(3);
    expect(projects[0].textContent).toBe("Alpha from 9 files");
    expect(projects[0].getAttribute("data-split-root")).toBe("Alpha");

    const excluded = Array.from(planPanel().querySelectorAll("[data-split-excluded]"));
    expect(excluded).toHaveLength(1);
    expect(excluded[0].textContent).toBe("Container/notes.psd: does not hold a model");
  });

  it("offers exactly one action, labelled with the outcome", async () => {
    // Catches a preview step the operator has to take first, or a second Apply:
    // one action named for what the plan produces, and no "Preview"/"Apply"
    // anywhere - the moves above it are information, not a gate.
    planOperationMock.mockResolvedValue(makePlan());
    renderPanel();
    await settlePlan();

    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].textContent).toBe("Split into 3 projects");
    expect(screen.queryByRole("button", { name: /preview/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /apply/i })).toBeNull();
  });

  it("submits the plan id with exactly one idempotency key, minted once", async () => {
    // Catches a key minted per click (or per render): a double-click would then
    // queue two splits of the same paths instead of replaying one intent. The
    // key is also NOT the plan id - the two say different things.
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockReturnValue(new Promise<Operation>(() => {}));
    renderPanel();
    await settlePlan();

    // Both clicks land before React flushes the disabled state, which is exactly
    // what a fast double-click does.
    act(() => {
      fireEvent.click(actionButton());
      fireEvent.click(actionButton());
    });

    const calls = submitOperationMock.mock.calls;
    expect(calls).toHaveLength(2);
    const keys = calls.map(([input]) => input.idempotencyKey);
    expect(keys[0]).toEqual(expect.any(String));
    expect(keys[0].length).toBeGreaterThan(0);
    expect(keys[0]).not.toBe("plan-1");
    expect(new Set(keys).size).toBe(1);
    expect(calls[0][0].planId).toBe("plan-1");
  });

  it("narrates the steps in order in a dialog, then names the result", async () => {
    // Catches a progress list rendered in payload order (the runner's index is
    // the order) and a result line that never says how many projects landed: the
    // popup is the only place the operator learns the split finished.
    vi.useFakeTimers();
    const onChanged = vi.fn();
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(makeOperation({ status: "queued" }));
    getOperationMock
      .mockResolvedValueOnce(makeOperation({ status: "running", steps: [...SEVEN_STEPS].reverse() }))
      .mockResolvedValueOnce(makeOperation({ status: "done", steps: SEVEN_STEPS }));

    renderPanel({ onChanged });
    await act(async () => {});
    await act(async () => {
      fireEvent.click(actionButton());
    });

    expect(screen.getByRole("dialog", { name: "Split progress" })).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(getOperationMock).toHaveBeenCalledWith("op-7");
    expect(stepLines()).toHaveLength(7);
    expect(stepLines().map(node => node.getAttribute("data-split-op"))).toEqual([
      "move",
      "create-project",
      "create-project",
      "create-project",
      "verify-members",
      "verify-members",
      "verify-members",
    ]);
    expect(stepLines()[0].textContent).toBe("moved the folders");
    expect(stepLines()[0].getAttribute("data-split-status")).toBe("done");
    expect(stepLines()[6].textContent).toBe("checking the project's files\u2026");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(document.querySelector("[data-split-result]")?.textContent).toBe("Done: 3 projects");
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("offers the undo while the operation can still be undone, and drops it after", async () => {
    // Catches an undo that outlives its answer: once the row is undone the button
    // is a lie, and the host keeps a report of projects that no longer exist.
    const onChanged = vi.fn();
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(
      makeOperation({ status: "done", steps: SEVEN_STEPS, undo_state: "available" }),
    );
    undoOperationMock.mockResolvedValue(makeUndone());
    renderPanel({ onChanged });
    await settlePlan();

    await act(async () => {
      fireEvent.click(actionButton());
    });
    expect(undoButton()).not.toBeNull();
    expect(undoButton()?.textContent).toBe("Undo this split");

    await act(async () => {
      fireEvent.click(undoButton() as HTMLButtonElement);
    });

    expect(undoOperationMock).toHaveBeenCalledWith("op-7");
    expect(undoButton()).toBeNull();
    expect(getToasts().some(toast => toast.message === "Split undone")).toBe(true);
    expect(onChanged).toHaveBeenCalled();
  });

  it("shows the server's refusal verbatim and offers no action", async () => {
    // Catches a sentence rebuilt from a status code, and a live action under it:
    // the client decided no refusal, so the server's own words stand alone and
    // nothing may look like a working button.
    planOperationMock.mockRejectedValue(new Error(REFUSAL));
    renderPanel();

    const alert = await screen.findByRole("alert");
    // The failure handle is now the shared panel's own: one plan-read failure
    // tag for every door (`data-<prefix>-plan-failure`), not a door-private one.
    expect(alert.getAttribute("data-split-plan-failure")).toBe("true");
    expect(alert.textContent).toBe(REFUSAL);
    expect(document.querySelector("[data-split-action-button]")).toBeNull();
  });

  it("hands a 401 to the host and writes no sentence of its own", async () => {
    // Catches a local catch that turns a 401 into "Failed to plan the split.": a
    // signed-out operator would read a generic failure and stay on a session that
    // is already gone, with no way back.
    const onUnauthorized = vi.fn();
    planOperationMock.mockRejectedValue(new UnauthorizedError());
    renderPanel({ onUnauthorized });

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the undo button mounted under a refusal so the server's sentence stands where the button was", async () => {
    // Catches a dead undo: a button that swallows "the operation has already been
    // undone" leaves the operator pressing something that answers nothing.
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(
      makeOperation({ status: "done", steps: SEVEN_STEPS, undo_state: "available" }),
    );
    undoOperationMock.mockRejectedValue(new Error("the operation has already been undone"));
    renderPanel();
    await settlePlan();
    await act(async () => {
      fireEvent.click(actionButton());
    });

    await act(async () => {
      fireEvent.click(undoButton() as HTMLButtonElement);
    });

    const alert = await screen.findByRole("alert");
    expect(alert.getAttribute("data-split-undo-failure")).toBe("true");
    expect(alert.textContent).toBe("the operation has already been undone");
    expect(undoButton()).not.toBeNull();
  });
});
