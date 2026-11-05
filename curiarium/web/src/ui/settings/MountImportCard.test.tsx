// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import MountImportCard from "./MountImportCard";

type CardProps = Parameters<typeof MountImportCard>[0];

function renderCard(overrides: Partial<CardProps> = {}) {
  const props: CardProps = {
    mountEnabled: false,
    mountCopyFiles: true,
    mountPath: "/imports",
    mountInitial: { enabled: false, copy: true },
    mountLoading: false,
    mountSaving: false,
    setMountEnabled: vi.fn(),
    setMountCopyFiles: vi.fn(),
    saveMountSettings: vi.fn(),
    ...overrides,
  };
  const { rerender } = render(<MountImportCard {...props} />);
  return {
    props,
    // The card is controlled: a checkbox click only calls a setter, so "dirty"
    // can only be observed by feeding the new value back in as a prop.
    withProps: (next: Partial<CardProps>) => rerender(<MountImportCard {...props} {...next} />),
  };
}

afterEach(() => {
  cleanup();
});

describe("MountImportCard", () => {
  it("shows the configured mount path", () => {
    renderCard({ mountPath: "/imports" });

    expect(screen.getByText(/Mount path: \/imports/)).toBeTruthy();
  });

  // The acceptance case: "Not configured" alone leaves the operator hunting for
  // a control that does not exist, because IMPORT_MOUNT_PATH is read at startup.
  it("names IMPORT_MOUNT_PATH when the server reports no mount path", () => {
    renderCard({ mountPath: null });

    expect(screen.getByText(/Not configured/)).toBeTruthy();
    expect(screen.getByText(/IMPORT_MOUNT_PATH/)).toBeTruthy();
  });

  it("keeps Save disabled until the form differs from what the server stored", () => {
    const { withProps } = renderCard();
    const saveButton = () => screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;

    expect(saveButton().disabled).toBe(true);

    fireEvent.click(screen.getByLabelText(/Enable mount import on startup/));
    withProps({ mountEnabled: true });

    expect(saveButton().disabled).toBe(false);
  });

  it("toggling the enable checkbox reports the new value upward", () => {
    const { props } = renderCard();

    fireEvent.click(screen.getByLabelText(/Enable mount import on startup/));

    expect(props.setMountEnabled).toHaveBeenCalledWith(true);
  });

  it("toggling the copy checkbox reports the new value upward", () => {
    const { props } = renderCard();

    fireEvent.click(screen.getByLabelText(/Copy files into Curiarium storage/));

    expect(props.setMountCopyFiles).toHaveBeenCalledWith(false);
  });
});
