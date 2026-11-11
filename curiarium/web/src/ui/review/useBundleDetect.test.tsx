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
  type BundleCreateItem,
  type BundleCreateItemResult,
  type BundleCreateResponse,
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

/** One row of a create response, in the shape `POST /bundles/create` serialises
 *  (`bundle_service._result`). A `skipped` row can carry a real `project_id` -
 *  the already-created case is answered that way - which is why the doors filter
 *  on `status` and the hook must hand the rows through untouched. */
function createItemResult(
  overrides: Partial<BundleCreateItemResult> = {},
): BundleCreateItemResult {
  return {
    key: "Moai statue",
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

/** A whole create response. The counters derive from `results` exactly the way
 *  the server counts them (`counters[result["status"]] += 1`), so a fixture cannot
 *  claim a `created` its own rows do not contain. */
function createResponse(results: BundleCreateItemResult[]): BundleCreateResponse {
  const count = (status: string) => results.filter(result => result.status === status).length;
  return {
    created: count("created"),
    skipped: count("skipped"),
    blocked: count("blocked"),
    failed: count("failed"),
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

  // The bulk door's half of the contract: this member waits and does nothing
  // else. A walk here would be paid once per archive by the many-archive click,
  // which re-walks the library itself exactly once when the queue is done.
  it("waits for the scan without re-walking the queue", async () => {
    const settle = deferred<SettleOutcome>();
    settleAfterExtractMock.mockReturnValueOnce(settle.promise);

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    let outcome!: Promise<SettleOutcome>;
    act(() => {
      outcome = result.current.waitAfterExtract([extractResult]);
    });

    expect(settleAfterExtractMock).toHaveBeenCalledTimes(1);
    expect(settleAfterExtractMock).toHaveBeenCalledWith(
      [extractResult],
      expect.objectContaining({ cancelled: expect.any(Function) }),
    );

    await act(async () => {
      settle.resolve("settled");
      await outcome;
    });

    expect(await outcome).toBe("settled");
    // Still the activation's one walk: the settle alone refetches nothing.
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
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
  it("hands the bulk doors the server's verdict", async () => {
    const items: BundleCreateItem[] = [{ key: "Moai statue", name: "Moai" }];
    const answer = createResponse([
      createItemResult({
        key: "Moai statue",
        status: "created",
        project_id: "project-1",
        name: "Moai",
        assets_added: 12,
      }),
    ]);
    createBundlesMock.mockResolvedValueOnce(answer);

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    let verdict: BundleCreateResponse | null | undefined;
    await act(async () => {
      verdict = await result.current.create(items);
    });

    // The whole wire response - counters and per-key results - unchanged. Only
    // the doors know what the click meant (one ticked row, or a whole page of
    // them), so only they may decide which rows are worth reporting.
    expect(verdict).toEqual(answer);
    // The invariant, stated the other way round: a door that is handed nothing
    // cannot say which of the operator's ticks became a project, which is exactly
    // what this hook used to answer.
    expect(verdict).not.toBeUndefined();
    expect(createBundlesMock).toHaveBeenCalledWith(items);
  });

  it("hands a create that made no project back row for row, because filtering is the door's job", async () => {
    // Both rows are the server's own "nothing was written" answers, and both
    // come back `skipped`: `bundle_service._create_one` reports the already-
    // created case as `skipped`/`already_created` carrying the EXISTING project's
    // id, and a typed name that cleans onto an existing one as `skipped` with no
    // id at all. A hook that dropped non-`created` rows - or handed any non-null
    // id up - would be deciding for the operator what counts as a create.
    const items: BundleCreateItem[] = [
      { key: "Moai statue" },
      { key: "Wizard Tower", name_only: true },
    ];
    const answer = createResponse([
      createItemResult({
        key: "Moai statue",
        status: "skipped",
        project_id: "project-1",
        name: "Moai statue",
        assets_added: 0,
        reason: "already_created",
      }),
      createItemResult({
        key: "Wizard Tower",
        status: "skipped",
        project_id: null,
        name: "Wizard Tower",
        assets_added: 0,
        reason: "project name already exists",
      }),
    ]);
    createBundlesMock.mockResolvedValueOnce(answer);

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    let verdict: BundleCreateResponse | null | undefined;
    await act(async () => {
      verdict = await result.current.create(items);
    });

    expect(verdict).toEqual(answer);
    expect(verdict?.created).toBe(0);
    expect(verdict?.skipped).toBe(2);
    // Nothing was dropped, reordered or summarised on the way out - including the
    // id a door must not report as landed-on.
    expect(verdict?.results.map(row => [row.key, row.status, row.project_id])).toEqual([
      ["Moai statue", "skipped", "project-1"],
      ["Wizard Tower", "skipped", null],
    ]);
    expect(createBundlesMock).toHaveBeenCalledWith(items);
  });

  it("answers a create the server refused with null, so the doors stay silent", async () => {
    createBundlesMock.mockRejectedValueOnce(new Error("only editors may create projects"));

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    let verdict: BundleCreateResponse | null | undefined;
    await act(async () => {
      verdict = await result.current.create([{ key: "Moai statue" }]);
    });

    // A failed create is not a verdict: the error line already speaks it, and a
    // door that reported from `null` would be reporting a project that does not
    // exist. The message and the no-refetch-over-an-error half are pinned by
    // "turns a failed create into an error that keeps the server's message";
    // what is pinned here is the value the door is handed.
    expect(verdict).toBeNull();
  });

  it("clears the last enrichment's notice, because a create is a new decision", async () => {
    // A notice in place the way the enrichment tests put one there.
    enrichBundlesMock.mockResolvedValueOnce(
      enrichResponse([enrichResult({ status: "cached", filled: [] })]),
    );

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.enrich(["Moai statue"]);
    });

    expect(result.current.notice).toBe("Already checked from the source page");
    // Initial walk + the refetch the enrichment waited for.
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      await result.current.create([{ key: "Moai statue", name: "Moai" }]);
    });

    // The `mutate` contract the routed-through implementation had and the
    // hand-written one keeps: the last enrichment's sentence no longer describes
    // what the operator just did, so it is gone.
    expect(result.current.notice).toBeNull();
    // And it took exactly one fetch with it - the post-create refetch. The
    // enrichment's own refetch is the second call counted above, so three is the
    // honest number for "the create refetched once, and nothing refetched twice".
    expect(getBundleReportMock).toHaveBeenCalledTimes(3);
  });
});
