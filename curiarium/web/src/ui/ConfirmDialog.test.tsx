// @vitest-environment jsdom
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import ConfirmDialog from "./ConfirmDialog";

function renderDialog(overrides: Partial<ComponentProps<typeof ConfirmDialog>> = {}) {
  const props = {
    title: "Delete 3 assets?",
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  };
  const utils = render(<ConfirmDialog {...props} />);
  return { ...utils, props };
}

describe("ConfirmDialog", () => {
  afterEach(cleanup);

  it("renders a modal dialog titled by its heading, plus the body", () => {
    renderDialog({ body: "This cannot be undone." });

    const dialog = screen.getByRole("dialog", { name: "Delete 3 assets?" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(screen.getByText("This cannot be undone.")).toBeTruthy();
  });

  it("calls onConfirm from the confirm button and honours a custom label", () => {
    const { props } = renderDialog({ confirmLabel: "Delete" });

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    expect(props.onConfirm).toHaveBeenCalledTimes(1);
  });

  it("calls onCancel from the cancel button", () => {
    const { props } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it("calls onCancel when Escape is pressed", () => {
    const { props } = renderDialog();

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it("disables both buttons and ignores Escape and confirm clicks while busy", () => {
    const { props } = renderDialog({ busy: true });

    const buttons = screen.getAllByRole("button") as HTMLButtonElement[];
    expect(buttons).toHaveLength(2);
    for (const button of buttons) expect(button.disabled).toBe(true);

    fireEvent.click(buttons[1]);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(props.onConfirm).not.toHaveBeenCalled();
    expect(props.onCancel).not.toHaveBeenCalled();
  });

  it("styles the confirm button red only when danger is set", () => {
    const { unmount } = renderDialog({ danger: true });
    expect(screen.getByRole("button", { name: "Confirm" }).className).toContain("bg-red-600");
    unmount();

    renderDialog();
    expect(screen.getByRole("button", { name: "Confirm" }).className).not.toContain("bg-red-600");
  });

  // The trash queue's empty confirmation carries more than one paragraph, so a
  // node body must render as-is: wrapping it in a `<p>` would nest paragraphs.
  it("renders a node body unwrapped", () => {
    const { container } = renderDialog({
      body: (
        <>
          <p>Trash holds 12 deleted files.</p>
          <p>Emptying it cannot be undone.</p>
        </>
      ),
    });

    expect(screen.getByText("Trash holds 12 deleted files.")).toBeTruthy();
    expect(screen.getByText("Emptying it cannot be undone.")).toBeTruthy();
    expect(container.querySelectorAll("p p")).toHaveLength(0);
    expect(container.querySelectorAll("p")).toHaveLength(2);
  });

  it("disables only the confirm button when confirming is refused, and focuses Cancel", () => {
    renderDialog({ confirmDisabled: true });

    const cancelButton = screen.getByRole("button", { name: "Cancel" });
    const confirmButton = screen.getByRole("button", { name: "Confirm" }) as HTMLButtonElement;

    expect(confirmButton.disabled).toBe(true);
    expect((cancelButton as HTMLButtonElement).disabled).toBe(false);
    // Focus stays inside the dialog: a disabled confirm button cannot hold it.
    expect(document.activeElement).toBe(cancelButton);
  });

  it("renders a busy label while working and ignores a confirm click", () => {
    const { props } = renderDialog({ busy: true, busyLabel: "Emptying\u2026" });

    const working = screen.getByRole("button", { name: "Emptying\u2026" });
    expect(screen.queryByRole("button", { name: "Working..." })).toBeNull();

    fireEvent.click(working);
    expect(props.onConfirm).not.toHaveBeenCalled();
  });
});
