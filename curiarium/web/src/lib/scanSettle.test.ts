// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api/settings", () => ({
  getLibraryScanStatus: vi.fn(),
  startLibraryScan: vi.fn(),
}));

vi.mock("./api/http", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
}));

import { UnauthorizedError } from "./api/http";
import {
  getLibraryScanStatus,
  startLibraryScan,
  type LibraryScanStatus,
} from "./api/settings";
import type { ArchiveExtractResult } from "./api/imports";
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
    const results: ArchiveExtractResult[] = [
      { ...RESULT, already_present: true, scan_started: false },
    ];

    await expect(settleAfterExtract(results)).resolves.toBe("settled");
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

  it("treats a scan that grabbed the slot as success", async () => {
    startLibraryScanMock.mockRejectedValue(new Error("A scan is already running"));

    await expect(settleAfterExtract([{ ...RESULT, scan_started: false }])).resolves.toBe("settled");

    expect(startLibraryScanMock).toHaveBeenCalledTimes(1);
    expect(getLibraryScanStatusMock).toHaveBeenCalledTimes(2);
  });

  it("gives up when the re-kick fails for any other reason", async () => {
    startLibraryScanMock.mockRejectedValue(new Error("library is unavailable"));

    await expect(settleAfterExtract([{ ...RESULT, scan_started: false }])).resolves.toBe("timeout");

    expect(startLibraryScanMock).toHaveBeenCalledTimes(1);
    expect(getLibraryScanStatusMock).toHaveBeenCalledTimes(1);
  });
});
