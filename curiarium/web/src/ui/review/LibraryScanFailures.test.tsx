// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { getToasts, resetToasts } from "../../lib/toast";
import LibraryScanFailures from "./LibraryScanFailures";

function makeErrors(count: number): string[] {
  return Array.from({ length: count }, (_, index) => `folder-${index}/part.stl: IntegrityError: duplicate name`);
}

beforeEach(() => {
  resetToasts();
});

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("LibraryScanFailures", () => {
  it("renders nothing when the run had no failures", () => {
    const { container } = render(<LibraryScanFailures failed={0} errors={[]} />);

    expect(container.innerHTML).toBe("");
  });

  it("names the count, caps the list and says how much of it is shown", () => {
    const errors = makeErrors(20);
    render(<LibraryScanFailures failed={67} errors={errors} />);

    expect(screen.getByText("67 files failed")).toBeTruthy();
    for (const message of errors.slice(0, 10)) {
      expect(screen.getByText(message)).toBeTruthy();
    }
    expect(screen.queryByText(errors[10])).toBeNull();
    expect(screen.getByText("+10 more")).toBeTruthy();
    expect(screen.getByText("Showing the first 10 of 67 failures")).toBeTruthy();
    // failed - errors.length: the cap kept these out of the captured list entirely.
    expect(screen.getByText("47 more failures were not recorded individually.")).toBeTruthy();
  });

  it("says everything is shown when the captured list is complete", () => {
    render(<LibraryScanFailures failed={2} errors={makeErrors(2)} />);

    expect(screen.getByText("Showing all 2 failures")).toBeTruthy();
    expect(screen.queryByText(/not recorded individually/)).toBeNull();
  });

  it("copies every captured text, not just the visible ones, and toasts the result", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const errors = makeErrors(20);
    render(<LibraryScanFailures failed={67} errors={errors} />);

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(errors.join("\n")));
    const [toast] = getToasts();
    expect(toast.tone).toBe("info");
    expect(toast.message).toContain("20");
  });

  it("toasts an error instead of pretending it copied", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    Object.defineProperty(document, "execCommand", { configurable: true, value: () => false });
    render(<LibraryScanFailures failed={1} errors={makeErrors(1)} />);

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(getToasts()[0]?.tone).toBe("error"));
    Reflect.deleteProperty(document, "execCommand");
  });
});
