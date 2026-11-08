// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, within } from "@testing-library/react";

import type { BundleRefusal, BundleReport, BundleRuleSet } from "../../lib/api/bundles";
import ScanFidelityNotice from "./ScanFidelityNotice";

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

function panel(): HTMLElement {
  const node = document.querySelector("[data-scan-fidelity]");
  if (!node) throw new Error("no scan fidelity panel");
  return node as HTMLElement;
}

afterEach(() => {
  cleanup();
});

describe("ScanFidelityNotice", () => {
  it("names the refused count and keeps the whole stamp reachable", () => {
    render(<ScanFidelityNotice report={makeReport({ refused: REFUSALS, rule_set: RULE_SET })} />);

    const notice = panel();
    expect(within(notice).getByText("2 paths refused")).toBeTruthy();
    // The full stamp is in the sr-only span: truncating it out of the DOM would
    // make the one value that proves comparability unreachable.
    expect(notice.textContent).toContain(STAMP);
    expect(notice.textContent).toContain("2026-11-04.2");
    expect(notice.textContent).toContain(STAMP.slice(0, 8));
  });

  it("adds no element at all when the server said nothing about the scan", () => {
    const { container } = render(<ScanFidelityNotice report={makeReport({})} />);

    // No empty shell either: there is nothing to hold in the notices column.
    expect(container.innerHTML).toBe("");
    expect(document.querySelector("[data-scan-fidelity]")).toBeNull();
  });

  it("renders a real zero as zero", () => {
    render(<ScanFidelityNotice report={makeReport({ refused: [], rule_set: RULE_SET })} />);

    // The notice exists because the server answered, and its answer is `0`.
    const text = panel().textContent ?? "";
    expect(text).toContain("0 paths refused");
    expect(text).not.toContain("\u2013");
  });

  it("renders a dash for the count of a cut-short walk and still shows the stamp", () => {
    render(
      <ScanFidelityNotice report={makeReport({ refused: REFUSALS, rule_set: RULE_SET, truncated: true })} />,
    );

    const text = panel().textContent ?? "";
    expect(text).toContain(`Paths refused: \u2013`);
    expect(text).not.toContain("2 paths refused");
    // The stamp describes the rule tables, not the walk, so it stays known.
    expect(text).toContain(STAMP);
  });

  it("offers nothing clickable", () => {
    render(<ScanFidelityNotice report={makeReport({ refused: REFUSALS, rule_set: RULE_SET })} />);

    // A reading aid that acts on a path would invite an operator to act on a
    // path this notice cannot see.
    expect(panel().querySelectorAll("button, a")).toHaveLength(0);
  });

  it("carries the full stamp in the stamp element's title", () => {
    render(<ScanFidelityNotice report={makeReport({ refused: REFUSALS, rule_set: RULE_SET })} />);

    const stamped = within(panel()).getByTitle(STAMP);
    expect(stamped.getAttribute("title")).toBe(STAMP);
  });
});
