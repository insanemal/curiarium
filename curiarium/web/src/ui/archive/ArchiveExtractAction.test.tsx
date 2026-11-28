// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// The action drives the operations ledger through the shared panel, which imports
// the domain module directly (`ui/review/ExtractAndCreatePanel.test.tsx` mocks it
// the same way). The original is spread back so every other export keeps working.
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

import type { ArchiveExtractResult } from "../../lib/api/imports";
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
import { getToasts, resetToasts } from "../../lib/toast";
import Toaster from "../Toaster";
import ArchiveExtractAction from "./ArchiveExtractAction";

const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);
const undoOperationMock = vi.mocked(undoOperation);
const listOperationsMock = vi.mocked(listOperations);

const ARCHIVE = "Moai.zip";

/** The extractor's own report, as the step writes it: two files, no walk (the
 *  `register` step after it writes the rows). */
const RESULT: ArchiveExtractResult = {
  path: ARCHIVE,
  destination: "Moai",
  files: 2,
  bytes: 2560,
  already_present: false,
  scan_started: false,
};

/** The plan's review: the census the confirm stage renders, including one member
 *  the extractor leaves out with its own reason - the capability the deleted
 *  entries listing had. */
function makeReview(overrides: Partial<OperationReview> = {}): OperationReview {
  return {
    workflow: "extract",
    target: ARCHIVE,
    moves: 0,
    creates: 0,
    adopts: 0,
    deletes: 0,
    members: 2,
    groups: [
      { name: "01_Wizard", members: 1, models: 1 },
      { name: "", members: 1, models: 0 },
    ],
    cover: { asset_id: null, rel_path: "01_Wizard/tower.stl", reason: "first model" },
    excluded: [{ path: "__MACOSX/._tower.stl", reason: "system metadata" }],
    existing_project_id: null,
    existing_project_name: null,
    move_list: [],
    projects: [],
    ...overrides,
  };
}

function makePlan(overrides: Partial<OperationPlan> = {}): OperationPlan {
  return {
    plan_id: "plan-1",
    workflow: "extract",
    target: ARCHIVE,
    steps: [
      { index: 0, op: "extract", summary: "extract 2 file(s) from 'Moai.zip' into 'Moai'" },
      { index: 1, op: "register", summary: "register the 2 file(s) the extraction wrote" },
    ],
    review: makeReview(),
    ...overrides,
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
    target: ARCHIVE,
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

/** A finished run whose extract step reports `result`. */
function finished(result: Partial<ArchiveExtractResult> = {}): Operation {
  return makeOperation({
    status: "done",
    steps: [
      makeStep({
        index: 0,
        op: "extract",
        status: "done",
        result: { ...RESULT, ...result },
      }),
      makeStep({ index: 1, op: "register", status: "done" }),
    ],
  });
}

function makeUndone(overrides: Partial<OperationUndo> = {}): OperationUndo {
  return {
    operation_id: "op-7",
    undone: true,
    project_id: null,
    project_name: null,
    moves: 0,
    projects_removed: [],
    ...overrides,
  };
}

/** The trigger: the only control before the operator asks for the review. */
function trigger(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Extract Moai.zip" }) as HTMLButtonElement;
}

/** The ONE action in the review. */
function actionButton(): HTMLButtonElement {
  return document.querySelector("[data-extract-action-button]") as HTMLButtonElement;
}

/** The finished operation's own words, which only exist once the poll has read a
 *  terminal row. */
async function resultLine(): Promise<HTMLElement> {
  return waitFor(() => {
    const line = document.querySelector<HTMLElement>("[data-extract-result]");
    expect(line).toBeTruthy();
    return line as HTMLElement;
  });
}

/** Reveal the review and submit it: the whole happy path through the control. */
async function extractOnce(): Promise<void> {
  fireEvent.click(trigger());
  fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));
}

beforeEach(() => {
  resetToasts();
  planOperationMock.mockReset();
  planOperationMock.mockResolvedValue(makePlan());
  submitOperationMock.mockReset();
  submitOperationMock.mockResolvedValue(makeOperation());
  getOperationMock.mockReset();
  getOperationMock.mockResolvedValue(finished());
  undoOperationMock.mockReset();
  // The shared panel's in-flight lookup is a READ on mount; nothing is running
  // for this target unless a test says so.
  listOperationsMock.mockReset();
  listOperationsMock.mockResolvedValue([]);
});

afterEach(cleanup);

describe("ArchiveExtractAction", () => {
  it("renders nothing at all without the contribute capability", () => {
    const { container } = render(
      <ArchiveExtractAction canContribute={false} path={ARCHIVE} label={ARCHIVE} />,
    );

    expect(container.innerHTML).toBe("");
    expect(screen.queryByRole("button")).toBeNull();
    // A viewer issues no request at all: the plan is a READ and the gate is the
    // shape of the component, not a disabled button's.
    expect(planOperationMock).not.toHaveBeenCalled();
    expect(listOperationsMock).not.toHaveBeenCalled();
  });

  it("plans the archive, shows the census and submits that plan", async () => {
    const onExtracted = vi.fn();
    render(
      <ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} onExtracted={onExtracted} />,
    );

    fireEvent.click(trigger());

    await waitFor(() =>
      expect(planOperationMock).toHaveBeenCalledWith({
        workflow: "extract",
        target: ARCHIVE,
        params: {},
      }),
    );
    // The count sentence is the shared facts line now: one `<span>` per count the
    // plan carries, tagged with the count's own machine name.
    expect(
      await screen.findByText("2 files", { selector: '[data-extract-fact="files"]' }),
    ).toBeTruthy();
    // Planning alone writes nothing.
    expect(submitOperationMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Extract 2 files" }));

    // The plan's id is the whole argument: the client cannot invent a step list.
    await waitFor(() =>
      expect(submitOperationMock).toHaveBeenCalledWith({
        planId: "plan-1",
        idempotencyKey: expect.any(String),
      }),
    );
    expect((await resultLine()).textContent).toBe(
      "Extracted 2 files. The files are registered.",
    );
    // Progress is announced, not just painted: the shared dialog is the same
    // `role="dialog"` every door mounts.
    expect(screen.getByRole("dialog", { name: "Extract progress" })).toBeTruthy();
    expect(onExtracted).toHaveBeenCalledWith(RESULT);
  });

  it("says nothing was written when the destination already exists", async () => {
    // Zero files here too: both nothing-written outcomes report `files: 0`, and
    // this branch has to be read first or it would borrow the junk-only wording.
    getOperationMock.mockResolvedValue(finished({ files: 0, bytes: 0, already_present: true }));
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    await extractOnce();

    const line = await resultLine();
    expect(line.textContent).toBe("Already extracted - nothing to do.");
    expect(line.textContent).not.toContain("Extracted 2");
  });

  it("says nothing was written when the archive held no extractable files", async () => {
    // Junk-only: the server writes nothing and registers nothing, but it is not
    // `already_present` either, so it needs its own line - and no scan claim.
    getOperationMock.mockResolvedValue(finished({ files: 0, bytes: 0, already_present: false }));
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    await extractOnce();

    const line = await resultLine();
    expect(line.textContent).toContain("Nothing was written");
    expect(line.textContent).not.toContain("scan");
  });

  it("promises no scan anywhere in the finished run", async () => {
    // The completion line used to promise a walk. The `register` step writes the
    // rows in-process, so a scan claim anywhere here would be a lie.
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    await extractOnce();

    await resultLine();
    const dialog = screen.getByRole("dialog", { name: "Extract progress" });
    expect(dialog.textContent).not.toMatch(/library scan/i);
    expect(dialog.textContent).toContain("The files are registered.");
    // The register step is narrated as its own step, in index order.
    expect(
      Array.from(dialog.querySelectorAll("[data-extract-step]")).map(node =>
        node.getAttribute("data-extract-op"),
      ),
    ).toEqual(["extract", "register"]);
  });

  it("calls onExtracted exactly once on success", async () => {
    const onExtracted = vi.fn();
    render(
      <ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} onExtracted={onExtracted} />,
    );

    await extractOnce();

    await waitFor(() => expect(onExtracted).toHaveBeenCalledTimes(1));
    expect(onExtracted).toHaveBeenCalledWith(RESULT);
    // Nothing failed, so a success raises no toast.
    expect(getToasts()).toHaveLength(0);
  });

  it("reports a completed run once per operation id on a route that never passes null", async () => {
    // The host's contract is `extractResult(operation)` and null is never a
    // report: a done row whose extract step reported nothing means nothing landed
    // to report, so no call is made at all.
    getOperationMock.mockResolvedValue(
      makeOperation({
        status: "done",
        steps: [makeStep({ index: 0, op: "extract", status: "done", result: null })],
      }),
    );
    const onExtracted = vi.fn();
    render(
      <ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} onExtracted={onExtracted} />,
    );

    await extractOnce();
    await resultLine();

    expect(onExtracted).not.toHaveBeenCalled();
  });

  it("surfaces a refused submit as the shared failure line", async () => {
    // The server refuses the plan (a destination that already holds files, or a
    // plan that went stale): nothing is queued, so the sentence is the report.
    const message = "Refusing to overwrite: Moai.stl, readme.txt";
    submitOperationMock.mockRejectedValue(new Error(message));
    const onExtracted = vi.fn();
    render(
      <ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} onExtracted={onExtracted} />,
    );

    await extractOnce();

    expect(await screen.findByText(message)).toBeTruthy();
    expect(onExtracted).not.toHaveBeenCalled();
    // Still mounted, so the inline line is the whole report: a regression that
    // painted AND toasted would tell the operator twice.
    expect(getToasts()).toHaveLength(0);
    // Nothing was queued, so nothing is polled.
    expect(getOperationMock).not.toHaveBeenCalled();
  });

  it("surfaces a refused plan as the error text, with nothing submitted", async () => {
    // The planner's own refusal (a running scan, an archive that is not a zip)
    // arrives before any confirm: it is the plan's sentence or nothing.
    const message = "a library scan is running; nothing was planned for 'Moai.zip'";
    planOperationMock.mockRejectedValue(new Error(message));
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    fireEvent.click(trigger());

    expect(await screen.findByText(message)).toBeTruthy();
    expect(submitOperationMock).not.toHaveBeenCalled();
  });

  it("disables the trigger when the caller has another write in flight", async () => {
    // The caller says a second extract would race its own run: every control is
    // refused, and the shared panel is told so rather than re-deriving it.
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} disabled />);

    await waitFor(() => expect(trigger().disabled).toBe(true));
    fireEvent.click(trigger());
    expect(screen.queryByRole("button", { name: "Extract 2 files" })).toBeNull();
    expect(submitOperationMock).not.toHaveBeenCalled();
  });

  it("offers the undo while the server says it is available, and toasts the fold", async () => {
    // The news is the undo's own answer, and it comes from the shared panel's
    // `onUndo` - the door keeps the wording, the panel keeps the state.
    submitOperationMock.mockResolvedValue(
      makeOperation({ status: "done", steps: [], undo_state: "available" }),
    );
    undoOperationMock.mockResolvedValue(makeUndone());
    render(
      <>
        <ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />
        <Toaster />
      </>,
    );

    fireEvent.click(trigger());
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

    const undo = await waitFor(() => {
      const button = document.querySelector<HTMLButtonElement>("[data-extract-undo]");
      expect(button).toBeTruthy();
      return button as HTMLButtonElement;
    });
    expect(undo.textContent).toBe("Undo this extraction");

    await act(async () => {
      fireEvent.click(undo);
    });

    expect(undoOperationMock).toHaveBeenCalledWith("op-7");
    expect(document.querySelector("[data-extract-undo]")).toBeNull();
    const toast = getToasts().find(entry => entry.message === "Extract undone");
    expect(toast).toBeTruthy();
    expect(toast?.details?.join(" ")).toContain("The extracted files stay in the library.");
  });
});

describe("ArchiveExtractAction picker", () => {
  it("offers the plan's own listing in a picker, re-plans for the confirmed subset and submits THAT plan", async () => {
    // Catches a selection lost between the picker and the plan (the submit would
    // name the whole archive's plan, extracting everything the operator narrowed
    // away) and a picker whose listing disagrees with the review: the entries are
    // the SAME array `review.entries`, and an excluded member is refused with the
    // plan's own sentence.
    const MEMBER_A = "01_Wizard/tower.stl";
    const MEMBER_B = "01_Wizard/base.stl";
    planOperationMock
      .mockResolvedValueOnce(
        makePlan({
          plan_id: "plan-whole",
          review: makeReview({
            entries: [
              { path: MEMBER_A, size: 1024, junk: false },
              { path: MEMBER_B, size: 512, junk: false },
            ],
          }),
        }),
      )
      .mockResolvedValueOnce(makePlan({ plan_id: "plan-subset" }));
    submitOperationMock.mockReturnValue(new Promise<Operation>(() => {}));

    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);
    fireEvent.click(trigger());
    await screen.findByRole("button", { name: "Extract 2 files" });

    const choose = document.querySelector<HTMLButtonElement>("[data-extract-choose-entries]");
    expect(choose?.textContent).toBe("Choose files");
    fireEvent.click(choose as HTMLButtonElement);

    const dialog = screen.getByRole("dialog", { name: /Choose files/ });
    const rows = Array.from(dialog.querySelectorAll('[role="option"]'));
    expect(rows.map(row => row.textContent)).toEqual([
      expect.stringContaining(MEMBER_A),
      expect.stringContaining(MEMBER_B),
    ]);

    fireEvent.click(rows[1]);
    fireEvent.click(screen.getByRole("button", { name: "Use these files" }));

    // A confirmed selection is a new plan READ: the params name the subset, and
    // the confirm then submits the plan that review showed.
    await waitFor(() => expect(planOperationMock).toHaveBeenCalledTimes(2));
    expect(planOperationMock.mock.calls[1][0]).toEqual({
      workflow: "extract",
      target: ARCHIVE,
      params: { entries: [MEMBER_B] },
    });

    await waitFor(() => expect(actionButton()).toBeTruthy());
    act(() => {
      fireEvent.click(actionButton());
    });

    await waitFor(() =>
      expect(submitOperationMock).toHaveBeenCalledWith({
        planId: "plan-subset",
        idempotencyKey: expect.any(String),
      }),
    );
  });

  it("disables the picker when the plan listed no members", async () => {
    // An empty listing has nothing to pick, so the button says so rather than
    // opening a dialog over nothing.
    planOperationMock.mockResolvedValue(makePlan({ review: makeReview({ entries: [] }) }));
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    fireEvent.click(trigger());

    const choose = await waitFor(() => {
      const button = document.querySelector<HTMLButtonElement>("[data-extract-choose-entries]");
      expect(button).toBeTruthy();
      return button as HTMLButtonElement;
    });
    expect(choose.disabled).toBe(true);
    expect(choose.getAttribute("data-extract-choose-entries-empty")).toBe("true");
  });
});

describe("ArchiveExtractAction confirm review", () => {
  // The archive from the live report (Mf-0ck5): the operator pressed Extract
  // expecting a tower and got a row named after the archive, because the models
  // were packed inside a nested `.zip` that extraction leaves whole. The plan's
  // own review is what the confirm step speaks now.
  const TOWER_ROOT = "01_Fates End - Wizard Tower";

  function towerReview(): OperationReview {
    return makeReview({
      members: 5,
      groups: [
        { name: `${TOWER_ROOT}/01_Wizard`, members: 2, models: 2 },
        { name: `${TOWER_ROOT}/images`, members: 1, models: 0 },
        { name: "", members: 2, models: 0 },
      ],
      cover: { asset_id: null, rel_path: `${TOWER_ROOT}/01_Wizard/base.stl`, reason: "first model" },
      excluded: [
        { path: "__MACOSX/._wizard_tower_base.stl", reason: "system metadata" },
        { path: `${TOWER_ROOT}/notes.psd`, reason: "not a file extraction writes" },
      ],
    });
  }

  async function openConfirm(review: OperationReview): Promise<void> {
    planOperationMock.mockResolvedValue(makePlan({ review }));
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);
    fireEvent.click(trigger());
    await screen.findByRole("button", { name: "Extract 5 files" });
  }

  function groupLines(): HTMLElement[] {
    return Array.from(document.querySelectorAll<HTMLElement>("[data-extract-group]"));
  }

  function excludedLines(): HTMLElement[] {
    return Array.from(document.querySelectorAll<HTMLElement>("[data-extract-excluded]"));
  }

  it("states the member count as the plan's own fact", async () => {
    await openConfirm(towerReview());

    // The count sentence is the shared facts line: the plan's `members`, tagged
    // with the count's own machine name.
    const summary = document.querySelector<HTMLElement>('[data-extract-fact="files"]');
    expect(summary?.textContent).toBe("5 files");
  });

  it("says which groups the plan would write, one item per group", async () => {
    await openConfirm(towerReview());

    // The groups come from the WIRE ARRAY, never from a prose rendering of it.
    expect(groupLines().map(node => node.getAttribute("data-extract-group-name"))).toEqual([
      `${TOWER_ROOT}/01_Wizard`,
      `${TOWER_ROOT}/images`,
      "",
    ]);
    expect(groupLines()[0].textContent).toBe(`${TOWER_ROOT}/01_Wizard - 2 files, 2 models`);
    // The `""` bucket is the archive's own loose files, never a folder that does
    // not exist.
    expect(groupLines()[2].textContent).toBe("loose files - 2 files, 0 models");
  });

  it("lists every member the plan leaves out WITH the server's reason", async () => {
    // This is the capability the deleted entries listing had: a junk member is
    // still visible - as excluded, with the reason the server gave.
    await openConfirm(towerReview());

    expect(excludedLines().map(node => node.textContent)).toEqual([
      "__MACOSX/._wizard_tower_base.stl: system metadata",
      `${TOWER_ROOT}/notes.psd: not a file extraction writes`,
    ]);
  });

  it("offers the same one action whatever the review says", async () => {
    await openConfirm(towerReview());

    const extract = screen.getByRole("button", { name: "Extract 5 files" }) as HTMLButtonElement;
    expect(extract.disabled).toBe(false);
    // One action, not a preview/apply pair: the review is information.
    expect(screen.queryByRole("button", { name: /preview/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /apply/i })).toBeNull();
  });

  it("says nothing about the archive before the plan has been read", async () => {
    // The review is offered only once the plan is in hand: a door that painted an
    // empty census over a pending request would invite a confirm of nothing.
    planOperationMock.mockReturnValue(new Promise<OperationPlan>(() => {}));
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    expect(trigger().getAttribute("data-extract-trigger")).toBe("true");
    fireEvent.click(trigger());

    // The trigger is spent and the review has nothing to show yet: the panel
    // holds a pending READ (a disabled "Planning..." control, so the press is not
    // silent) rather than painting an empty census over it.
    await waitFor(() =>
      expect(screen.getByRole("button").getAttribute("data-extract-planning")).toBe("true"),
    );
    expect(screen.getByRole("button")).toHaveProperty("disabled", true);
    expect(document.querySelector("[data-extract-group]")).toBeNull();
    expect(document.querySelector("[data-extract-excluded]")).toBeNull();
    expect(document.querySelector("[data-extract-action-button]")).toBeNull();
    expect(submitOperationMock).not.toHaveBeenCalled();
  });
});
