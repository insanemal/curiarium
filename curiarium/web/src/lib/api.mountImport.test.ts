// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getMountImportStatus } from "./api";
import { storeToken } from "./auth";

type MockResponse = {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
  headers: Headers;
};

type MockFetch = ReturnType<typeof vi.fn<(input: string, init?: RequestInit) => Promise<MockResponse>>>;

function respond(
  payload: unknown,
  init: { ok?: boolean; status?: number; body?: string } = {},
): MockResponse {
  const body = init.body ?? JSON.stringify(payload);
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => payload,
    text: async () => body,
    headers: new Headers(),
  };
}

function installFetch(response: MockResponse): MockFetch {
  const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<MockResponse>>(
    async () => response,
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function onlyCall(fetchMock: MockFetch): { url: string; init: RequestInit } {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0];
  if (!init) throw new Error("fetch was called without an init object");
  return { url, init };
}

/** jsdom's localStorage can resolve to `undefined` under Node's global; install a real one. */
function installMemoryStorage(): void {
  const store = new Map<string, string>();
  const storage: Storage = {
    get length(): number {
      return store.size;
    },
    clear(): void {
      store.clear();
    },
    getItem(key: string): string | null {
      return store.has(key) ? store.get(key)! : null;
    },
    key(index: number): string | null {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string): void {
      store.delete(key);
    },
    setItem(key: string, value: string): void {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
}

/** The one step a `mount-import` operation plans, with the report the runner
 *  writes into its `result`. The times are epoch seconds, as the walk's own
 *  clock reports them. */
const report = {
  outcome: "registered",
  mode: "copy",
  root: "/mnt/models",
  started_at: 1_700_000_000,
  finished_at: 1_700_000_042,
  registered: 4,
  known: 2,
  ignored: 1,
  failed: 0,
  rejected: 0,
  discovered: 7,
  skipped: 3,
  reason: null,
  errors: [],
};

type StepOverrides = {
  status?: string;
  result?: Record<string, unknown> | null;
  error?: string | null;
};

function makeStep(overrides: StepOverrides = {}): Record<string, unknown> {
  return {
    index: 0,
    op: "mount-import",
    status: "done",
    result: report,
    error: null,
    started_at: "2023-11-14T22:13:20Z",
    finished_at: "2023-11-14T22:14:02Z",
    ...overrides,
  };
}

function makeRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "op-1",
    workflow: "mount-import",
    target: "/mnt/models",
    status: "done",
    submitted_by: "system",
    created_at: "2023-11-14T22:13:19Z",
    started_at: "2023-11-14T22:13:20Z",
    finished_at: "2023-11-14T22:14:02Z",
    error: null,
    undo_state: "unavailable",
    superseded_by: null,
    steps: [makeStep()],
    ...overrides,
  };
}

const finished = {
  status: "done",
  running: false,
  outcome: "registered",
  mode: "copy",
  root: "/mnt/models",
  reason: null,
  error: null,
  started_at: 1_700_000_000,
  finished_at: 1_700_000_042,
  registered: 4,
  known: 2,
  ignored: 1,
  failed: 0,
  rejected: 0,
  discovered: 7,
  skipped: 3,
  errors: [],
};

/** The empty answer: what a ledger with no `mount-import` row says. */
const empty = {
  status: null,
  running: false,
  outcome: null,
  mode: null,
  root: null,
  reason: null,
  error: null,
  started_at: null,
  finished_at: null,
  registered: 0,
  known: 0,
  ignored: 0,
  failed: 0,
  rejected: 0,
  discovered: 0,
  skipped: 0,
  errors: [],
};

describe("getMountImportStatus", () => {
  beforeEach(() => {
    installMemoryStorage();
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks the ledger for the newest mount-import row and derives the status from it", async () => {
    const fetchMock = installFetch(respond([makeRow()]));

    await expect(getMountImportStatus()).resolves.toEqual(finished);

    const { url, init } = onlyCall(fetchMock);
    expect(url.endsWith("/operations?workflow=mount-import&limit=1")).toBe(true);
    expect(init.method).toBeUndefined();
  });

  it("answers the empty status for an empty ledger, because no walk is a fact", async () => {
    installFetch(respond([]));

    await expect(getMountImportStatus()).resolves.toEqual(empty);
  });

  it("reads the report's own epoch seconds in preference to the row's ISO times", async () => {
    // The report says 1_700_000_000; the row's started_at is 2023-11-14T22:13:20Z
    // (the same instant), and its finished_at is a second later than the report's.
    installFetch(
      respond([
        makeRow({
          finished_at: "2023-11-14T22:14:03Z",
          steps: [makeStep()],
        }),
      ]),
    );

    const status = await getMountImportStatus();

    expect(status.started_at).toBe(1_700_000_000);
    expect(status.finished_at).toBe(1_700_000_042);
  });

  it("converts the row's ISO times when the step never reported them", async () => {
    installFetch(
      respond([
        makeRow({
          status: "failed",
          error: "Permission denied on /mnt/models",
          started_at: "2023-11-14T22:13:20Z",
          finished_at: "2023-11-14T22:14:02Z",
          steps: [makeStep({ status: "failed", result: null, error: "The mount is unreadable" })],
        }),
      ]),
    );

    const status = await getMountImportStatus();

    expect(status.started_at).toBe(1_700_000_000);
    expect(status.finished_at).toBe(1_700_000_042);
  });

  it("keeps a failed step's own sentence and zero counters", async () => {
    installFetch(
      respond([
        makeRow({
          status: "failed",
          error: "Permission denied on /mnt/models",
          steps: [makeStep({ status: "failed", result: null, error: "The mount is unreadable" })],
        }),
      ]),
    );

    const status = await getMountImportStatus();

    expect(status.status).toBe("failed");
    expect(status.error).toBe("The mount is unreadable");
    // A failed step wrote no report at all, so nothing was counted.
    expect(status.discovered).toBe(0);
    expect(status.registered).toBe(0);
    expect(status.errors).toEqual([]);
    expect(status.outcome).toBeNull();
  });

  it("falls back to the operation's sentence when the failed step carries none", async () => {
    installFetch(
      respond([
        makeRow({
          status: "failed",
          error: "Permission denied on /mnt/models",
          steps: [makeStep({ status: "failed", result: null, error: null })],
        }),
      ]),
    );

    const status = await getMountImportStatus();

    expect(status.error).toBe("Permission denied on /mnt/models");
  });

  it("keeps a skipped walk's reason", async () => {
    installFetch(
      respond([
        makeRow({
          steps: [
            makeStep({
              result: { ...report, outcome: "skipped", reason: "Mount path is not configured" },
            }),
          ],
        }),
      ]),
    );

    const status = await getMountImportStatus();

    expect(status.outcome).toBe("skipped");
    expect(status.reason).toBe("Mount path is not configured");
    expect(status.running).toBe(false);
  });

  it("reports a queued row as running with nothing reported yet", async () => {
    installFetch(respond([makeRow({ status: "queued", steps: [makeStep({ result: null })] })]));

    const status = await getMountImportStatus();

    expect(status.running).toBe(true);
    expect(status.outcome).toBeNull();
    expect(status.discovered).toBe(0);
    expect(status.mode).toBeNull();
  });

  it("does not count a cancelling row as running", async () => {
    installFetch(respond([makeRow({ status: "cancelling" })]));

    await expect(getMountImportStatus()).resolves.toMatchObject({ running: false });
  });

  it("reads the per-file failures the step reported", async () => {
    installFetch(
      respond([
        makeRow({
          steps: [
            makeStep({
              result: { ...report, failed: 2, errors: ["a.stl: unreadable", 7, "b.3mf: truncated"] },
            }),
          ],
        }),
      ]),
    );

    const status = await getMountImportStatus();

    // A non-string entry is the server's JSON going off-shape, not a report.
    expect(status.errors).toEqual(["a.stl: unreadable", "b.3mf: truncated"]);
  });

  it("sends the bearer token like the other ledger reads", async () => {
    storeToken("token-1");
    const fetchMock = installFetch(respond([makeRow()]));

    await getMountImportStatus();

    const { init } = onlyCall(fetchMock);
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer token-1");
  });

  it("throws the ledger read's message on a failing read", async () => {
    // `listOperations` goes through `assertOk`, which answers the read's own
    // sentence rather than the body, so that is what the hook will surface.
    installFetch(respond(null, { ok: false, status: 500, body: "" }));

    const error = await getMountImportStatus().catch((err: unknown) => err);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toBe("Failed to load operations");
  });
});
