// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

vi.mock("../../lib/api/bundles", () => ({
  getBundleReport: vi.fn(),
  createBundles: vi.fn(),
  setBundleDismissed: vi.fn(),
  enrichBundles: vi.fn(),
}));

// The real settle loop polls for ten minutes; a hook test must never run it.
vi.mock("../../lib/scanSettle", () => ({
  settleAfterExtract: vi.fn(),
  waitForScanSettle: vi.fn(),
}));

import {
  createBundles,
  enrichBundles,
  getBundleReport,
  setBundleDismissed,
  type BundleDetectItem,
  type BundleEnrichItemResult,
  type BundleEnrichResponse,
  type BundleReport,
} from "../../lib/api/bundles";
import { UnauthorizedError } from "../../lib/api/http";
import type { ArchiveExtractResult } from "../../lib/api/imports";
import { settleAfterExtract, type SettleOutcome } from "../../lib/scanSettle";
import { resetSettingsCache, saveSettings, type AppSettings } from "../../lib/settings";
import { useBundleDetect } from "./useBundleDetect";

const getBundleReportMock = vi.mocked(getBundleReport);
const createBundlesMock = vi.mocked(createBundles);
const setBundleDismissedMock = vi.mocked(setBundleDismissed);
const enrichBundlesMock = vi.mocked(enrichBundles);
const settleAfterExtractMock = vi.mocked(settleAfterExtract);

function makeBundle(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return {
    key: "Moai statue",
    rel_path: "Moai statue",
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
    cover: {
      asset_id: "asset-1",
      rel_path: "images/hero.jpg",
      reason: "largest-image",
      url: "/file/asset-1/hero.jpg",
      thumb_url: "/thumb/asset-1.jpg?v=1",
    },
    assets_total: 8,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project_id: null,
    existing_project_name: null,
    dismissed: false,
    ...overrides,
  };
}

const emptyCounts = {
  total: 0,
  high: 0,
  low: 0,
  ready: 0,
  blocked: 0,
  dismissed: 0,
  already_created: 0,
};

function makeReport(bundles: BundleDetectItem[] = []): BundleReport {
  return {
    root: "",
    truncated: false,
    counts: { ...emptyCounts },
    bundles,
    stale_dismissals: [],
  };
}

const report = makeReport([makeBundle()]);

/** The POST's answer for one extracted archive, as the view hands it back. */
const extractResult: ArchiveExtractResult = {
  path: "/library/Moai.zip",
  destination: "/library/Moai statue",
  files: 12,
  bytes: 4096,
  already_present: false,
  scan_started: true,
};

/** A promise whose settlement the test drives, for ordering two responses. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

/** One result in an enrichment response; the counts are derived below so a
 *  fixture cannot claim a status its `results` do not contain. */
function enrichResult(overrides: Partial<BundleEnrichItemResult> = {}): BundleEnrichItemResult {
  return {
    key: "Moai statue",
    status: "fetched",
    source_url: "https://www.thingiverse.com/thing:2493386",
    creator: "sculpteo",
    licence: "CC BY",
    licence_url: null,
    filled: ["creator", "licence"],
    fetched_at: "2026-11-05T12:00:00+00:00",
    reason: null,
    ...overrides,
  };
}

function enrichResponse(results: BundleEnrichItemResult[]): BundleEnrichResponse {
  const count = (status: string) => results.filter(result => result.status === status).length;
  return {
    fetched: count("fetched"),
    cached: count("cached"),
    refused: count("refused"),
    unavailable: count("unavailable"),
    unknown: count("unknown"),
    results,
  };
}

beforeEach(() => {
  getBundleReportMock.mockReset();
  getBundleReportMock.mockResolvedValue(report);
  createBundlesMock.mockReset();
  createBundlesMock.mockResolvedValue({ created: 1, skipped: 0, blocked: 0, failed: 0, results: [] });
  setBundleDismissedMock.mockReset();
  setBundleDismissedMock.mockResolvedValue({ dismissed: ["Moai statue"], stale_dismissals: [] });
  enrichBundlesMock.mockReset();
  enrichBundlesMock.mockResolvedValue(enrichResponse([enrichResult()]));
  settleAfterExtractMock.mockReset();
});

afterEach(() => {
  cleanup();
  // Restores the Storage.prototype.setItem spy even when an assertion threw,
  // so a red run cannot leak a spy into the tests after it.
  vi.restoreAllMocks();
  // A seeded cookie must not leak into the tests after it: clear both stores
  // and the settings cache, guarded so a storage-less environment cannot throw.
  try {
    window.localStorage.clear();
    window.sessionStorage.clear();
  } catch {
    // An environment without storage has nothing to clear.
  }
  resetSettingsCache();
});

describe("useBundleDetect", () => {
  it("fetches once with the full walk when the queue opens", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));

    await waitFor(() => expect(result.current.report).toEqual(report));

    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
    expect(getBundleReportMock).toHaveBeenCalledWith({ includeDismissed: true, limit: 1000 });
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("stays inert while the role cannot write", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: false }));

    // Give a request every chance to fire before asserting it never did.
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(getBundleReportMock).not.toHaveBeenCalled();
    expect(result.current.report).toBeNull();
  });

  it("walks the library once under StrictMode, and the report still lands", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }), {
      wrapper: React.StrictMode,
    });

    await waitFor(() => expect(result.current.report).toEqual(report));

    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("lets the newest request win, so a superseded response cannot land", async () => {
    const first = deferred<BundleReport>();
    const second = deferred<BundleReport>();
    getBundleReportMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const older = makeReport([makeBundle({ key: "Older", title: "Older walk" })]);
    const newer = makeReport([makeBundle({ key: "Newer", title: "Newer walk" })]);

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(1));

    // A manual refresh starts while the first walk is still on the wire.
    act(() => {
      void result.current.refresh();
    });
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));

    // The newer request answers first...
    await act(async () => {
      second.resolve(newer);
    });
    await waitFor(() => expect(result.current.report).toEqual(newer));

    // ...and the older one's late answer is dropped.
    await act(async () => {
      first.resolve(older);
    });

    expect(result.current.report).toEqual(newer);
  });

  it("refetches on every re-entry, and never polls", async () => {
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useBundleDetect({ enabled }),
      { initialProps: { enabled: false } },
    );
    expect(getBundleReportMock).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(1));

    rerender({ enabled: false });
    rerender({ enabled: true });
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));

    // No timer, no poll: the count stays at two while the queue stays open.
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
  });

  it("creates and then refetches the report", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.create([{ key: "Moai statue", name: "Moai" }]);
    });

    expect(createBundlesMock).toHaveBeenCalledWith([{ key: "Moai statue", name: "Moai" }]);
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
    expect(result.current.busy).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("dismisses and then refetches the report", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.dismiss(["Moai statue"], true);
    });

    expect(setBundleDismissedMock).toHaveBeenCalledWith(["Moai statue"], true);
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
    expect(result.current.busy).toBe(false);
  });

  it("enriches the keys, then refetches before it speaks", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    const answering = deferred<BundleEnrichResponse>();
    enrichBundlesMock.mockReturnValueOnce(answering.promise);

    let pending!: Promise<void>;
    act(() => {
      pending = result.current.enrich(["Moai statue"]);
    });

    // The keys reach the client with no forced refresh and no saved cookie: a
    // first pass reads the page only to fill the empties.
    expect(enrichBundlesMock).toHaveBeenCalledWith(["Moai statue"], {
      refresh: undefined,
      thingiverseCookie: undefined,
    });
    // Nothing is fetched while the page is still in flight.
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
    expect(result.current.busy).toBe(true);

    await act(async () => {
      answering.resolve(enrichResponse([enrichResult()]));
      await pending;
    });

    // The refetch, and only then the message: `refresh` writes `error` itself,
    // so a message set first would be cleared by the refetch it waited for.
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
    expect(result.current.error).toBeNull();
    expect(result.current.notice).toBeNull();
    expect(result.current.busy).toBe(false);
  });

  it("keeps a refusal's sentence and calls the page once, not twice", async () => {
    const reason = "The site refused to be read. Set THINGIVERSE_COOKIE and try again.";
    enrichBundlesMock.mockResolvedValueOnce(
      enrichResponse([enrichResult({ status: "refused", reason })]),
    );

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.enrich(["Moai statue"]);
    });

    // The server's operator sentence, verbatim: rewriting it would lose the
    // env var it names.
    expect(result.current.error).toBe(reason);
    expect(result.current.notice).toBeNull();
    // A refusal is not a flaky network: a retry would only refuse again.
    expect(enrichBundlesMock).toHaveBeenCalledTimes(1);
  });

  it("prints each distinct refusal reason once, joined", async () => {
    const cookie = "Set THINGIVERSE_COOKIE and try again.";
    const blocked = "The site blocked this client.";
    enrichBundlesMock.mockResolvedValueOnce(
      enrichResponse([
        enrichResult({ key: "One", status: "refused", reason: cookie }),
        enrichResult({ key: "Two", status: "refused", reason: cookie }),
        enrichResult({ key: "Three", status: "refused", reason: blocked }),
      ]),
    );

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.enrich(["One", "Two", "Three"]);
    });

    expect(result.current.error).toBe(`${cookie} - ${blocked}`);
  });

  it("says a cached answer did something rather than nothing, and a refetch keeps it", async () => {
    enrichBundlesMock.mockResolvedValueOnce(
      enrichResponse([enrichResult({ status: "cached", filled: [] })]),
    );

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.enrich(["Moai statue"]);
    });

    expect(result.current.notice).toBe("Already checked from the source page");
    expect(result.current.error).toBeNull();

    // `refresh` is not a new answer, so it must not clear the notice: only a
    // new enrich (or a create/dismiss) does.
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.notice).toBe("Already checked from the source page");
  });

  it.each([
    ["unavailable", "This bundle names no vendor page to read"],
    ["unknown", "That bundle is no longer in the report"],
  ] as const)("says what a %s result means", async (status, message) => {
    enrichBundlesMock.mockResolvedValueOnce(enrichResponse([enrichResult({ status })]));

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.enrich(["Moai statue"]);
    });

    expect(result.current.notice).toBe(message);
    expect(result.current.error).toBeNull();
  });

  it("turns a failed enrichment into an error that keeps the server's message", async () => {
    enrichBundlesMock.mockRejectedValueOnce(
      new Error("enrich at most 25 bundles at a time"),
    );

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.enrich(["Moai statue"]);
    });

    expect(result.current.error).toBe("enrich at most 25 bundles at a time");
    expect(result.current.busy).toBe(false);
    // A failed mutation must not refetch over the error it just recorded.
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("forwards a forced re-check", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.enrich(["Moai statue"], { refresh: true });
    });

    expect(enrichBundlesMock).toHaveBeenCalledWith(["Moai statue"], {
      refresh: true,
      thingiverseCookie: undefined,
    });
  });

  it("sends the cookie the Settings card saved", async () => {
    // Seeded through the real `saveSettings` on its DEFAULT session-storage
    // route (`rememberCookies: false`): that session copy is the operator path
    // this ticket is about, and a hand-written localStorage seed would leave it
    // untested. `DEFAULT_SETTINGS` is not exported, so the literal is explicit.
    saveSettings({
      slicer: { enabled: false, selected: "orca" },
      engraving: { enabled: false, selected: "lightburn" },
      theme: { selected: "purple" },
      makerworld: { cookie: "" },
      thingiverse: { cookie: "session=fake-cookie" },
      imports: { rememberCookies: false },
      network: { publicUrl: "" },
      previews: { mode: "automatic" },
    } satisfies AppSettings);

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.enrich(["Moai statue"]);
    });

    // The exact stored string, not a re-trimmed copy: trimming is the body builder's job.
    expect(enrichBundlesMock).toHaveBeenCalledWith(["Moai statue"], {
      refresh: undefined,
      thingiverseCookie: "session=fake-cookie",
    });
  });

  it("reports an unauthorized enrichment to the caller", async () => {
    enrichBundlesMock.mockRejectedValueOnce(new UnauthorizedError());
    const onUnauthorized = vi.fn();

    const { result } = renderHook(() => useBundleDetect({ enabled: true, onUnauthorized }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.enrich(["Moai statue"]);
    });

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    // Exactly what `mutate` records for the same failure: the error's own
    // message, never a second wording.
    expect(result.current.error).toBe("Unauthorized");
  });

  it("keeps the show-dismissed toggle", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    expect(result.current.showDismissed).toBe(false);

    act(() => {
      result.current.setShowDismissed(true);
    });

    expect(result.current.showDismissed).toBe(true);
  });

  it("keeps the show-created toggle", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    expect(result.current.showCreated).toBe(false);

    act(() => {
      result.current.setShowCreated(true);
    });

    expect(result.current.showCreated).toBe(true);
  });

  // The persistence decision, pinned for both toggles: session state only. A
  // persisted Show created would put the settled rows back in the queue for
  // whoever ever flipped it, which is the bug this queue avoids.
  it.each([
    ["showDismissed", "setShowDismissed"],
    ["showCreated", "setShowCreated"],
  ] as const)(
    "keeps %s across queue re-entry and writes no localStorage key",
    async (field, setter) => {
      const setItem = vi.spyOn(Storage.prototype, "setItem");
      const { result, rerender } = renderHook(
        ({ enabled }: { enabled: boolean }) => useBundleDetect({ enabled }),
        { initialProps: { enabled: false } },
      );
      expect(result.current[field]).toBe(false);

      act(() => {
        result.current[setter](true);
      });
      expect(result.current[field]).toBe(true);

      rerender({ enabled: true });
      await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(1));
      rerender({ enabled: false });
      rerender({ enabled: true });

      // Still on after the round trip, because leaving the queue is not a reset.
      expect(result.current[field]).toBe(true);
      // Only a page reload resets it, because nothing was ever stored.
      expect(setItem).not.toHaveBeenCalled();
    },
  );

  it("turns a failed report into an error and never throws", async () => {
    getBundleReportMock.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));

    await waitFor(() => expect(result.current.error).toBe("Bundle detection is unavailable."));
    expect(result.current.report).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("turns a failed create into an error that keeps the server's message", async () => {
    createBundlesMock.mockRejectedValue(new Error("A project with that name already exists"));

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.create([{ key: "Moai statue", name: "Moai" }]);
    });

    expect(result.current.error).toBe("A project with that name already exists");
    expect(result.current.busy).toBe(false);
    // A failed mutation must not refetch over the error it just recorded.
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("reports an unauthorized failure to the caller", async () => {
    getBundleReportMock.mockRejectedValue(new UnauthorizedError());
    const onUnauthorized = vi.fn();

    const { result } = renderHook(() => useBundleDetect({ enabled: true, onUnauthorized }));

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(result.current.error).toBe("Bundle detection is unavailable.");
  });

  it("stamps lastRefreshedAt only on a successful fetch", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    expect(result.current.lastRefreshedAt).toBeNull();

    await waitFor(() => expect(result.current.report).toEqual(report));
    expect(result.current.lastRefreshedAt).toEqual(expect.any(Number));
    const stamped = result.current.lastRefreshedAt;

    createBundlesMock.mockRejectedValue(new Error("nope"));
    await act(async () => {
      await result.current.create([{ key: "Moai statue", name: "Moai" }]);
    });

    expect(result.current.lastRefreshedAt).toBe(stamped);
  });

  // Proves the ordering the whole mechanism exists for: no refetch happens
  // while the walk is still running, and the report the operator then sees is
  // the post-settle one where the extracted bundle finally turned ready.
  it("settles the library scan and then refetches, so an extracted bundle turns ready", async () => {
    const zipOnly = makeReport([
      makeBundle({
        key: "import/Moai.zip",
        rel_path: "import/Moai.zip",
        status: "blocked",
        blocked_reason: "archive_only",
        archive: null,
      }),
    ]);
    const settled = makeReport([
      makeBundle({
        key: "import/Moai",
        rel_path: "import/Moai",
        status: "ready",
        blocked_reason: null,
        archive: { rel_path: "import/Moai.zip", twin_verified: true },
      }),
    ]);
    getBundleReportMock.mockResolvedValueOnce(zipOnly).mockResolvedValueOnce(settled);
    const settle = deferred<SettleOutcome>();
    settleAfterExtractMock.mockReturnValueOnce(settle.promise);

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(zipOnly));

    let outcome!: Promise<SettleOutcome>;
    act(() => {
      outcome = result.current.afterExtract([extractResult]);
    });

    expect(settleAfterExtractMock).toHaveBeenCalledTimes(1);
    expect(settleAfterExtractMock).toHaveBeenCalledWith(
      [extractResult],
      expect.objectContaining({ cancelled: expect.any(Function) }),
    );
    // Still one: the settle has not finished, so there is nothing new to fetch.
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      settle.resolve("settled");
      await outcome;
    });

    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
    expect(result.current.report?.bundles[0]?.status).toBe("ready");
    expect(result.current.report?.bundles[0]?.blocked_reason).toBeNull();
    expect(await outcome).toBe("settled");
  });

  // This module's half of the contract only: it hands the results through
  // untouched and refetches once afterwards. The two-settle / one-startLibraryScan
  // / 409-swallowed proof belongs to lib/scanSettle.test.ts and is not repeated here.
  it("re-kicks a scan when the extraction rode an already-running one", async () => {
    const rodeRunningScan: ArchiveExtractResult = { ...extractResult, scan_started: false };
    settleAfterExtractMock.mockResolvedValue("settled");

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    let outcome!: SettleOutcome;
    await act(async () => {
      outcome = await result.current.afterExtract([rodeRunningScan]);
    });

    // (a) the results array reached the settle unchanged, flag and all
    expect(settleAfterExtractMock.mock.calls[0][0]).toEqual([rodeRunningScan]);
    // (b) exactly one refetch followed the settle
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
    // (c) the settle's verdict is what the caller gets back
    expect(outcome).toBe("settled");
  });

  // Proves the settle loop reads the live open/closed state: the `cancelled`
  // predicate it was handed flips when the queue closes, and a closed queue
  // paints no refetch.
  it("stops waiting when the queue closes mid-settle", async () => {
    const settle = deferred<SettleOutcome>();
    settleAfterExtractMock.mockReturnValueOnce(settle.promise);

    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useBundleDetect({ enabled }),
      { initialProps: { enabled: true } },
    );
    await waitFor(() => expect(result.current.report).toEqual(report));

    let outcome!: Promise<SettleOutcome>;
    act(() => {
      outcome = result.current.afterExtract([extractResult]);
    });

    const cancelled = settleAfterExtractMock.mock.calls[0][1]?.cancelled;
    expect(cancelled).toEqual(expect.any(Function));
    expect(cancelled?.()).toBe(false);

    rerender({ enabled: false });
    expect(cancelled?.()).toBe(true);

    await act(async () => {
      settle.resolve("cancelled");
      await outcome;
    });

    expect(await outcome).toBe("cancelled");
    // Nothing is mounted to paint a new report, so nothing is fetched.
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  // A closed queue must not start a ten-minute poll it would then have to abort.
  it("does not settle a queue that is already closed", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: false }));
    await waitFor(() => expect(result.current.loading).toBe(false));

    let outcome!: SettleOutcome;
    await act(async () => {
      outcome = await result.current.afterExtract([extractResult]);
    });

    expect(settleAfterExtractMock).not.toHaveBeenCalled();
    expect(outcome).toBe("cancelled");
    expect(getBundleReportMock).not.toHaveBeenCalled();
  });
});

describe("useBundleDetect create", () => {
  it("leaves the bulk create door returning nothing", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    let answer: unknown = "not called";
    await act(async () => {
      answer = await result.current.create([{ key: "Moai statue" }]);
    });

    // The bulk doors and `createActive` depend on this staying a void action.
    // The one-step door no longer has a reporting create on the hook at all
    // (Mf-0ck5): it calls the archive-project door itself, whose verdict is one
    // object for one archive rather than a per-key result to match.
    expect(answer).toBeUndefined();
    expect(createBundlesMock).toHaveBeenCalledWith([{ key: "Moai statue" }]);
  });
});
