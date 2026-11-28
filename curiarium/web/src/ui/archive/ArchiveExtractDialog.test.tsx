// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

// The shared action drives the operations ledger through `useOperationRun`, which
// imports the domain module directly (`ui/review/ExtractAndCreatePanel.test.tsx`
// mocks it the same way). The original is spread back so the real types and the
// other exports keep working.
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

import type { ArchiveExtractResult } from "../../lib/api/imports";
import {
  getOperation,
  planOperation,
  submitOperation,
  type Operation,
  type OperationPlan,
  type OperationReview,
  type OperationStep,
} from "../../lib/api/operations";
import ArchiveExtractDialog, { type ExtractTarget } from "./ArchiveExtractDialog";

const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);

const RESULT: ArchiveExtractResult = {
  path: "Moai.zip",
  destination: "Moai",
  files: 2,
  bytes: 2560,
  already_present: false,
  scan_started: false,
};

const SECOND: ArchiveExtractResult = {
  ...RESULT,
  path: "Buddha.zip",
  destination: "Buddha",
};

const TARGET: ExtractTarget = { path: "Moai.zip", label: "Moai.zip" };

/** One archive's review: two members in one group, one member left out with the
 *  extractor's own reason. */
function makeReview(target: string): OperationReview {
  return {
    workflow: "extract",
    target,
    moves: 0,
    creates: 0,
    adopts: 0,
    deletes: 0,
    members: 2,
    groups: [{ name: "", members: 2, models: 1 }],
    cover: null,
    excluded: [{ path: "__MACOSX/._model.stl", reason: "system metadata" }],
    existing_project_id: null,
    existing_project_name: null,
    move_list: [],
    projects: [],
  };
}

function makePlan(target: string): OperationPlan {
  return {
    plan_id: `plan-${target}`,
    workflow: "extract",
    target,
    steps: [
      { index: 0, op: "extract", summary: `extract 2 file(s) from '${target}'` },
      { index: 1, op: "register", summary: "register the 2 file(s) the extraction wrote" },
    ],
    review: makeReview(target),
  };
}

function makeStep(overrides: Partial<OperationStep> = {}): OperationStep {
  return {
    index: 0,
    op: "extract",
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
    workflow: "extract",
    target: "Moai.zip",
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

/** The row a finished run leaves behind: its extract step reports `result`. */
function finished(result: ArchiveExtractResult): Operation {
  return makeOperation({
    id: `op-${result.path}`,
    target: result.path,
    status: "done",
    steps: [
      makeStep({ index: 0, op: "extract", status: "done", result: { ...result } }),
      makeStep({ index: 1, op: "register", status: "done" }),
    ],
  });
}

/**
 * A promise the test settles by hand. Typed at the call site: an untyped
 * deferred infers `unknown` and fails TS2345 where the mock wants
 * `Promise<OperationPlan>` / `Promise<Operation>`.
 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(settle => {
    resolve = settle;
  });
  return { promise, resolve };
}

/** The action's own busy-stage focus home, found structurally. */
function busyHome(control: HTMLElement): HTMLElement {
  const home = control.closest<HTMLElement>('[tabindex="-1"]');
  if (!home) throw new Error("the control has no tabindex=-1 container");
  return home;
}

/** Plan, confirm and submit through the shared action. */
async function extractOnce(label = "Moai.zip"): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: `Extract ${label}` }));
  fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));
}

function renderDialog(overrides: {
  targets?: ExtractTarget[];
  canContribute?: boolean;
  onExtracted?: (results: ArchiveExtractResult[]) => void;
  onClose?: () => void;
} = {}) {
  const onExtracted = overrides.onExtracted ?? vi.fn();
  const onClose = overrides.onClose ?? vi.fn();
  render(
    <ArchiveExtractDialog
      targets={overrides.targets ?? [TARGET]}
      canContribute={overrides.canContribute ?? true}
      onExtracted={onExtracted}
      onClose={onClose}
    />,
  );
  return { onExtracted, onClose };
}

beforeEach(() => {
  planOperationMock.mockReset();
  planOperationMock.mockImplementation(async ({ target }) => makePlan(target));
  submitOperationMock.mockReset();
  submitOperationMock.mockImplementation(async ({ planId }) =>
    makeOperation({ id: `op-${planId}`, workflow: "extract" }),
  );
  getOperationMock.mockReset();
  getOperationMock.mockImplementation(async id => {
    // The row's id carries the plan it came from, so a batch's second archive
    // reports its own result rather than the first one's.
    const target = id.replace(/^op-plan-/, "");
    return finished(target === "Buddha.zip" ? SECOND : RESULT);
  });
});

afterEach(cleanup);

describe("ArchiveExtractDialog", () => {
  it("extracts through the shared action and reports the result to its caller", async () => {
    const onExtracted = vi.fn();
    renderDialog({ onExtracted });

    await extractOnce();

    await waitFor(() => expect(onExtracted).toHaveBeenCalledTimes(1));
    // The plan is the argument: the dialog never assembles a request of its own.
    expect(submitOperationMock).toHaveBeenCalledWith({
      planId: "plan-Moai.zip",
      idempotencyKey: expect.any(String),
    });
  });

  it("says the archive is registered, not that a scan will register it", () => {
    // The `extract` operation's register step writes the rows in-process, so the
    // dialog must promise no walk.
    renderDialog();

    expect(
      screen.getByText("Each archive is extracted into a new folder beside it and registered."),
    ).toBeTruthy();
    expect(screen.queryByText(/library scan/i)).toBeNull();
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    renderDialog({ onClose });

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps Tab inside the panel", () => {
    const { onClose } = renderDialog();
    const dialog = screen.getByRole("dialog");
    const close = within(dialog).getByRole("button", { name: "Close" });
    const extract = within(dialog).getByRole("button", { name: "Extract Moai.zip" });

    expect(document.activeElement).toBe(close);

    extract.focus();
    fireEvent.keyDown(extract, { key: "Tab" });

    expect(document.activeElement).toBe(close);
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps the first control focused on mount, before any advance", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog", { name: "Extract Moai.zip" });
    const close = within(dialog).getByRole("button", { name: "Close" });

    // The recovery effect must not replace the documented mount behaviour.
    expect(document.activeElement).toBe(close);
  });

  it("keeps focus inside the dialog when a batch advances to the next archive", async () => {
    renderDialog({ targets: [TARGET, { path: "Buddha.zip", label: "Buddha.zip" }] });

    // jsdom does not move focus on a click, a browser does. Focus each control
    // by hand before pressing it, or the button whose removal this test is
    // about was never the active element and focus could never be lost.
    const open = screen.getByRole("button", { name: "Extract Moai.zip" });
    open.focus();
    fireEvent.click(open);
    const confirm = await screen.findByRole("button", { name: "Extract 2 files" });
    confirm.focus();
    fireEvent.click(confirm);

    expect(await screen.findByText("Archive 2 of 2 - Buddha.zip")).toBeTruthy();

    const dialog = screen.getByRole("dialog", { name: "Extract 2 archives" });
    // The pressed button remounted away, so focus has to be re-homed inside the
    // panel - from <body> neither the trap nor Escape would ever reach it.
    expect(document.activeElement).not.toBe(document.body);
    expect(dialog.contains(document.activeElement)).toBe(true);

    const tabbable = within(dialog)
      .getAllByRole("button")
      .filter(button => !(button as HTMLButtonElement).disabled);
    const last = tabbable[tabbable.length - 1];
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });

    expect(document.activeElement).toBe(tabbable[0]);
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("takes a forward Tab from the planning stage's unlisted home to the first member", async () => {
    const planning = deferred<OperationPlan>();
    // Never settles, so the stage stays `planning` with every control disabled.
    planOperationMock.mockReturnValue(planning.promise);
    const { onClose } = renderDialog();

    // jsdom does not focus on a click; focus first so the press disables the
    // active element and the action's recovery effect re-homes onto its
    // container, which is inside the panel but not in the Tab ring.
    const trigger = screen.getByRole("button", { name: "Extract Moai.zip" });
    trigger.focus();
    fireEvent.click(trigger);

    // The accessible name is pinned by the button's `aria-label`, so the busy
    // stage is found by its visible text, not its role name.
    const planningControl = await screen.findByRole("button", { name: "Extract Moai.zip" });
    await waitFor(() => expect(planningControl.textContent).toBe("Planning..."));
    const home = busyHome(planningControl);
    await waitFor(() => expect(document.activeElement).toBe(home));

    const dialog = screen.getByRole("dialog");
    const close = within(dialog).getByRole("button", { name: "Close" });
    expect(home).not.toBe(dialog);

    fireEvent.keyDown(home, { key: "Tab" });

    expect(document.activeElement).toBe(close);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("takes a backward Tab from the planning stage's unlisted home to the member before it", async () => {
    const planning = deferred<OperationPlan>();
    planOperationMock.mockReturnValue(planning.promise);
    const { onClose } = renderDialog();

    const trigger = screen.getByRole("button", { name: "Extract Moai.zip" });
    trigger.focus();
    fireEvent.click(trigger);

    const planningControl = await screen.findByRole("button", { name: "Extract Moai.zip" });
    await waitFor(() => expect(planningControl.textContent).toBe("Planning..."));
    const home = busyHome(planningControl);
    await waitFor(() => expect(document.activeElement).toBe(home));

    const dialog = screen.getByRole("dialog");
    const close = within(dialog).getByRole("button", { name: "Close" });

    fireEvent.keyDown(home, { key: "Tab", shiftKey: true });

    expect(document.activeElement).toBe(close);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps both Tab directions inside the extracting stage's unlisted home", async () => {
    const submitting = deferred<Operation>();
    submitOperationMock.mockReturnValue(submitting.promise);
    const { onClose } = renderDialog();

    const trigger = screen.getByRole("button", { name: "Extract Moai.zip" });
    trigger.focus();
    fireEvent.click(trigger);
    const confirm = await screen.findByRole("button", { name: "Extract 2 files" });
    confirm.focus();
    fireEvent.click(confirm);

    const extractingControl = await screen.findByRole("button", { name: "Extracting..." });
    expect((extractingControl as HTMLButtonElement).disabled).toBe(true);
    const home = busyHome(extractingControl);
    await waitFor(() => expect(document.activeElement).toBe(home));

    const dialog = screen.getByRole("dialog");
    const close = within(dialog).getByRole("button", { name: "Close" });

    fireEvent.keyDown(home, { key: "Tab" });
    expect(document.activeElement).toBe(close);

    home.focus();
    fireEvent.keyDown(home, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(close);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("takes a forward Tab from the batch progress paragraph to the member after it", () => {
    renderDialog({ targets: [TARGET, { path: "Buddha.zip", label: "Buddha.zip" }] });
    const dialog = screen.getByRole("dialog");
    const trigger = within(dialog).getByRole("button", { name: "Extract Moai.zip" });
    const close = within(dialog).getByRole("button", { name: "Close" });
    const progress = screen.getByRole("status");

    progress.focus();
    fireEvent.keyDown(progress, { key: "Tab" });

    expect(document.activeElement).toBe(trigger);
    expect(document.activeElement).not.toBe(close);
  });

  it("returns the single result as an array", async () => {
    const onExtracted = vi.fn();
    renderDialog({ onExtracted });

    await extractOnce();

    await waitFor(() => expect(onExtracted).toHaveBeenCalledTimes(1));
    const reported = onExtracted.mock.calls[0][0];
    expect(reported).toHaveLength(1);
    expect(reported[0]).toEqual(RESULT);
  });

  it("is named by its heading", () => {
    renderDialog();

    expect(screen.getByRole("dialog", { name: "Extract Moai.zip" })).toBeTruthy();
  });

  it("walks a batch one archive at a time and reports them in order", async () => {
    const onExtracted = vi.fn();
    renderDialog({
      targets: [TARGET, { path: "Buddha.zip", label: "Buddha.zip" }],
      onExtracted,
    });

    expect(screen.getByText("Archive 1 of 2 - Moai.zip")).toBeTruthy();
    await extractOnce("Moai.zip");

    expect(await screen.findByText("Archive 2 of 2 - Buddha.zip")).toBeTruthy();
    expect(onExtracted).not.toHaveBeenCalled();

    await extractOnce("Buddha.zip");

    await waitFor(() => expect(onExtracted).toHaveBeenCalledTimes(1));
    const reported = onExtracted.mock.calls[0][0];
    expect(reported).toHaveLength(2);
    expect(reported[0]).toEqual(RESULT);
    expect(reported[1]).toEqual(SECOND);
  });
});
