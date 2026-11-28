// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { OperationPlan, OperationReview as Review } from "../../lib/api/operations";
import OperationReview from "./OperationReview";

/** A review with every count zeroed, so each test states only what it renders. */
function makeReview(overrides: Partial<Review> = {}): Review {
  return {
    workflow: "create-project",
    target: "Container",
    moves: 0,
    creates: 0,
    adopts: 0,
    deletes: 0,
    members: 0,
    groups: [],
    cover: null,
    excluded: [],
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
    workflow: "create-project",
    target: "Container",
    steps: [],
    review: makeReview(),
    ...overrides,
  };
}

/** The step summaries as rendered, in document order. */
function stepSummaries(container: HTMLElement): (string | null)[] {
  return Array.from(container.querySelectorAll("[data-review-step-summary]")).map(
    node => node.textContent,
  );
}

afterEach(cleanup);

describe("OperationReview", () => {
  it("lists a create-project plan's steps in index order with its facts", () => {
    // Catches a review that trusts the payload's step order and a fact list that
    // loses the machine word naming each count.
    const plan = makePlan({
      steps: [
        { index: 1, op: "register", summary: "register the new project's files" },
        { index: 0, op: "create-project", summary: "create a project named 'New'" },
      ],
      review: makeReview({
        members: 4,
        groups: [{ name: "Alpha", members: 4, models: 4 }],
      }),
    });
    const { container } = render(
      <OperationReview
        plan={plan}
        actionLabel="Create project"
        busy={false}
        onConfirm={vi.fn()}
        dataPrefix="review"
      />,
    );

    expect(stepSummaries(container)).toEqual([
      "create a project named 'New'",
      "register the new project's files",
    ]);
    const files = container.querySelector('[data-review-fact="files"]');
    expect(files?.textContent).toBe("4 files");
    expect(files?.getAttribute("data-review-fact-text")).toBe("true");
    expect(container.querySelector('[data-review-fact="groups"]')?.textContent).toBe("1 group");

    // Exactly ONE action: a second confirm over one plan is a second queue entry.
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].textContent).toBe("Create project");
  });

  it("renders a split's moves, projects and its one action", () => {
    // Catches a split review that drops either half of the intent - the hoists
    // and the creates are approved in one click, so both must be listed.
    const plan = makePlan({
      workflow: "split-and-create",
      steps: [{ index: 0, op: "move", summary: "hoist three folders" }],
      review: makeReview({
        workflow: "split-and-create",
        moves: 3,
        members: 27,
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
      }),
    });
    const { container } = render(
      <OperationReview
        plan={plan}
        actionLabel="Split into 3 projects"
        busy={false}
        onConfirm={vi.fn()}
        dataPrefix="review"
      />,
    );

    expect(container.querySelectorAll("[data-review-move]")).toHaveLength(3);
    expect(screen.getByText('move "Container/Alpha" -> "Alpha"')).toBeTruthy();
    expect(screen.getAllByText("hoist")).toHaveLength(3);
    expect(container.querySelectorAll("[data-review-project]")).toHaveLength(3);
    expect(screen.getByText("Alpha from 9 files")).toBeTruthy();
    expect(screen.getByRole("button").textContent).toBe("Split into 3 projects");
  });

  it("renders an adopt's role census and its one action", () => {
    // Catches a census that prints every zero, a missing `text` -> `document`
    // reading, and a count that is not the plan's.
    const plan = makePlan({
      workflow: "adopt",
      steps: [{ index: 0, op: "adopt", summary: "add the grown files" }],
      review: makeReview({
        workflow: "adopt",
        members: 171,
        groups: Array.from({ length: 9 }, (_unused, index) => ({
          name: `G${index + 1}`,
          members: 19,
          models: 18,
        })),
        by_role: { model: 160, image: 7, text: 1, archive: 2, other: 1 },
      }),
    });
    const { container } = render(
      <OperationReview
        plan={plan}
        actionLabel="Add 171 files in 9 groups"
        busy={false}
        onConfirm={vi.fn()}
        dataPrefix="review"
      />,
    );

    expect(container.querySelector('[data-review-role="model"]')?.textContent).toBe("160 models");
    expect(container.querySelector('[data-review-role="image"]')?.textContent).toBe("7 images");
    expect(container.querySelector('[data-review-role="text"]')?.textContent).toBe("1 document");
    expect(container.querySelector('[data-review-role="text"]')?.getAttribute("data-review-count")).toBe(
      "1",
    );
    // No zero-role line is written for a role the plan carries none of.
    expect(container.querySelector('[data-review-role="other"]')?.textContent).toBe("1 other");
    expect(screen.getByRole("button").textContent).toBe("Add 171 files in 9 groups");
  });

  it("renders an extract's groups with one action", () => {
    // Catches a group line that words the counts differently from the extract
    // door the operator already knows.
    const plan = makePlan({
      workflow: "extract",
      steps: [{ index: 0, op: "extract", summary: "extract the archive" }],
      review: makeReview({
        workflow: "extract",
        members: 6,
        groups: [{ name: "01_Wizard", members: 14, models: 12 }],
      }),
    });
    const { container } = render(
      <OperationReview
        plan={plan}
        actionLabel="Extract 6 files"
        busy={false}
        onConfirm={vi.fn()}
        dataPrefix="review"
      />,
    );

    expect(container.querySelector('[data-review-group-name="01_Wizard"]')?.textContent).toBe(
      "01_Wizard - 14 files, 12 models",
    );
    expect(screen.getByRole("button").textContent).toBe("Extract 6 files");
  });

  it("renders the cover and the existing-project lines for extract-and-create", () => {
    // Catches a review that drops the two facts the operator needs to recognise
    // the archive: which image becomes the cover, and which project already owns it.
    const plan = makePlan({
      workflow: "extract-and-create",
      steps: [{ index: 0, op: "extract", summary: "extract the archive" }],
      review: makeReview({
        workflow: "extract-and-create",
        members: 6,
        creates: 1,
        cover: { asset_id: "asset-1", rel_path: "01_Wizard/cover.png", reason: "first image" },
        existing_project_name: "Old Tower",
      }),
    });
    const { container } = render(
      <OperationReview
        plan={plan}
        actionLabel="Extract and create 'Old Tower' from 6 files"
        busy={false}
        onConfirm={vi.fn()}
        dataPrefix="review"
      />,
    );

    expect(container.querySelector("[data-review-cover]")?.textContent).toBe(
      'The project\'s cover is "01_Wizard/cover.png".',
    );
    expect(container.querySelector("[data-review-existing]")?.textContent).toBe(
      'The project "Old Tower" already exists, so there is nothing to create.',
    );
  });

  it("renders no facts container for a library scan but still lists its steps and exclusions", () => {
    // Catches a container printed empty (a scan measures no counts at plan time)
    // and an excluded member lost with its reason.
    const plan = makePlan({
      workflow: "library-scan",
      target: "",
      steps: [{ index: 0, op: "scan", summary: "walk the whole library" }],
      review: makeReview({
        workflow: "library-scan",
        target: "",
        members: 0,
        groups: [],
        excluded: [{ path: "Models/skip.psd", reason: "does not hold a model" }],
      }),
    });
    const { container } = render(
      <OperationReview
        plan={plan}
        actionLabel="Scan the whole library"
        busy={false}
        onConfirm={vi.fn()}
        dataPrefix="review"
      />,
    );

    expect(container.querySelector("[data-review-facts]")).toBeNull();
    expect(stepSummaries(container)).toEqual(["walk the whole library"]);
    expect(screen.getByText("Models/skip.psd: does not hold a model")).toBeTruthy();
  });

  it("says Nothing is excluded for an empty list and the server's reason verbatim otherwise", () => {
    // Catches an empty exclusion list reading as a missing one, and a paraphrase
    // of a reason the client did not decide.
    const empty = render(
      <OperationReview
        plan={makePlan()}
        actionLabel={null}
        busy={false}
        onConfirm={vi.fn()}
        dataPrefix="review"
      />,
    );
    const nothing = empty.container.querySelector("[data-review-nothing-excluded]");
    expect(nothing?.textContent).toBe("Nothing is excluded");
    expect(empty.container.querySelector("[data-review-excluded]")).toBeNull();
    empty.unmount();

    const refused = render(
      <OperationReview
        plan={makePlan({
          review: makeReview({
            excluded: [{ path: "Container/notes.psd", reason: "does not hold a model" }],
          }),
        })}
        actionLabel={null}
        busy={false}
        onConfirm={vi.fn()}
        dataPrefix="review"
      />,
    );
    expect(refused.container.querySelector("[data-review-excluded]")?.textContent).toBe(
      "Container/notes.psd: does not hold a model",
    );
    expect(refused.container.querySelector("[data-review-nothing-excluded]")).toBeNull();
  });

  it("renders no button for a plan with nothing to approve", () => {
    // Catches a confirm offered over a zero-step plan: there is nothing to submit,
    // so there must be nothing to press.
    render(
      <OperationReview
        plan={makePlan()}
        actionLabel={null}
        busy={false}
        onConfirm={vi.fn()}
        dataPrefix="review"
      />,
    );

    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("refuses the action while a run is busy", () => {
    // Catches a double click that queues two operations over one plan.
    const onConfirm = vi.fn();
    render(
      <OperationReview
        plan={makePlan()}
        actionLabel="Create project"
        busy
        onConfirm={onConfirm}
        dataPrefix="review"
      />,
    );

    expect((screen.getByRole("button", { name: "Create project" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
});
