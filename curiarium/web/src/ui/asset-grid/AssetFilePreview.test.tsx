// @vitest-environment jsdom
import { useState, type ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { AssetFile } from "../../lib/api";
import WorkspaceHeader from "../WorkspaceHeader";
import AssetFilePreview from "./AssetFilePreview";

vi.mock("../../lib/api", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  assetFilePreviewUrl: vi.fn((rel: string) => rel),
}));

afterEach(() => {
  cleanup();
});

// `.stl` under `application/octet-stream` has no preview kind, so the body never
// fetches and the dialog is a stable target for the Escape assertions.
const FILE: AssetFile = {
  id: "sf1",
  filename: "model.stl",
  mime: "application/octet-stream",
  size: 2048,
  position: 0,
  url: "/asset/a1/files/sf1",
};

function Host(): ReactElement {
  const [open, setOpen] = useState(true);
  return (
    <>
      <WorkspaceHeader narrow mode="files" role="viewer" onModeChange={vi.fn()} />
      {open && <AssetFilePreview assetId="a1" file={FILE} onClose={() => setOpen(false)} />}
    </>
  );
}

describe("AssetFilePreview Escape ownership", () => {
  it("one Escape closes only the innermost surface", async () => {
    render(<Host />);
    await screen.findByText("Preview is not available for this file type.");
    expect(screen.getByRole("dialog")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "More destinations" }));
    const reviewButton = screen.getByRole("button", { name: "Review" });

    // The disclosure consumes this Escape, so the preview underneath must stay.
    fireEvent.keyDown(reviewButton, { key: "Escape" });
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    expect(screen.getByRole("dialog")).toBeTruthy();

    // Nothing in the tree claims the second one, so it reaches the document.
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
