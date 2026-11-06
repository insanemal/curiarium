import { describe, expect, it } from "vitest";

import type {
  BundleCreateItem,
  BundleCreateItemResult,
  BundleCreateResponse,
} from "../../lib/api/bundles";
import type { ArchiveEntry, ArchiveExtractResult } from "../../lib/api/imports";
import type { SettleOutcome } from "../../lib/scanSettle";
import {
  createItem,
  outcomeLine,
  phaseLine,
  runExtractAndCreate,
  type ExtractAndCreateDeps,
  type ExtractAndCreateOutcome,
  type ExtractAndCreatePhase,
  type ExtractAndCreateTarget,
} from "./extractAndCreate";

const KEY = "Moai statue";
const ARCHIVE = "Moai.zip";

/** The row the action runs on; the operator's name arrives already trimmed. */
const TARGET: ExtractAndCreateTarget = { key: KEY, name: "Moai" };

function archiveEntries(count: number): ArchiveEntry[] {
  return Array.from({ length: count }, (_unused, index) => ({
    path: `part-${index}.stl`,
    size: 1024,
  }));
}

function extractResult(overrides: Partial<ArchiveExtractResult> = {}): ArchiveExtractResult {
  return {
    path: ARCHIVE,
    destination: "Moai statue",
    files: 12,
    bytes: 4096,
    already_present: false,
    scan_started: true,
    ...overrides,
  };
}

function resultRow(overrides: Partial<BundleCreateItemResult> = {}): BundleCreateItemResult {
  return {
    key: KEY,
    status: "created",
    project_id: "project-1",
    name: "Moai",
    assets_added: 12,
    skipped_assets: 0,
    failed_assets: [],
    reason: null,
    ...overrides,
  };
}

/** The counts derive from the one row, so a fixture cannot claim a count its
 *  own `results` contradict. */
function createResponse(overrides: Partial<BundleCreateItemResult> = {}): BundleCreateResponse {
  const result = resultRow(overrides);
  return {
    created: result.status === "created" ? 1 : 0,
    skipped: result.status === "skipped" ? 1 : 0,
    blocked: result.status === "blocked" ? 1 : 0,
    failed: result.status === "failed" ? 1 : 0,
    results: [result],
  };
}

type HarnessConfig = {
  listing?: () => Promise<{ path: string; entries: ArchiveEntry[] }>;
  extract?: () => Promise<ArchiveExtractResult>;
  settle?: () => Promise<SettleOutcome>;
  create?: (items: BundleCreateItem[]) => Promise<BundleCreateResponse | null>;
};

type Harness = {
  deps: ExtractAndCreateDeps;
  phases: ExtractAndCreatePhase[];
  listCalls: string[];
  extractCalls: { path: string; entries?: string[] | null }[];
  settleCalls: ArchiveExtractResult[][];
  createCalls: BundleCreateItem[][];
  primed: () => number;
};

/** Deps that record what they were asked to do; every member answers the happy
 *  path unless the test overrides it. */
function harness(config: HarnessConfig = {}): Harness {
  const phases: ExtractAndCreatePhase[] = [];
  const listCalls: string[] = [];
  const extractCalls: { path: string; entries?: string[] | null }[] = [];
  const settleCalls: ArchiveExtractResult[][] = [];
  const createCalls: BundleCreateItem[][] = [];
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
    create: async items => {
      createCalls.push(items);
      if (config.create) return config.create(items);
      return createResponse();
    },
    beforeCreate: () => {
      cover.primes += 1;
    },
    onPhase: phase => {
      phases.push(phase);
    },
  };

  return {
    deps,
    phases,
    listCalls,
    extractCalls,
    settleCalls,
    createCalls,
    primed: () => cover.primes,
  };
}

function runWith(h: Harness, item: ExtractAndCreateTarget = TARGET): Promise<ExtractAndCreateOutcome> {
  return runExtractAndCreate({ archivePath: ARCHIVE, item, deps: h.deps });
}

describe("runExtractAndCreate", () => {
  it("lists, extracts, settles and creates, in that order", async () => {
    const h = harness();

    const outcome = await runWith(h);

    expect(h.phases).toEqual(["listing", "extracting", "scanning", "creating"]);
    expect(h.listCalls).toEqual([ARCHIVE]);
    expect(h.extractCalls).toEqual([{ path: ARCHIVE, entries: null }]);
    expect(h.settleCalls).toEqual([[extractResult()]]);
    expect(h.createCalls).toEqual([[{ key: KEY, name: "Moai" }]]);
    expect(h.primed()).toBe(1);
    expect(outcome).toEqual({ kind: "created", name: "Moai" });
    expect(outcomeLine(outcome)).toContain("Created project Moai");
  });

  it("claims no count and no create when the listing fails", async () => {
    const h = harness({
      listing: async () => {
        throw new Error("Archive is not readable");
      },
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "listing-failed", detail: "Archive is not readable" });
    expect(h.extractCalls).toEqual([]);
    expect(h.createCalls).toEqual([]);
    expect(h.phases).toEqual(["listing"]);
  });

  it("reports the listing's length when the extraction fails, and never creates", async () => {
    const h = harness({
      listing: async () => ({ path: ARCHIVE, entries: archiveEntries(2) }),
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
    expect(h.createCalls).toEqual([]);
    expect(h.settleCalls).toEqual([]);
    const line = outcomeLine(outcome);
    expect(line).toContain("0 of 2 entries");
    expect(line).toContain("disk is full");
  });

  it("stops at an archive nothing could be extracted from", async () => {
    const h = harness({
      extract: async () => extractResult({ files: 0, already_present: false }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({
      kind: "nothing-written",
      cause: "junk-only",
      entries: 2,
      detail: null,
    });
    expect(h.settleCalls).toEqual([]);
    expect(h.createCalls).toEqual([]);
    expect(h.phases).toEqual(["listing", "extracting"]);
    const line = outcomeLine(outcome);
    expect(line).toContain("0 of 2 entries");
    expect(line).toContain("Nothing will register");
  });

  it("skips the settle when every entry was already present, and still creates", async () => {
    const h = harness({
      extract: async () => extractResult({ files: 0, already_present: true }),
    });

    const outcome = await runWith(h);

    expect(h.phases).toEqual(["listing", "extracting", "creating"]);
    expect(h.settleCalls).toEqual([]);
    expect(h.createCalls).toHaveLength(1);
    expect(outcome).toEqual({ kind: "created", name: "Moai" });
  });

  it("abandons the create when the scan does not settle", async () => {
    const h = harness({ settle: async () => "timeout" });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "settle-abandoned", entries: 2 });
    expect(h.createCalls).toEqual([]);
    expect(outcomeLine(outcome)).toContain("did not settle");
  });

  it("stays silent when the settle is cancelled with the queue", async () => {
    const h = harness({ settle: async () => "cancelled" });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "cancelled" });
    expect(h.createCalls).toEqual([]);
    expect(outcomeLine(outcome)).toBeNull();
  });

  it("abandons rather than creating when the settle itself throws", async () => {
    const h = harness({
      settle: async () => {
        throw new Error("status read exploded");
      },
    });

    const outcome = await runWith(h);

    // No proof the scan finished is not proof that it did.
    expect(outcome).toEqual({ kind: "settle-abandoned", entries: 2 });
    expect(h.createCalls).toEqual([]);
  });

  it("names the project an identical second run already created", async () => {
    // The id is not what the sentence speaks, so the fixture keeps names only.
    const projects = new Map<string, string>();
    const h = harness({
      create: async items => {
        const item = items[0];
        const existing = projects.get(item.key);
        if (existing) {
          return createResponse({
            status: "skipped",
            reason: "already_created",
            project_id: "project-1",
            name: existing,
          });
        }
        const name = item.name ?? "Moai statue";
        projects.set(item.key, name);
        return createResponse({ project_id: "project-1", name });
      },
    });

    const first = await runWith(h);
    expect(first).toEqual({ kind: "created", name: "Moai" });
    expect(projects.size).toBe(1);

    const second = await runWith(h);
    expect(second).toEqual({ kind: "already-created", name: "Moai" });
    expect(projects.size).toBe(1);

    const line = outcomeLine(second);
    expect(line).toContain("Already created");
    expect(line).toContain("no second project");
  });

  it("reports a failed create without pretending anything rolled back", async () => {
    const h = harness({
      create: async () =>
        createResponse({
          status: "failed",
          project_id: null,
          name: null,
          reason: "A project with that name already exists",
        }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({
      kind: "create-failed",
      detail: "A project with that name already exists",
    });
    const line = outcomeLine(outcome);
    expect(line).toContain("files are in the library");
    expect(line).toContain("nothing was rolled back");
    expect(line).toContain("A project with that name already exists");
    // The extract is all-or-nothing on the server, so there is no partial write
    // for a rollback door to undo - a fact the deps TYPE carries, not one this
    // test's own object can prove.
  });

  it("does not read a skip with an unknown reason as a creation", async () => {
    // `already_created` is the only skip that means "the project is there". Any
    // other skip reason says nothing was created, so claiming otherwise would
    // be the one lie this door exists to avoid.
    const h = harness({
      create: async () =>
        createResponse({ status: "skipped", reason: "exclusion_changed", name: null }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "create-failed", detail: "exclusion_changed" });
    expect(outcomeLine(outcome)).toContain("exclusion_changed");
  });

  it("renders the run's verdict even when the cover prime throws", async () => {
    const h = harness();
    h.deps.beforeCreate = () => {
      throw new Error("cover prime exploded");
    };

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "created", name: "Moai" });
    expect(h.createCalls).toHaveLength(1);
  });

  it("keeps the server's reason when the row is still blocked", async () => {
    const h = harness({
      create: async () =>
        createResponse({ status: "blocked", project_id: null, name: null, reason: "not_scanned" }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "still-blocked", reason: "not_scanned", wrote: true });
    const line = outcomeLine(outcome);
    expect(line).toContain("no model file registered by a scan");
    expect(line).toContain("The extraction stands");
    expect(line).not.toContain("zip only - extract");
  });

  it("answers archive_only with the pairing it still lacks, not a re-extract", async () => {
    const h = harness({
      create: async () =>
        createResponse({ status: "blocked", project_id: null, name: null, reason: "archive_only" }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "still-blocked", reason: "archive_only", wrote: true });
    const line = outcomeLine(outcome);
    expect(line).toContain("has not paired the extracted folder with the archive");
    expect(line).not.toContain("extract the archive in place");
    expect(line).toContain("The extraction stands");
  });

  it("does not claim a write on the already-present route", async () => {
    const h = harness({
      extract: async () => extractResult({ files: 0, already_present: true }),
      create: async () =>
        createResponse({ status: "blocked", project_id: null, name: null, reason: "not_scanned" }),
    });

    const outcome = await runWith(h);

    expect(h.settleCalls).toEqual([]);
    expect(outcome).toEqual({ kind: "still-blocked", reason: "not_scanned", wrote: false });
    const line = outcomeLine(outcome);
    expect(line).toContain("wrote nothing to the library");
    expect(line).not.toContain("The extraction stands");
  });

  it("never claims a create for a status it does not know", async () => {
    const h = harness({
      create: async () =>
        createResponse({ status: "exploded", project_id: null, name: null, reason: "something new" }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "create-failed", detail: "something new" });
  });

  it("refuses a response that names another row's project", async () => {
    const h = harness({
      create: async () => ({
        created: 1,
        skipped: 0,
        blocked: 0,
        failed: 0,
        results: [resultRow({ key: "Another row" })],
      }),
    });

    const outcome = await runWith(h);

    // The server echoes the request's key, so a result for another key answers
    // nothing about this row and must not be borrowed as its verdict.
    expect(outcome).toEqual({
      kind: "create-failed",
      detail: "The server returned no result for this bundle.",
    });
  });

  it("refuses a response with no results at all", async () => {
    const h = harness({
      create: async () => ({ created: 0, skipped: 0, blocked: 0, failed: 0, results: [] }),
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({
      kind: "create-failed",
      detail: "The server returned no result for this bundle.",
    });
  });

  it("turns a throwing create into a failure that carries the message", async () => {
    const h = harness({
      create: async () => {
        throw new Error("Network is down");
      },
    });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "create-failed", detail: "Network is down" });
    expect(outcomeLine(outcome)).toContain("Network is down");
  });

  it("stays silent when no create response arrives at all", async () => {
    const h = harness({ create: async () => null });

    const outcome = await runWith(h);

    expect(outcome).toEqual({ kind: "cancelled" });
    expect(outcomeLine(outcome)).toBeNull();
  });

  it("sends one item for a compound-shaped row, and nothing else", async () => {
    const h = harness();

    await runWith(h, { key: "Compound kit", name: "Compound kit" });

    expect(h.createCalls).toHaveLength(1);
    const [items] = h.createCalls;
    expect(items).toHaveLength(1);
    const [item] = items;
    expect(item).toEqual({ key: "Compound kit", name: "Compound kit" });
    // No group exclusions, no per-group items and no `name_only`: the server
    // builds the groups from the row itself.
    expect(Object.keys(item)).toEqual(["key", "name"]);
  });

  it("omits the name key when the operator typed only whitespace", async () => {
    const h = harness();

    await runWith(h, { key: "Compound kit", name: "   " });

    const [item] = h.createCalls[0];
    expect(item).toEqual({ key: "Compound kit" });
    expect("name" in item).toBe(false);
  });

  it("resolves even when every dependency throws", async () => {
    const boom = (): never => {
      throw new Error("boom");
    };
    const deps: ExtractAndCreateDeps = {
      listEntries: async () => boom(),
      extract: async () => boom(),
      settle: async () => boom(),
      create: async () => boom(),
      beforeCreate: boom,
      onPhase: boom,
    };

    await expect(
      runExtractAndCreate({ archivePath: ARCHIVE, item: TARGET, deps }),
    ).resolves.toEqual({ kind: "listing-failed", detail: "boom" });
  });
});

describe("createItem", () => {
  it("carries the trimmed name when there is one", () => {
    expect(createItem({ key: KEY, name: "  Moai  " })).toEqual({ key: KEY, name: "Moai" });
  });

  it("omits the name key for a null or blank name", () => {
    expect(createItem({ key: KEY, name: null })).toEqual({ key: KEY });
    expect(createItem({ key: KEY })).toEqual({ key: KEY });
    expect(createItem({ key: KEY, name: " " })).toEqual({ key: KEY });
  });
});

describe("phaseLine", () => {
  const lines: [ExtractAndCreatePhase, string][] = [
    ["listing", "Reading the archive's file list…"],
    ["extracting", "Extracting the archive…"],
    ["scanning", "Waiting for the library scan to register the extracted files…"],
    ["creating", "Creating the project…"],
  ];

  it.each(lines)("says what %s is doing", (phase, line) => {
    expect(phaseLine(phase)).toBe(line);
  });
});

describe("outcomeLine", () => {
  it("ends a detail-less listing failure with a single full stop", () => {
    expect(outcomeLine({ kind: "listing-failed", detail: null })).toBe(
      "Could not read this archive's file list. Nothing was extracted, so no project was created.",
    );
  });

  it("speaks an empty archive as empty rather than as zero of zero", () => {
    expect(
      outcomeLine({ kind: "nothing-written", cause: "junk-only", entries: 0, detail: null }),
    ).toBe(
      "Nothing was written: the archive holds no entries at all, so nothing will register and no project was created.",
    );
  });

  it("names the project when it has a name, and says so plainly when it does not", () => {
    expect(outcomeLine({ kind: "created", name: "Moai" })).toBe("Created project Moai.");
    expect(outcomeLine({ kind: "created", name: null })).toBe(
      "Created the project from this archive.",
    );
  });

  it("says whose project an already-created row is, or admits the name is gone", () => {
    expect(outcomeLine({ kind: "already-created", name: "Moai" })).toBe(
      "Already created as project Moai - this run created no second project.",
    );
    expect(outcomeLine({ kind: "already-created", name: null })).toBe(
      "This bundle already has a project (its name is unavailable) - this run created no second project.",
    );
  });

  it("falls back to the server's own reason for an unknown blocked label", () => {
    expect(
      outcomeLine({ kind: "still-blocked", reason: "a_reason_from_the_future", wrote: true }),
    ).toBe(
      "No project was created: a_reason_from_the_future. The extraction stands - the files are in the library.",
    );
  });

  it("says nothing for a cancelled run", () => {
    expect(outcomeLine({ kind: "cancelled" })).toBeNull();
  });
});
