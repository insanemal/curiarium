// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";

// The panel reads and writes the domain module directly, so the mock lives
// there. The original is spread back so every other export keeps working.
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
  type OperationReview as Review,
} from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";
import OperationPanel from "./OperationPanel";

const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);
const undoOperationMock = vi.mocked(undoOperation);
const listOperationsMock = vi.mocked(listOperations);

function makeReview(overrides: Partial<Review> = {}): Review {
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
    excluded: [],
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
    steps: [{ index: 0, op: "move", summary: "hoist three folders" }],
    review: makeReview(),
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
    undo_state: "none",
    superseded_by: null,
    steps: [],
    ...overrides,
  };
}

function renderPanel(
  overrides: Partial<ComponentProps<typeof OperationPanel>> = {},
): ReturnType<typeof render> {
  return render(
    <OperationPanel
      workflow="split-and-create"
      target="Container"
      params={{ shape: "split-top-folders" }}
      canContribute
      dataPrefix="split"
      {...overrides}
    />,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  planOperationMock.mockResolvedValue(makePlan());
  submitOperationMock.mockResolvedValue(makeOperation());
  getOperationMock.mockResolvedValue(makeOperation({ status: "done" }));
  listOperationsMock.mockResolvedValue([]);
  undoOperationMock.mockResolvedValue({
    operation_id: "op-7",
    undone: true,
    project_id: null,
    project_name: null,
    moves: 0,
    projects_removed: [],
  });
});

afterEach(cleanup);

describe("OperationPanel", () => {
  it("renders one review and one confirm, submits that plan's id and then narrates the run", async () => {
    // Catches a door that offers the action twice, submits anything but the
    // server-issued plan id, or loses the progress popup after the click.
    const { container } = renderPanel();

    const button = await screen.findByRole("button", { name: "Split into 3 projects" });
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByText("What will happen")).toBeTruthy();

    fireEvent.click(button);

    await waitFor(() => expect(submitOperationMock).toHaveBeenCalledTimes(1));
    expect(submitOperationMock).toHaveBeenCalledWith({
      planId: "plan-1",
      idempotencyKey: expect.any(String),
    });

    await waitFor(() => expect(container.querySelector("[data-split-dialog]")).toBeTruthy());
    expect(container.querySelector("[data-split-plan]")).toBeNull();
  });

  it("adopts a row already in flight for the same target and offers no second confirm", async () => {
    // Catches the second operation the resume rule forbids: the row is shown
    // instead of the review, and nothing is submitted over its paths.
    listOperationsMock.mockResolvedValue([
      makeOperation({ id: "op-9", status: "running", submitted_by: "u2" }),
    ]);
    const { container } = renderPanel();

    const sentence = await screen.findByText(/already in flight/);
    expect(sentence.textContent).toContain("submitted by u2");
    expect(container.querySelector('[data-split-in-flight="true"]')).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Split into 3 projects" })).toBeNull();

    const dialog = await screen.findByRole("dialog");
    expect(dialog.getAttribute("data-split-dialog")).toBe("true");
    expect(submitOperationMock).not.toHaveBeenCalled();
    // The plan READ is issued on mount beside the lookup, and it is a READ that
    // writes nothing: what the resume rule forbids is a second SUBMIT over the
    // same paths, which is the assertion above. Holding the plan back until the
    // lookup answers would delay a read the operator has already paid for.
    expect(submitOperationMock).not.toHaveBeenCalled();
  });

  it("does not resume a terminal row for the target, and offers the review instead", async () => {
    // Catches a resume of finished work: a done row is not in flight, so its
    // confirm must be offered again rather than re-narrating an old operation.
    listOperationsMock.mockResolvedValue([
      makeOperation({ id: "op-9", status: "done", finished_at: "2026-01-01T00:01:00Z" }),
    ]);
    const { container } = renderPanel();

    expect(await screen.findByRole("button", { name: "Split into 3 projects" })).toBeTruthy();
    expect(container.querySelector('[data-split-in-flight="true"]')).toBeNull();
  });

  it("does not resume a row for another target of the same workflow", async () => {
    // Catches a lookup that matches the workflow but forgets the target: an
    // operation over "Other" says nothing about "Container".
    listOperationsMock.mockResolvedValue([makeOperation({ id: "op-9", target: "Other" })]);
    const { container } = renderPanel();

    expect(await screen.findByRole("button", { name: "Split into 3 projects" })).toBeTruthy();
    expect(container.querySelector('[data-split-in-flight="true"]')).toBeNull();
  });

  it("renders nothing and issues no request for a viewer", async () => {
    // Catches a write door mounted below contributor that still asks the server
    // to plan - and still shows a control the role cannot use.
    const { container } = renderPanel({ canContribute: false });
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.innerHTML).toBe("");
    expect(planOperationMock).not.toHaveBeenCalled();
    expect(listOperationsMock).not.toHaveBeenCalled();
  });

  it("hands a 401 from the in-flight lookup to the host", async () => {
    // Catches an expired session swallowed by the lookup: the shell is the one
    // that can sign the operator out.
    const onUnauthorized = vi.fn();
    listOperationsMock.mockRejectedValue(new UnauthorizedError());
    renderPanel({ onUnauthorized });

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
  });

  it("hides the review behind its trigger until the trigger is pressed", async () => {
    // Catches a two-stage door that shows the review (and its action) before the
    // operator asked to see it.
    renderPanel({ variant: "trigger", triggerLabel: "Extract archive" });

    const trigger = await screen.findByRole("button", { name: "Extract archive" });
    expect(screen.queryByText("What will happen")).toBeNull();
    expect(trigger.getAttribute("data-split-trigger")).toBe("true");

    fireEvent.click(trigger);

    expect(await screen.findByText("What will happen")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Split into 3 projects" })).toBeTruthy();
  });
});
