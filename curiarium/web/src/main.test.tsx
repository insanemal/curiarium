// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import type { Project } from "./lib/api/projects";
import type { BootDecision } from "./lib/modeBootstrap";

// The boot decision is this file's subject, so a deferred stands in for it: the
// test holds it open and inspects #root before it settles.
vi.mock("./lib/modeBootstrap", () => ({ resolveInitialMode: vi.fn() }));
// App and Toaster become markers: this file covers the boot sequence, not the
// shell's own rendering. The marker prints the handed-over project list too, so
// the boot decision reaching `initialProjects` is observable.
vi.mock("./ui/App", async () => {
  const react = await import("react");
  return {
    default: ({
      initialMode,
      initialProjects,
    }: {
      initialMode?: string;
      initialProjects?: Project[] | null;
    }) =>
      react.createElement("div", {
        "data-testid": "app-marker",
        "data-mode": initialMode,
        "data-projects": initialProjects == null ? "null" : String(initialProjects.length),
      }),
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
  settle: (decision: BootDecision) => void;
}> {
  let settle!: (decision: BootDecision) => void;
  const decision = new Promise<BootDecision>(resolve => {
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

  it("renders App with the decided mode and its project list once the decision settles", async () => {
    const { settle } = await bootWithDeferredDecision();
    expect(document.getElementById("root")?.childElementCount).toBe(0);

    settle({ mode: "projects", projects: [{ id: "p1" }, { id: "p2" }] as unknown as Project[] });

    await waitFor(() => expect(screen.getByTestId("app-marker")).toBeTruthy());
    expect(screen.getByTestId("app-marker").dataset.mode).toBe("projects");
    // The list the probe fetched reaches App as a prop, so boot pays one read.
    expect(screen.getByTestId("app-marker").dataset.projects).toBe("2");
  });

  it("hands a null project list down when the decision fetched none", async () => {
    const { settle } = await bootWithDeferredDecision();

    settle({ mode: "files", projects: null });

    await waitFor(() => expect(screen.getByTestId("app-marker")).toBeTruthy());
    expect(screen.getByTestId("app-marker").dataset.mode).toBe("files");
    expect(screen.getByTestId("app-marker").dataset.projects).toBe("null");
  });
});
