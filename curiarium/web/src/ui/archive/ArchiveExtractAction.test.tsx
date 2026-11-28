// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// The action drives the operations ledger through `useOperationRun`, which
// imports the domain module directly (`ui/review/ExtractAndCreatePanel.test.tsx`
// mocks it the same way). The original is spread back so every other export -
// the real types, `splitActionLabel` - keeps working.
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
  undoOperation,
  type Operation,
  type OperationPlan,
  type OperationReview,
  type OperationStep,
} from "../../lib/api/operations";
import { getToasts, resetToasts } from "../../lib/toast";
import ArchiveExtractAction from "./ArchiveExtractAction";

const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);

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

/** The finished operation's own words, which only exist once the poll has read a
 *  terminal row. */
async function resultLine(): Promise<HTMLElement> {
  return waitFor(() => {
    const line = document.querySelector<HTMLElement>("[data-extract-result]");
    expect(line).toBeTruthy();
    return line as HTMLElement;
  });
}

/** Plan, confirm and submit: the whole happy path through the control. */
async function extractOnce(): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
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
  vi.mocked(undoOperation).mockReset();
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
    // hook's, not a disabled button's.
    expect(planOperationMock).not.toHaveBeenCalled();
  });

  it("plans the archive, shows the census and submits that plan", async () => {
    const onExtracted = vi.fn();
    render(
      <ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} onExtracted={onExtracted} />,
    );

    const button = screen.getByRole("button", { name: "Extract Moai.zip" });
    fireEvent.click(button);

    await waitFor(() =>
      expect(planOperationMock).toHaveBeenCalledWith({
        workflow: "extract",
        target: ARCHIVE,
        params: expect.any(Object),
      }),
    );
    expect(
      await screen.findByText("2 files — extract into a new folder beside the archive?"),
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
    // Progress is announced, not just painted.
    expect(screen.getByRole("status").getAttribute("aria-live")).toBe("polite");
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
    const status = screen.getByRole("status");
    expect(status.textContent).not.toMatch(/library scan/i);
    expect(status.textContent).toContain("The files are registered.");
    // The register step is narrated as its own step, in index order.
    expect(
      Array.from(status.querySelectorAll("[data-extract-step]")).map(node =>
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

  it("surfaces a refused submit as the error text", async () => {
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

    fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));

    expect(await screen.findByText(message)).toBeTruthy();
    expect(submitOperationMock).not.toHaveBeenCalled();
  });

  it("cancelling the confirmation leaves the archive untouched", async () => {
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(screen.getByRole("button", { name: "Extract Moai.zip" })).toBeTruthy();
    expect(submitOperationMock).not.toHaveBeenCalled();
  });
});

describe("keyboard focus across stages", () => {
  /** A promise the test settles itself, to observe a stage that lasts one request. */
  function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>(res => {
      resolve = res;
    });
    return { promise, resolve };
  }

  function trigger(): HTMLElement {
    return screen.getByRole("button", { name: "Extract Moai.zip" });
  }

  /** The idle/done/error primary control: visible label "Extract archive". */
  function archiveButton(): HTMLElement {
    const button = trigger();
    expect(button.textContent).toBe("Extract archive");
    return button;
  }

  /** The idle/planning/done/error root, found through the trigger it holds. */
  function idleContainer(): HTMLElement {
    const parent = trigger().parentElement;
    if (!parent) throw new Error("the trigger has no container");
    return parent;
  }

  /** The confirm/extracting root, found through the prompt it holds. */
  function confirmContainer(): HTMLElement {
    const parent = screen.getByText(/extract into a new folder beside the archive\?$/)
      .parentElement;
    if (!parent) throw new Error("the prompt has no container");
    return parent;
  }

  it("keeps focus on each stage's own control from idle through done", async () => {
    const planning = deferred<OperationPlan>();
    planOperationMock.mockReturnValue(planning.promise);
    const submitting = deferred<Operation>();
    submitOperationMock.mockReturnValue(submitting.promise);

    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    const idle = idleContainer();
    trigger().focus();
    fireEvent.click(trigger());

    // `planning` disables the only control: its container is the focus home.
    await waitFor(() => expect(document.activeElement).toBe(idle));
    expect(document.activeElement).not.toBe(trigger());
    expect(document.activeElement).not.toBe(document.body);
    expect((trigger() as HTMLButtonElement).disabled).toBe(true);
    expect(trigger().textContent).toBe("Planning...");

    await act(async () => {
      planning.resolve(makePlan());
    });

    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    await waitFor(() => expect(document.activeElement).toBe(confirmButton));

    confirmButton.focus();
    fireEvent.click(confirmButton);

    // `extracting` disables every control: the panel itself is the home again.
    const extractingButton = screen.getByRole("button", {
      name: "Extracting...",
    }) as HTMLButtonElement;
    const cancelButton = screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement;
    expect(extractingButton.disabled).toBe(true);
    expect(cancelButton.disabled).toBe(true);
    const panel = confirmContainer();
    await waitFor(() => expect(document.activeElement).toBe(panel));
    expect(document.activeElement).not.toBe(extractingButton);
    expect(document.activeElement).not.toBe(document.body);

    await act(async () => {
      submitting.resolve(makeOperation());
    });

    expect((await resultLine()).textContent).toBe(
      "Extracted 2 files. The files are registered.",
    );
    await waitFor(() => expect(document.activeElement).toBe(archiveButton()));
  });

  it("re-homes onto the trigger when the extraction finishes", async () => {
    // `extracting -> done` unmounts the confirmation panel under the caret, so
    // this pins that transition on its own rather than behind the planning one.
    const submitting = deferred<Operation>();
    submitOperationMock.mockReturnValue(submitting.promise);
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    trigger().focus();
    fireEvent.click(trigger());
    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    confirmButton.focus();
    fireEvent.click(confirmButton);

    await waitFor(() => expect(document.activeElement).toBe(confirmContainer()));
    expect(document.activeElement).not.toBe(document.body);

    await act(async () => {
      submitting.resolve(makeOperation());
    });

    expect((await resultLine()).textContent).toBe(
      "Extracted 2 files. The files are registered.",
    );
    await waitFor(() => expect(document.activeElement).toBe(archiveButton()));
    expect(document.activeElement).not.toBe(document.body);
  });

  it("returns focus to the trigger when the confirmation is cancelled", async () => {
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    trigger().focus();
    fireEvent.click(trigger());

    const cancelButton = await screen.findByRole("button", { name: "Cancel" });
    cancelButton.focus();
    fireEvent.click(cancelButton);

    await waitFor(() => expect(document.activeElement).toBe(trigger()));
  });

  it("re-homes after a planning error", async () => {
    // The container survives `planning -> error`, so a container-only guard
    // would leave focus on it; the new stage's control has to win.
    planOperationMock.mockRejectedValue(new Error("boom"));
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    trigger().focus();
    fireEvent.click(trigger());

    expect(await screen.findByText("boom")).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(archiveButton()));
  });

  it("re-homes after an extraction error", async () => {
    submitOperationMock.mockRejectedValue(new Error("Refusing to overwrite"));
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    trigger().focus();
    fireEvent.click(trigger());
    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    confirmButton.focus();
    fireEvent.click(confirmButton);

    expect(await screen.findByText("Refusing to overwrite")).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(archiveButton()));
  });

  it("does not steal focus on mount", () => {
    // A bulk-dialog batch remount or an aside mounts this action with no focus
    // of its own; grabbing it would yank a caret the user never lost.
    const outside = document.createElement("button");
    outside.textContent = "Elsewhere";
    document.body.appendChild(outside);
    outside.focus();

    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  it("does not take focus on mount when nothing else holds it", () => {
    // `<body>` means no one owns the caret: this mount is an aside or a batch
    // remount and must still not grab it.
    expect(document.activeElement).toBe(document.body);

    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    expect(document.activeElement).toBe(document.body);
  });

  it("leaves a focus move made elsewhere during the round-trip alone", async () => {
    const planning = deferred<OperationPlan>();
    planOperationMock.mockReturnValue(planning.promise);
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    trigger().focus();
    fireEvent.click(trigger());

    const outside = document.createElement("button");
    outside.textContent = "Elsewhere";
    document.body.appendChild(outside);
    outside.focus();

    await act(async () => {
      planning.resolve(makePlan());
    });

    // Anywhere in the document counts, not just this panel.
    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  it("leaves focus alone when a re-render does not change the stage", async () => {
    const { rerender } = render(
      <ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />,
    );

    trigger().focus();
    fireEvent.click(trigger());
    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    confirmButton.focus();
    fireEvent.click(confirmButton);
    await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());

    const outside = document.createElement("button");
    outside.textContent = "Elsewhere";
    document.body.appendChild(outside);
    outside.focus();

    rerender(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  it("adds no key handling of its own", async () => {
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    trigger().focus();
    fireEvent.click(trigger());

    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    confirmButton.focus();
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });

    // The enclosing dialog owns Escape; this action neither closes nor restarts.
    expect(screen.getByRole("button", { name: "Extract 2 files" })).toBeTruthy();
    expect(submitOperationMock).not.toHaveBeenCalled();
    expect(planOperationMock).toHaveBeenCalledTimes(1);
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
    fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
    await screen.findByText(/extract into a new folder beside the archive\?$/);
  }

  function groupLines(): HTMLElement[] {
    return Array.from(document.querySelectorAll<HTMLElement>("[data-extract-group]"));
  }

  function excludedLines(): HTMLElement[] {
    return Array.from(document.querySelectorAll<HTMLElement>("[data-extract-excluded]"));
  }

  it("keeps the count sentence as the panel's first line, and counts the plan's members", async () => {
    await openConfirm(towerReview());

    const summary = document.querySelector<HTMLElement>("[data-extract-summary]");
    expect(summary?.textContent).toBe(
      "5 files — extract into a new folder beside the archive?",
    );
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

  it("offers the same two controls whatever the review says", async () => {
    await openConfirm(towerReview());

    const extract = screen.getByRole("button", { name: "Extract 5 files" }) as HTMLButtonElement;
    expect(extract.disabled).toBe(false);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  });

  it("says nothing about the archive before the plan has been read", async () => {
    planOperationMock.mockReturnValue(new Promise<OperationPlan>(() => {}));
    render(<ArchiveExtractAction canContribute path={ARCHIVE} label={ARCHIVE} />);

    fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Extract Moai.zip" }).textContent).toBe(
        "Planning...",
      ),
    );
    expect(document.querySelector("[data-extract-group]")).toBeNull();
    expect(document.querySelector("[data-extract-excluded]")).toBeNull();
    expect(submitOperationMock).not.toHaveBeenCalled();
  });
});
