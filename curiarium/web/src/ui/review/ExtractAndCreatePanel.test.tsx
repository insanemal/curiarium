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
import ExtractAndCreatePanel from "./ExtractAndCreatePanel";

const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);
const undoOperationMock = vi.mocked(undoOperation);
const listOperationsMock = vi.mocked(listOperations);

const ARCHIVE = "import/01_Fates End - Wizard Tower.zip";
const TWIN = "import/01_Fates End - Wizard Tower";
const FILED = `${TWIN}/_source/01_Fates End - Wizard Tower.zip`;
const PROJECT = "01_Fates End - Wizard Tower";

/** The server's refusal, whole: a sentence naming the archive, so a test that
 *  paraphrased it would be caught by the byte comparison. */
const REFUSAL = "the archive at 'import/broken.zip' cannot be opened: it is not a zip file";

/** One archive's plan: a move (filing the archive under `_source`), the groups the
 *  listing would write (including the server's `""` root bucket), the planned
 *  cover and the one file the server leaves out with its own reason. */
function makeReview(overrides: Partial<OperationReview> = {}): OperationReview {
  return {
    workflow: "extract-and-create",
    target: ARCHIVE,
    moves: 1,
    creates: 1,
    adopts: 0,
    deletes: 0,
    members: 20,
    groups: [
      { name: "01_Wizard", members: 14, models: 12 },
      { name: "", members: 6, models: 2 },
    ],
    cover: { asset_id: null, rel_path: "01_Wizard/tower.stl", reason: "first model" },
    excluded: [{ path: "01_Wizard/notes.psd", reason: "does not hold a model" }],
    existing_project_id: null,
    existing_project_name: null,
    move_list: [
      { source: ARCHIVE, target: FILED, kind: "file", action: "file-archive" },
    ],
    projects: [{ name: PROJECT, root: TWIN, members: 20 }],
    ...overrides,
  };
}

function makePlan(overrides: Partial<OperationPlan> = {}): OperationPlan {
  return {
    plan_id: "plan-1",
    workflow: "extract-and-create",
    target: ARCHIVE,
    steps: [
      { index: 0, op: "extract", summary: "extract 20 files" },
      { index: 1, op: "move", summary: "file the archive" },
      { index: 2, op: "register", summary: "register 20 files" },
      { index: 3, op: "create-project", summary: "create 1 project" },
    ],
    review: makeReview(),
    ...overrides,
  };
}

/** The zero-step plan: the server already found the project, so the review names
 *  it and claims no create. */
function makeAlreadyPlan(name = "Old Tower"): OperationPlan {
  return makePlan({
    steps: [],
    review: makeReview({
      moves: 0,
      creates: 0,
      existing_project_id: "p-existing",
      existing_project_name: name,
      move_list: [],
      projects: [],
      cover: null,
      excluded: [],
    }),
  });
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
    workflow: "extract-and-create",
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

/** The runner's four steps for a fresh archive, in index order. */
const FOUR_STEPS: OperationStep[] = [
  makeStep({ index: 0, op: "extract", status: "done" }),
  makeStep({ index: 1, op: "move", status: "done" }),
  makeStep({ index: 2, op: "register", status: "done" }),
  makeStep({
    index: 3,
    op: "create-project",
    status: "done",
    result: { project_id: "p1", name: PROJECT, status: "created", created: true },
  }),
];

function makeUndone(overrides: Partial<OperationUndo> = {}): OperationUndo {
  return {
    operation_id: "op-7",
    undone: true,
    project_id: null,
    project_name: null,
    moves: 1,
    projects_removed: [PROJECT],
    ...overrides,
  };
}

function renderPanel(
  props: Partial<ComponentProps<typeof ExtractAndCreatePanel>> = {},
): ReturnType<typeof render> {
  return render(
    <>
      <ExtractAndCreatePanel archivePath={ARCHIVE} canContribute name={PROJECT} {...props} />
      <Toaster />
    </>,
  );
}

/** The shared review surface, named by the ONE vocabulary: the group's name is
 *  the workflow's label plus "review", not a door-private "preview". */
function planPreview(): HTMLElement {
  return screen.getByRole("group", { name: "Extract and create review" });
}

function actionButton(): HTMLButtonElement | null {
  return document.querySelector("[data-extract-action-button]");
}

function undoButton(): HTMLButtonElement | null {
  return document.querySelector("[data-extract-undo]");
}

function stepLines(): HTMLElement[] {
  return Array.from(document.querySelectorAll("[data-extract-step]"));
}

async function settlePlan(): Promise<void> {
  await waitFor(() => expect(planPreview()).toBeTruthy());
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

describe("ExtractAndCreatePanel", () => {
  it("renders nothing and asks for nothing for a viewer", async () => {
    // Catches a viewer gate that renders an empty wrapper (which still claims a
    // layout track) or that plans anyway: a READ-only role must cause neither a
    // pixel nor a request.
    const { container } = renderPanel({ canContribute: false });

    expect(container.firstChild).toBeNull();
    await act(async () => {});
    expect(planOperationMock).not.toHaveBeenCalled();
  });

  it("plans for the archive on mount and names what it would write", async () => {
    // Catches a preview that counts the files but never says where they go, and
    // one that pretends the server's `""` bucket is a directory: the group is the
    // archive's own loose files and is rendered as such.
    planOperationMock.mockResolvedValue(makePlan());
    renderPanel();
    await settlePlan();

    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "extract-and-create",
      target: ARCHIVE,
      params: { name: PROJECT },
    });

    const panel = planPreview();
    // The count sentence is the shared facts line now: one `<span>` per count the
    // plan carries, tagged with the count's own machine name. The "files" fact is
    // the sentence the confirm stage used to spell out.
    expect(panel.querySelector('[data-extract-fact="files"]')?.textContent).toBe("20 files");

    const groups = Array.from(panel.querySelectorAll("[data-extract-group]"));
    expect(groups.map(node => node.getAttribute("data-extract-group-name"))).toEqual([
      "01_Wizard",
      "",
    ]);
    expect(groups[0].textContent).toBe("01_Wizard - 14 files, 12 models");
    expect(groups[1].textContent).toBe("loose files - 6 files, 2 models");

    // The archive's new home is the server's own MOVE row now: the target path
    // is on the wire once, in `move_list`, so the review does not restate it in a
    // second sentence. A path the client assembled from the archive and its twin
    // would not appear here at all.
    expect(panel.querySelector("[data-extract-move]")?.textContent).toContain(`"${FILED}"`);
    // The planned cover, by path: its asset id is resolved later by the server.
    // The shared surface flags no attribute for it (the cover is one line among
    // the facts), so this asserts the sentence itself.
    expect(panel.textContent).toContain('The project\'s cover is "01_Wizard/tower.stl".');
    // The server's refusal of one file, verbatim.
    expect(panel.querySelector("[data-extract-excluded]")?.textContent).toBe(
      "01_Wizard/notes.psd: does not hold a model",
    );
  });

  it("renders a zero-step plan as the already-existed sentence with no action", async () => {
    // Catches a finished plan painted as a ladder: the server said the project is
    // already there, so there is nothing to approve and nothing to press.
    planOperationMock.mockResolvedValue(makeAlreadyPlan());
    renderPanel();
    await settlePlan();

    expect(
      document.querySelector("[data-extract-existing]")?.textContent,
    ).toBe('The project "Old Tower" already exists, so there is nothing to create.');
    expect(actionButton()).toBeNull();
    expect(screen.queryByRole("button", { name: /extract and create/i })).toBeNull();
    expect(submitOperationMock).not.toHaveBeenCalled();
  });

  it("offers exactly one action, labelled with the outcome, and submits the plan id", async () => {
    // Catches a preview/apply pair, a label that names the subsystem, and a key
    // minted per click: a double-click must replay one intent, not queue two.
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockReturnValue(new Promise<Operation>(() => {}));
    renderPanel();
    await settlePlan();

    // The ONE submit control: the entry picker's button is a selection control,
    // not a second way to approve the plan, so the rule is about the action.
    const buttons = screen.getAllByRole("button");
    expect(buttons.map(button => button.textContent)).toEqual([
      "Choose files",
      `Extract and create '${PROJECT}' from 20 files`,
    ]);
    expect(screen.queryByRole("button", { name: /preview/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /apply/i })).toBeNull();

    act(() => {
      fireEvent.click(actionButton() as HTMLButtonElement);
      fireEvent.click(actionButton() as HTMLButtonElement);
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

  it("narrates the server's steps in index order and names the project it created", async () => {
    // Catches a progress list rendered in payload order and a done line that
    // refuses to read the create step's own result: the popup is the only place
    // the operator learns the extraction finished.
    vi.useFakeTimers();
    const onChanged = vi.fn();
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(makeOperation({ status: "queued" }));
    getOperationMock
      .mockResolvedValueOnce(makeOperation({ status: "running", steps: [...FOUR_STEPS].reverse() }))
      .mockResolvedValueOnce(makeOperation({ status: "done", steps: FOUR_STEPS }));

    renderPanel({ onChanged });
    await act(async () => {});
    await act(async () => {
      fireEvent.click(actionButton() as HTMLButtonElement);
    });

    // The progress dialog is the SHARED one, named from the ONE vocabulary: the
    // workflow's label plus "progress".
    expect(screen.getByRole("dialog", { name: "Extract and create progress" })).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(getOperationMock).toHaveBeenCalledWith("op-7");
    expect(stepLines()).toHaveLength(4);
    expect(stepLines().map(node => node.getAttribute("data-extract-op"))).toEqual([
      "extract",
      "move",
      "register",
      "create-project",
    ]);
    expect(stepLines()[0].textContent).toBe("extracted the archive");
    expect(stepLines()[2].textContent).toBe("registered the extracted files");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(document.querySelector("[data-extract-result]")?.textContent).toBe(
      `Done: project '${PROJECT}' created`,
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("narrates the create step's race rather than claiming a create", async () => {
    // Catches a done line read from the plan instead of the step: the runner can
    // find the project already there, and claiming a create would be a lie about
    // a write that never happened.
    vi.useFakeTimers();
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(
      makeOperation({
        status: "done",
        steps: [
          makeStep({ index: 0, op: "register", status: "done" }),
          makeStep({
            index: 1,
            op: "create-project",
            status: "done",
            result: { project_id: "p1", name: "Tower", status: "already_created" },
          }),
        ],
      }),
    );

    renderPanel();
    await act(async () => {});
    await act(async () => {
      fireEvent.click(actionButton() as HTMLButtonElement);
    });

    expect(document.querySelector("[data-extract-result]")?.textContent).toBe(
      "Done: project 'Tower' already existed - nothing new was created",
    );
  });

  it("offers the undo only while the server says it is available, and says the files stay", async () => {
    // Catches an undo offered on a guess, and a toast that claims the extracted
    // tree was removed: the residue is documented, so the sentence says so.
    const onChanged = vi.fn();
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(
      makeOperation({ status: "done", steps: FOUR_STEPS, undo_state: "available" }),
    );
    undoOperationMock.mockResolvedValue(makeUndone());
    renderPanel({ onChanged });
    await settlePlan();

    await act(async () => {
      fireEvent.click(actionButton() as HTMLButtonElement);
    });
    expect(undoButton()?.textContent).toBe("Undo this extraction");

    await act(async () => {
      fireEvent.click(undoButton() as HTMLButtonElement);
    });

    expect(undoOperationMock).toHaveBeenCalledWith("op-7");
    expect(undoButton()).toBeNull();
    const toast = getToasts().find(entry => entry.message === "Extract undone");
    expect(toast).toBeTruthy();
    expect(toast?.details?.join(" ")).toContain("The extracted files stay in the library.");
    expect(onChanged).toHaveBeenCalled();
  });

  it("offers no undo when the server says the run is not undoable", async () => {
    planOperationMock.mockResolvedValue(makePlan());
    submitOperationMock.mockResolvedValue(
      makeOperation({ status: "done", steps: FOUR_STEPS, undo_state: "unavailable" }),
    );
    renderPanel();
    await settlePlan();

    await act(async () => {
      fireEvent.click(actionButton() as HTMLButtonElement);
    });

    expect(document.querySelector("[data-extract-result]")).toBeTruthy();
    expect(undoButton()).toBeNull();
  });

  it("shows the server's refusal verbatim and offers no action", async () => {
    planOperationMock.mockRejectedValue(new Error(REFUSAL));
    renderPanel();

    const alert = await screen.findByRole("alert");
    // The failure handle is the shared panel's own: one plan-read failure tag for
    // every door (`data-<prefix>-plan-failure`), not a door-private one.
    expect(alert.getAttribute("data-extract-plan-failure")).toBe("true");
    expect(alert.textContent).toBe(REFUSAL);
    expect(actionButton()).toBeNull();
  });

  it("hands a 401 to the host and writes no sentence of its own", async () => {
    const onUnauthorized = vi.fn();
    planOperationMock.mockRejectedValue(new UnauthorizedError());
    renderPanel({ onUnauthorized });

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers the plan's own listing in a picker, re-plans for the confirmed subset and submits THAT plan", async () => {
    // Catches a selection lost between the picker and the plan (the submit would
    // name the WHOLE archive's plan), and a picker listing built from anything
    // but the plan's own `review.entries`: a member the review excludes must be
    // refused here with the same array, not a second, drifting list.
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
              { path: "01_Wizard/notes.psd", size: 64, junk: true },
            ],
          }),
        }),
      )
      .mockResolvedValueOnce(makePlan({ plan_id: "plan-subset" }));
    submitOperationMock.mockReturnValue(new Promise<Operation>(() => {}));

    renderPanel();
    await settlePlan();

    const choose = document.querySelector<HTMLButtonElement>("[data-extract-choose-entries]");
    expect(choose?.textContent).toBe("Choose files");
    fireEvent.click(choose as HTMLButtonElement);

    const dialog = screen.getByRole("dialog");
    // The listing is the plan's, path for path, and the excluded member is
    // refused with the plan's own sentence.
    const rows = Array.from(dialog.querySelectorAll('[role="option"]'));
    expect(rows.map(row => row.textContent)).toEqual([
      expect.stringContaining(MEMBER_A),
      expect.stringContaining(MEMBER_B),
      expect.stringContaining("01_Wizard/notes.psd"),
    ]);
    expect(rows[2].getAttribute("aria-disabled")).toBe("true");
    expect(rows[2].textContent).toContain("does not hold a model");

    fireEvent.click(rows[0]);
    fireEvent.click(screen.getByRole("button", { name: "Use these files" }));

    // A confirmed selection is a new plan READ: the params name the subset, and
    // the confirm then submits the plan that review showed.
    await waitFor(() => expect(planOperationMock).toHaveBeenCalledTimes(2));
    expect(planOperationMock.mock.calls[1][0]).toEqual({
      workflow: "extract-and-create",
      target: ARCHIVE,
      params: { name: PROJECT, entries: [MEMBER_A] },
    });

    await waitFor(() =>
      expect(document.querySelector("[data-extract-action-button]")).toBeTruthy(),
    );
    act(() => {
      fireEvent.click(document.querySelector("[data-extract-action-button]") as HTMLButtonElement);
    });

    await waitFor(() =>
      expect(submitOperationMock).toHaveBeenCalledWith({
        planId: "plan-subset",
        idempotencyKey: expect.any(String),
      }),
    );
  });

  it("sends no name param when the operator typed nothing", async () => {
    // The server's twin-derived default is what names the project then, so the
    // body must be byte-identical to the one that never mentions the field.
    planOperationMock.mockResolvedValue(makePlan());
    renderPanel({ name: "   " });
    await settlePlan();

    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "extract-and-create",
      target: ARCHIVE,
      params: {},
    });
  });
});
