import { describe, expect, it } from "vitest";

import type { BundleRefusal, BundleReport, BundleRuleSet } from "../../lib/api/bundles";
import {
  SHORT_STAMP,
  refusedCount,
  refusedLine,
  ruleSetLine,
  ruleSetStamp,
  showsScanFidelity,
} from "./scanFidelity";

const STAMP = "a".repeat(64);
const RULE_SET: BundleRuleSet = { version: "2026-11-04.2", stamp: STAMP };

const REFUSALS: BundleRefusal[] = [
  { rel_path: "import/models-only.zip", rule: "R3:trio-no-image", kind: "archive" },
  { rel_path: "import/stray-folder", rule: "R1:no-markers", kind: "directory" },
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

describe("showsScanFidelity", () => {
  it("shows the notice when the server sent a ledger, even an empty one", () => {
    // Red if the presence test became a length test: `[]` is a real answer.
    expect(showsScanFidelity(makeReport({ refused: [] }))).toBe(true);
  });

  it("shows the notice when the server sent only the rule set", () => {
    expect(showsScanFidelity(makeReport({ rule_set: RULE_SET }))).toBe(true);
  });

  it("stays hidden when neither field was sent", () => {
    // Red if `showsScanFidelity` were gated on `truncated` like its duplicate
    // sibling: an older payload would then invent a notice from nothing.
    expect(showsScanFidelity(makeReport({}))).toBe(false);
  });

  it("stays hidden for a truncated walk that said nothing about the scan", () => {
    // The deliberate difference from `showsDuplicateReport`: truncation cut the
    // bundle ROWS and never the ledger, so a cut-short walk is not evidence
    // about refusals at all. Red if `|| report.truncated` were copied over.
    expect(showsScanFidelity(makeReport({ truncated: true }))).toBe(false);
  });
});

describe("refusedCount", () => {
  it("counts the entries the ledger carries", () => {
    expect(refusedCount(makeReport({ refused: REFUSALS }))).toBe(2);
  });

  it("counts every kind, not just archives", () => {
    // Red if the count filtered to `kind === "archive"`: the line would silently
    // become "N archives refused" while still claiming to count paths.
    const mixed: BundleRefusal[] = [
      { rel_path: "a.zip", rule: "R3:trio-no-image", kind: "archive" },
      { rel_path: "folder", rule: "R1:no-markers", kind: "directory" },
    ];
    expect(refusedCount(makeReport({ refused: mixed }))).toBe(2);
  });

  it("is a known zero when the server said nothing was refused", () => {
    expect(refusedCount(makeReport({ refused: [] }))).toBe(0);
  });

  it("is unknown when the field was omitted", () => {
    // Red if the reader used `?? 0`: an omitted field would claim an empty ledger.
    expect(refusedCount(makeReport({}))).toBeNull();
  });

  it("is unknown when the walk was cut short", () => {
    // Red if the truncation guard were dropped: 2 would be a plausible undercount.
    expect(refusedCount(makeReport({ refused: REFUSALS, truncated: true }))).toBeNull();
  });
});

describe("refusedLine", () => {
  it("names a real zero as a zero", () => {
    // A zero IS the server's answer, so it must render as itself.
    expect(refusedLine(0)).toBe("0 paths refused");
  });

  it("pluralises the noun", () => {
    expect(refusedLine(2)).toBe("2 paths refused");
    expect(refusedLine(1)).toBe("1 path refused");
  });

  it("renders an en dash for an unknown count, never a zero", () => {
    // Red if `null` fell through to the count: "0 paths refused" is a claim.
    expect(refusedLine(null)).toBe(`Paths refused: \u2013`);
    expect(refusedLine(null)).not.toContain("0");
  });
});

describe("ruleSetStamp", () => {
  it("returns the whole stamp the server sent", () => {
    expect(ruleSetStamp(makeReport({ rule_set: RULE_SET }))).toBe(STAMP);
  });

  it("is null when the field was omitted", () => {
    expect(ruleSetStamp(makeReport({}))).toBeNull();
  });

  it("is null for an empty stamp", () => {
    // An empty string is the server saying nothing, not a comparable rule set.
    expect(ruleSetStamp(makeReport({ rule_set: { version: "2026-11-04.2", stamp: "" } }))).toBeNull();
  });

  it("is still known when the walk was cut short", () => {
    // The deliberate difference from `refusedCount`: the stamp describes the
    // process's rule tables, not the walk. Red if `truncated` gated this too.
    expect(ruleSetStamp(makeReport({ rule_set: RULE_SET, truncated: true }))).toBe(STAMP);
  });
});

describe("ruleSetLine", () => {
  it("names the version and a short prefix of the stamp", () => {
    const line = ruleSetLine(RULE_SET);

    expect(line).toContain("2026-11-04.2");
    expect(line).toContain(STAMP.slice(0, SHORT_STAMP));
    // The full stamp rides in the title and the `sr-only` span, not here.
    // Red if the whole stamp were printed into the visible line.
    expect(line).not.toContain(STAMP);
  });

  it("renders an en dash when no rule set was sent", () => {
    // Red if an absent rule set borrowed a version from somewhere: a plausible
    // version is a claim the report cannot make.
    expect(ruleSetLine(null)).toBe(`Rule set \u2013`);
    expect(ruleSetLine(undefined)).toBe(`Rule set \u2013`);
  });

  it("renders the version alone for an empty stamp, with no parentheses", () => {
    // Red if the empty stamp fell through to the parenthesised branch: `()` is
    // nonsense on a line that still knows the version.
    const line = ruleSetLine({ version: "2026-11-04.2", stamp: "" });

    expect(line).toBe("Rule set 2026-11-04.2");
    expect(line).not.toContain("(");
  });

  it("renders the version alone for a missing stamp, without throwing", () => {
    // Red if the stamp were sliced unguarded: a type-violating payload would
    // throw during render instead of degrading to the half it can prove.
    const malformed = { version: "2026-11-04.2" } as BundleRuleSet;

    expect(() => ruleSetLine(malformed)).not.toThrow();
    expect(ruleSetLine(malformed)).toBe("Rule set 2026-11-04.2");
  });
});
