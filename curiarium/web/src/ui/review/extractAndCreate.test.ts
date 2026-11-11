import { describe, expect, it } from "vitest";

import type {
  ArchiveEntry,
  ArchiveExtractResult,
  ArchiveProjectResult,
} from "../../lib/api/imports";
import type { Project } from "../../lib/api/projects";
import type { SettleOutcome } from "../../lib/scanSettle";
import { planArchive } from "../archive/archivePlan";
import {
  bulkPhaseLine,
  bulkPlanLines,
  bulkStopLine,
  bulkSummaryLine,
  bulkVerdictLines,
  createPayload,
  outcomeLine,
  phaseLine,
  planExtractAndCreateMany,
  runExtractAndCreate,
  runExtractAndCreateMany,
  type BulkArchiveTarget,
  type BulkArchiveVerdict,
  type BulkPlan,
  type BulkRunResult,
  type ExtractAndCreateDeps,
  type ExtractAndCreatePhase,
  type ExtractAndCreateTarget,
} from "./extractAndCreate";

const ARCHIVE = "import/01_Fates End - Wizard Tower.zip";
const TWIN = "import/01_Fates End - Wizard Tower";

/** The row the action runs on; the operator's name arrives already trimmed. There
 *  is no bundle key any more: the archive path is the whole identity, which is
 *  what makes a second run on the same archive idempotent. */
const TARGET: ExtractAndCreateTarget = { name: "Wizard v5.4" };

function archiveEntries(count: number): ArchiveEntry[] {
  return Array.from({ length: count }, (_unused, index) => ({
    path: `01_Wizard/part-${index}.stl`,
    size: 1024,
  }));
}

function extractResult(overrides: Partial<ArchiveExtractResult> = {}): ArchiveExtractResult {
  return {
    path: ARCHIVE,
    destination: TWIN,
    files: 12,
    bytes: 4096,
    already_present: false,
    scan_started: true,
    ...overrides,
  };
}

/** The row the create door hands back - the server's own re-read project, with a
 *  fixed id and fixed timestamps so an identity assertion can be read against it.
 *  One definition for both halves of this file: `verdict` embeds it, and the
 *  `outcomeLine` block names it directly, so neither restates the other.
 *
 *  `asset_count` defaults to the count `verdict` defaults `resolved` to, so a bare
 *  `projectRow()` is exactly the row the happy path answers with. */
function projectRow(overrides: Partial<Project> = {}): Project {
  return {
    id: "project-1",
    name: "Wizard v5.4",
    description: null,
    creator: null,
    source_url: null,
    licence: null,
    licence_url: null,
    origin: "own",
    cover_asset_id: null,
    cover_thumb_url: null,
    asset_count: 21,
    total_size: 4096,
    metadata_json: {},
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/** One door verdict, in the shape `ArchiveProjectOut` serialises. The counts
 *  derive from the overrides, so a fixture cannot claim a `resolved` that its own
 *  `failed` list contradicts. */
function verdict(overrides: Partial<ArchiveProjectResult> = {}): ArchiveProjectResult {
  const status = overrides.status ?? "created";
  const planned = overrides.planned ?? 21;
  const resolved = overrides.resolved ?? (status === "created" || status === "already_created" ? planned : 0);
  const failed = overrides.failed ?? (status === "partial" ? ["01_Wizard/missing.stl"] : []);
  return {
    status,
    archive: ARCHIVE,
    twin: TWIN,
    planned,
    resolved,
    failed,
    project:
      status === "created" || status === "already_created"
        ? projectRow({ asset_count: resolved })
        : null,
    ...overrides,
  };
}

type HarnessConfig = {
  listing?: () => Promise<{ path: string; entries: ArchiveEntry[] }>;
  extract?: () => Promise<ArchiveExtractResult>;
  settle?: () => Promise<SettleOutcome>;
  create?: (payload: { path: string; name?: string }) => Promise<ArchiveProjectResult | null>;
  onPlan?: (plan: unknown) => void;
};

type Harness = {
  deps: ExtractAndCreateDeps;
  phases: ExtractAndCreatePhase[];
  listCalls: string[];
  extractCalls: { path: string; entries?: string[] | null }[];
  settleCalls: ArchiveExtractResult[][];
  createCalls: { path: string; name?: string }[];
  plans: { files: number; models: number }[];
  primed: () => number;
};

/** Deps that record what they were asked to do; every member answers the happy
 *  path unless the test overrides it. */
function harness(config: HarnessConfig = {}): Harness {
  const phases: ExtractAndCreatePhase[] = [];
  const listCalls: string[] = [];
  const extractCalls: { path: string; entries?: string[] | null }[] = [];
  const settleCalls: ArchiveExtractResult[][] = [];
  const createCalls: { path: string; name?: string }[] = [];
  const plans: { files: number; models: number }[] = [];
  const cover = { primes: 0 };

  const deps: ExtractAndCreateDeps = {
    listEntries: async path => {
      listCalls.push(path);
      if (config.listing) return config.listing();
      return { path, entries: archiveEntries(2) };
    },
    extract: async payload => {
      extractCalls.push(payload);
      if (config.extract) return config.extract();
      return extractResult();
    },
    settle: async results => {
      settleCalls.push(results);
      if (config.settle) return config.settle();
      return "settled";
    },
    create: async payload => {
      createCalls.push(payload);
      if (config.create) return config.create(payload);
      return verdict();
    },
    beforeCreate: () => {
      cover.primes += 1;
    },
    onPhase: phase => {
      phases.push(phase);
    },
    onPlan: plan => {
      plans.push({ files: plan.files, models: plan.models });
      config.onPlan?.(plan);
    },
  };

  return {
    deps,
    phases,
    listCalls,
    extractCalls,
    settleCalls,
    createCalls,
    plans,
    primed: () => cover.primes,
  };
}

function runWith(h: Harness, item: ExtractAndCreateTarget = TARGET) {
  return runExtractAndCreate({ archivePath: ARCHIVE, item, deps: h.deps });
}

describe("runExtractAndCreate", () => {
  it("lists, extracts, settles and creates, in that order", async () => {
    const h = harness();

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "created", project: projectRow() });
    expect(h.phases).toEqual(["listing", "extracting", "scanning", "creating"]);
    expect(h.listCalls).toEqual([ARCHIVE]);
    expect(h.extractCalls).toEqual([{ path: ARCHIVE, entries: null }]);
    expect(h.settleCalls).toEqual([[extractResult()]]);
    expect(h.createCalls).toEqual([{ path: ARCHIVE, name: "Wizard v5.4" }]);
    expect(h.primed()).toBe(1);
    expect(outcomeLine(outcome)).toContain("Created project Wizard v5.4");
  });

  it("says what the archive holds as soon as the listing is read", async () => {
    const h = harness();

    await runWith(h);

    // The one-step door has no confirm panel, so the plan is painted from here:
    // two `.stl` entries, both models, and the classifier is the confirm step's.
    expect(h.plans).toEqual([{ files: 2, models: 2 }]);
  });

  it("still runs when the plan callback throws", async () => {
    const h = harness({
      onPlan: () => {
        throw new Error("the panel unmounted mid-run");
      },
    });

    await expect(runWith(h)).resolves.toEqual({ kind: "created", project: projectRow() });
  });

  it("claims no count and no create when the listing fails", async () => {
    const h = harness({
      listing: async () => {
        throw new Error("Archive is not readable");
      },
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "listing-failed", detail: "Archive is not readable" });
    expect(h.phases).toEqual(["listing"]);
    expect(h.createCalls).toEqual([]);
  });

  it("reports the listing's length when the extraction fails, and never creates", async () => {
    const h = harness({
      extract: async () => {
        throw new Error("disk is full");
      },
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({
      kind: "nothing-written",
      cause: "extract-failed",
      entries: 2,
      detail: "disk is full",
    });
    const line = outcomeLine(outcome) ?? "";
    expect(line).toContain("0 of 2 entries");
    expect(line).toContain("disk is full");
    expect(h.createCalls).toEqual([]);
  });

  it("stops at an archive nothing could be extracted from", async () => {
    const h = harness({
      extract: async () => extractResult({ files: 0, bytes: 0, scan_started: false }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "nothing-written", cause: "junk-only", entries: 2, detail: null });
    expect(h.phases).toEqual(["listing", "extracting"]);
    expect(outcomeLine(outcome)).toContain("0 of 2 entries");
    expect(outcomeLine(outcome)).toContain("Nothing will register");
    expect(h.createCalls).toEqual([]);
  });

  it("skips the settle when every entry was already present, and still creates", async () => {
    const h = harness({
      extract: async () => extractResult({ files: 0, bytes: 0, already_present: true, scan_started: false }),
    });

    await runWith(h);

    expect(h.phases).toEqual(["listing", "extracting", "creating"]);
    expect(h.settleCalls).toEqual([]);
    expect(h.createCalls).toEqual([{ path: ARCHIVE, name: "Wizard v5.4" }]);
  });

  it("creates anyway when the scan does not settle, and lets the door judge", async () => {
    const h = harness({
      settle: async () => "timeout",
      // A scan that never proved itself leaves assets half-registered, which is
      // the case the old flow refused to speak about.
      create: async () => verdict({ status: "partial", planned: 21, resolved: 15 }),
    });

    const outcome = await runWith(h);

    // The settle is a courtesy, not the proof. The door compared planned against
    // registered paths and said "15 of 21", which is a truer sentence than the
    // "the scan did not settle, try later" this flow used to answer with.
    expect(outcome).toEqual({
      kind: "partial",
      planned: 21,
      resolved: 15,
      failed: ["01_Wizard/missing.stl"],
    });
    expect(h.phases).toEqual(["listing", "extracting", "scanning", "creating"]);
  });

  it("creates nothing when the run is cancelled with the queue", async () => {
    const h = harness({ settle: async () => "cancelled" });

    const outcome = await runWith(h);

    // Leaving the queue stays the one way out that writes nothing.
    expect(outcome).toEqual({ kind: "cancelled" });
    expect(outcomeLine(outcome)).toBeNull();
    expect(h.createCalls).toEqual([]);
  });

  it("treats a settle that throws as a timeout, and still calls the door", async () => {
    const h = harness({
      settle: async () => {
        throw new Error("status read exploded");
      },
    });

    await expect(runWith(h)).resolves.toEqual({ kind: "created", project: projectRow() });
  });

  it("reports a partial extraction with the server's own counts", async () => {
    const h = harness({
      create: async () =>
        verdict({ status: "partial", planned: 21, resolved: 15, failed: ["01_Wizard/missing.stl"] }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "partial", planned: 21, resolved: 15, failed: ["01_Wizard/missing.stl"] });
    const line = outcomeLine(outcome) ?? "";
    expect(line).toContain("promised 21 entries");
    expect(line).toContain("15 entries are registered");
    expect(line).toContain("6 entries never arrived");
    expect(line).toContain('"01_Wizard/missing.stl"');
    expect(line).toContain("nothing was rolled back");
  });

  it("names every failure beyond the third as a count, not a wall of names", async () => {
    const failed = ["a.stl", "b.stl", "c.stl", "d.stl", "e.stl"];
    const h = harness({
      create: async () => verdict({ status: "partial", planned: 5, resolved: 0, failed }),
    });

    const line = outcomeLine(await runWith(h)) ?? "";

    expect(line).toContain('"a.stl", "b.stl", "c.stl" and 2 more');
  });

  it("names the twin directory when none of the planned paths resolved", async () => {
    const h = harness({
      create: async () => verdict({ status: "nothing_resolved", planned: 21, resolved: 0, failed: ["x.stl"] }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "nothing-resolved", planned: 21, twin: TWIN });
    const line = outcomeLine(outcome) ?? "";
    expect(line).toContain("none of the 21 entries");
    expect(line).toContain(`"${TWIN}/"`);
    expect(line).toContain("No project was created");
  });

  it("says an archive with nothing to register has nothing to make a project from", async () => {
    const line = outcomeLine({ kind: "nothing-resolved", planned: 0, twin: TWIN });

    expect(line).toBe(
      "No project was created: this archive holds no files the library can register, so there is nothing to make a project from.",
    );
  });

  it("tells the operator to start a library scan rather than wait for one", async () => {
    // The create door starts no scan: `create_project_from_extract` writes nothing
    // at all, so a sentence telling the operator the scan has to finish sends them
    // after a walk that was never started. Telling them where the scan lives is
    // not an invented path: the "Rescan storage folder" button is mounted by
    // `MissingFilesReviewView`, which is the Missing files queue of the same Review
    // destination this line is spoken in.
    const line = outcomeLine({ kind: "nothing-resolved", planned: 21, twin: TWIN }) ?? "";

    expect(line).toContain("start a library scan");
    expect(line).toContain("this run started none");
    expect(line).toContain("Review's Missing files queue");
    expect(line).toContain("the \"Rescan storage folder\" control");
    expect(line).toContain("then this runs again");
    // No wording that implies a walk is already running may survive here.
    expect(line).not.toMatch(/scan (has finished|is running|is in progress)/);
    expect(line).not.toContain("let the library scan finish");
  });

  it("names the project an identical second run already created", async () => {
    const h = harness({ create: async () => verdict({ status: "already_created" }) });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "already-created", project: projectRow() });
    expect(outcomeLine(outcome)).toContain("Already created as project Wizard v5.4");
    expect(outcomeLine(outcome)).toContain("no second project");
  });

  // The hand-over, pinned as identity rather than equality. A `name` field of its
  // own on these variants was a second representation of one wire fact - the row
  // and the copy could drift, and the view could be handed a client-fabricated
  // project instead of the id the server wrote. So: the very object the door's
  // `create` returned, and nothing beside it.
  it("hands the view the server's own row for a create, not a copy of it", async () => {
    // The three fields this sentence and the view render all disagree with what
    // the run itself measured - the extract reported 4096 bytes and the verdict
    // counted 21 resolved assets - so a row built from the run's own data could
    // not answer below.
    const row = projectRow({
      asset_count: 3,
      cover_thumb_url: "/thumb/asset-7.jpg?v=2",
      total_size: 987654,
    });
    const h = harness({ create: async () => verdict({ project: row }) });

    const outcome = await runWith(h);

    expect(outcome.kind).toBe("created");
    if (outcome.kind !== "created") throw new Error("expected a created verdict");
    expect(outcome.project).toBe(row);
    expect(outcome.project?.asset_count).toBe(3);
    expect(outcome.project?.cover_thumb_url).toBe("/thumb/asset-7.jpg?v=2");
    expect(outcome.project?.total_size).toBe(987654);
    // The deletion witness: the variant carries the wire's field and nothing else.
    expect(Object.keys(outcome).sort()).toEqual(["kind", "project"]);
    // And the sentence is read out of that row, not out of a copy of it.
    expect(outcomeLine(outcome)).toBe("Created project Wizard v5.4.");
  });

  it("hands the view the server's own row for an already-created verdict", async () => {
    const row = projectRow({ id: "project-0", asset_count: 8, total_size: 2048 });
    const h = harness({ create: async () => verdict({ status: "already_created", project: row }) });

    const outcome = await runWith(h);

    expect(outcome.kind).toBe("already-created");
    if (outcome.kind !== "already-created") throw new Error("expected an already-created verdict");
    expect(outcome.project).toBe(row);
    expect(outcome.project?.id).toBe("project-0");
    expect(Object.keys(outcome).sort()).toEqual(["kind", "project"]);
  });

  it("counts a create the server answered with no row as a create", async () => {
    // The branch the view falls back on: a verdict that says `created` but carries
    // no row is still a create, and its sentence has to stand without a name in it.
    const h = harness({ create: async () => verdict({ project: null }) });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "created", project: null });
    expect(outcomeLine(outcome)).toBe("Created the project from this archive.");
  });

  it("reports a failed create without pretending anything rolled back", async () => {
    const h = harness({
      create: async () => {
        throw new Error("A project with that name already exists");
      },
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "create-failed", detail: "A project with that name already exists" });
    const line = outcomeLine(outcome) ?? "";
    expect(line).toContain("files are in the library");
    expect(line).toContain("nothing was rolled back");
    // No bundle rule was consulted, so none can be the reason; the sentence must
    // not claim the queue's row is ready, which is a detection claim.
    expect(line).not.toContain("the row is ready");
  });

  it("never claims a create for a status it does not know", async () => {
    const h = harness({
      create: async () => ({ ...(verdict() as ArchiveProjectResult), status: "exploded" as never }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "create-failed", detail: null });
  });

  it("stays silent when no create response arrives at all", async () => {
    const h = harness({ create: async () => null });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "cancelled" });
    expect(outcomeLine(outcome)).toBeNull();
  });

  it("resolves even when every dependency throws", async () => {
    const boom = async (): Promise<never> => {
      throw new Error("boom");
    };
    const deps: ExtractAndCreateDeps = {
      listEntries: boom,
      extract: boom,
      settle: boom,
      create: boom,
      beforeCreate: () => {
        throw new Error("boom");
      },
      onPhase: () => {
        throw new Error("boom");
      },
      onPlan: () => {
        throw new Error("boom");
      },
    };

    await expect(runExtractAndCreate({ archivePath: ARCHIVE, item: TARGET, deps })).resolves.toEqual({
      kind: "listing-failed",
      detail: "boom",
    });
  });
});

describe("createPayload", () => {
  it("sends the archive path and the trimmed name, and nothing else", () => {
    const payload = createPayload(ARCHIVE, { name: "  Wizard v5.4  " });

    expect(Object.keys(payload).sort()).toEqual(["name", "path"]);
    expect(payload).toEqual({ path: ARCHIVE, name: "Wizard v5.4" });
  });

  it("omits the name key when the operator typed only whitespace", () => {
    const payload = createPayload(ARCHIVE, { name: "   " });

    expect("name" in payload).toBe(false);
    expect(payload).toEqual({ path: ARCHIVE });
  });

  it("sends no bundle key: the archive path is the identity", () => {
    expect(createPayload(ARCHIVE, {})).toEqual({ path: ARCHIVE });
  });
});

describe("phaseLine", () => {
  it.each([
    ["listing", "Reading the archive's file list…"],
    ["extracting", "Extracting the archive…"],
    ["scanning", "Waiting for the library scan to register the extracted files…"],
    ["creating", "Creating the project…"],
  ] as [ExtractAndCreatePhase, string][])("says the %s phase in one sentence", (phase, line) => {
    expect(phaseLine(phase)).toBe(line);
  });
});

describe("outcomeLine", () => {
  it("keeps the sentences that outlived the bundle door byte-for-byte", () => {
    expect(outcomeLine({ kind: "listing-failed", detail: null })).toBe(
      "Could not read this archive's file list. Nothing was extracted, so no project was created.",
    );
    expect(
      outcomeLine({ kind: "nothing-written", cause: "junk-only", entries: 0, detail: null }),
    ).toBe(
      "Nothing was written: the archive holds no entries at all, so nothing will register and no project was created.",
    );
    expect(outcomeLine({ kind: "created", project: projectRow({ name: "Moai" }) })).toBe(
      "Created project Moai.",
    );
    expect(outcomeLine({ kind: "created", project: null })).toBe(
      "Created the project from this archive.",
    );
    expect(outcomeLine({ kind: "already-created", project: projectRow({ name: "Moai" }) })).toBe(
      "Already created as project Moai - this run created no second project.",
    );
    expect(outcomeLine({ kind: "already-created", project: null })).toBe(
      "This archive already has a project (its name is unavailable) - this run created no second project.",
    );
    expect(outcomeLine({ kind: "cancelled" })).toBeNull();
  });

  it("never ends a sentence with two punctuation marks", () => {
    const withDetail = outcomeLine({ kind: "create-failed", detail: "no reason given" }) ?? "";
    const without = outcomeLine({ kind: "create-failed", detail: null }) ?? "";

    expect(withDetail).toContain(": no reason given.");
    expect(without).not.toContain(": .");
  });
});

// ---------------------------------------------------------------------------
// The bulk door: many archives, one click.
//
// The harness below is deliberately small: it scripts one answer per archive path
// (a value, a thrown error, or a thunk awaited later so a test can hold a phase
// open) and records every call, so each test can assert the order rather than the
// outcome alone. `listCalls` counts the *injected* listing only - the run's
// listing must never reach it, which is what makes "read each archive once"
// measurable.

/** A promise with a handle on its resolver, so a test can hold one phase open and
 *  watch what the run does - or does not do - while it waits. */
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

type BulkArchiveSpec = {
  path: string;
  label: string;
  name?: string | null;
  entries?: Answer<ArchiveEntry[]>;
  extract?: Answer<ArchiveExtractResult>;
  create?: Answer<ArchiveProjectResult | null>;
};

type BulkHarness = {
  targets: BulkArchiveTarget[];
  /** The injected listing, which only the plan pass may call. */
  listCalls: string[];
  extractCalls: { path: string; entries?: string[] | null }[];
  settleCalls: ArchiveExtractResult[][];
  createCalls: { path: string; name?: string }[];
  planProgress: string[];
  phases: string[];
  verdicts: BulkArchiveVerdict[];
  primed: string[];
  plan: () => Promise<BulkPlan>;
  run: (plan: BulkPlan) => Promise<BulkRunResult>;
};

function bulkHarness(specs: readonly BulkArchiveSpec[]): BulkHarness {
  const byPath = new Map(specs.map(spec => [spec.path, spec] as const));
  const listCalls: string[] = [];
  const extractCalls: { path: string; entries?: string[] | null }[] = [];
  const settleCalls: ArchiveExtractResult[][] = [];
  const createCalls: { path: string; name?: string }[] = [];
  const planProgress: string[] = [];
  const phases: string[] = [];
  const verdicts: BulkArchiveVerdict[] = [];
  const primed: string[] = [];

  const targets: BulkArchiveTarget[] = specs.map(spec => ({
    path: spec.path,
    label: spec.label,
    name: spec.name ?? null,
  }));

  function entriesFor(path: string): Promise<ArchiveEntry[]> {
    return answerOr(byPath.get(path)?.entries, () => archiveEntries(2));
  }

  const deps: Pick<ExtractAndCreateDeps, "extract" | "settle" | "create"> = {
    extract: async payload => {
      extractCalls.push(payload);
      return answerOr(byPath.get(payload.path)?.extract, () => extractResult({ path: payload.path }));
    },
    settle: async results => {
      settleCalls.push(results);
      return "settled";
    },
    create: async payload => {
      createCalls.push(payload);
      return answerOr(byPath.get(payload.path)?.create, () => verdict({ archive: payload.path }));
    },
  };

  return {
    targets,
    listCalls,
    extractCalls,
    settleCalls,
    createCalls,
    planProgress,
    phases,
    verdicts,
    primed,
    plan: () =>
      planExtractAndCreateMany({
        targets,
        listEntries: async path => {
          listCalls.push(path);
          return { path, entries: await entriesFor(path) };
        },
        onProgress: line => {
          planProgress.push(line);
        },
      }),
    run: plan =>
      runExtractAndCreateMany({
        plan,
        deps,
        beforeCreate: archive => {
          primed.push(archive.path);
        },
        onProgress: line => {
          phases.push(line);
        },
        onVerdict: verdict => {
          verdicts.push(verdict);
        },
      }),
  };
}

/** One archive the door answers "created" for, named after its label - the shape
 *  a happy queue is made of. */
function createdRow(label: string): BulkArchiveSpec {
  return {
    path: `import/${label}.zip`,
    label,
    create: async () => verdict({ archive: `import/${label}.zip`, project: projectRow({ name: label }) }),
  };
}

/** One archive the door answers "already created" for, with the server's own zero
 *  counts: the repeat-click shape. */
function alreadyCreatedRow(label: string): BulkArchiveSpec {
  return {
    path: `import/${label}.zip`,
    label,
    create: async () =>
      verdict({
        archive: `import/${label}.zip`,
        status: "already_created",
        planned: 0,
        resolved: 0,
        project: projectRow({ name: label }),
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
  it("says the aggregate payload before anything is written", async () => {
    const h = bulkHarness([
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
    // archive and no bytes.
    expect(h.extractCalls).toEqual([]);
    expect(h.createCalls).toEqual([]);
    expect(h.planProgress).toEqual([
      "Reading 1 of 3 archive file lists…",
      "Reading 2 of 3 archive file lists…",
      "Reading 3 of 3 archive file lists…",
    ]);
  });

  it("names an unreadable archive rather than folding it in as a zero", async () => {
    const h = bulkHarness([
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
  });

  it("says a click with nothing readable would write nothing", async () => {
    const h = bulkHarness([
      { path: "import/Alpha.zip", label: "Alpha", entries: new Error("Invalid zip file") },
      { path: "import/Beta.zip", label: "Beta", entries: new Error("Archive is not readable") },
    ]);

    const plan = await h.plan();

    // No aggregate may stand in for the payload when no archive could be read: the
    // header says the click would write nothing, and then each failure is named.
    expect(bulkPlanLines(plan)).toEqual([
      "None of the 2 archives could be read, so this click would write nothing.",
      'Could not read the file list of "Alpha" (Invalid zip file) - it will be skipped and nothing in it will be written.',
      'Could not read the file list of "Beta" (Archive is not readable) - it will be skipped and nothing in it will be written.',
    ]);
  });

  it("says a click whose archives hold nothing extractable would write nothing", async () => {
    const h = bulkHarness([
      { path: "import/Alpha.zip", label: "Alpha", entries: [{ path: ".DS_Store", size: 10 }] },
      { path: "import/Beta.zip", label: "Beta", entries: [{ path: "Alpha/", size: 0 }, { path: "__MACOSX/", size: 0 }] },
    ]);

    const plan = await h.plan();

    // Both archives read fine, and both hold only junk or directory members - so
    // every count is zero and the aggregate header has no payload to name. It says
    // the truth once, instead of dangling a colon, and then each archive names
    // itself the way `bulkArchiveLine` already does.
    expect(bulkPlanLines(plan)).toEqual([
      "Nothing in these 2 archives can be extracted, so this click would write nothing.",
      '"Alpha" holds nothing extractable, so extracting it writes nothing and no project will be made from it.',
      '"Beta" holds nothing extractable, so extracting it writes nothing and no project will be made from it.',
    ]);

    const single = await bulkHarness([
      { path: "import/Alpha.zip", label: "Alpha", entries: [{ path: ".DS_Store", size: 10 }] },
    ]).plan();
    expect(bulkPlanLines(single)).toEqual([
      "Nothing in this archive can be extracted, so this click would write nothing.",
      '"Alpha" holds nothing extractable, so extracting it writes nothing and no project will be made from it.',
    ]);

    // A mixed plan must not over-claim: the unreadable archive is unread, not
    // empty, so the subject counts the one readable archive and the failure is
    // still named on its own line.
    const mixed = await bulkHarness([
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
    const h = bulkHarness([]);

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

describe("runExtractAndCreateMany", () => {
  it("runs one archive at a time: no create starts before the previous one resolves", async () => {
    const started = deferred<void>();
    const gate = deferred<ArchiveProjectResult | null>();
    const h = bulkHarness([
      {
        path: "import/Alpha.zip",
        label: "Alpha",
        create: async () => {
          started.resolve(undefined);
          return gate.promise;
        },
      },
      { path: "import/Beta.zip", label: "Beta" },
    ]);
    const plan = await h.plan();

    const running = h.run(plan);
    await started.promise;

    // Alpha's create is open, so nothing of Beta's may have begun: the queue is
    // sequential by contract, not merely by node ordering.
    expect(h.extractCalls.map(call => call.path)).toEqual(["import/Alpha.zip"]);
    expect(h.createCalls.map(call => call.path)).toEqual(["import/Alpha.zip"]);

    gate.resolve(verdict({ archive: "import/Alpha.zip", project: projectRow({ name: "Alpha" }) }));
    const result = await running;

    expect(h.extractCalls.map(call => call.path)).toEqual(["import/Alpha.zip", "import/Beta.zip"]);
    expect(h.createCalls.map(call => call.path)).toEqual(["import/Alpha.zip", "import/Beta.zip"]);
    expect(result.created).toBe(2);
  });

  it("reads each archive's listing once across the plan and the run", async () => {
    const h = bulkHarness([
      { path: "import/Alpha.zip", label: "Alpha" },
      { path: "import/Beta.zip", label: "Beta" },
      { path: "import/Gamma.zip", label: "Gamma" },
    ]);
    const plan = await h.plan();
    const afterPlan = [...h.listCalls];

    await h.run(plan);

    // Every archive listed exactly once by the confirm pass, and the run's own
    // `listEntries` is the memoised one: the injected listing was not touched
    // again, which is the only way to measure "zero listing calls in the run".
    expect(afterPlan).toEqual(["import/Alpha.zip", "import/Beta.zip", "import/Gamma.zip"]);
    expect(h.listCalls).toEqual(afterPlan);
  });

  it("names each archive in its own verdict and keeps going", async () => {
    const h = bulkHarness([
      createdRow("Alpha"),
      createdRow("Beta"),
      createdRow("Gamma"),
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
    // Every archive was primed and waited on in its own turn.
    expect(h.primed).toEqual(["import/Alpha.zip", "import/Beta.zip", "import/Gamma.zip"]);
    expect(h.settleCalls).toHaveLength(3);
  });

  it("a listing that failed is named, skips its write, and the rest still run", async () => {
    const h = bulkHarness([
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
    // No extract, no settle, no create for the unreadable one - and the archives
    // on either side of it were still run.
    expect(h.extractCalls.map(call => call.path)).toEqual(["import/Alpha.zip", "import/Gamma.zip"]);
    expect(h.createCalls.map(call => call.path)).toEqual(["import/Alpha.zip", "import/Gamma.zip"]);
    expect(result.created).toBe(2);
    expect(result.written.map(target => target.label)).toEqual(["Alpha", "Gamma"]);
  });

  it("an extract that threw is named as nothing-written and the rest still run", async () => {
    const h = bulkHarness([
      { path: "import/Alpha.zip", label: "Alpha" },
      { path: "import/Beta.zip", label: "Beta", extract: new Error("disk is full") },
      { path: "import/Gamma.zip", label: "Gamma" },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.verdicts[1].outcome).toEqual({
      kind: "nothing-written",
      cause: "extract-failed",
      entries: 2,
      detail: "disk is full",
    });
    expect(bulkVerdictLines(result.verdicts)[1]).toContain("0 of 2 entries");
    expect(h.createCalls.map(call => call.path)).toEqual(["import/Alpha.zip", "import/Gamma.zip"]);
    expect(result.created).toBe(2);
    // Beta was visited and its extract spoke, but nothing was written there.
    expect(result.written.map(target => target.label)).toEqual(["Alpha", "Gamma"]);
  });

  it("stops when the queue is gone and says how many it never touched", async () => {
    const h = bulkHarness([
      { path: "import/Alpha.zip", label: "Alpha" },
      // A null response is the hook's "no response arrived": the queue was left.
      { path: "import/Beta.zip", label: "Beta", create: async () => null },
      { path: "import/Gamma.zip", label: "Gamma" },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.stopped).toBe(true);
    // Two verdicts: Alpha's create and Beta's cancellation. The untouched tail
    // gets none, because nothing happened there.
    expect(result.verdicts).toHaveLength(2);
    expect(h.extractCalls.map(call => call.path)).toEqual(["import/Alpha.zip", "import/Beta.zip"]);
    expect(bulkStopLine(result)).toBe("Stopped with 2 of 3 archives done - 1 archive was never touched.");
    // `cancelled` speaks no verdict line, exactly as the single-archive door does.
    expect(bulkVerdictLines(result.verdicts)).toHaveLength(1);
  });

  it("never lands on an arbitrary one of many", async () => {
    const h = bulkHarness([
      createdRow("Alpha"),
      createdRow("Beta"),
      createdRow("Gamma"),
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.created).toBe(3);
    // Three projects were made; picking one to land on would hide the other two.
    expect(result.landing).toBeNull();
    expect(result.targets.map(target => target.label)).toEqual(["Alpha", "Beta", "Gamma"]);
  });

  it("does not land on a one-archive already-created click", async () => {
    const h = bulkHarness([
      {
        path: "import/Alpha.zip",
        label: "Alpha",
        // A repeat click: the server's idempotency lookup answers with the project
        // it made the first time, and this run writes nothing.
        create: alreadyCreatedRow("Alpha").create,
      },
    ]);
    const plan = await h.plan();

    const result = await h.run(plan);

    expect(result.created).toBe(0);
    expect(result.alreadyCreated).toBe(1);
    // Success with no write: reported, counted, and not landed on - the operator is
    // in the queue, and the project already existed before this click.
    expect(result.landing).toBeNull();
    expect(result.verdicts[0].outcome.kind).toBe("already-created");
  });

  // Measured at HEAD: a repeat run answers `already_created` with `planned: 0,
  // resolved: 0` because `curiarium/api/archive_project_service.py:181-196` runs the
  // idempotency lookup by archive path BEFORE opening the archive. `partial` is
  // therefore only reachable on a first run - the fixture below depends on both.
  it("reports a clean archive, a partial one and an already-extracted one in one queue", async () => {
    const h = bulkHarness([
      {
        path: "import/Alpha.zip",
        label: "Alpha",
        create: async () =>
          verdict({ archive: "import/Alpha.zip", project: projectRow({ name: "Alpha" }) }),
      },
      {
        path: "import/Beta.zip",
        label: "Beta",
        // The server's own counts: three planned, two registered, one named miss.
        create: async () =>
          verdict({
            archive: "import/Beta.zip",
            status: "partial",
            planned: 3,
            resolved: 2,
            failed: ["beta_foot.stl"],
          }),
      },
      {
        path: "import/Gamma.zip",
        label: "Gamma",
        create: async () =>
          verdict({
            archive: "import/Gamma.zip",
            status: "already_created",
            planned: 0,
            resolved: 0,
            project: projectRow({ name: "Gamma" }),
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
      "partial",
      "already-created",
    ]);
    expect(result.verdicts.map(item => item.label)).toEqual(["Alpha", "Beta", "Gamma"]);
    // Each verdict names its own archive, and the sentences are the single-archive
    // ones, untouched.
    const lines = bulkVerdictLines(result.verdicts);
    expect(lines).toHaveLength(3);
    expect(lines[0]).toBe("Alpha - Created project Alpha.");
    expect(lines[1]).toContain("Beta - ");
    expect(lines[1]).toContain("1 entry never arrived");
    expect(lines[2].startsWith("Gamma - Already created")).toBe(true);
    // Beta created NOTHING: three archives were asked, one project was made.
    expect(h.createCalls.map(call => call.path)).toEqual([
      "import/Alpha.zip",
      "import/Beta.zip",
      "import/Gamma.zip",
    ]);
    expect(result.verdicts.filter(item => item.outcome.kind === "created")).toHaveLength(1);
    expect(bulkSummaryLine(result)).toBe("Created 1 project.");
  });

  it("says each archive's phase over the existing phase line", async () => {
    expect(bulkPhaseLine(2, 3, "extracting")).toBe("Archive 2 of 3 - Extracting the archive…");

    expect(bulkSummaryLine({ created: 0 })).toBeNull();
    expect(bulkStopLine({ stopped: false, targets: [], verdicts: [] })).toBeNull();
    // Nothing was created, so the summary has nothing to claim - and a run that
    // reached the end never says what it did not touch.
    expect(bulkSummaryLine({ created: 3 })).toBe("Created 3 projects.");
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
});
