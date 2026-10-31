// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  getBundleReport: vi.fn(),
  createBundles: vi.fn(),
  setBundleDismissed: vi.fn(),
}));

import {
  createBundles,
  getBundleReport,
  setBundleDismissed,
  type BundleDetectItem,
  type BundleReport,
} from "../../lib/api";
import BundleDetectCard from "./BundleDetectCard";
import { useBundleDetect } from "./useBundleDetect";

const getBundleReportMock = vi.mocked(getBundleReport);
const createBundlesMock = vi.mocked(createBundles);
const setBundleDismissedMock = vi.mocked(setBundleDismissed);

function makeBundle(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return {
    key: "Moai",
    rel_path: "Moai",
    title: "Moai statue",
    name_suggestion: "Moai statue",
    creator: "sculpteo",
    licence: "CC BY",
    licence_url: "https://creativecommons.org/licenses/by/4.0/",
    source_url: "https://www.thingiverse.com/thing:2493386",
    detector: "thingiverse-readme",
    confidence: "high",
    metadata_shape: "root-docs",
    description_excerpt: "A Moai statue.",
    model_count: 3,
    image_count: 5,
    text_count: 2,
    other_count: 0,
    cover: { asset_id: "asset-1", rel_path: "images/hero.jpg", reason: "largest-image" },
    assets_total: 8,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project: null,
    dismissed: false,
    ...overrides,
  };
}

const moai = makeBundle();
const cableClip = makeBundle({
  key: "Cable_clip",
  rel_path: "Cable_clip",
  title: "Cable clip",
  name_suggestion: "Cable clip",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "low",
  metadata_shape: "generic",
  description_excerpt: null,
  model_count: 1,
  image_count: 1,
  text_count: 1,
  cover: { asset_id: null, rel_path: "preview.png", reason: "largest-image-unregistered" },
  assets_total: 0,
});
const swordHilt = makeBundle({
  key: "Sword_hilt",
  rel_path: "Sword_hilt",
  title: "Sword hilt",
  name_suggestion: "Sword hilt",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "low",
  metadata_shape: "generic",
  model_count: 2,
  image_count: 1,
  text_count: 1,
  other_count: 1,
  cover: { asset_id: "asset-2", rel_path: "previews/hilt.png", reason: "largest-image" },
  assets_total: 5,
  archive: { rel_path: "Sword hilt.zip", twin_verified: false },
});
const legacyVase = makeBundle({
  key: "Legacy_vase",
  rel_path: "Legacy_vase",
  title: "Legacy vase",
  name_suggestion: "Legacy vase",
  detector: "thingiverse-sibling-docs",
  metadata_shape: "sibling-docs",
  model_count: 1,
  image_count: 2,
  cover: { asset_id: "asset-3", rel_path: "images/vase.jpg", reason: "largest-image" },
  assets_total: 3,
  status: "blocked",
  blocked_reason: "unresolved_directory",
});

function makeReport(bundles: BundleDetectItem[]): BundleReport {
  const high = bundles.filter(row => row.confidence === "high").length;
  const low = bundles.filter(row => row.confidence === "low").length;
  const ready = bundles.filter(row => row.status === "ready").length;
  return {
    root: "",
    truncated: false,
    counts: {
      total: bundles.length,
      high,
      low,
      ready,
      blocked: bundles.length - ready,
      dismissed: bundles.filter(row => row.dismissed).length,
      already_created: bundles.filter(row => row.existing_project).length,
    },
    bundles,
    stale_dismissals: [],
  };
}

const report = makeReport([moai, cableClip, swordHilt, legacyVase]);

/** The card is presentational; the real hook drives it against the mocked api. */
function Harness() {
  const state = useBundleDetect({ section: "imports", enabled: true });
  return <BundleDetectCard state={state} />;
}

/** Render the card and wait for the first report row to land. */
async function renderCard(anchor = "Moai statue"): Promise<void> {
  render(<Harness />);
  await screen.findByText(anchor);
}

beforeEach(() => {
  getBundleReportMock.mockReset();
  getBundleReportMock.mockResolvedValue(report);
  createBundlesMock.mockReset();
  createBundlesMock.mockResolvedValue({ created: 1, skipped: 0, blocked: 0, failed: 0, results: [] });
  setBundleDismissedMock.mockReset();
  setBundleDismissedMock.mockResolvedValue({ dismissed: [], stale_dismissals: [] });
});

afterEach(() => {
  cleanup();
});

describe("BundleDetectCard", () => {
  it("heads the report with the counts and explains detection is a dry run", async () => {
    await renderCard();

    expect(screen.getByText("Vendor bundles")).toBeTruthy();
    expect(screen.getByText("4 bundles found - 2 high confidence, 2 low")).toBeTruthy();
    expect(screen.getByText(/Detection writes nothing/)).toBeTruthy();
  });

  it("shows a row's confidence, counts, cover and archive", async () => {
    await renderCard();

    // Two high and two low rows; the filter tab shares a label but not the tone.
    const highBadges = screen
      .getAllByText("High confidence")
      .filter(node => node.className.includes("text-emerald-400"));
    expect(highBadges.length).toBe(2);
    expect(highBadges[0].className).toContain("text-emerald-400");
    const lowBadges = screen
      .getAllByText("Low confidence")
      .filter(node => node.className.includes("text-amber-400"));
    expect(lowBadges.length).toBe(2);
    expect(screen.getByText("3 models, 5 images, 2 text files, 0 other files - 8 registered assets")).toBeTruthy();
    expect(screen.getByText("Cover images/hero.jpg")).toBeTruthy();
    expect(screen.getByText("Archive Sword hilt.zip - twin not verified")).toBeTruthy();
    expect(screen.getByText("2 models, 1 image, 1 text file, 1 other file - 5 registered assets")).toBeTruthy();
    expect(screen.getByText("Cover previews/hilt.png")).toBeTruthy();
    expect(screen.getByText("Blocked - no matching folder on disk")).toBeTruthy();
  });

  it("creates a selected bundle under the name the operator typed", async () => {
    await renderCard();

    fireEvent.click(screen.getByLabelText("Select Moai statue"));
    fireEvent.change(screen.getByLabelText("Name for Moai statue"), {
      target: { value: "Moai statue (renamed)" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create selected" }));

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    expect(createBundlesMock).toHaveBeenCalledWith([
      { key: "Moai", name: "Moai statue (renamed)" },
    ]);
    // The mutation refetches, so the card is showing a fresh report.
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));
  });

  it("creates only the low-confidence rows as name-only, never a high one", async () => {
    await renderCard();

    fireEvent.click(screen.getByRole("button", { name: "Create all as name-only" }));

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    expect(createBundlesMock).toHaveBeenCalledWith([
      { key: "Cable_clip", name_only: true },
      { key: "Sword_hilt", name_only: true },
    ]);
    const items = createBundlesMock.mock.calls[0][0];
    expect(items.every(item => item.name_only === true)).toBe(true);
    expect(items.some(item => item.key === "Moai" || item.key === "Legacy_vase")).toBe(false);
  });

  it("dismisses the selected bundles and the row leaves the list", async () => {
    getBundleReportMock.mockReset();
    getBundleReportMock
      .mockResolvedValueOnce(report)
      .mockResolvedValue(makeReport([{ ...moai, dismissed: true }, cableClip, swordHilt, legacyVase]));

    await renderCard();

    fireEvent.click(screen.getByLabelText("Select Moai statue"));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss selected" }));

    await waitFor(() => expect(setBundleDismissedMock).toHaveBeenCalledWith(["Moai"], true));
    await waitFor(() => expect(screen.queryByText("Moai statue")).toBeNull());
    // The sibling rows are untouched.
    expect(screen.getByText("Cable clip")).toBeTruthy();
  });

  it("reveals a dismissed bundle again and lets the operator undo it", async () => {
    const dismissedReport = makeReport([
      { ...moai, dismissed: true },
      cableClip,
      swordHilt,
      legacyVase,
    ]);
    getBundleReportMock.mockReset();
    getBundleReportMock
      .mockResolvedValueOnce(dismissedReport)
      .mockResolvedValue(report);

    await renderCard("Cable clip");
    expect(screen.queryByText("Moai statue")).toBeNull();

    fireEvent.click(screen.getByLabelText("Show dismissed"));
    expect(await screen.findByText("Moai statue")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Undo dismiss" }));
    await waitFor(() => expect(setBundleDismissedMock).toHaveBeenCalledWith(["Moai"], false));
  });

  it("filters the rows by readiness and confidence", async () => {
    await renderCard();

    fireEvent.click(screen.getByRole("button", { name: "Blocked" }));
    expect(screen.getByText("Legacy vase")).toBeTruthy();
    expect(screen.queryByText("Moai statue")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Low confidence" }));
    expect(screen.getByText("Cable clip")).toBeTruthy();
    expect(screen.queryByText("Legacy vase")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "All" }));
    expect(screen.getByText("Moai statue")).toBeTruthy();
  });

  it("never lets a blocked bundle be selected for creation", async () => {
    await renderCard();

    const blocked = screen.getByLabelText("Select Legacy vase") as HTMLInputElement;
    expect(blocked.disabled).toBe(true);

    fireEvent.click(screen.getByLabelText("Select Cable clip"));
    fireEvent.click(screen.getByRole("button", { name: "Create selected" }));

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    expect(createBundlesMock).toHaveBeenCalledWith([{ key: "Cable_clip", name: "Cable clip" }]);
  });

  it("marks a bundle that already became a project", async () => {
    getBundleReportMock.mockReset();
    getBundleReportMock.mockResolvedValue(
      makeReport([{ ...moai, existing_project: "project-7" }]),
    );

    render(<Harness />);

    expect(
      await screen.findByText("Already created as project project-7"),
    ).toBeTruthy();
    expect(screen.getByText("1 bundle found - 1 high confidence, 0 low")).toBeTruthy();
  });

  it("says so when nothing was detected", async () => {
    getBundleReportMock.mockReset();
    getBundleReportMock.mockResolvedValue(makeReport([]));

    render(<Harness />);

    expect(await screen.findByText("No vendor bundles were detected.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create selected" })).toBeNull();
  });
});
