import { describe, expect, it } from "vitest";

import type { ArchiveEntry } from "../../lib/api/imports";
import { UnauthorizedError } from "../../lib/api/http";
import type { Operation, OperationPlan, OperationStep } from "../../lib/api/operations";
import { planArchive } from "../archive/archivePlan";
import {
  bulkPhaseLine,
  bulkPlanLines,
  bulkStopLine,
  bulkSummaryLine,
  bulkVerdictLines,
  outcomeLine,
  phaseLine,
  planExtractAndCreateMany,
  runExtractAndCreate,
  runExtractAndCreateMany,
  type BulkArchiveTarget,
  type BulkArchiveVerdict,
  type BulkPlan,
  type BulkRunResult,
  type ExtractRunDeps,
} from "./extractAndCreate";

const ARCHIVE = "import/01_Fates End - Wizard Tower.zip";
const TWIN = "import/01_Fates End - Wizard Tower";
const PROJECT = "01_Fates End - Wizard Tower";

function archiveEntries(count: number): ArchiveEntry[] {
  return Array.from({ length: count }, (_unused, index) => ({
    path: `01_Wizard/part-${index}.stl`,
    size: 1024,
  }));
}

/** The plan one archive's operation is issued from. `plan_id` is derived from the
 *  path so a harness can map a submit back to the archive it belongs to. */
function operationPlan(overrides: Partial<OperationPlan> = {}): OperationPlan {
  const target = overrides.target ?? ARCHIVE;
  return {
    plan_id: `plan:${target}`,
    workflow: "extract-and-create",
    target,
    steps: [
      { index: 0, op: "extract", summary: "extract the archive" },
      { index: 1, op: "move", summary: "file the archive" },
      { index: 2, op: "scan", summary: "register the files" },
      { index: 3, op: "create-project", summary: "create 1 project" },
    ],
    review: {
      workflow: "extract-and-create",
      target,
      moves: 1,
      creates: 1,
      adopts: 0,
      deletes: 0,
      members: 12,
      groups: [{ name: "01_Wizard", members: 12, models: 12 }],
      cover: null,
      excluded: [],
      existing_project_id: null,
      existing_project_name: null,
      move_list: [{ source: target, target: `${target}/_source/x.zip`, kind: "file", action: "file-archive" }],
      projects: [{ name: PROJECT, root: TWIN, members: 12 }],
    },
    ...overrides,
  };
}

/** A plan the server answers when the project already exists: zero steps and no
 *  create. */
function alreadyPlan(target: string, name: string): OperationPlan {
  return operationPlan({
    target,
    steps: [],
    review: {
      ...operationPlan({ target }).review,
      moves: 0,
      creates: 0,
      existing_project_id: "p-existing",
      existing_project_name: name,
      move_list: [],
      projects: [],
    },
  });
}

function step(overrides: Partial<OperationStep> = {}): OperationStep {
  return {
    index: 0,
    op: "extract",
    status: "done",
    result: null,
    error: null,
    started_at: "2026-01-01T00:00:01Z",
    finished_at: "2026-01-01T00:00:02Z",
    ...overrides,
  };
}

function createdSteps(name = PROJECT): OperationStep[] {
  return [
    step({ index: 0, op: "extract" }),
    step({ index: 1, op: "move" }),
    step({ index: 2, op: "scan" }),
    step({
      index: 3,
      op: "create-project",
      result: { project_id: "p1", name, status: "created", created: true },
    }),
  ];
}

function operation(overrides: Partial<Operation> = {}): Operation {
  const target = overrides.target ?? ARCHIVE;
  return {
    id: `op:${target}`,
    workflow: "extract-and-create",
    target,
    status: "done",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:01Z",
    finished_at: "2026-01-01T00:00:09Z",
    error: null,
    undo_state: "unavailable",
    superseded_by: null,
    steps: createdSteps(),
    ...overrides,
  };
}

/** A promise with a handle on its resolver, so a test can hold one archive's turn
 *  open and watch what the run does - or does not do - while it waits. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

/** One archive's scripted answer: a value, a thrown error, or a thunk to await. */
type Answer<T> = T | Error | (() => Promise<T>);

async function answerOr<T>(answer: Answer<T> | undefined, fallback: () => T): Promise<T> {
  if (answer === undefined) return fallback();
  if (answer instanceof Error) throw answer;
  if (typeof answer === "function") return (answer as () => Promise<T>)();
  return answer;
}

/** `Answer<T> | undefined` is not narrowed by `instanceof` inside `answerOr`'s
 *  generic, so a test that wants to throw uses this explicit wrapper. */
function throws<T>(err: Error): () => Promise<T> {
  return async () => {
    throw err;
  };
}

type ArchiveSpec = {
  path: string;
  label: string;
  name?: string | null;
  /** Whether the confirm-step listing can read this archive. */
  entries?: Answer<ArchiveEntry[]>;
  plan?: Answer<OperationPlan>;
  submit?: Answer<Operation>;
  /** Successive poll answers; the last one repeats. */
  reads?: Answer<Operation>[];
};

type Harness = {
  targets: BulkArchiveTarget[];
  listCalls: string[];
  planCalls: { workflow: string; target: string; params?: Record<string, unknown> }[];
  submitCalls: { planId: string; idempotencyKey: string }[];
  readCalls: string[];
  phaseLines: string[];
  verdicts: BulkArchiveVerdict[];
  deps: ExtractRunDeps;
  plan: () => Promise<BulkPlan>;
  run: (plan: BulkPlan) => Promise<BulkRunResult>;
};

function harness(specs: readonly ArchiveSpec[]): Harness {
  const byPath = new Map(specs.map(spec => [spec.path, spec] as const));
  const listCalls: string[] = [];
  const planCalls: { workflow: string; target: string; params?: Record<string, unknown> }[] = [];
  const submitCalls: { planId: string; idempotencyKey: string }[] = [];
  const readCalls: string[] = [];
  const phaseLines: string[] = [];
  const verdicts: BulkArchiveVerdict[] = [];
  let keys = 0;

  const targets: BulkArchiveTarget[] = specs.map(spec => ({
    path: spec.path,
    label: spec.label,
    name: spec.name ?? null,
  }));

  const deps: ExtractRunDeps = {
    plan: async input => {
      planCalls.push(input);
      return answerOr(byPath.get(input.target)?.plan, () => operationPlan({ target: input.target }));
    },
    submit: async input => {
      submitCalls.push(input);
      const target = input.planId.replace(/^plan:/, "");
      return answerOr(byPath.get(target)?.submit, () => operation({ target }));
    },
    read: async id => {
      readCalls.push(id);
      const target = id.replace(/^op:/, "");
      const reads = byPath.get(target)?.reads ?? [];
      // One answer means "every poll answers this"; more than one drains.
      const next = reads.length > 1 ? reads.shift() : reads[0];
      return answerOr(next, () => operation({ target }));
    },
    pause: async () => {},
    // Minted per archive, so a test can see that two archives never share one key.
    newKey: () => {
      keys += 1;
      return `key-${keys}`;
    },
  };

  return {
    targets,
    listCalls,
    planCalls,
    submitCalls,
    readCalls,
    phaseLines,
    verdicts,
    deps,
    plan: () =>
      planExtractAndCreateMany({
        targets,
        listEntries: async path => {
          listCalls.push(path);
          const entries = await answerOr(byPath.get(path)?.entries, () => archiveEntries(2));
          return { path, entries };
        },
      }),
    run: plan =>
      runExtractAndCreateMany({
        plan,
        deps,
        onProgress: line => {
          phaseLines.push(line);
        },
        onVerdict: verdict => {
          verdicts.push(verdict);
        },
      }),
  };
}

/** One archive listing for the aggregate tests: two models, an image, a document,
 *  a hidden member the library cannot register and a dropped `.DS_Store`. */
function richListing(): ArchiveEntry[] {
  return [
    { path: "Alpha/robot.stl", size: 100 },
    { path: "Alpha/robot.obj", size: 200 },
    { path: "Alpha/photo.jpg", size: 300 },
    { path: "Alpha/manual.pdf", size: 400 },
    { path: "Alpha/.hidden/extra.stl", size: 500 },
    { path: ".DS_Store", size: 10 },
  ];
}

describe("planExtractAndCreateMany", () => {
  it("says the aggregate payload before anything is queued", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha", entries: richListing() },
      { path: "import/Beta.zip", label: "Beta", entries: [{ path: "Beta/parts.zip", size: 64 }] },
      { path: "import/Gamma.zip", label: "Gamma", entries: [{ path: ".DS_Store", size: 10 }] },
    ]);

    const plan = await h.plan();

    // One classifier: the totals are the element-wise sum of the very plans the
    // single-archive confirm step would show, which is why the number cannot drift
    // from what each archive's own sentence says.
    const summed = plan.archives.reduce(
      (totals, archive) => {
        if (archive.plan === null) return totals;
        const one = planArchive(archive.entries);
        return {
          files: totals.files + one.files,
          bytes: totals.bytes + one.bytes,
          models: totals.models + one.models,
          images: totals.images + one.images,
          documents: totals.documents + one.documents,
          nestedArchives: totals.nestedArchives + one.nestedArchives,
          junk: totals.junk + one.junk,
          unregistered: totals.unregistered + one.unregistered,
        };
      },
      {
        files: 0,
        bytes: 0,
        models: 0,
        images: 0,
        documents: 0,
        nestedArchives: 0,
        junk: 0,
        unregistered: 0,
      },
    );
    expect(plan.totals).toEqual(summed);

    expect(bulkPlanLines(plan)).toEqual([
      "Extracting 3 archives: 2 models, 1 image, 1 nested archive, 1 document, 6 files in all.",
      '"Beta" packs no model of its own: the only archive inside is "parts.zip", which extraction releases whole.',
      '"Gamma" holds nothing extractable, so extracting it writes nothing and no project will be made from it.',
    ]);
    // Planning is a read, not a write: the confirm screen costs one listing per
    // archive and no operations.
    expect(h.planCalls).toEqual([]);
    expect(h.submitCalls).toEqual([]);
  });

  it("names an unreadable archive rather than folding it in as a zero", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha", entries: richListing() },
      { path: "import/Broken.zip", label: "Broken", entries: new Error("Archive is not readable") },
    ]);

    const plan = await h.plan();

    // Nothing from the failed read is in the totals - not even a zero for its
    // bytes, which would be a claim that it holds nothing.
    const alpha = planArchive(richListing());
    expect(plan.totals).toEqual({
      files: alpha.files,
      bytes: alpha.bytes,
      models: alpha.models,
      images: alpha.images,
      documents: alpha.documents,
      nestedArchives: alpha.nestedArchives,
      junk: alpha.junk,
      unregistered: alpha.unregistered,
    });
    expect(bulkPlanLines(plan)).toEqual([
      "Extracting 2 archives: 2 models, 1 image, 1 document, 5 files in all.",
      'Could not read the file list of "Broken" (Archive is not readable) - it will be skipped and nothing in it will be written.',
    ]);
    expect(h.listCalls).toEqual(["import/Alpha.zip", "import/Broken.zip"]);
  });

  it("says a click with nothing readable would write nothing", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha", entries: new Error("Invalid zip file") },
      { path: "import/Beta.zip", label: "Beta", entries: new Error("Archive is not readable") },
    ]);

    const plan = await h.plan();

    expect(bulkPlanLines(plan)).toEqual([
      "None of the 2 archives could be read, so this click would write nothing.",
      'Could not read the file list of "Alpha" (Invalid zip file) - it will be skipped and nothing in it will be written.',
      'Could not read the file list of "Beta" (Archive is not readable) - it will be skipped and nothing in it will be written.',
    ]);
  });

  it("says a click whose archives hold nothing extractable would write nothing", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha", entries: [{ path: ".DS_Store", size: 10 }] },
      {
        path: "import/Beta.zip",
        label: "Beta",
        entries: [
          { path: "Alpha/", size: 0 },
          { path: "__MACOSX/", size: 0 },
        ],
      },
    ]);

    const plan = await h.plan();

    expect(bulkPlanLines(plan)).toEqual([
      "Nothing in these 2 archives can be extracted, so this click would write nothing.",
      '"Alpha" holds nothing extractable, so extracting it writes nothing and no project will be made from it.',
      '"Beta" holds nothing extractable, so extracting it writes nothing and no project will be made from it.',
    ]);

    const single = await harness([
      { path: "import/Alpha.zip", label: "Alpha", entries: [{ path: ".DS_Store", size: 10 }] },
    ]).plan();
    expect(bulkPlanLines(single)).toEqual([
      "Nothing in this archive can be extracted, so this click would write nothing.",
      '"Alpha" holds nothing extractable, so extracting it writes nothing and no project will be made from it.',
    ]);

    // A mixed plan must not over-claim: the unreadable archive is unread, not
    // empty, so the subject counts the one readable archive and the failure is
    // still named on its own line.
    const mixed = await harness([
      { path: "import/Broken.zip", label: "Broken", entries: new Error("Archive is not readable") },
      { path: "import/Alpha.zip", label: "Alpha", entries: [{ path: ".DS_Store", size: 10 }] },
    ]).plan();
    expect(bulkPlanLines(mixed)).toEqual([
      "Nothing in this archive can be extracted, so this click would write nothing.",
      'Could not read the file list of "Broken" (Archive is not readable) - it will be skipped and nothing in it will be written.',
      '"Alpha" holds nothing extractable, so extracting it writes nothing and no project will be made from it.',
    ]);
  });

  it("says nothing at all when the click named no archive", async () => {
    const h = harness([]);

    const plan = await h.plan();

    expect(plan).toEqual({
      archives: [],
      totals: {
        files: 0,
        bytes: 0,
        models: 0,
        images: 0,
        documents: 0,
        nestedArchives: 0,
        junk: 0,
        unregistered: 0,
      },
    });
    expect(bulkPlanLines(plan)).toEqual([]);
  });
});

describe("runExtractAndCreate", () => {
  const deps = (overrides: Partial<ExtractRunDeps> = {}): ExtractRunDeps => ({
    plan: async input => operationPlan({ target: input.target }),
    submit: async () => operation(),
    read: async () => operation(),
    pause: async () => {},
    newKey: () => "key-1",
    ...overrides,
  });

  it("plans the archive itself, with the typed name and no entries", async () => {
    // Catches an entry list the browser has no picker for: the whole archive is
    // the subject, and the name is the operator's own.
    const calls: unknown[] = [];
    await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: "Wizard v5.4",
      deps: deps({
        plan: async input => {
          calls.push(input);
          return operationPlan();
        },
      }),
    });

    expect(calls).toEqual([
      {
        workflow: "extract-and-create",
        target: ARCHIVE,
        params: { name: "Wizard v5.4" },
      },
    ]);
  });

  it("omits the name param when the operator typed nothing", async () => {
    const calls: unknown[] = [];
    await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: null,
      deps: deps({
        plan: async input => {
          calls.push(input);
          return operationPlan();
        },
      }),
    });

    expect(calls).toEqual([{ workflow: "extract-and-create", target: ARCHIVE, params: {} }]);
  });

  it("submits the server's plan id with one idempotency key and reports the create", async () => {
    const submits: { planId: string; idempotencyKey: string }[] = [];
    const outcome = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({
        submit: async input => {
          submits.push(input);
          return operation({ status: "queued", steps: [] });
        },
      }),
    });

    expect(submits).toEqual([{ planId: `plan:${ARCHIVE}`, idempotencyKey: "key-1" }]);
    expect(outcome).toEqual({ kind: "created", project_id: "p1", name: PROJECT });
  });

  it("polls the row until the runner reaches a terminal status", async () => {
    const reads: string[] = [];
    const outcome = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({
        submit: async () => operation({ status: "queued", steps: [] }),
        read: async id => {
          reads.push(id);
          // The first read is still running; the second is the finished row.
          return reads.length === 1
            ? operation({ status: "running", steps: [step({ index: 0, op: "extract", status: "running" })] })
            : operation({ id });
        },
      }),
    });

    expect(reads).toEqual([`op:${ARCHIVE}`, `op:${ARCHIVE}`]);
    expect(outcome.kind).toBe("created");
  });

  it("does not poll a row the submit already answered as terminal", async () => {
    let readCalls = 0;
    await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({
        read: async () => {
          readCalls += 1;
          return operation();
        },
      }),
    });

    expect(readCalls).toBe(0);
  });

  it("reports the runner's race rather than claiming a create", async () => {
    // The create step can answer `already_created` when the project appeared
    // between the plan and the run: the outcome must carry that, not a create.
    const outcome = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({
        submit: async () =>
          operation({
            steps: [
              step({ index: 0, op: "scan" }),
              step({
                index: 1,
                op: "create-project",
                result: { project_id: "p9", name: "Old Tower", status: "already_created" },
              }),
            ],
          }),
      }),
    });

    expect(outcome).toEqual({ kind: "already-created", project_id: "p9", name: "Old Tower" });
  });

  it("submits nothing for a zero-step plan and names the existing project", async () => {
    // The server's own answer that the project is already there: a submit would
    // queue a no-op the operator never asked for.
    const submits: unknown[] = [];
    const outcome = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({
        plan: async () => alreadyPlan(ARCHIVE, "Old Tower"),
        submit: async input => {
          submits.push(input);
          return operation();
        },
      }),
    });

    expect(submits).toEqual([]);
    expect(outcome).toEqual({
      kind: "already-created",
      project_id: "p-existing",
      name: "Old Tower",
    });
  });

  it("reports the server's own sentence for a failed operation", async () => {
    const outcome = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({
        submit: async () =>
          operation({ status: "failed", error: "the archive moved while it was being read" }),
      }),
    });

    expect(outcome).toEqual({ kind: "failed", detail: "the archive moved while it was being read" });
  });

  it("reports a done operation whose create step reported neither status as a failure", async () => {
    const outcome = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({
        submit: async () =>
          operation({ steps: [step({ index: 0, op: "scan", result: { registered: 0 } })] }),
      }),
    });

    expect(outcome).toEqual({ kind: "failed", detail: null });
  });

  it("reports a plan or submit failure as a value, never a rejection", async () => {
    const planned = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({ plan: throws(new Error("no such archive")) }),
    });
    expect(planned).toEqual({ kind: "failed", detail: "no such archive" });

    const submitted = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({ submit: throws(new Error("the plan is stale")) }),
    });
    expect(submitted).toEqual({ kind: "failed", detail: "the plan is stale" });
  });

  it("reports a failed follow-up read as a failure with the server's sentence", async () => {
    const outcome = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps({
        submit: async () => operation({ status: "queued", steps: [] }),
        read: throws(new Error("the ledger is unreachable")),
      }),
    });

    expect(outcome).toEqual({ kind: "failed", detail: "the ledger is unreachable" });
  });

  it("hands a 401 to the caller instead of writing a sentence about it", async () => {
    await expect(
      runExtractAndCreate({
        archivePath: ARCHIVE,
        name: PROJECT,
        deps: deps({ plan: throws(new UnauthorizedError()) }),
      }),
    ).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("still plans and submits when a cosmetic phase callback throws", async () => {
    const outcome = await runExtractAndCreate({
      archivePath: ARCHIVE,
      name: PROJECT,
      deps: deps(),
      onPhase: () => {
        throw new Error("the paint broke");
      },
    });

    expect(outcome.kind).toBe("created");
  });

  it("says each phase over the one phase line", () => {
    expect(phaseLine("planning")).toBe("Planning the extraction…");
    expect(phaseLine("running")).toBe("Running the extraction…");
    expect(bulkPhaseLine(2, 3, "planning")).toBe("Archive 2 of 3 - Planning the extraction…");
  });
});

describe("outcomeLine", () => {
  it("keeps the sentences the bulk door speaks byte-for-byte", () => {
    expect(outcomeLine({ kind: "created", project_id: "p1", name: "Alpha" })).toBe(
      "Created project Alpha.",
    );
    // A create that named no row is still a create.
    expect(outcomeLine({ kind: "created", project_id: null, name: null })).toBe(
      "Created the project from this archive.",
    );
    expect(outcomeLine({ kind: "already-created", project_id: "p1", name: "Gamma" })).toBe(
      "Already created as project Gamma - this run created no second project.",
    );
    expect(outcomeLine({ kind: "already-created", project_id: "p1", name: null })).toBe(
      "This archive already has a project (its name is unavailable) - this run created no second project.",
    );
    expect(outcomeLine({ kind: "listing-failed", detail: "Archive is not readable" })).toBe(
      "Could not read this archive's file list: Archive is not readable Nothing was extracted, so no project was created.",
    );
    expect(outcomeLine({ kind: "failed", detail: "disk is full" })).toBe(
      "The project could not be created: disk is full. Nothing was rolled back.",
    );
    // `cancelled` speaks nothing: the runner stopped, and the shell already says so.
    expect(outcomeLine({ kind: "cancelled" })).toBeNull();
  });

  it("never ends a sentence with two punctuation marks", () => {
    const lines = [
      outcomeLine({ kind: "listing-failed", detail: null }),
      outcomeLine({ kind: "listing-failed", detail: "nope" }),
      outcomeLine({ kind: "failed", detail: null }),
    ];
    for (const line of lines) {
      expect(line).not.toBeNull();
      expect((line as string).includes("..")).toBe(false);
      expect((line as string).includes(":.")).toBe(false);
    }
    expect(outcomeLine({ kind: "listing-failed", detail: null })).toBe(
      "Could not read this archive's file list. Nothing was extracted, so no project was created.",
    );
    expect(outcomeLine({ kind: "failed", detail: null })).toBe(
      "The project could not be created. Nothing was rolled back.",
    );
  });
});

describe("runExtractAndCreateMany", () => {
  it("runs one archive at a time: no operation is planned before the previous one resolves", async () => {
    const gate = deferred<Operation>();
    const started = deferred<void>();
    const h = harness([
      {
        path: "import/Alpha.zip",
        label: "Alpha",
        submit: async () => {
          started.resolve(undefined);
          return gate.promise;
        },
      },
      { path: "import/Beta.zip", label: "Beta" },
    ]);
    const plan = await h.plan();

    const running = h.run(plan);
    await started.promise;
    // Alpha's submit is open, so nothing of Beta's may have begun: the queue is
    // sequential by contract, not merely by node ordering.
    expect(h.planCalls.map(call => call.target)).toEqual(["import/Alpha.zip"]);

    gate.resolve(operation({ target: "import/Alpha.zip" }));
    const result = await running;

    expect(h.planCalls.map(call => call.target)).toEqual([
      "import/Alpha.zip",
      "import/Beta.zip",
    ]);
    expect(result.created).toBe(2);
  });

  it("mints one idempotency key per archive", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha" },
      { path: "import/Beta.zip", label: "Beta" },
    ]);

    await h.run(await h.plan());

    expect(h.submitCalls.map(call => call.planId)).toEqual([
      "plan:import/Alpha.zip",
      "plan:import/Beta.zip",
    ]);
    expect(h.submitCalls.map(call => call.idempotencyKey)).toEqual(["key-1", "key-2"]);
  });

  it("names each archive in its own verdict and keeps going", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha" },
      { path: "import/Beta.zip", label: "Beta" },
      { path: "import/Gamma.zip", label: "Gamma" },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.verdicts.map(item => item.label)).toEqual(["Alpha", "Beta", "Gamma"]);
    expect(result.created).toBe(3);
    expect(h.verdicts.map(item => item.label)).toEqual(["Alpha", "Beta", "Gamma"]);
    expect(bulkVerdictLines(result.verdicts).map(line => line.split(" - ")[0])).toEqual([
      "Alpha",
      "Beta",
      "Gamma",
    ]);
  });

  it("a listing that failed is named, skips its operation, and the rest still run", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha" },
      { path: "import/Broken.zip", label: "Broken", entries: new Error("Archive is not readable") },
      { path: "import/Gamma.zip", label: "Gamma" },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    const broken = result.verdicts[1];
    expect(broken.outcome).toEqual({ kind: "listing-failed", detail: "Archive is not readable" });
    expect(bulkVerdictLines(result.verdicts)[1]).toBe(
      "Broken - Could not read this archive's file list: Archive is not readable Nothing was extracted, so no project was created.",
    );
    // No plan, no submit, no poll for the unreadable one - and the archives on
    // either side of it were still run.
    expect(h.planCalls.map(call => call.target)).toEqual([
      "import/Alpha.zip",
      "import/Gamma.zip",
    ]);
    expect(result.created).toBe(2);
  });

  it("names a failed operation and the rest still run", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha" },
      {
        path: "import/Beta.zip",
        label: "Beta",
        submit: new Error("the runner could not open the archive"),
      },
      { path: "import/Gamma.zip", label: "Gamma" },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.verdicts[1].outcome).toEqual({
      kind: "failed",
      detail: "the runner could not open the archive",
    });
    expect(bulkVerdictLines(result.verdicts)[1]).toBe(
      "Beta - The project could not be created: the runner could not open the archive. Nothing was rolled back.",
    );
    expect(result.created).toBe(2);
  });

  it("stops when the session goes and says how many it never touched", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha" },
      { path: "import/Beta.zip", label: "Beta", plan: new UnauthorizedError() },
      { path: "import/Gamma.zip", label: "Gamma" },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.stopped).toBe(true);
    expect(result.unauthorized).toBe(true);
    // One verdict: Alpha's. The untouched tail gets none, because nothing
    // happened there.
    expect(result.verdicts).toHaveLength(1);
    expect(h.planCalls.map(call => call.target)).toEqual([
      "import/Alpha.zip",
      "import/Beta.zip",
    ]);
    expect(bulkStopLine(result)).toBe(
      "Stopped with 1 of 3 archives done - 2 archives were never touched.",
    );
  });

  it("never lands on an arbitrary one of many", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha" },
      { path: "import/Beta.zip", label: "Beta" },
      { path: "import/Gamma.zip", label: "Gamma" },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.created).toBe(3);
    // Three projects were made; picking one to land on would hide the other two.
    expect(result.landing).toBeNull();
    expect(result.targets.map(target => target.label)).toEqual(["Alpha", "Beta", "Gamma"]);
  });

  it("does not land on a one-archive already-created click", async () => {
    const h = harness([
      {
        path: "import/Alpha.zip",
        label: "Alpha",
        plan: alreadyPlan("import/Alpha.zip", "Alpha"),
      },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.created).toBe(0);
    expect(result.alreadyCreated).toBe(1);
    // Success with no write: reported, counted, and not landed on - the project
    // already existed before this click.
    expect(result.landing).toBeNull();
    expect(result.verdicts[0].outcome.kind).toBe("already-created");
  });

  it("lands on the one project a one-archive click created", async () => {
    const h = harness([{ path: "import/Alpha.zip", label: "Alpha" }]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.landing).toBe("p1");
    expect(bulkSummaryLine(result)).toBe("Created 1 project.");
  });

  it("reports a created, an already-existing and a failed archive in one queue", async () => {
    const h = harness([
      { path: "import/Alpha.zip", label: "Alpha", name: "Alpha v2" },
      {
        path: "import/Beta.zip",
        label: "Beta",
        plan: alreadyPlan("import/Beta.zip", "Beta"),
      },
      {
        path: "import/Gamma.zip",
        label: "Gamma",
        submit: operation({
          target: "import/Gamma.zip",
          status: "failed",
          error: "the archive was deleted mid-run",
        }),
      },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.created).toBe(1);
    expect(result.alreadyCreated).toBe(1);
    expect(result.landing).toBeNull();
    expect(result.verdicts.map(item => item.outcome.kind)).toEqual([
      "created",
      "already-created",
      "failed",
    ]);
    const lines = bulkVerdictLines(result.verdicts);
    expect(lines).toHaveLength(3);
    expect(lines[0]).toBe("Alpha - Created project 01_Fates End - Wizard Tower.");
    expect(lines[1]).toBe(
      "Beta - Already created as project Beta - this run created no second project.",
    );
    expect(lines[2]).toBe(
      "Gamma - The project could not be created: the archive was deleted mid-run. Nothing was rolled back.",
    );
    // The operator's typed name went on the plan, not the verifier's guess.
    expect(h.planCalls[0].params).toEqual({ name: "Alpha v2" });
    expect(bulkSummaryLine(result)).toBe("Created 1 project.");
  });

  it("summarises an empty or stopped run without claiming anything", () => {
    expect(bulkSummaryLine({ created: 0 })).toBeNull();
    expect(bulkSummaryLine({ created: 3 })).toBe("Created 3 projects.");
    expect(bulkStopLine({ stopped: false, targets: [], verdicts: [] })).toBeNull();
    expect(
      bulkStopLine({
        stopped: true,
        targets: [
          { path: "import/Alpha.zip", label: "Alpha", name: null },
          { path: "import/Beta.zip", label: "Beta", name: null },
        ],
        verdicts: [],
      }),
    ).toBe("Stopped with 0 of 2 archives done - 2 archives were never touched.");
  });

  it("says each archive's phase over the existing phase line", async () => {
    const h = harness([{ path: "import/Alpha.zip", label: "Alpha" }]);

    await h.run(await h.plan());

    expect(h.phaseLines).toEqual([
      "Archive 1 of 1 - Planning the extraction…",
      "Archive 1 of 1 - Running the extraction…",
    ]);
  });
});
