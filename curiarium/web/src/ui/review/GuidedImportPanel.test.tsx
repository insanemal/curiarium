// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";

// The panel imports the domain module directly, so the mock lives there. The
// original is spread back so every other export keeps working - `enrichBundles`
// included: enrichment is one `enrich` OPERATION now, and the adapter that plans,
// submits and polls it is the code under test here, so its own reads are the
// operations mock below rather than a stubbed function.
const bundleApi = vi.hoisted(() => ({
  createBundles: vi.fn(),
}));
vi.mock("../../lib/api/bundles", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/bundles")>()),
  createBundles: bundleApi.createBundles,
}));

// The split door the panel mounts reads its own domain module, so its calls are
// mocked there. The original is spread back so every other export keeps working.
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
  createBundles,
  type BundleCreateResponse,
  type BundleDetectItem,
  type BundleEnrichResponse,
  type BundleMatch,
} from "../../lib/api/bundles";
import {
  getOperation,
  planOperation,
  submitOperation,
  type Operation,
  type OperationPlan,
} from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";
import { getToasts, resetToasts } from "../../lib/toast";
import Toaster from "../Toaster";
import GuidedImportPanel from "./GuidedImportPanel";

const createBundlesMock = vi.mocked(createBundles);
const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);

const LARGE_IMAGE = {
  asset_id: "img-large",
  rel_path: "images/b.jpg",
  url: "/file/img-large/b.jpg",
  thumb_url: null,
};
const SMALL_IMAGE = {
  asset_id: "img-small",
  rel_path: "images/a.jpg",
  url: "/file/img-small/a.jpg",
  thumb_url: null,
};

const ONE_PROJECT: BundleMatch = {
  shape: "one-project",
  rule: "engine:one-project",
  confidence: "high",
  model_count: 3,
  image_count: 2,
  text_count: 1,
  other_count: 0,
  note: null,
  creatable: true,
};

const SPLIT_TOP_FOLDERS: BundleMatch = {
  shape: "split-top-folders",
  rule: "engine:split-top-folders",
  confidence: "low",
  model_count: 3,
  image_count: 2,
  text_count: 1,
  other_count: 0,
  note: "needs arranging first: 2 parts become separate projects",
  creatable: false,
};

function makeRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  const row: BundleDetectItem = {
    key: "import/Renamed Moai",
    rel_path: "import/Renamed Moai",
    title: "Renamed Moai",
    name_suggestion: "Renamed Moai",
    creator: null,
    licence: null,
    licence_url: null,
    source_url: null,
    detector: "engine:one-project",
    confidence: "high",
    metadata_shape: "one-project",
    description_excerpt: null,
    model_count: 3,
    image_count: 2,
    text_count: 1,
    other_count: 0,
    archive_count: 1,
    cover: {
      asset_id: "img-large",
      rel_path: "images/b.jpg",
      reason: "largest-image",
      url: "/file/img-large/b.jpg",
      thumb_url: null,
    },
    assets_total: 6,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project_id: null,
    existing_project_name: null,
    dismissed: false,
    metadata_suggestions: [
      { field: "licence", value: "CC BY-NC", where: "README.txt", confidence: "high" },
      {
        field: "source_url",
        value: "https://www.thingiverse.com/thing:2493386",
        where: "manual.pdf p2",
        confidence: "medium",
      },
    ],
    provenance: {
      candidates: [
        {
          url: "https://www.thingiverse.com/thing:2493386",
          provider: "thingiverse",
          subject_id: "2493386",
          where: "manual.pdf p2",
          confidence: "medium",
          also_in: [{ where: "README.txt", confidence: "low" }],
        },
      ],
      unresolved: [],
      confidence: "medium",
    },
    cover_candidates: [
      {
        rel_path: "images/b.jpg",
        reason: "largest-image",
        bytes: 900,
        asset_id: "img-large",
        url: "/file/img-large/b.jpg",
        thumb_url: null,
      },
      {
        rel_path: "images/a.jpg",
        reason: "largest-image",
        bytes: 400,
        asset_id: "img-small",
        url: "/file/img-small/a.jpg",
        thumb_url: null,
      },
    ],
    images: [LARGE_IMAGE, SMALL_IMAGE],
    matches: [ONE_PROJECT, SPLIT_TOP_FOLDERS],
  };
  return { ...row, ...overrides };
}

/** The SAME row under another key: a fixture that hardcoded one key would pass
 *  the single-key tests and fail this one. */
function makeRenamedTwin(): BundleDetectItem {
  return makeRow({
    key: "import/Renamed Moai (copy)",
    rel_path: "import/Renamed Moai (copy)",
    title: "Renamed Moai (copy)",
    name_suggestion: "Renamed Moai (copy)",
  });
}

/** A split the server planned: two hoists and the two projects they make, so
 *  the panel has a list to read and one action to offer. */
function makePlan(overrides: Partial<OperationPlan> = {}): OperationPlan {
  return {
    plan_id: "plan-1",
    workflow: "split-and-create",
    target: "import/Renamed Moai",
    steps: [],
    review: {
      workflow: "split-and-create",
      target: "import/Renamed Moai",
      moves: 2,
      creates: 2,
      adopts: 0,
      deletes: 0,
      members: 4,
      groups: [],
      cover: null,
      excluded: [],
      existing_project_id: null,
      existing_project_name: null,
      move_list: [
        { source: "Container/Alpha", target: "Alpha", kind: "dir", action: "hoist" },
        { source: "Container/Beta", target: "Beta", kind: "dir", action: "hoist" },
      ],
      projects: [
        { name: "Alpha", root: "Alpha", members: 2 },
        { name: "Beta", root: "Beta", members: 2 },
      ],
    },
    ...overrides,
  };
}

/** The plan the server issues for one bundle's enrichment: one `enrich` step, no
 *  moves and no creates, because enrichment writes no files. */
function makeEnrichPlan(): OperationPlan {
  const base = makePlan();
  return {
    ...base,
    plan_id: "plan-enrich",
    workflow: "enrich",
    target: "",
    steps: [{ index: 0, op: "enrich", summary: "Read the source page for 1 bundle" }],
    review: {
      ...base.review,
      workflow: "enrich",
      target: "",
      moves: 0,
      creates: 0,
      members: 0,
      move_list: [],
      projects: [],
    },
  };
}

/** One `enrich` operation row on the ledger, for the status under test. */
function makeEnrichOperation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: "op-enrich",
    workflow: "enrich",
    target: "",
    status: "done",
    submitted_by: "operator",
    created_at: "2026-11-26T00:00:00Z",
    started_at: "2026-11-26T00:00:00Z",
    finished_at: "2026-11-26T00:00:01Z",
    error: null,
    undo_state: "none",
    superseded_by: null,
    steps: [
      {
        index: 0,
        op: "enrich",
        status: "done",
        result: null,
        error: null,
        started_at: "2026-11-26T00:00:00Z",
        finished_at: "2026-11-26T00:00:01Z",
      },
    ],
    ...overrides,
  };
}

function createdResponse(key: string, name: string): BundleCreateResponse {
  return {
    created: 1,
    skipped: 0,
    blocked: 0,
    failed: 0,
    results: [
      {
        key,
        status: "created",
        project_id: "project-1",
        name,
        assets_added: 3,
        skipped_assets: 0,
        failed_assets: [],
        reason: null,
      },
    ],
  };
}

function renderPanel(
  row: BundleDetectItem = makeRow(),
  props: Partial<Omit<ComponentProps<typeof GuidedImportPanel>, "row">> = {},
): ReturnType<typeof render> {
  return render(
    <>
      <GuidedImportPanel row={row} canContribute {...props} />
      <Toaster />
    </>,
  );
}

function applyButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Import project" }) as HTMLButtonElement;
}

async function waitForApply(): Promise<void> {
  await waitFor(() => expect(applyButton().disabled).toBe(false));
}

function suggestionBox(field: string): HTMLElement {
  const box = document.querySelector(`[data-suggestion][data-suggestion-field="${field}"]`);
  expect(box).not.toBeNull();
  return box as HTMLElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  resetToasts();
  // The split door plans on mount when it is offered, so every test's read must
  // resolve.
  planOperationMock.mockResolvedValue(makePlan());
});

afterEach(() => {
  cleanup();
});

describe("GuidedImportPanel", () => {
  it("renders nothing and asks for nothing for a viewer", () => {
    const { container } = renderPanel(makeRow(), { canContribute: false });

    expect(container.firstChild).toBeNull();
    expect(planOperationMock).not.toHaveBeenCalled();
  });

  it("shows a PDF-sourced suggestion with its source and keeps it visually distinct from a fact", async () => {
    renderPanel();

    const suggestion = suggestionBox("source_url");
    expect(suggestion.querySelector("[data-suggestion-source]")?.textContent).toBe(
      "Suggested from manual.pdf p2",
    );
    expect(suggestion.querySelector("[data-suggestion-source]")?.textContent).toContain(
      "manual.pdf p2",
    );
    expect(suggestion.querySelector("[data-suggestion-tag]")?.textContent).toBe("Suggested");
    expect(suggestion.querySelector("[data-suggestion-value]")?.textContent).toBe(
      "https://www.thingiverse.com/thing:2493386",
    );

    const facts = document.querySelector("[data-guided-facts]") as HTMLElement;
    expect(facts).not.toBeNull();
    expect(facts.textContent).toContain("The app measured:");

    // The distinction is visual: a suggestion is dashed, a measured fact is not.
    expect(suggestion.className).toContain("border-dashed");
    expect(facts.className).not.toContain("border-dashed");
  });

  it("uses an accepted source as the project's source_url", async () => {
    createBundlesMock.mockResolvedValue(createdResponse("import/Renamed Moai", "Renamed Moai"));
    renderPanel();
    await waitForApply();

    fireEvent.change(document.querySelector("[data-guided-source-url]") as HTMLElement, {
      target: { value: "https://example.test/thing/1" },
    });
    fireEvent.click(document.querySelector("[data-guided-accept-source]") as HTMLElement);
    fireEvent.click(applyButton());

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    const [items] = createBundlesMock.mock.calls[0];
    expect(items[0].metadata).toEqual({ source_url: "https://example.test/thing/1" });
  });

  it("shows a suggestion as accepted after the accept click", async () => {
    renderPanel();

    fireEvent.click(
      suggestionBox("licence").querySelector("[data-suggestion-accept]") as HTMLElement,
    );

    const accepted = suggestionBox("licence");
    expect(accepted.getAttribute("data-suggestion-accepted")).toBe("true");
    expect(accepted.textContent).toContain("Accepted");
    expect(accepted.querySelector("[data-suggestion-accept]")).toBeNull();
  });

  it("writes only the suggestions the operator accepted", async () => {
    createBundlesMock.mockResolvedValue(createdResponse("import/Renamed Moai", "Renamed Moai"));
    renderPanel();
    await waitForApply();

    // Nothing pre-accepted: an empty metadata map means nothing outside it is written.
    fireEvent.click(applyButton());
    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    expect(createBundlesMock).toHaveBeenCalledWith([
      {
        key: "import/Renamed Moai",
        name: "Renamed Moai",
        shape: "one-project",
        metadata: {},
      },
    ]);

    cleanup();
    createBundlesMock.mockClear();
    renderPanel();
    await waitForApply();

    fireEvent.click(suggestionBox("licence").querySelector("[data-suggestion-accept]") as HTMLElement);
    fireEvent.click(applyButton());

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    expect(createBundlesMock).toHaveBeenCalledWith([
      {
        key: "import/Renamed Moai",
        name: "Renamed Moai",
        shape: "one-project",
        metadata: { licence: "CC BY-NC" },
      },
    ]);
  });

  it("applies the operator's cover and shape, not the recommendation", async () => {
    const row = makeRow({
      detector: "engine:git-project",
      metadata_shape: "git-project",
      matches: [
        { ...ONE_PROJECT, shape: "git-project", rule: "engine:git-project" },
        ONE_PROJECT,
      ],
    });
    createBundlesMock.mockResolvedValue(createdResponse(row.key, row.name_suggestion));
    renderPanel(row);
    await waitForApply();

    fireEvent.click(document.querySelector('[data-guided-shape="one-project"]') as HTMLElement);
    expect(
      (document.querySelector('[data-guided-shape="one-project"]') as HTMLInputElement).checked,
    ).toBe(true);
    fireEvent.click(document.querySelector('[data-guided-cover-asset="img-small"]') as HTMLElement);
    await waitForApply();

    fireEvent.click(applyButton());

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    const [items] = createBundlesMock.mock.calls[0];
    expect(items).toEqual([
      {
        key: "import/Renamed Moai",
        name: "Renamed Moai",
        shape: "one-project",
        cover: { mode: "asset", asset_id: "img-small" },
        metadata: {},
      },
    ]);
    // Guided never claims AUTO: that band is the engine's alone.
    expect(items[0]).not.toHaveProperty("auto");
  });

  it("hands a split to the split door and never calls createBundles", async () => {
    renderPanel();
    await waitForApply();

    // Nothing is planned while the row's shape is one the server can enact.
    expect(planOperationMock).not.toHaveBeenCalled();

    fireEvent.click(document.querySelector('[data-guided-shape="split-top-folders"]') as HTMLElement);

    expect(screen.getByText(/Needs arranging:/)).toBeTruthy();
    // The panel that owns the WRITE is the one that plans: this panel no longer
    // fetches a second copy of the same move list.
    await waitFor(() =>
      expect(planOperationMock).toHaveBeenCalledWith({
        workflow: "split-and-create",
        target: "import/Renamed Moai",
        params: { shape: "split-top-folders" },
      }),
    );
    await waitFor(() => expect(document.querySelectorAll("[data-split-move]")).toHaveLength(2));
    const moves = Array.from(document.querySelectorAll("[data-split-move]")).map(
      node => node.textContent,
    );
    expect(moves[0]).toContain('move "Container/Alpha" -> "Alpha"');
    expect(moves[1]).toContain('move "Container/Beta" -> "Beta"');

    // The door that CAN move the bytes is right beside the disabled import.
    expect(document.querySelector("[data-split-action-button]")).toBeTruthy();
    expect(applyButton().disabled).toBe(true);
    expect(createBundlesMock).not.toHaveBeenCalled();
  });

  it("says already created on a second run", async () => {
    const onLanded = vi.fn();
    createBundlesMock.mockResolvedValue({
      created: 0,
      skipped: 1,
      blocked: 0,
      failed: 0,
      results: [
        {
          key: "import/Renamed Moai",
          status: "skipped",
          project_id: null,
          name: "Renamed Moai",
          assets_added: 0,
          skipped_assets: 0,
          failed_assets: [],
          reason: "already_created",
        },
      ],
    });
    renderPanel(makeRow(), { onLanded });
    await waitForApply();

    fireEvent.click(applyButton());

    await waitFor(() =>
      expect(document.querySelector("[data-guided-already]")?.textContent).toBe(
        "Already created as Renamed Moai",
      ),
    );
    expect(getToasts().some(toast => toast.message === "Already created as Renamed Moai")).toBe(true);
    expect(onLanded).not.toHaveBeenCalled();
  });

  it("forwards a 401 to the host", async () => {
    const onUnauthorized = vi.fn();
    createBundlesMock.mockRejectedValue(new UnauthorizedError());
    renderPanel(makeRow(), { onUnauthorized });
    await waitForApply();

    fireEvent.click(applyButton());

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(document.querySelector("[data-guided-failure]")).toBeNull();
  });

  it("enrich runs the operations walk and shows what the page filled", async () => {
    const onChanged = vi.fn();
    const response: BundleEnrichResponse = {
      fetched: 1,
      cached: 0,
      refused: 0,
      unavailable: 0,
      unknown: 0,
      results: [
        {
          key: "import/Renamed Moai",
          status: "fetched",
          source_url: "https://www.thingiverse.com/thing:2493386",
          creator: "moai_maker",
          licence: "CC BY-NC",
          licence_url: "https://creativecommons.org/licenses/by-nc/4.0/",
          filled: ["creator", "licence"],
          fetched_at: "2026-11-26T00:00:00Z",
          reason: null,
        },
      ],
    };
    planOperationMock.mockResolvedValue(makeEnrichPlan());
    // Queued, then done: the panel's one leg is the whole plan/submit/poll walk
    // now, and a submit that answers a terminal row would leave it unproven.
    submitOperationMock.mockResolvedValue(makeEnrichOperation({ status: "queued" }));
    const finished = makeEnrichOperation({
      steps: [
        {
          index: 0,
          op: "enrich",
          status: "done",
          result: response as unknown as Record<string, unknown>,
          error: null,
          started_at: "2026-11-26T00:00:00Z",
          finished_at: "2026-11-26T00:00:01Z",
        },
      ],
    });
    getOperationMock.mockResolvedValue(finished);
    renderPanel(makeRow(), { onChanged });

    fireEvent.click(document.querySelector("[data-guided-enrich]") as HTMLElement);

    await waitFor(() =>
      expect(document.querySelector("[data-guided-enrich-line]")?.textContent).toBe(
        "Filled creator, licence from https://www.thingiverse.com/thing:2493386",
      ),
    );
    // The row's key is the plan's PARAM, with an empty target: enrichment names
    // its subjects in `params`, and this leg carries no cookie and no refresh.
    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "enrich",
      target: "",
      params: { keys: ["import/Renamed Moai"], refresh: false },
    });
    expect(submitOperationMock).toHaveBeenCalledWith({
      planId: "plan-enrich",
      idempotencyKey: expect.any(String),
    });
    expect(getOperationMock).toHaveBeenCalledWith("op-enrich");
    // The host hears about the run once, after it finished - not at submit time.
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("enrich reports a failed run's sentence as a failure", async () => {
    planOperationMock.mockResolvedValue(makeEnrichPlan());
    submitOperationMock.mockResolvedValue(
      makeEnrichOperation({
        status: "failed",
        error: "The source page refused to be read.",
        steps: [
          {
            index: 0,
            op: "enrich",
            status: "failed",
            result: null,
            error: "The source page refused to be read.",
            started_at: "2026-11-26T00:00:00Z",
            finished_at: "2026-11-26T00:00:01Z",
          },
        ],
      }),
    );
    renderPanel();

    fireEvent.click(document.querySelector("[data-guided-enrich]") as HTMLElement);

    // A run that produced no report is a FAILURE, not a quiet "filled nothing":
    // the sentence goes where every other failed write goes.
    await waitFor(() =>
      expect(document.querySelector("[data-guided-failure]")?.textContent).toBe(
        "The source page refused to be read.",
      ),
    );
    expect(document.querySelector("[data-guided-enrich-line]")).toBeNull();
  });

  it("runs on the key it was handed, for both twins", async () => {
    for (const row of [makeRow(), makeRenamedTwin()]) {
      createBundlesMock.mockResolvedValue(createdResponse(row.key, row.name_suggestion));
      renderPanel(row);
      await waitForApply();

      fireEvent.click(applyButton());

      await waitFor(() =>
        expect(createBundlesMock).toHaveBeenCalledWith([
          expect.objectContaining({ key: row.key }),
        ]),
      );
      cleanup();
      createBundlesMock.mockClear();
    }
  });
});
