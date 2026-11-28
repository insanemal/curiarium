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

/** Every control in the bar writes, so a viewer must see none of them. */
const WRITE_BUTTONS = ["Upload", "Upload folder"];

describe("UploadBar role capability", () => {
  afterEach(cleanup);

  it("shows the whole write bar to an editor", () => {
    render(<UploadBar canContribute onUploaded={vi.fn()} />);

    for (const name of WRITE_BUTTONS) {
      expect(screen.getByRole("button", { name })).toBeTruthy();
    }
  });

  it("renders no bar and no write control for a viewer", () => {
    const { container } = render(<UploadBar canContribute={false} onUploaded={vi.fn()} />);

    expect(container.innerHTML).toBe("");
    for (const name of WRITE_BUTTONS) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });
});
