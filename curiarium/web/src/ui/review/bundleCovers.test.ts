// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The default renderer pulls in three.js; these tests pass their own, so the
// module is only mocked to keep the import graph light and to make the default
// explicit rather than accidental.
vi.mock("../ModelViewer", () => ({
  ModelSnapshot: () => null,
  generateModelSnapshot: vi.fn(),
}));

import type { BundleCover, BundleDetectItem } from "../../lib/api/bundles";
import { COVER_SNAPSHOT_SIZE, primeModelCovers } from "./bundleCovers";

/** A detected bundle whose only interesting field here is its cover. */
function makeRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return {
    key: "Gear_bearing",
    rel_path: "Gear_bearing",
    title: "Gear bearing",
    name_suggestion: "Gear bearing",
    creator: null,
    licence: null,
    licence_url: null,
    source_url: null,
    detector: "thingiverse-readme",
    confidence: "high",
    metadata_shape: "root-docs",
    description_excerpt: null,
    model_count: 2,
    image_count: 0,
    text_count: 1,
    other_count: 0,
    cover: {
      asset_id: null,
      rel_path: null,
      reason: "none",
      url: null,
      thumb_url: null,
    },
    assets_total: 3,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project_id: null,
    existing_project_name: null,
    dismissed: false,
    ...overrides,
  };
}

function previewCover(overrides: Partial<BundleCover> = {}): BundleCover {
  return {
    asset_id: "asset-9",
    rel_path: "models/gear.stl",
    reason: "model-preview",
    url: "/file/asset-9/gear.stl",
    thumb_url: null,
    ...overrides,
  };
}

/** jsdom will not fetch a `data:` URL, so stand in for the image response. */
function stubImageFetch(): void {
  vi.stubGlobal("fetch", async () => ({
    blob: async () => new Blob(["png-bytes"], { type: "image/png" }),
  }));
}

/** The browser-loadable form of a cover path: the production-safety assertion is
 *  that the renderer receives this, never the raw API-relative path. */
function mediaUrl(rel: string): string {
  return `https://api.example.test${rel}?sig=1`;
}

const consoleWarn = vi.spyOn(console, "warn");

beforeEach(() => {
  consoleWarn.mockReset();
  stubImageFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("primeModelCovers", () => {
  it("generates and persists a derived preview exactly once", async () => {
    const render = vi.fn(async () => "data:image/png;base64,AA==");
    const save = vi.fn(async () => ({}));

    const saved = await primeModelCovers([makeRow({ cover: previewCover() })], {
      mediaUrl,
      render,
      save,
    });

    expect(saved).toBe(1);
    expect(render).toHaveBeenCalledTimes(1);
    const [url, ext, width, height, theme] = render.mock.calls[0] as unknown as [
      string,
      string,
      number,
      number,
      string,
    ];
    expect(url).toBe(mediaUrl("/file/asset-9/gear.stl"));
    expect(ext).toBe("stl");
    expect(width).toBe(COVER_SNAPSHOT_SIZE);
    expect(height).toBe(COVER_SNAPSHOT_SIZE);
    expect(theme).toBe("light");

    expect(save).toHaveBeenCalledTimes(1);
    const [assetId, blob] = save.mock.calls[0] as unknown as [string, Blob];
    expect(assetId).toBe("asset-9");
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.size).toBeGreaterThan(0);
  });

  it("skips a served image, an unregistered path and an empty cover", async () => {
    const render = vi.fn(async () => "data:image/png;base64,AA==");
    const save = vi.fn(async () => ({}));

    const saved = await primeModelCovers(
      [
        // Already has a thumbnail: nothing to derive.
        makeRow({
          key: "Served",
          cover: previewCover({ thumb_url: "/thumb/asset-9.jpg?v=4" }),
        }),
        // The image is referenced but never registered: no asset to save onto.
        makeRow({
          key: "Unregistered",
          cover: {
            asset_id: null,
            rel_path: "images/hero.jpg",
            reason: "largest-image-unregistered",
            url: null,
            thumb_url: null,
          },
        }),
        // Genuinely nothing renderable.
        makeRow({
          key: "Nothing",
          cover: previewCover({ asset_id: null, rel_path: null, reason: "none", url: null }),
        }),
      ],
      { mediaUrl, render, save },
    );

    expect(saved).toBe(0);
    expect(render).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
  });

  it("keeps going after a failed save and never rejects", async () => {
    const render = vi.fn(async () => "data:image/png;base64,AA==");
    const save = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({});

    const rows = [
      makeRow({
        key: "First",
        cover: previewCover({ asset_id: "asset-1", url: "/file/asset-1/a.stl" }),
      }),
      makeRow({
        key: "Second",
        cover: previewCover({ asset_id: "asset-2", url: "/file/asset-2/b.stl" }),
      }),
    ];

    await expect(primeModelCovers(rows, { mediaUrl, render, save })).resolves.toBe(1);
    expect(save).toHaveBeenCalledTimes(2);
    expect(save.mock.calls[1][0]).toBe("asset-2");
    expect(consoleWarn).toHaveBeenCalledTimes(1);
  });
});
