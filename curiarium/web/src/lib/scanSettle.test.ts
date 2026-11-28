// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The two waiters are mocked; everything else in `lib/api/settings` is the real
// module, because the scan status is DERIVED from an operation row now and that
// mapper is the thing the cases below have to pin.
vi.mock("./api/settings", async importOriginal => ({
  ...(await importOriginal<typeof import("./api/settings")>()),
  getLibraryScanStatus: vi.fn(),
  startLibraryScan: vi.fn(),
}));

// `UnauthorizedError` is one class so the 401 case compares the same
// constructor the module under test does. `assertOk`/`readErrorMessage` are
// named because the REAL settings module imports them from here.
vi.mock("./api/http", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  assertOk: vi.fn(),
  readErrorMessage: vi.fn(),
}));

import { UnauthorizedError } from "./api/http";
import { getApiBase } from "./api/baseUrl";
import {
  SCAN_ALREADY_RUNNING,
  getLibraryScanStatus,
  scanStatusFromOperation,
  startLibraryScan,
  type LibraryScanStatus,
} from "./api/settings";
import type { ArchiveExtractResult } from "./api/imports";
import type { Operation, OperationStep } from "./api/operations";
import {
  SETTLE_POLL_MS,
  SETTLE_TIMEOUT_MS,
  settleAfterExtract,
  waitForScanSettle,
} from "./scanSettle";

const getLibraryScanStatusMock = vi.mocked(getLibraryScanStatus);
const startLibraryScanMock = vi.mocked(startLibraryScan);

const RUNNING: LibraryScanStatus = {
  running: true,
  started_at: 1,
  finished_at: null,
  registered: 0,
  known: 0,
  ignored: 0,
  missing: 0,
  failed: 0,
  errors: [],
};

const IDLE: LibraryScanStatus = {
  ...RUNNING,
  running: false,
  finished_at: 2,
  registered: 2,
};

const RESULT: ArchiveExtractResult = {
  path: "Moai.zip",
  destination: "Moai",
  files: 2,
  bytes: 2560,
  already_present: false,
  scan_started: true,
};

beforeEach(() => {
  getLibraryScanStatusMock.mockReset();
  getLibraryScanStatusMock.mockResolvedValue(IDLE);
  startLibraryScanMock.mockReset();
  startLibraryScanMock.mockResolvedValue(RUNNING);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("waitForScanSettle", () => {
  it("polls until the scan is no longer running", async () => {
    getLibraryScanStatusMock.mockResolvedValueOnce(RUNNING).mockResolvedValue(IDLE);

    const settling = waitForScanSettle();
    await vi.advanceTimersByTimeAsync(SETTLE_POLL_MS);

    await expect(settling).resolves.toBe("settled");
    expect(getLibraryScanStatusMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("gives up at the deadline instead of polling forever", async () => {
    getLibraryScanStatusMock.mockResolvedValue(RUNNING);

    const settling = waitForScanSettle();
    await vi.advanceTimersByTimeAsync(SETTLE_TIMEOUT_MS);

    await expect(settling).resolves.toBe("timeout");
    // One read per poll plus the entry read: the wait stopped at the deadline
    // rather than spinning until the test runner gave up.
    expect(getLibraryScanStatusMock.mock.calls.length).toBeLessThanOrEqual(
      SETTLE_TIMEOUT_MS / SETTLE_POLL_MS + 1,
    );
  });

  it("resolves at once when no scan is running", async () => {
    await expect(waitForScanSettle()).resolves.toBe("settled");

    expect(getLibraryScanStatusMock).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("bails when the caller cancels and reads nothing afterwards", async () => {
    let cancelled = false;
    getLibraryScanStatusMock.mockResolvedValue(RUNNING);

    const settling = waitForScanSettle({ cancelled: () => cancelled });
    await vi.advanceTimersByTimeAsync(SETTLE_POLL_MS);
    const readsBefore = getLibraryScanStatusMock.mock.calls.length;
    expect(readsBefore).toBeGreaterThan(0);

    cancelled = true;
    await vi.advanceTimersByTimeAsync(SETTLE_POLL_MS * 3);

    await expect(settling).resolves.toBe("cancelled");
    expect(getLibraryScanStatusMock.mock.calls.length).toBe(readsBefore);
  });

  it("forwards a revoked session instead of claiming a timeout", async () => {
    const onUnauthorized = vi.fn();
    getLibraryScanStatusMock.mockRejectedValue(new UnauthorizedError());

    await expect(waitForScanSettle({ onUnauthorized })).resolves.toBe("cancelled");
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});

describe("settleAfterExtract", () => {
  it("touches nothing when the extract wrote nothing", async () => {
    // The every-destination-exists no-op: the server reports it as zero files
    // with `already_present` true, so it is nothing written like any other.
    const results: ArchiveExtractResult[] = [
      { ...RESULT, files: 0, bytes: 0, already_present: true, scan_started: false },
    ];

    await expect(settleAfterExtract(results)).resolves.toBe("settled");
    expect(getLibraryScanStatusMock).not.toHaveBeenCalled();
    expect(startLibraryScanMock).not.toHaveBeenCalled();
  });

  it("touches nothing when the archive held no extractable files", async () => {
    // The server's junk-only early return: zero files with `already_present`
    // false, so filtering on that flag alone counted a no-write as a write and
    // kicked a full library rescan for it.
    const results: ArchiveExtractResult[] = [
      {
        path: "Moai.zip",
        destination: "Moai",
        files: 0,
        bytes: 0,
        already_present: false,
        scan_started: false,
      },
    ];

    await expect(settleAfterExtract(results, {})).resolves.toBe("settled");
    expect(getLibraryScanStatusMock).not.toHaveBeenCalled();
    expect(startLibraryScanMock).not.toHaveBeenCalled();
  });

  it("waits out the scan the extract started, without re-kicking it", async () => {
    await expect(settleAfterExtract([{ ...RESULT, scan_started: true }])).resolves.toBe("settled");

    expect(getLibraryScanStatusMock).toHaveBeenCalledTimes(1);
    expect(startLibraryScanMock).not.toHaveBeenCalled();
  });

  it("re-kicks a scan the extract could not start, then waits twice", async () => {
    await expect(settleAfterExtract([{ ...RESULT, scan_started: false }])).resolves.toBe("settled");

    expect(startLibraryScanMock).toHaveBeenCalledTimes(1);
    expect(getLibraryScanStatusMock).toHaveBeenCalledTimes(2);
  });

  it("follows a re-kick the server refused for the slot instead of calling it a failure", async () => {
    // The refusal is a ROW now, not a 409: `startLibraryScan` resolves with a
    // status that says a scan holds the slot, and the second wait follows it to
    // the real finish. Reporting a timeout here is what the old 409 branch
    // existed to prevent, and it still does not happen.
    const refused: LibraryScanStatus = {
      ...IDLE,
      running: true,
      finished_at: null,
      errors: [SCAN_ALREADY_RUNNING],
    };
    startLibraryScanMock.mockResolvedValue(refused);
    getLibraryScanStatusMock
      .mockResolvedValueOnce(IDLE)
      .mockResolvedValueOnce(refused)
      .mockResolvedValue(IDLE);

    const settling = settleAfterExtract([{ ...RESULT, scan_started: false }]);
    await vi.advanceTimersByTimeAsync(SETTLE_POLL_MS);

    await expect(settling).resolves.toBe("settled");
    expect(startLibraryScanMock).toHaveBeenCalledTimes(1);
    expect(getLibraryScanStatusMock).toHaveBeenCalledTimes(3);
  });

  it("gives up when the re-kick fails for any other reason", async () => {
    startLibraryScanMock.mockRejectedValue(new Error("library is unavailable"));

    await expect(settleAfterExtract([{ ...RESULT, scan_started: false }])).resolves.toBe("timeout");

    expect(startLibraryScanMock).toHaveBeenCalledTimes(1);
    expect(getLibraryScanStatusMock).toHaveBeenCalledTimes(1);
  });
});

/** One operation row, so a case states only the field it is about. */
function operationRow(overrides: Partial<Operation> = {}): Operation {
  return {
    id: "op-1",
    workflow: "library-scan",
    target: "",
    status: "queued",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
    error: null,
    undo_state: "none",
    superseded_by: null,
    steps: [],
    ...overrides,
  };
}

/** The scan step, the only one this workflow plans. */
function scanStep(overrides: Partial<OperationStep> = {}): OperationStep {
  return {
    index: 0,
    op: "scan",
    status: "done",
    result: null,
    error: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

// The scan has no route of its own any more, so the newest `library-scan`
// operation row is the browser's ONLY evidence about it. These cases pin the
// mapping, because every surface downstream reads whatever it answers.
describe("scanStatusFromOperation", () => {
  it("reports no scan at all as zeros, never as a finished clean scan", () => {
    expect(scanStatusFromOperation(null)).toEqual({
      running: false,
      started_at: null,
      finished_at: null,
      registered: 0,
      known: 0,
      ignored: 0,
      missing: 0,
      failed: 0,
      errors: [],
    });
  });

  it("reads a queued scan as running with nothing counted yet", () => {
    const status = scanStatusFromOperation(operationRow({ status: "queued" }));

    expect(status.running).toBe(true);
    expect(status.finished_at).toBeNull();
    expect(status.registered).toBe(0);
    expect(status.errors).toEqual([]);
  });

  it("counts nothing for a step that is still pending", () => {
    const status = scanStatusFromOperation(
      operationRow({ status: "running", steps: [scanStep({ status: "pending" })] }),
    );

    expect(status.running).toBe(true);
    expect(status.registered).toBe(0);
    expect(status.failed).toBe(0);
    expect(status.errors).toEqual([]);
  });

  it("reads the counters and errors off the finished step's own result", () => {
    // The step's `result` is the old wire shape verbatim, so it is copied across
    // field for field - a derived count would drift from the runner.
    const status = scanStatusFromOperation(
      operationRow({
        status: "done",
        started_at: "2026-01-01T00:00:00Z",
        finished_at: "2026-01-01T00:00:10Z",
        steps: [
          scanStep({
            result: {
              running: false,
              started_at: 1_767_225_600,
              finished_at: 1_767_225_610,
              registered: 3,
              known: 1,
              ignored: 2,
              missing: 4,
              failed: 1,
              errors: ["a.stl: IntegrityError: duplicate name"],
            },
          }),
        ],
      }),
    );

    expect(status).toEqual({
      running: false,
      started_at: Date.parse("2026-01-01T00:00:00Z") / 1000,
      finished_at: Date.parse("2026-01-01T00:00:10Z") / 1000,
      registered: 3,
      known: 1,
      ignored: 2,
      missing: 4,
      failed: 1,
      errors: ["a.stl: IntegrityError: duplicate name"],
    });
  });

  it("reads the server's refusal as a scan holding the slot, with its sentence in errors", () => {
    // This is the whole replacement for the deleted 409: the operator is told a
    // scan is already running, and `running: true` is what the panel acts on.
    const status = scanStatusFromOperation(
      operationRow({
        status: "failed",
        error: SCAN_ALREADY_RUNNING,
        finished_at: "2026-01-01T00:00:10Z",
        steps: [scanStep({ status: "failed", error: SCAN_ALREADY_RUNNING })],
      }),
    );

    expect(status.running).toBe(true);
    expect(status.errors).toEqual([SCAN_ALREADY_RUNNING]);
    expect(status.registered).toBe(0);
  });

  it("does not pretend a scan walks when its own step failed", () => {
    const status = scanStatusFromOperation(
      operationRow({
        status: "failed",
        error: "permission denied",
        finished_at: "2026-01-01T00:00:10Z",
        steps: [scanStep({ status: "failed", error: "permission denied" })],
      }),
    );

    expect(status.running).toBe(false);
    expect(status.errors).toEqual(["permission denied"]);
    expect(status.finished_at).toBe(Date.parse("2026-01-01T00:00:10Z") / 1000);
  });

  it("reads the scan off the operations ledger, not the deleted route", async () => {
    // The false state this catches: a client that reverted to the deleted
    // `GET /library/scan`. The only read a scan has now is the newest row of its
    // own workflow, so the real module's own reader is exercised here (the two
    // waiters above stand in for it) and its one request is asserted below.
    const { getLibraryScanStatus: readRealScan } =
      await vi.importActual<typeof import("./api/settings")>("./api/settings");
    const fetchMock = vi.fn(async (_input: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => [operationRow({ status: "running" })],
      text: async () => "",
      headers: new Headers(),
    }));
    vi.stubGlobal("fetch", fetchMock);

    try {
      await expect(readRealScan()).resolves.toMatchObject({ running: true });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(String(fetchMock.mock.calls[0][0])).toBe(
        `${getApiBase()}/operations?workflow=library-scan&limit=1`,
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
