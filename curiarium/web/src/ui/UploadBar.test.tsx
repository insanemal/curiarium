// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

// Only the network layer is mocked: the real `useUpload`/`ZipImportModal` prompt
// runs behind the bar, exactly as a user would meet it.
vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {
    constructor(message = "Unauthorized") {
      super(message);
      this.name = "UnauthorizedError";
    }
  },
  createFolder: vi.fn(),
  uploadAsset: vi.fn(),
}));

import UploadBar from "./UploadBar";

/** Every control in the bar contributes - the two uploads and the URL import -
 *  so a viewer must see none of them. */
const WRITE_BUTTONS = ["Upload", "Upload folder", "Check the link"];

/** The URL import door's own control, next to the buttons. */
function importLinkInput(): HTMLElement | null {
  return screen.queryByRole("textbox", { name: "Import link" });
}

describe("UploadBar role capability", () => {
  afterEach(cleanup);

  it("shows the whole write bar, URL import included, to an editor", () => {
    render(<UploadBar canContribute onUploaded={vi.fn()} />);

    for (const name of WRITE_BUTTONS) {
      expect(screen.getByRole("button", { name })).toBeTruthy();
    }
    // The URL-import family that left with three routes is back (Mf-x65b), in the
    // same row the removed input lived in.
    expect(importLinkInput()).toBeTruthy();
  });

  it("renders no bar and no write control for a viewer", () => {
    const { container } = render(<UploadBar canContribute={false} onUploaded={vi.fn()} />);

    expect(container.innerHTML).toBe("");
    for (const name of WRITE_BUTTONS) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(importLinkInput()).toBeNull();
  });
});
