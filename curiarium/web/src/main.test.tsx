// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";

// The boot decision is this file's subject, so a deferred stands in for it: the
// test holds it open and inspects #root before it settles.
vi.mock("./lib/modeBootstrap", () => ({ resolveInitialMode: vi.fn() }));
// App and Toaster become markers: this file covers the boot sequence, not the
// shell's own rendering.
vi.mock("./ui/App", async () => {
  const react = await import("react");
  return {
    default: ({ initialMode }: { initialMode?: string }) =>
      react.createElement("div", { "data-testid": "app-marker", "data-mode": initialMode }),
  };
});
vi.mock("./ui/Toaster", () => ({ default: () => null }));

beforeEach(() => {
  // `main.tsx` looks up #root at module scope, so it must exist before the
  // import below, and the module graph is rebuilt per test to re-run that scope.
  vi.resetModules();
  document.body.innerHTML = '<div id="root"></div>';
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

/** Imports a fresh `main.tsx` with a boot decision this test settles by hand. */
async function bootWithDeferredDecision(): Promise<{
  settle: (mode: "files" | "projects") => void;
}> {
  let settle!: (mode: "files" | "projects") => void;
  const decision = new Promise<"files" | "projects">(resolve => {
    settle = resolve;
  });
  const boot = await import("./lib/modeBootstrap");
  vi.mocked(boot.resolveInitialMode).mockReturnValue(decision);
  await import("./main");
  return { settle };
}

describe("boot", () => {
  it("paints nothing into #root until the mode decision settles", async () => {
    await bootWithDeferredDecision();
    // Let any stray microtask run: a render must still not have happened.
    await Promise.resolve();

    expect(document.getElementById("root")?.childElementCount).toBe(0);
    expect(screen.queryByTestId("app-marker")).toBeNull();
  });

  it("renders App with the decided mode once the decision settles", async () => {
    const { settle } = await bootWithDeferredDecision();
    expect(document.getElementById("root")?.childElementCount).toBe(0);

    settle("projects");

    await waitFor(() => expect(screen.getByTestId("app-marker")).toBeTruthy());
    expect(screen.getByTestId("app-marker").dataset.mode).toBe("projects");
  });
});
