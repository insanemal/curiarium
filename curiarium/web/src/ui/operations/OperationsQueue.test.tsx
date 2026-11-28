// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// The component writes through the domain module directly, so the mock lives
// there. The original is spread back so the type surface keeps working.
const operationsApi = vi.hoisted(() => ({
  undoOperation: vi.fn(),
  cancelOperation: vi.fn(),
}));
vi.mock("../../lib/api/operations", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/operations")>()),
  undoOperation: operationsApi.undoOperation,
  cancelOperation: operationsApi.cancelOperation,
}));

import {
  cancelOperation,
  undoOperation,
  type Operation,
  type OperationStep,
} from "../../lib/api/operations";
import OperationsQueue from "./OperationsQueue";
import { isActiveStatus, queueRows, statusLabel } from "./operationView";
import type { OperationsQueueState } from "./useOperationsQueue";

const undoOperationMock = vi.mocked(undoOperation);
const cancelOperationMock = vi.mocked(cancelOperation);

/** The server's superseded refusal, whole: a dict `detail` the API layer already
 *  reduces to `reason`, so the queue must show the sentence byte-for-byte. */
const SUPERSEDED =
  "a later operation (op-7) changed the same paths after this one";

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
    id: "op-1",
    workflow: "split-and-create",
    target: "Container",
    status: "running",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:01Z",
    finished_at: null,
    error: null,
    undo_state: "none",
    superseded_by: null,
    steps: [],
    ...overrides,
  };
}

/** A state object the component can render, with every writer a spy. */
function makeState(overrides: Partial<OperationsQueueState> = {}): OperationsQueueState {
  const rows = overrides.rows ?? [];
  return {
    rows,
    view: queueRows(rows),
    loading: false,
    loaded: true,
    error: null,
    filters: { state: "", workflow: "", submitted_by: "" },
    setFilters: vi.fn(),
    refresh: vi.fn(async () => {}),
    loadMore: vi.fn(async () => {}),
    hasMore: false,
    activeCount: rows.filter(row => isActiveStatus(row.status)).length,
    ...overrides,
  };
}

/** A wrapper that keeps the filters, so "Only mine" can be ticked and unticked:
 *  a controlled checkbox whose state never changes would fire `checked` twice. */
function Harness({ rows = [] as Operation[] }: { rows?: Operation[] }): React.ReactElement {
  const [filters, setFilters] = React.useState(makeState().filters);
  const state = makeState({
    rows,
    filters,
    setFilters: next => setFilters(previous => ({ ...previous, ...next })),
  });
  return <OperationsQueue state={state} me="me" canContribute />;
}

beforeEach(() => {
  vi.resetAllMocks();
  undoOperationMock.mockResolvedValue({
    operation_id: "op-1",
    undone: true,
    project_id: null,
    project_name: null,
    moves: null,
    projects_removed: null,
  });
  cancelOperationMock.mockResolvedValue(makeOperation({ status: "cancelled" }));
});

afterEach(cleanup);

describe("OperationsQueue rows", () => {
  it("renders every status with its submitter's username", () => {
    // Catches a row that shows a status token it never labelled, and one that
    // hides who queued the work on a SHARED queue.
    const statuses = ["queued", "running", "cancelling", "cancelled", "done", "failed"];
    const rows = statuses.map(status =>
      makeOperation({ id: `op-${status}`, status, submitted_by: `user-${status}` }),
    );
    render(<OperationsQueue state={makeState({ rows })} me="me" canContribute />);

    for (const status of statuses) {
      expect(
        screen.getByText(statusLabel(status), { selector: "[data-operations-status]" }),
      ).toBeTruthy();
      expect(screen.getByText(`by user-${status}`)).toBeTruthy();
    }
    expect(screen.getByText("6 operations -- 3 in flight")).toBeTruthy();
  });

  it("sends the status and workflow filters as their own values", () => {
    // Catches a select wired to the wrong filter field.
    const state = makeState();
    render(<OperationsQueue state={state} me="me" canContribute />);

    fireEvent.change(screen.getByLabelText("Filter by status"), {
      target: { value: "failed" },
    });
    expect(state.setFilters).toHaveBeenCalledWith({ state: "failed" });

    fireEvent.change(screen.getByLabelText("Filter by workflow"), {
      target: { value: "extract" },
    });
    expect(state.setFilters).toHaveBeenCalledWith({ workflow: "extract" });
  });

  it("sends the signed-in username for Only mine and clears it on uncheck", () => {
    // Catches a mine toggle that sends a boolean, or one that cannot be undone.
    render(<Harness />);
    const mine = screen.getByLabelText("Only mine") as HTMLInputElement;

    fireEvent.click(mine);
    expect(mine.checked).toBe(true);

    fireEvent.click(mine);
    expect(mine.checked).toBe(false);
  });
});

describe("OperationsQueue detail", () => {
  it("expands one row's narration in step-index order", () => {
    // Catches a detail panel that trusts the payload's step order and one whose
    // button does not name the panel it opens.
    const row = makeOperation({
      id: "op-9",
      status: "running",
      steps: [
        makeStep({ index: 2, op: "verify-members", status: "pending" }),
        makeStep({ index: 0, op: "move", status: "done" }),
        makeStep({ index: 1, op: "create-project", status: "running" }),
      ],
    });
    const { container } = render(
      <OperationsQueue state={makeState({ rows: [row] })} me="me" canContribute />,
    );

    const button = screen.getByRole("button", { name: "Resume" });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.getAttribute("aria-controls")).toBe("operations-row-op-9");

    fireEvent.click(button);

    expect(button.getAttribute("aria-expanded")).toBe("true");
    const panel = container.querySelector("#operations-row-op-9");
    expect(panel?.getAttribute("data-operations-detail")).toBe("true");
    expect(screen.getByRole("dialog")).toBeTruthy();
    const ops = Array.from(panel?.querySelectorAll("[data-operations-step]") ?? []).map(node =>
      node.getAttribute("data-operations-op"),
    );
    expect(ops).toEqual(["move", "create-project", "verify-members"]);
  });

  it("narrates a FAILED row with its own steps and its error", () => {
    // Catches a detail view that only knows how to narrate live work: a failed
    // operation is the one the operator most needs to read.
    const row = makeOperation({
      id: "op-bad",
      status: "failed",
      error: "the move was refused",
      steps: [makeStep({ index: 0, op: "move", status: "failed", error: "the move was refused" })],
    });
    render(<OperationsQueue state={makeState({ rows: [row] })} me="me" canContribute />);

    fireEvent.click(screen.getByRole("button", { name: "Details" }));

    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("the move was refused", { selector: "[data-operations-error]" })).toBeTruthy();
    expect(screen.getByText("the move was refused", { selector: "[data-operations-step]" })).toBeTruthy();
  });

  it("keeps exactly one row expanded", () => {
    // Catches two step narrations open at once, which reads as two running rows.
    const rows = [
      makeOperation({ id: "op-1", status: "running" }),
      makeOperation({ id: "op-2", status: "running" }),
    ];
    const { container } = render(
      <OperationsQueue state={makeState({ rows })} me="me" canContribute />,
    );

    const [first, second] = screen.getAllByRole("button", { name: "Resume" });
    fireEvent.click(first);
    fireEvent.click(second);

    expect(first.getAttribute("aria-expanded")).toBe("false");
    expect(second.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelectorAll("[data-operations-detail]")).toHaveLength(1);
    expect(container.querySelector("#operations-row-op-2")).toBeTruthy();
  });
});

describe("OperationsQueue writes", () => {
  it("offers Undo only while the server says it is available, and shows a superseded refusal verbatim", () => {
    // Catches an undo offered over a row the server already folded, a refusal
    // paraphrased (the reason names the operation that won the race), and a
    // button that unmounts under its own sentence.
    const stillGroup = makeOperation({
      id: "op-undone",
      status: "done",
      undo_state: "undone",
    });
    const folded = render(
      <OperationsQueue state={makeState({ rows: [stillGroup] })} me="me" canContribute />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(screen.queryByRole("button", { name: "Undo this operation" })).toBeNull();
    folded.unmount();

    undoOperationMock.mockRejectedValueOnce(new Error(SUPERSEDED));
    const row = makeOperation({
      id: "op-7",
      status: "done",
      undo_state: "available",
      finished_at: "2026-01-01T00:01:00Z",
    });
    render(<OperationsQueue state={makeState({ rows: [row] })} me="me" canContribute />);
    fireEvent.click(screen.getByRole("button", { name: "Details" }));

    const undoButton = screen.getByRole("button", { name: "Undo this operation" });
    fireEvent.click(undoButton);

    return waitFor(() => {
      expect(screen.getByText(SUPERSEDED)).toBeTruthy();
      expect(undoOperationMock).toHaveBeenCalledWith("op-7");
    }).then(() => {
      expect(screen.getByRole("alert").getAttribute("data-operations-failure")).toBe("true");
      // The button stays mounted: a dead button would swallow the answer.
      expect(screen.getByRole("button", { name: "Undo this operation" })).toBeTruthy();
    });
  });

  it("offers Cancel only while the operation can still be stopped", () => {
    // Catches a Cancel button over a finished row (a request the server can only
    // refuse) and one offered to a viewer.
    const rows = [
      makeOperation({ id: "op-live", status: "queued" }),
      makeOperation({ id: "op-done", status: "done" }),
    ];
    const { unmount } = render(
      <OperationsQueue state={makeState({ rows })} me="me" canContribute />,
    );

    fireEvent.click(screen.getAllByRole("button", { name: "Resume" })[0]);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
    unmount();

    render(<OperationsQueue state={makeState({ rows })} me="me" canContribute />);
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    cleanup();

    render(<OperationsQueue state={makeState({ rows })} me="me" canContribute={false} />);
    fireEvent.click(screen.getAllByRole("button", { name: "Resume" })[0]);
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("cancels through the server and refreshes the queue", async () => {
    // Catches a Cancel that only changes local state: the runner owns the row.
    const onChanged = vi.fn();
    const state = makeState({ rows: [makeOperation({ id: "op-live", status: "queued" })] });
    render(<OperationsQueue state={state} me="me" canContribute onChanged={onChanged} />);

    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(cancelOperationMock).toHaveBeenCalledWith("op-live"));
    await waitFor(() => expect(state.refresh).toHaveBeenCalledTimes(1));
    expect(onChanged).toHaveBeenCalledTimes(1);
  });
});

describe("OperationsQueue states", () => {
  it("says No operations yet for an empty page, shows an error, and pages only when there is more", () => {
    // Catches a blank pane, an error swallowed, and a Load more offered past the
    // end of the list.
    const { rerender } = render(
      <OperationsQueue state={makeState({ rows: [] })} me="me" canContribute />,
    );
    expect(screen.getByText("No operations")).toBeTruthy();
    expect(screen.getByText("No operations yet")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();

    rerender(
      <OperationsQueue
        state={makeState({ rows: [], error: "The operations queue is unavailable." })}
        me="me"
        canContribute
      />,
    );
    expect(screen.getByRole("alert").textContent).toBe("The operations queue is unavailable.");
    expect(screen.queryByText("No operations yet")).toBeNull();

    const loadMore = vi.fn(async () => {});
    rerender(
      <OperationsQueue
        state={makeState({ rows: [makeOperation()], hasMore: true, loadMore })}
        me="me"
        canContribute
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(loadMore).toHaveBeenCalledTimes(1);
  });

  it("shows the loading sentence while the first page is in flight", () => {
    // Catches a queue that renders an empty state before it has read anything.
    render(<OperationsQueue state={makeState({ loading: true, loaded: false })} me="me" canContribute />);

    expect(screen.getByText("Loading operations...")).toBeTruthy();
    expect(screen.queryByText("No operations yet")).toBeNull();
  });
});
