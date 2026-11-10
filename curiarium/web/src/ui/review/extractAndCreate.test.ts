import { describe, expect, it } from "vitest";

import type {
  ArchiveEntry,
  ArchiveExtractResult,
  ArchiveProjectResult,
} from "../../lib/api/imports";
import type { Project } from "../../lib/api/projects";
import type { SettleOutcome } from "../../lib/scanSettle";
import {
  createPayload,
  outcomeLine,
  phaseLine,
  runExtractAndCreate,
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
