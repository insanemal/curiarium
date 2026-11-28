// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// The control imports the domain module directly, so the mock lives there. The
// original is spread back so every other export keeps working.
const operationsApi = vi.hoisted(() => ({
  planOperation: vi.fn(),
  submitOperation: vi.fn(),
  getOperation: vi.fn(),
  undoOperation: vi.fn(),
}));
vi.mock("../../lib/api/operations", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/operations")>()),
  planOperation: operationsApi.planOperation,
  submitOperation: operationsApi.submitOperation,
  getOperation: operationsApi.getOperation,
  undoOperation: operationsApi.undoOperation,
}));

import {
  getOperation,
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
import AdoptGrownParts from "./AdoptGrownParts";

const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);
const undoOperationMock = vi.mocked(undoOperation);

/** The server's refusal, whole: a sentence naming the shape and the folder, so
 *  a test that paraphrased it would be caught by the byte comparison. */
const REFUSAL =
  "this folder has files no scan has registered; run a storage scan, then adopt again";

/** A folder that grew in nine groups: 171 files, every bias a label can have. */
function makeReview(overrides: Partial<OperationReview> = {}): OperationReview {
  return {
    workflow: "adopt",
    target: "p1",
    moves: 0,
    creates: 0,
    adopts: 1,
    deletes: 0,
    members: 171,
    groups: Array.from({ length: 9 }, (_, index) => ({
      name: `Group ${index + 1}`,
      members: 19,
      models: 19,
    })),
    by_role: { model: 171, image: 0, text: 0, archive: 0, other: 0 },
    cover: null,
    excluded: [{ path: "Hydra/notes.psd", reason: "does not hold a model" }],
    existing_project_id: "p1",
    existing_project_name: "Hydra",
    move_list: [],
    projects: [],
    ...overrides,
  };
}

function makePlan(overrides: Partial<OperationPlan> = {}): OperationPlan {
  return {
    plan_id: "plan-adopt-1",
    workflow: "adopt",
    target: "p1",
    steps: [
      { index: 0, op: "adopt", summary: "add 171 files" },
      { index: 1, op: "refresh-groups", summary: "refresh the project's groups" },
      { index: 2, op: "verify-members", summary: "verify 171 members" },
    ],
    review: makeReview(),
    ...overrides,
  };
}

/** A delta whose files all sit at the folder's root: no group to name. */
function makeRootOnlyPlan(): OperationPlan {
  return makePlan({ review: makeReview({ groups: [] }) });
}

function makeStep(overrides: Partial<OperationStep> = {}): OperationStep {
  return {
    index: 0,
    op: "adopt",
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
    id: "op-adopt",
    workflow: "adopt",
    target: "p1",
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

/** The three steps an adopt runs, each finished. */
const THREE_STEPS: OperationStep[] = [
  makeStep({ index: 0, op: "adopt", status: "done" }),
  makeStep({ index: 1, op: "refresh-groups", status: "done" }),
  makeStep({ index: 2, op: "verify-members", status: "done" }),
];

function makeUndone(overrides: Partial<OperationUndo> = {}): OperationUndo {
  return {
    operation_id: "op-adopt",
    undone: true,
    project_id: "p1",
    project_name: "Hydra",
    moves: null,
    projects_removed: null,
    members_removed: 3,
    ...overrides,
  };
}

function renderControl(
  props: Partial<Parameters<typeof AdoptGrownParts>[0]> = {},
): ReturnType<typeof render> {
  return render(
    <>
      <AdoptGrownParts projectId="p1" {...props} />
      <Toaster />
    </>,
  );
}

function planPanel(): HTMLElement {
  return screen.getByRole("group", { name: "Adopt preview" });
}

function actionButton(): HTMLButtonElement {
  return document.querySelector("[data-adopt-action-button]") as HTMLButtonElement;
}

function undoButton(): HTMLButtonElement | null {
  return document.querySelector("[data-adopt-undo]");
}

function stepLines(): HTMLElement[] {
  return Array.from(document.querySelectorAll("[data-adopt-step]"));
}

/** Settle the plan the control asks for on mount. */
async function settlePlan(): Promise<void> {
  await waitFor(() => expect(planPanel()).toBeTruthy());
}

beforeEach(() => {
  vi.clearAllMocks();
  resetToasts();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("AdoptGrownParts", () => {
  it("plans the adopt on mount and shows the role census and the server's exclusions", async () => {
    // Catches a click-time plan (the operator would approve an unread request),
    // a census that prints its zeros, and an exclusion sentence the client
    // paraphrased instead of reading the server's.
    planOperationMock.mockResolvedValue(
      makePlan({
        review: makeReview({
          members: 10,
          groups: [{ name: "Hydra", members: 10, models: 3 }],
          by_role: { model: 3, image: 1, text: 2, archive: 0, other: 4 },
        }),
      }),
    );
    renderControl();
    await settlePlan();

    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "adopt",
      target: "p1",
      params: {},
    });

    const roles = Array.from(planPanel().querySelectorAll<HTMLElement>("[data-adopt-role]"));
    expect(roles.map(node => node.getAttribute("data-adopt-role"))).toEqual([
      "model",
      "image",
      "text",
      "other",
    ]);
    expect(roles.map(node => node.getAttribute("data-adopt-count"))).toEqual(["3", "1", "2", "4"]);
    expect(roles.map(node => node.textContent)).toEqual([
      "3 models",
      "1 image",
      "2 documents",
      "4 others",
    ]);

    const excluded = Array.from(planPanel().querySelectorAll("[data-adopt-excluded]"));
    expect(excluded).toHaveLength(1);
    expect(excluded[0].textContent).toBe("Hydra/notes.psd: does not hold a model");
  });

  it("offers exactly one action, named with the outcome, for a grouped and a root-only delta", async () => {
    // Catches a label named for the subsystem, a group count that includes the
    // folder's own `""` bucket, and a root-only delta promising a group it does
    // not have.
    planOperationMock.mockResolvedValueOnce(makePlan()).mockResolvedValueOnce(makeRootOnlyPlan());

    renderControl();
    await settlePlan();

    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].textContent).toBe("Add 171 files in 9 groups");
    expect(screen.queryByRole("button", { name: /preview/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /apply/i })).toBeNull();

    cleanup();
    planOperationMock.mockResolvedValue(makeRootOnlyPlan());
    renderControl();
    await settlePlan();

    const rootButtons = screen.getAllByRole("button");
    expect(rootButtons).toHaveLength(1);
    expect(rootButtons[0].textContent).toBe("Add 171 files");
  });

  it("submits the plan id with exactly one idempotency key, minted once", async () => {
    // Catches a key minted per click: a double-click would queue two adopts of
    // the same paths instead of replaying one intent. The key is also NOT the
    // plan id - the two say different things.
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockReturnValue(new Promise<Operation>(() => {}));
    renderControl();
    await settlePlan();

    // Both clicks land before React flushes the disabled state, which is what a
    // fast double-click does.
    act(() => {
      fireEvent.click(actionButton());
      fireEvent.click(actionButton());
    });

    const calls = submitOperationMock.mock.calls;
    expect(calls).toHaveLength(2);
    const keys = calls.map(([input]) => input.idempotencyKey);
    expect(keys[0]).toEqual(expect.any(String));
    expect(keys[0].length).toBeGreaterThan(0);
    expect(keys[0]).not.toBe("plan-adopt-1");
    expect(new Set(keys).size).toBe(1);
    expect(calls[0][0].planId).toBe("plan-adopt-1");
  });

  it("narrates the steps in index order in a dialog, then names the plan's own count", async () => {
    // Catches a progress list in payload order (the runner's index is the
    // order), step words taken from the token rather than the door's table, and
    // a result line that never says how many files landed.
    vi.useFakeTimers();
    const onChanged = vi.fn();
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(makeOperation({ status: "queued" }));
    getOperationMock
      .mockResolvedValueOnce(makeOperation({ status: "running", steps: [...THREE_STEPS].reverse() }))
      .mockResolvedValueOnce(makeOperation({ status: "done", steps: THREE_STEPS }));

    renderControl({ onChanged });
    await act(async () => {});
    await act(async () => {
      fireEvent.click(actionButton());
    });

    expect(screen.getByRole("dialog", { name: "Adopt progress" })).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(getOperationMock).toHaveBeenCalledWith("op-adopt");
    expect(stepLines().map(node => node.getAttribute("data-adopt-op"))).toEqual([
      "adopt",
      "refresh-groups",
      "verify-members",
    ]);
    expect(stepLines().map(node => node.textContent)).toEqual([
      "added the files",
      "refreshed the project's groups",
      "checked the project's files",
    ]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(document.querySelector("[data-adopt-result]")?.textContent).toBe("Done: 171 files added");
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("offers the undo while the operation can still be undone, and drops it after", async () => {
    // Catches an undo that outlives its answer: once the row is undone the
    // button is a lie, and the host keeps a report of members that no longer
    // exist.
    const onChanged = vi.fn();
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(
      makeOperation({ status: "done", steps: THREE_STEPS, undo_state: "available" }),
    );
    undoOperationMock.mockResolvedValue(makeUndone());
    renderControl({ onChanged });
    await settlePlan();

    await act(async () => {
      fireEvent.click(actionButton());
    });
    expect(undoButton()).not.toBeNull();
    expect(undoButton()?.textContent).toBe("Undo this adopt");

    await act(async () => {
      fireEvent.click(undoButton() as HTMLButtonElement);
    });

    expect(undoOperationMock).toHaveBeenCalledWith("op-adopt");
    expect(undoButton()).toBeNull();
    expect(getToasts().some(toast => toast.message === "Adopt undone")).toBe(true);
    expect(onChanged).toHaveBeenCalled();
  });

  it("shows the server's refusal verbatim and offers no action", async () => {
    // Catches a sentence rebuilt from a status code, and a live action under it:
    // the client decided no refusal, so the server's own words stand alone.
    planOperationMock.mockRejectedValue(new Error(REFUSAL));
    renderControl();

    const alert = await screen.findByRole("alert");
    expect(alert.getAttribute("data-adopt-failure")).toBe("true");
    expect(alert.textContent).toBe(REFUSAL);
    expect(document.querySelector("[data-adopt-action-button]")).toBeNull();
  });

  it("hands a 401 to the host and writes no sentence of its own", async () => {
    // Catches a local catch that turns a 401 into "Failed to plan the adopt.": a
    // signed-out operator would read a generic failure and stay on a session
    // that is already gone.
    const onUnauthorized = vi.fn();
    planOperationMock.mockRejectedValue(new UnauthorizedError());
    renderControl({ onUnauthorized });

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the undo button mounted under a refusal so the server's sentence stands", async () => {
    // Catches a dead undo: a button that swallows "the operation has already
    // been undone" leaves the operator pressing something that answers nothing.
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(
      makeOperation({ status: "done", steps: THREE_STEPS, undo_state: "available" }),
    );
    undoOperationMock.mockRejectedValue(new Error("the operation has already been undone"));
    renderControl();
    await settlePlan();
    await act(async () => {
      fireEvent.click(actionButton());
    });

    await act(async () => {
      fireEvent.click(undoButton() as HTMLButtonElement);
    });

    const alert = await screen.findByRole("alert");
    expect(alert.getAttribute("data-adopt-undo-failure")).toBe("true");
    expect(alert.textContent).toBe("the operation has already been undone");
    expect(undoButton()).not.toBeNull();
  });
});
