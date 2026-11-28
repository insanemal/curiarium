import { describe, expect, it } from "vitest";

import type { MountImportStatus } from "./api";
import {
  countsLine,
  formatDuration,
  formatTime,
  isFailure,
  modeLabel,
  reason,
  rejectedNote,
  statusLabel,
} from "./mountImport";

function makeStatus(overrides: Partial<MountImportStatus> = {}): MountImportStatus {
  return {
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
    ...overrides,
  };
}

describe("statusLabel", () => {
  it.each([
    ["queued", "Queued"],
    ["running", "Running"],
    ["cancelling", "Cancelling"],
    ["failed", "Failed"],
    ["cancelled", "Cancelled"],
  ])("labels %s as %s", (status, label) => {
    expect(statusLabel(makeStatus({ status, outcome: null }))).toBe(label);
  });

  it("reads a done row's outcome, because done alone says nothing", () => {
    expect(statusLabel(makeStatus({ status: "done", outcome: "registered" }))).toBe("Succeeded");
    expect(statusLabel(makeStatus({ status: "done", outcome: "skipped" }))).toBe("Skipped");
    // A done row whose step reported no outcome is not evidence of a skip.
    expect(statusLabel(makeStatus({ status: "done", outcome: null }))).toBe("Succeeded");
  });

  it("says so when the ledger holds no walk at all", () => {
    expect(statusLabel(makeStatus({ status: null, outcome: null }))).toBe("No walk recorded");
  });

  it("renders a neutral label for a status the UI does not know", () => {
    expect(statusLabel(makeStatus({ status: "reticulating", outcome: null }))).toBe(
      "Unknown (reticulating)",
    );
  });
});

describe("isFailure", () => {
  it("treats only failed as a failure", () => {
    expect(isFailure(makeStatus({ status: "failed", outcome: null }))).toBe(true);
  });

  it("does not treat a cancelled, skipped or finished walk as a failure", () => {
    expect(isFailure(makeStatus({ status: "cancelled", outcome: null }))).toBe(false);
    expect(isFailure(makeStatus({ status: "done", outcome: "skipped" }))).toBe(false);
    expect(isFailure(makeStatus({ status: "done", outcome: "registered" }))).toBe(false);
    expect(isFailure(makeStatus({ status: "running", outcome: null }))).toBe(false);
    expect(isFailure(makeStatus({ status: "mystery", outcome: null }))).toBe(false);
  });
});

describe("modeLabel", () => {
  it("describes copy and reference modes", () => {
    expect(modeLabel(makeStatus({ mode: "copy" }))).toBe("Copied into storage");
    expect(modeLabel(makeStatus({ mode: "reference" }))).toBe("Referenced in place");
  });

  it("falls back when the mode is missing or unknown", () => {
    expect(modeLabel(makeStatus({ mode: null }))).toBe("Mode not recorded");
    expect(modeLabel(makeStatus({ mode: "teleport" }))).toBe("Mode not recorded");
  });
});

describe("formatDuration", () => {
  it("returns an empty string while the walk has no finish time", () => {
    expect(formatDuration(1_700_000_000, null)).toBe("");
  });

  it("returns an empty string when the server recorded no start", () => {
    expect(formatDuration(null, 1_700_000_042)).toBe("");
  });

  it("formats a zero duration", () => {
    expect(formatDuration(1_700_000_000, 1_700_000_000)).toBe("0s");
  });

  it("formats seconds, minutes and hours", () => {
    expect(formatDuration(100, 142)).toBe("42s");
    expect(formatDuration(100, 220)).toBe("2m");
    expect(formatDuration(100, 160)).toBe("1m");
    expect(formatDuration(100, 190)).toBe("1m 30s");
    expect(formatDuration(0, 7200)).toBe("2h");
    expect(formatDuration(0, 7500)).toBe("2h 5m");
  });

  it("never renders a negative duration", () => {
    expect(formatDuration(200, 100)).toBe("0s");
  });
});

describe("formatTime", () => {
  it("returns an empty string when there is no timestamp", () => {
    expect(formatTime(null)).toBe("");
  });

  it("converts epoch seconds to a locale date string", () => {
    const formatted = formatTime(0);
    expect(formatted).toBe(new Date(0).toLocaleString());
    expect(formatted).not.toBe("");
  });

  it("returns an empty string for an unparseable timestamp", () => {
    expect(formatTime(Number.NaN)).toBe("");
  });
});

describe("countsLine", () => {
  it("lists discovered, registered, skipped and failed counters", () => {
    expect(countsLine(makeStatus())).toBe("7 discovered, 4 registered, 3 skipped, 0 failed");
  });

  it("appends the oversize subset only when the server counted rejections", () => {
    expect(countsLine(makeStatus({ ignored: 2, rejected: 2, skipped: 4 }))).toBe(
      "7 discovered, 4 registered, 4 skipped, 0 failed, 2 oversize",
    );
  });

  it("keeps a failed counter visible", () => {
    expect(countsLine(makeStatus({ failed: 3, discovered: 10 }))).toBe(
      "10 discovered, 4 registered, 3 skipped, 3 failed",
    );
  });

  it("prints zeros for a walk that has not reported yet, because that IS the count", () => {
    expect(
      countsLine(
        makeStatus({ outcome: null, discovered: 0, registered: 0, skipped: 0, failed: 0 }),
      ),
    ).toBe("0 discovered, 0 registered, 0 skipped, 0 failed");
  });
});

describe("rejectedNote", () => {
  it("is empty when nothing was rejected", () => {
    expect(rejectedNote(makeStatus())).toBe("");
  });

  it("explains the oversize subset with the right verb", () => {
    expect(rejectedNote(makeStatus({ rejected: 1 }))).toBe(
      "1 oversize file exceeded the size limit and was skipped.",
    );
    expect(rejectedNote(makeStatus({ rejected: 3 }))).toBe(
      "3 oversize files exceeded the size limit and were skipped.",
    );
  });
});

describe("reason", () => {
  it("reports the reason for a walk that skipped the mount", () => {
    expect(
      reason(makeStatus({ status: "done", outcome: "skipped", reason: "Mount path is not configured" })),
    ).toBe("Mount path is not configured");
  });

  it("reports the reason for failed and cancelled walks", () => {
    expect(reason(makeStatus({ status: "failed", outcome: null, reason: "Permission denied" }))).toBe(
      "Permission denied",
    );
    expect(reason(makeStatus({ status: "cancelled", outcome: null, reason: "Cancelled by hand" }))).toBe(
      "Cancelled by hand",
    );
  });

  it("falls back to a placeholder for a skipped walk without a reason", () => {
    expect(reason(makeStatus({ status: "done", outcome: "skipped", reason: null }))).toBe(
      "No reason was recorded.",
    );
    expect(reason(makeStatus({ status: "done", outcome: "skipped", reason: "   " }))).toBe(
      "No reason was recorded.",
    );
  });

  it("is empty for walks that carry no reason by design", () => {
    expect(reason(makeStatus({ status: "done", outcome: "registered", reason: "ignored" }))).toBe("");
    expect(reason(makeStatus({ status: "running", outcome: null, reason: null }))).toBe("");
    expect(reason(makeStatus({ status: "failed", outcome: null, reason: null }))).toBe("");
  });
});
