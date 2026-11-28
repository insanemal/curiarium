import { describe, expect, it } from "vitest";

import type {
  Operation,
  OperationExcluded,
  OperationPlan,
  OperationReview,
  OperationStep,
  OperationUndo,
} from "../../lib/api/operations";
import {
  actionLabel,
  doneLine,
  excludedLine,
  extractActionLabel,
  extractArchiveHomeLine,
  extractCoverLine,
  extractDoneLine,
  extractExistingLine,
  extractGroupLine,
  extractResult,
  extractSummaryLine,
  extractUndoLine,
  inFlightFor,
  operationActionLabel,
  operationMoveLine,
  operationStatusLine,
  planSteps,
  projectLine,
  reviewFacts,
  stateLine,
  statusLabel,
  stepLine,
  undoLine,
  workflowLabel,
} from "./operationView";

/** A review with every count zeroed, so each test overrides only what it reads. */
function review(overrides: Partial<OperationReview> = {}): OperationReview {
  return {
    workflow: "split-and-create",
    target: "Widget",
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

const namedGroups = (count: number): OperationReview["groups"] =>
  Array.from({ length: count }, (_, index) => ({
    name: `Group ${index + 1}`,
    members: 19,
    models: 19,
  }));

/** The one step a `mount-import` operation plans, carrying the report the walk
 *  writes: the fields each done-line case reads are overridden per test. */
function mountImportStep(result: Record<string, unknown> | null): OperationStep {
  return {
    index: 0,
    op: "mount-import",
    status: "done",
    result,
    error: null,
    started_at: null,
    finished_at: null,
  };
}

/** One finished operation, with the create step the runner wrote. */
function doneOperation(status: string, name: string | null): OperationStep[] {
  return [
    {
      index: 0,
      op: "create-project",
      status: "done",
      result: { project_id: "p1", name, status, created: status === "created" },
      error: null,
      started_at: null,
      finished_at: null,
    },
  ];
}

function planWith(projects: OperationPlan["review"]["projects"]): OperationPlan {
  return {
    plan_id: "plan-1",
    workflow: "extract-and-create",
    target: "import/Tower.zip",
    steps: [],
    review: review({
      workflow: "extract-and-create",
      target: "import/Tower.zip",
      projects,
    }),
  };
}

/** One operation row with only the fields an assertion reads set. */
function makeOperation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: "op-1",
    workflow: "split-and-create",
    target: "Widget",
    status: "done",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
    error: null,
    undo_state: "available",
    superseded_by: null,
    steps: [],
    ...overrides,
  };
}

/** One step of a running operation, with only the two fields the narration may
 *  read set to anything interesting. */
function step(op: string, status: string): OperationStep {
  return {
    index: 0,
    op,
    status,
    result: null,
    error: null,
    started_at: null,
    finished_at: null,
  };
}

describe("extract-and-create words", () => {
  function extractReview(overrides: Partial<OperationReview> = {}): OperationReview {
    return {
      workflow: "extract-and-create",
      target: "import/Tower.zip",
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
        {
          source: "import/Tower.zip",
          target: "import/Tower/_source/Tower.zip",
          kind: "file",
          action: "file-archive",
        },
      ],
      projects: [{ name: "Tower", root: "import/Tower", members: 20 }],
      ...overrides,
    };
  }

  it("names the action for its outcome, with the plan's own name and count", () => {
    // Catches a button named for the subsystem, or a count the client estimated:
    // the plan measured both, so the promise is the review's.
    expect(extractActionLabel(extractReview())).toBe(
      "Extract and create 'Tower' from 20 files",
    );
    expect(extractActionLabel(extractReview({ members: 1 }))).toBe(
      "Extract and create 'Tower' from 1 file",
    );
    // A plan that named no project still promises the one project it would make.
    expect(extractActionLabel(extractReview({ projects: [] }))).toBe(
      "Extract and create a project from 20 files",
    );
  });

  it("says how much the plan would write", () => {
    expect(extractSummaryLine(extractReview())).toBe("Would write 20 files.");
    expect(extractSummaryLine(extractReview({ members: 1 }))).toBe("Would write 1 file.");
  });

  it("renders the server's empty group name as loose files, never as a folder", () => {
    // The `""` bucket is the archive's own root files: naming it a directory
    // would point the operator at a folder that does not exist.
    expect(extractGroupLine({ name: "01_Wizard", members: 14, models: 12 })).toBe(
      "01_Wizard - 14 files, 12 models",
    );
    expect(extractGroupLine({ name: "", members: 6, models: 1 })).toBe(
      "loose files - 6 files, 1 model",
    );
    expect(extractGroupLine({ name: "", members: 1, models: 1 })).toBe(
      "loose files - 1 file, 1 model",
    );
  });

  it("names the archive's new home from the server's own move, and nothing when it has none", () => {
    // A plan with no move is an archive already in its place: the client must not
    // assemble a target for it.
    expect(extractArchiveHomeLine(extractReview())).toBe(
      'The archive moves to "import/Tower/_source/Tower.zip".',
    );
    expect(extractArchiveHomeLine(extractReview({ move_list: [] }))).toBeNull();
  });

  it("names the planned cover by path, and says nothing when the plan named none", () => {
    expect(extractCoverLine({ asset_id: null, rel_path: "a/b.stl", reason: "first model" })).toBe(
      'The project\'s cover is "a/b.stl".',
    );
    expect(extractCoverLine({ asset_id: null, rel_path: null, reason: "none" })).toBeNull();
    expect(extractCoverLine(null)).toBeNull();
  });

  it("says a zero-step plan already has its project, by the server's name", () => {
    expect(
      extractExistingLine(
        extractReview({ creates: 0, projects: [], existing_project_name: "Old Tower" }),
      ),
    ).toBe('The project "Old Tower" already exists, so there is nothing to create.');
    expect(
      extractExistingLine(extractReview({ creates: 0, projects: [], existing_project_name: null })),
    ).toBe("This archive already has a project, so there is nothing to create.");
  });

  it("reads the done line from the create step's own result", () => {
    // Catches a done line read from the PLAN: the runner can lose the race at run
    // time, and claiming a create that never happened is the one lie this line
    // must not tell.
    const plan = planWith([{ name: "Tower", root: "import/Tower", members: 20 }]);
    const operation = makeOperation({
      workflow: "extract-and-create",
      target: "import/Tower.zip",
      steps: doneOperation("created", "Tower"),
    });
    expect(extractDoneLine(operation, plan)).toBe("Done: project 'Tower' created");

    // The race: the step's own status is the authority, not the plan's promise.
    expect(
      extractDoneLine({ ...operation, steps: doneOperation("already_created", "Tower") }, plan),
    ).toBe("Done: project 'Tower' already existed - nothing new was created");

    // A step that named no project falls back to the plan's name, so the line
    // still says which project the dialog is about.
    expect(extractDoneLine({ ...operation, steps: doneOperation("created", null) }, plan)).toBe(
      "Done: project 'Tower' created",
    );
  });

  it("says an undo leaves the extracted files where they are", () => {
    // The residue is documented, so the toast must not imply the tree was erased.
    const answer: OperationUndo = {
      operation_id: "op-7",
      undone: true,
      project_id: null,
      project_name: null,
      moves: 1,
      projects_removed: ["Tower"],
    };
    expect(extractUndoLine(answer)).toBe(
      "Undone: 1 move folded back, removed 1 project. The extracted files stay in the library.",
    );
    expect(extractUndoLine({ ...answer, moves: null, projects_removed: null })).toBe(
      "Undone: 0 moves folded back, no projects removed. The extracted files stay in the library.",
    );
    // `undone: false` means the server changed nothing, and the line says so.
    expect(extractUndoLine({ ...answer, undone: false })).toBe("Nothing was undone.");
  });
});

describe("operationMoveLine / operationActionLabel / excludedLine", () => {
  it("quotes both paths of a move, so a path with spaces still reads as two", () => {
    // Catches dropping either pair of quotes: with the paths bare, `a b/c.stl ->
    // a b/d/c.stl` has four possible readings of where the source ends.
    expect(
      operationMoveLine({
        source: "Moai statue/a.stl",
        target: "Moai statue/Fates End/a.stl",
        kind: "file",
        action: "move",
      }),
    ).toBe('move "Moai statue/a.stl" -> "Moai statue/Fates End/a.stl"');
  });

  it("reads each known action token as its own word, and an unknown one verbatim", () => {
    // Catches a formatter that invents words for tokens it does not know, or
    // renders one as a dash: a newer server's token is still a real move, and a
    // dash would make it read as an absent one. The four known tokens are the
    // only ones the client may translate.
    expect(operationActionLabel("hoist")).toBe("hoist");
    expect(operationActionLabel("move")).toBe("move");
    expect(operationActionLabel("file-archive")).toBe("file archive");
    expect(operationActionLabel("rename")).toBe("rename");
    expect(operationActionLabel("retire-containerish")).toBe("retire-containerish");
  });

  it("renders an exclusion as the server's own reason beside its path, verbatim", () => {
    // Catches a sentence rebuilt or paraphrased here: the reason is the server's
    // measurement of the folder it walked, and a client that rewrote it would
    // print a second explanation of one exclusion.
    const entry: OperationExcluded = {
      path: "Moai statue/notes.psd",
      reason: "does not hold a model and is not a part of any group",
    };
    expect(excludedLine(entry)).toBe(
      "Moai statue/notes.psd: does not hold a model and is not a part of any group",
    );
  });
});

describe("operationStatusLine / stepLine", () => {
  it("narrates each known op from its own words, in every status", () => {
    // Catches a narration driven by a count, or one whose four statuses collapse:
    // the popup must say what the runner is DOING, and `waiting`, `doing`,
    // `done` and `refused` are four different things to the operator.
    expect(operationStatusLine(step("move", "done"))).toBe("moved the folders");
    expect(operationStatusLine(step("move", "running"))).toBe("moving the folders\u2026");
    expect(operationStatusLine(step("move", "failed"))).toBe("the move was refused");
    expect(operationStatusLine(step("move", "pending"))).toBe("waiting to move the folders");

    expect(operationStatusLine(step("create-project", "done"))).toBe("created a project");
    expect(operationStatusLine(step("create-project", "running"))).toBe(
      "creating a project\u2026",
    );
    expect(operationStatusLine(step("create-project", "failed"))).toBe("the create was refused");
    expect(operationStatusLine(step("create-project", "pending"))).toBe(
      "waiting to create a project",
    );

    expect(operationStatusLine(step("verify-members", "done"))).toBe(
      "checked the project's files",
    );
    expect(operationStatusLine(step("verify-members", "running"))).toBe(
      "checking the project's files\u2026",
    );
    expect(operationStatusLine(step("verify-members", "failed"))).toBe(
      "the project's files did not match",
    );
    expect(operationStatusLine(step("verify-members", "pending"))).toBe(
      "waiting to check the files",
    );

    expect(operationStatusLine(step("extract", "done"))).toBe("extracted the archive");
    expect(operationStatusLine(step("extract", "running"))).toBe("extracting the archive\u2026");
    expect(operationStatusLine(step("extract", "failed"))).toBe("the extraction was refused");
    expect(operationStatusLine(step("extract", "pending"))).toBe(
      "waiting to extract the archive",
    );

    expect(operationStatusLine(step("register", "done"))).toBe("registered the extracted files");
    expect(operationStatusLine(step("register", "running"))).toBe(
      "registering the extracted files\u2026",
    );
    expect(operationStatusLine(step("register", "failed"))).toBe(
      "the files could not be registered",
    );
    expect(operationStatusLine(step("register", "pending"))).toBe(
      "waiting to register the files",
    );

    expect(operationStatusLine(step("mount-import", "done"))).toBe(
      "walked the mounted folder and registered what it holds",
    );
    expect(operationStatusLine(step("mount-import", "running"))).toBe(
      "walking the mounted folder\u2026",
    );
    expect(operationStatusLine(step("mount-import", "failed"))).toBe(
      "the mounted folder could not be walked",
    );
    expect(operationStatusLine(step("mount-import", "pending"))).toBe(
      "waiting to walk the mounted folder",
    );
  });

  it("shows an unknown op's two tokens verbatim rather than inventing a word", () => {
    // A newer server's step is still a real step, so it is named as the server
    // named it: the client has no word for an op it does not know.
    expect(operationStatusLine(step("reindex", "running"))).toBe("reindex running");
  });

  it("delegates stepLine to the step narration", () => {
    expect(stepLine(step("move", "done"))).toBe(operationStatusLine(step("move", "done")));
  });
});

describe("extractResult", () => {
  it("returns null when the extract step has not reported, not a zero", () => {
    // A row that has not reached the step has no report; reading it as a
    // zero-file extraction would claim the archive was empty.
    expect(extractResult(makeOperation({ steps: [] }))).toBeNull();
    expect(extractResult(makeOperation({ steps: [step("extract", "running")] }))).toBeNull();
  });

  it("re-assembles the step's report into the deleted route's shape", () => {
    const operation = makeOperation({
      target: "import/Tower.zip",
      steps: [
        {
          index: 0,
          op: "extract",
          status: "done",
          result: {
            path: "import/Tower.zip",
            destination: "import/Tower",
            files: 6,
            bytes: 120,
            already_present: false,
            scan_started: false,
          },
          error: null,
          started_at: null,
          finished_at: null,
        },
      ],
    });
    expect(extractResult(operation)).toEqual({
      path: "import/Tower.zip",
      destination: "import/Tower",
      files: 6,
      bytes: 120,
      already_present: false,
      scan_started: false,
    });
  });
});

describe("actionLabel", () => {
  it("labels create-project for its outcome", () => {
    expect(actionLabel("create-project", review())).toBe("Create project");
  });

  it("labels split-and-create from the plan's project count", () => {
    expect(
      actionLabel("split-and-create", review({ projects: [{ name: "a", root: "a", members: 1 }] })),
    ).toBe("Split into 1 project");
    expect(
      actionLabel(
        "split-and-create",
        review({
          projects: [
            { name: "a", root: "a", members: 1 },
            { name: "b", root: "b", members: 1 },
            { name: "c", root: "c", members: 1 },
          ],
        }),
      ),
    ).toBe("Split into 3 projects");
  });

  it("labels adopt from the plan's files and groups, skipping the root bucket", () => {
    expect(
      actionLabel("adopt", review({ members: 171, groups: namedGroups(9) })),
    ).toBe("Add 171 files in 9 groups");
    expect(
      actionLabel("adopt", review({ members: 3, groups: [{ name: "", members: 2, models: 2 }, { name: "Core", members: 1, models: 1 }] })),
    ).toBe("Add 3 files in 1 group");
  });

  it("labels extract from the plan's member count", () => {
    expect(actionLabel("extract", review({ members: 6 }))).toBe("Extract 6 files");
  });

  it("labels extract-and-create exactly as its own action label does", () => {
    const target = review({
      workflow: "extract-and-create",
      members: 20,
      projects: [{ name: "Tower", root: "import/Tower", members: 20 }],
    });
    expect(actionLabel("extract-and-create", target)).toBe(extractActionLabel(target));
  });

  it("labels the mount import by the folder it walks, not by a count", () => {
    expect(actionLabel("mount-import", review({ workflow: "mount-import", target: "/mnt/models" }))).toBe(
      "Walk the mounted folder",
    );
  });

  it("labels library-scan for the whole library or one folder", () => {
    expect(actionLabel("library-scan", review({ target: "" }))).toBe("Scan the whole library");
    expect(actionLabel("library-scan", review({ target: "Tools" }))).toBe("Scan 'Tools'");
  });

  it("labels import-link for the page", () => {
    expect(actionLabel("import-link", review())).toBe("Import from the page");
  });

  it("labels import-zip from the plan's member count", () => {
    expect(actionLabel("import-zip", review({ members: 3 }))).toBe(
      "Import 3 files from the zip",
    );
  });

  it("labels enrich from the params keys, the working set the review cannot carry", () => {
    expect(actionLabel("enrich", review(), { keys: ["a", "b", "c"] })).toBe(
      "Read 3 vendor pages",
    );
  });

  it("falls back to a count-free sentence when enrich has no keys", () => {
    // Catches an invented count: enrich adds no files, so its `members` is 0 and
    // a number read from the review would be a fabrication.
    expect(actionLabel("enrich", review())).toBe("Read the source page");
    expect(actionLabel("enrich", review(), { keys: [] })).toBe("Read the source page");
  });

  it("names an unknown workflow without leaking a bare key or inventing a word", () => {
    expect(actionLabel("future-workflow", review())).toBe("Run future-workflow");
  });
});

describe("actionLabel singular and plural", () => {
  it("derives extract's noun from its count", () => {
    expect(actionLabel("extract", review({ members: 1 }))).toBe("Extract 1 file");
    expect(actionLabel("extract", review({ members: 3 }))).toBe("Extract 3 files");
  });

  it("derives import-zip's noun from its count", () => {
    expect(actionLabel("import-zip", review({ members: 1 }))).toBe("Import 1 file from the zip");
    expect(actionLabel("import-zip", review({ members: 3 }))).toBe(
      "Import 3 files from the zip",
    );
  });

  it("derives adopt's noun from its count", () => {
    expect(actionLabel("adopt", review({ members: 1 }))).toBe("Add 1 file");
    expect(actionLabel("adopt", review({ members: 3 }))).toBe("Add 3 files");
  });

  it("derives enrich's noun from the keys' count", () => {
    expect(actionLabel("enrich", review(), { keys: ["a"] })).toBe("Read 1 vendor page");
    expect(actionLabel("enrich", review(), { keys: ["a", "b", "c"] })).toBe(
      "Read 3 vendor pages",
    );
  });
});

describe("reviewFacts", () => {
  it("prints the plan's counts in one order and skips the zeros", () => {
    const facts = reviewFacts(
      review({
        members: 7,
        groups: [{ name: "a", members: 4, models: 4 }],
        moves: 2,
        projects: [
          { name: "a", root: "a", members: 4 },
          { name: "b", root: "b", members: 3 },
        ],
        adopts: 1,
        deletes: 3,
        existing_project_name: "Old Tower",
      }),
    );
    expect(facts).toEqual([
      "7 files",
      "1 group",
      "2 moves",
      "2 projects",
      "1 adopted file",
      "3 deletions",
      'Existing project "Old Tower"',
    ]);
  });

  it("says nothing for a review that carries no counts", () => {
    expect(reviewFacts(review())).toEqual([]);
  });

  it("never prints `creates`, which means two different things", () => {
    // `creates` counts projects for one workflow and asset rows for another, so a
    // line built from it would be two facts wearing one number.
    expect(reviewFacts(review({ creates: 99 }))).toEqual([]);
  });
});

describe("extractGroupLine / projectLine", () => {
  it("reads a project's name and its member count, singular or plural", () => {
    // The wording the split door used before it moved here, so the shared review
    // and the door cannot disagree about the same project.
    expect(projectLine({ name: "Alpha", root: "Alpha", members: 9 })).toBe("Alpha from 9 files");
    expect(projectLine({ name: "Solo", root: "Solo", members: 1 })).toBe("Solo from 1 file");
  });
});

describe("planSteps", () => {
  it("reads the plan's summaries in index order, whatever order they arrived in", () => {
    const plan: OperationPlan = {
      plan_id: "plan-1",
      workflow: "library-scan",
      target: "",
      steps: [
        { index: 2, op: "scan", summary: "third" },
        { index: 0, op: "walk", summary: "first" },
        { index: 1, op: "index", summary: "second" },
      ],
      review: review({ workflow: "library-scan", target: "" }),
    };
    expect(planSteps(plan)).toEqual(["first", "second", "third"]);
  });
});

describe("inFlightFor", () => {
  const running = (overrides: Partial<Operation> = {}): Operation =>
    makeOperation({ status: "running", ...overrides });

  it("returns the running row for the exact workflow and target", () => {
    const row = running({ id: "op-1", workflow: "extract", target: "Tools/a.zip" });
    expect(inFlightFor([row], "extract", "Tools/a.zip")).toBe(row);
  });

  it("never returns a terminal row, because resuming finished work would lie", () => {
    // Catches a lookup that treats a done row as resumable: the confirm would
    // offer to run work the server already completed.
    const row = makeOperation({
      id: "op-1",
      workflow: "extract",
      target: "Tools/a.zip",
      status: "done",
    });
    expect(inFlightFor([row], "extract", "Tools/a.zip")).toBeNull();
  });

  it("ignores the same workflow on another target", () => {
    const row = running({ id: "op-1", workflow: "extract", target: "Tools/a.zip" });
    expect(inFlightFor([row], "extract", "Tools/b.zip")).toBeNull();
  });

  it("ignores a different workflow on the same target", () => {
    const row = running({ id: "op-1", workflow: "extract", target: "Tools/a.zip" });
    expect(inFlightFor([row], "import-zip", "Tools/a.zip")).toBeNull();
  });

  it("picks the NEWEST when two are in flight, regardless of the caller's order", () => {
    const older = running({
      id: "op-1",
      workflow: "extract",
      target: "Tools/a.zip",
      created_at: "2026-01-01T00:00:00Z",
    });
    const newer = running({
      id: "op-2",
      workflow: "extract",
      target: "Tools/a.zip",
      created_at: "2026-01-02T00:00:00Z",
    });
    expect(inFlightFor([older, newer], "extract", "Tools/a.zip")).toBe(newer);
    expect(inFlightFor([newer, older], "extract", "Tools/a.zip")).toBe(newer);
  });

  it("breaks a same-instant tie by id descending", () => {
    const low = running({ id: "op-1", workflow: "extract", target: "Tools/a.zip" });
    const high = running({ id: "op-2", workflow: "extract", target: "Tools/a.zip" });
    expect(inFlightFor([low, high], "extract", "Tools/a.zip")).toBe(high);
  });
});

describe("doneLine on the resume path", () => {
  it("counts the split's create steps when there is no plan", () => {
    const operation = makeOperation({
      steps: [
        step("create-project", "done"),
        {
          index: 1,
          op: "create-project",
          status: "done",
          result: { created: true },
          error: null,
          started_at: null,
          finished_at: null,
        },
      ],
    });
    operation.steps[0].result = { created: true };
    expect(doneLine("split-and-create", operation, null)).toBe("Done: 2 projects");
  });

  it("reads the adopt step's own member count when there is no plan", () => {
    const operation = makeOperation({
      workflow: "adopt",
      steps: [
        {
          index: 0,
          op: "adopt",
          status: "done",
          result: { members: 3 },
          error: null,
          started_at: null,
          finished_at: null,
        },
      ],
    });
    expect(doneLine("adopt", operation, null)).toBe("Done: 3 files added");
    expect(doneLine("adopt", makeOperation({ workflow: "adopt" }), null)).toBe(
      "Done: the files were added.",
    );
  });

  it("says the extraction finished when extract-and-create has no plan", () => {
    expect(
      doneLine("extract-and-create", makeOperation({ workflow: "extract-and-create" }), null),
    ).toBe("Done: the extraction finished.");
  });
});

describe("doneLine per workflow", () => {
  it("uses the plan's project count when the split has one", () => {
    const plan: OperationPlan = {
      plan_id: "plan-1",
      workflow: "split-and-create",
      target: "Widget",
      steps: [],
      review: review({
        projects: [
          { name: "a", root: "a", members: 1 },
          { name: "b", root: "b", members: 1 },
        ],
      }),
    };
    expect(doneLine("split-and-create", makeOperation(), plan)).toBe("Done: 2 projects");
  });

  it("reads create-project's own result, including the lost race", () => {
    const created = makeOperation({
      workflow: "create-project",
      steps: doneOperation("created", "Tower"),
    });
    expect(doneLine("create-project", created, null)).toBe("Done: project 'Tower' created");
    const raced = makeOperation({
      workflow: "create-project",
      steps: doneOperation("already_created", "Tower"),
    });
    expect(doneLine("create-project", raced, null)).toBe(
      "Done: project 'Tower' already existed - nothing new was created",
    );
    expect(doneLine("create-project", makeOperation({ workflow: "create-project" }), null)).toBe(
      "Done: the project was created",
    );
  });

  it("reads the import's count from the plan, and says nothing when it has none", () => {
    const plan: OperationPlan = {
      plan_id: "plan-1",
      workflow: "import-zip",
      target: "Tools/a.zip",
      steps: [],
      review: review({ workflow: "import-zip", target: "Tools/a.zip", members: 6 }),
    };
    expect(doneLine("import-zip", makeOperation({ workflow: "import-zip" }), plan)).toBe(
      "Done: imported 6 files.",
    );
    expect(doneLine("import-zip", makeOperation({ workflow: "import-zip" }), null)).toBe(
      "Done: the import finished.",
    );
  });

  it("says the scan finished and the page was read", () => {
    expect(doneLine("library-scan", makeOperation({ workflow: "library-scan" }), null)).toBe(
      "Done: the scan finished.",
    );
    expect(doneLine("enrich", makeOperation({ workflow: "enrich" }), null)).toBe(
      "Done: the source page was read.",
    );
  });

  it("reads the walk's own report, including a skip and a walk that counted nothing", () => {
    const walked = makeOperation({
      workflow: "mount-import",
      steps: [mountImportStep({ outcome: "registered", registered: 4, failed: 0 })],
    });
    expect(doneLine("mount-import", walked, null)).toBe(
      "Done: registered 4 files from the mounted folder.",
    );

    const partly = makeOperation({
      workflow: "mount-import",
      steps: [mountImportStep({ outcome: "registered", registered: 1, failed: 2 })],
    });
    expect(doneLine("mount-import", partly, null)).toBe(
      "Done: registered 1 file, 2 files could not be read.",
    );

    const empty = makeOperation({
      workflow: "mount-import",
      steps: [mountImportStep({ outcome: "registered", registered: 0, failed: 0 })],
    });
    expect(doneLine("mount-import", empty, null)).toBe(
      "Done: the walk found nothing to register in the mounted folder.",
    );

    const skipped = makeOperation({
      workflow: "mount-import",
      steps: [mountImportStep({ outcome: "skipped", reason: "Mount path is not configured" })],
    });
    // The server's reason is rendered verbatim, so nothing is appended to it.
    expect(doneLine("mount-import", skipped, null)).toBe(
      "Done: the walk skipped the mounted folder - Mount path is not configured",
    );

    const skippedSilently = makeOperation({
      workflow: "mount-import",
      steps: [mountImportStep({ outcome: "skipped", reason: "" })],
    });
    expect(doneLine("mount-import", skippedSilently, null)).toBe(
      "Done: the walk skipped the mounted folder.",
    );
  });

  it("says the walk finished without a report rather than inventing zeros", () => {
    const unreported = makeOperation({ workflow: "mount-import" });
    expect(doneLine("mount-import", unreported, null)).toBe(
      "Done: the walk finished without a report.",
    );
  });

  it("says only Done for a workflow it does not know", () => {
    expect(doneLine("future-workflow", makeOperation(), null)).toBe("Done.");
  });
});

describe("undoLine", () => {
  const extracted: OperationUndo = {
    operation_id: "op-7",
    undone: true,
    project_id: null,
    project_name: null,
    moves: 1,
    projects_removed: ["Tower"],
  };

  it("says the extract's residue stays, for both extract workflows", () => {
    const expected =
      "Undone: 1 move folded back, removed 1 project. The extracted files stay in the library.";
    expect(undoLine("extract", extracted)).toBe(expected);
    expect(undoLine("extract-and-create", extracted)).toBe(expected);
  });

  it("says nothing was undone when the answer changed nothing", () => {
    // A superseded or already-undone operation answers `undone: false`, and the
    // line must not claim an erasure that did not happen.
    expect(undoLine("extract", { ...extracted, undone: false })).toBe("Nothing was undone.");
    expect(undoLine("split-and-create", { ...extracted, undone: false })).toBe(
      "Nothing was undone.",
    );
    expect(undoLine("library-scan", extracted)).toBe("Nothing was undone.");
  });

  it("counts the adopt's removed files", () => {
    expect(
      undoLine("adopt", { ...extracted, members_removed: 3, moves: null, projects_removed: null }),
    ).toBe("Undone: 3 files removed.");
  });

  it("counts the split's folded moves and removed projects", () => {
    expect(undoLine("split-and-create", extracted)).toBe(
      "Undone: 1 move folded back, 1 project removed.",
    );
  });

  it("names the create-project's removed project", () => {
    expect(
      undoLine("create-project", { ...extracted, project_name: "Tower", moves: null }),
    ).toBe("Undone: project 'Tower' removed.");
    expect(
      undoLine("create-project", { ...extracted, project_name: null, moves: null }),
    ).toBe("Undone: the project was removed.");
  });

  it("counts the import's removed assets", () => {
    expect(
      undoLine("import-zip", { ...extracted, assets_removed: 4, moves: null, projects_removed: null }),
    ).toBe("Undone: 4 assets removed.");
  });

  it("says only Undone for a workflow it does not know", () => {
    expect(undoLine("future-workflow", extracted)).toBe("Undone.");
    expect(undoLine("future-workflow", { ...extracted, undone: false })).toBe(
      "Nothing was undone.",
    );
  });
});

describe("workflowLabel / statusLabel", () => {
  it("names the workflows it knows", () => {
    expect(workflowLabel("create-project")).toBe("Create a project");
    expect(workflowLabel("split-and-create")).toBe("Split into projects");
    expect(workflowLabel("adopt")).toBe("Adopt grown files");
    expect(workflowLabel("extract-and-create")).toBe("Extract and create");
    expect(workflowLabel("extract")).toBe("Extract an archive");
    expect(workflowLabel("library-scan")).toBe("Library scan");
    expect(workflowLabel("import-link")).toBe("Import a link");
    expect(workflowLabel("import-zip")).toBe("Import a zip");
    expect(workflowLabel("enrich")).toBe("Enrich from the source page");
    expect(workflowLabel("mount-import")).toBe("Mounted import");
  });

  it("passes an unknown workflow through verbatim", () => {
    expect(workflowLabel("future-workflow")).toBe("future-workflow");
  });

  it("names the statuses it knows and passes an unknown one through", () => {
    expect(statusLabel("queued")).toBe("Queued");
    expect(statusLabel("running")).toBe("Running");
    expect(statusLabel("cancelling")).toBe("Cancelling");
    expect(statusLabel("done")).toBe("Done");
    expect(statusLabel("failed")).toBe("Failed");
    expect(statusLabel("cancelled")).toBe("Cancelled");
    expect(statusLabel("paused")).toBe("paused");
  });
});

describe("stateLine", () => {
  it("names the workflow's noun in every status", () => {
    expect(stateLine("split-and-create", "queued")).toBe("The split is queued.");
    expect(stateLine("split-and-create", "running")).toBe("The split is running.");
    expect(stateLine("split-and-create", "cancelling")).toBe(
      "The split is being cancelled.",
    );
    expect(stateLine("extract", "running")).toBe("The extraction is running.");
    expect(stateLine("library-scan", "queued")).toBe("The scan is queued.");
    expect(stateLine("import-zip", "queued")).toBe("The import is queued.");
    expect(stateLine("enrich", "queued")).toBe("The enrichment is queued.");
    expect(stateLine("mount-import", "queued")).toBe("The walk is queued.");
  });

  it("passes an unknown status through in the workflow's sentence", () => {
    expect(stateLine("split-and-create", "paused")).toBe("The split is paused.");
  });
});
