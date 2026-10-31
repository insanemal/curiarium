// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { copyText } from "./clipboard";

function defineClipboard(value: unknown): void {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value });
}

function defineExecCommand(value: unknown): void {
  Object.defineProperty(document, "execCommand", { configurable: true, value });
}

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
  Reflect.deleteProperty(document, "execCommand");
});

describe("copyText", () => {
  it("writes through the async clipboard API when the browser has it", async () => {
    const writeText = vi.fn(async () => {});
    defineClipboard({ writeText });

    await copyText("part.stl: IntegrityError");

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith("part.stl: IntegrityError");
  });

  it("falls back to execCommand on plain http, where navigator.clipboard is undefined", async () => {
    defineClipboard(undefined);
    const execCommand = vi.fn(() => true);
    defineExecCommand(execCommand);

    await copyText("a\nb");

    expect(execCommand).toHaveBeenCalledWith("copy");
    // The hidden textarea is always cleaned up, copied or not.
    expect(document.body.querySelector("textarea")).toBeNull();
  });

  it("throws when neither path can copy", async () => {
    const writeText = vi.fn(async () => {
      throw new Error("denied");
    });
    defineClipboard({ writeText });
    defineExecCommand(vi.fn(() => false));

    await expect(copyText("x")).rejects.toThrow("Clipboard is not available");
  });
});
