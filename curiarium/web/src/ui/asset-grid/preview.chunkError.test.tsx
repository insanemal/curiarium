// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

// preview.test.tsx's harness minus its ModelViewer stand-in: this one throws
// the browser's chunk-failure message during render. A throwing `vi.mock`
// factory would be wrapped in Vitest's own Error, hiding that message from the
// boundary's classifier.
vi.mock("../ModelViewer", () => {
  function RejectingChunk() {
    throw new TypeError("Failed to fetch dynamically imported module");
  }
  return { default: RejectingChunk, ModelSnapshot: RejectingChunk };
});

vi.mock("../LightBurnPreview", () => ({ default: () => null }));

// Only the two archive calls are replaced; the rest of the barrel (`fileUrl`
// among them) has to stay real for the preview to render at all.
vi.mock("../../lib/api", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  listArchiveEntries: vi.fn(),
  extractArchive: vi.fn(),
}));

import type { Asset } from "../../lib/api";
import { renderPreviewContent } from "./preview";

const asset = {
  id: "a1",
  filename: "x.stl",
  url: "/api/assets/a1/file",
  thumb_url: null,
  tags: [],
} as unknown as Asset;

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("preview chunk failure", () => {
  it("replaces only the card preview when the viewer chunk rejects", async () => {
    render(
      <div>
        <p>card sibling</p>
        {renderPreviewContent(asset, "card", "light")}
      </div>,
    );

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("card sibling")).toBeTruthy();
    expect(screen.getByText("This view failed to load")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload app" })).toBeTruthy();
  });

  it("replaces only the modal preview when the viewer chunk rejects", async () => {
    render(
      <div>
        <p>modal sibling</p>
        {renderPreviewContent(asset, "modal", "light")}
      </div>,
    );

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("modal sibling")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload app" })).toBeTruthy();
  });
});
