import { describe, expect, it } from "vitest";

import type { BundleDuplicateGroup, BundleReport } from "../../lib/api/bundles";
import {
  duplicateGroupCount,
  duplicateGroupLine,
  duplicateReclaimableBytes,
  duplicateReportText,
  duplicateSummaryLine,
  duplicateToggleLabel,
  showsDuplicateReport,
} from "./duplicateArchives";

const GROUPS: BundleDuplicateGroup[] = [
  { sha256: "a".repeat(64), size: 2048, paths: ["Moai.zip", "Moai (1).zip"], reclaimable_bytes: 2048 },
  { sha256: "b".repeat(64), size: 1024, paths: ["Ender3.zip", "Ender3 (1).zip"], reclaimable_bytes: 1024 },
];

function makeReport(overrides: Partial<BundleReport>): BundleReport {
  return {
    root: "",
    truncated: false,
    counts: {
      total: 0,
      high: 0,
      medium: 0,
      low: 0,
      ready: 0,
      blocked: 0,
      dismissed: 0,
      already_created: 0,
    },
    bundles: [],
    stale_dismissals: [],
    ...overrides,
  };
}

describe("showsDuplicateReport", () => {
  it("shows the panel when the walk found groups", () => {
    expect(showsDuplicateReport(makeReport({ duplicates: GROUPS }))).toBe(true);
  });

  it("stays hidden for a report that says it found none", () => {
    // Red if the length test became a mere presence test: `[]` is a real answer.
    expect(showsDuplicateReport(makeReport({ duplicates: [] }))).toBe(false);
  });

  it("stays hidden when the field was omitted", () => {
    // Red if the length were read without `?.`/`?? 0`.
    expect(showsDuplicateReport(makeReport({}))).toBe(false);
  });

  it("shows the panel for a truncated walk with no groups at all", () => {
    // Red if the `|| report.truncated` half were dropped: silence here is the
    // silent under-reporting the ticket forbids.
    expect(showsDuplicateReport(makeReport({ duplicates: [], truncated: true }))).toBe(true);
  });
});

describe("duplicateGroupCount", () => {
  it("counts the groups the report carries", () => {
    expect(duplicateGroupCount(makeReport({ duplicates: GROUPS }))).toBe(2);
  });

  it("is unknown when the field was omitted", () => {
    // Red if the reader used `?? 0`: an omitted field would claim an empty list.
    expect(duplicateGroupCount(makeReport({}))).toBeNull();
  });

  it("is unknown when the walk was cut short", () => {
    // Red if the truncation guard were dropped: 2 would be a plausible undercount.
    expect(duplicateGroupCount(makeReport({ duplicates: GROUPS, truncated: true }))).toBeNull();
  });

  it("is a known zero when the server said it found none", () => {
    // A zero here is the server's own answer, and this layer may say it.
    expect(duplicateGroupCount(makeReport({ duplicates: [] }))).toBe(0);
  });
});

describe("duplicateReclaimableBytes", () => {
  it("sums the bytes the report claims are reclaimable", () => {
    expect(duplicateReclaimableBytes(makeReport({ duplicates_total_bytes: 3072 }))).toBe(3072);
  });

  it("is unknown when the total was omitted", () => {
    // Red if the reader used `?? 0`: `0 B` would claim nothing is reclaimable.
    expect(duplicateReclaimableBytes(makeReport({}))).toBeNull();
  });

  it("is unknown when the walk was cut short", () => {
    // Red if the truncation guard were dropped.
    expect(duplicateReclaimableBytes(makeReport({ duplicates_total_bytes: 3072, truncated: true }))).toBeNull();
  });

  it("keeps a reported zero distinct from an unknown total", () => {
    expect(duplicateReclaimableBytes(makeReport({ duplicates_total_bytes: 0 }))).toBe(0);
  });
});

describe("duplicateReportText", () => {
  it("returns the service's text verbatim", () => {
    const text = "# duplicate archives: 1 group(s)";
    expect(duplicateReportText(makeReport({ duplicates_text: text }))).toBe(text);
  });

  it("treats an empty string as no report", () => {
    // Red if the reader tested only `typeof === "string"`: an empty clipboard
    // would be advertised as a report.
    expect(duplicateReportText(makeReport({ duplicates_text: "" }))).toBeNull();
  });

  it("treats an omitted field as no report", () => {
    expect(duplicateReportText(makeReport({}))).toBeNull();
  });
});

describe("duplicateSummaryLine", () => {
  it("names both halves when both are known", () => {
    expect(duplicateSummaryLine(2, 3072)).toBe("2 duplicate groups - 3.0 KB reclaimable");
  });

  it("uses the singular for one group", () => {
    expect(duplicateSummaryLine(1, 1024)).toBe("1 duplicate group - 1.0 KB reclaimable");
  });

  it("dashes only the reclaimable size when the groups are known", () => {
    // Mandatory case: an implementation that collapsed "either unknown" into one
    // unknown branch would print "Duplicate groups" here instead of the count.
    expect(duplicateSummaryLine(2, null)).toBe("2 duplicate groups - reclaimable size \u2013");
  });

  it("dashes only the group count when the size is known", () => {
    expect(duplicateSummaryLine(null, 3072)).toBe("Duplicate groups: \u2013 - 3.0 KB reclaimable");
  });

  it("dashes both halves when neither is known", () => {
    expect(duplicateSummaryLine(null, null)).toBe(
      "Duplicate groups: \u2013 - reclaimable size \u2013",
    );
  });
});

describe("duplicateGroupLine", () => {
  it("reads a group as size, copy count and reclaimable bytes", () => {
    expect(duplicateGroupLine(GROUPS[0])).toBe("2.0 KB - 2 copies - 2.0 KB reclaimable");
  });

  it("uses the singular for a lone path", () => {
    // A one-path group is not what the server sends, but the plural rule must
    // still be exercised: red if `countLabel` were given the wrong noun pair.
    const lonely: BundleDuplicateGroup = {
      sha256: "c".repeat(64),
      size: 1024,
      paths: ["Moai.zip"],
      reclaimable_bytes: 0,
    };
    expect(duplicateGroupLine(lonely)).toBe("1.0 KB - 1 copy - 0 B reclaimable");
  });
});

describe("duplicateToggleLabel", () => {
  it("says Hide groups while the list is open", () => {
    expect(duplicateToggleLabel(2, true, false)).toBe("Hide groups");
  });

  it("counts the groups in parentheses for a complete walk", () => {
    expect(duplicateToggleLabel(2, false, false)).toBe("Show groups (2)");
  });

  it("never pairs an exact-looking count with a truncated report", () => {
    // Red if the truncated branch were dropped: "(2)" beside a `–` total would
    // read as the whole list.
    expect(duplicateToggleLabel(2, false, true)).toBe("Show the 2 listed groups");
  });
});
