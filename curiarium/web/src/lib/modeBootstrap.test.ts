// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mocked at its domain module, not through the `../lib/api` barrel: the barrel
// is what App's tests replace wholesale (see the comment in `ui/App.tsx`), and
// modeBootstrap deliberately imports the one function it needs.
vi.mock("./api/projects", () => ({ listProjects: vi.fn() }));

import { listProjects } from "./api/projects";
import { UnauthorizedError } from "./api/http";
import { MODE_STORAGE_KEY } from "./mode";
import {
  MODE_DECISION_TIMEOUT_MS,
  resetInitialMode,
  resolveInitialMode,
} from "./modeBootstrap";

const listProjectsMock = vi.mocked(listProjects);

/** jsdom may resolve `window.localStorage` to undefined; install a real one. */
function installMemoryStorage(): void {
  const store = new Map<string, string>();
  const storage: Storage = {
    get length(): number {
      return store.size;
    },
    clear(): void {
      store.clear();
    },
    getItem(key: string): string | null {
      return store.has(key) ? store.get(key)! : null;
    },
    key(index: number): string | null {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string): void {
      store.delete(key);
    },
    setItem(key: string, value: string): void {
      store.set(key, value);
    },
  };
  Object.defineProperty(window, "localStorage", { value: storage, configurable: true });
}

beforeEach(() => {
  installMemoryStorage();
  resetInitialMode();
  listProjectsMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("resolveInitialMode", () => {
  it("uses a stored projects choice without touching the network", async () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");
    listProjectsMock.mockResolvedValue([]);

    await expect(resolveInitialMode()).resolves.toMatchObject({
      mode: "projects",
      projects: null,
    });
    expect(listProjectsMock).not.toHaveBeenCalled();
  });

  it("uses a stored files choice even when the library has projects", async () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "files");
    listProjectsMock.mockResolvedValue([{ id: "p1" }] as never);

    await expect(resolveInitialMode()).resolves.toMatchObject({
      mode: "files",
      projects: null,
    });
    expect(listProjectsMock).not.toHaveBeenCalled();
  });

  it("starts in files on an unrecognised stored value, with no network call", async () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "grid");
    listProjectsMock.mockResolvedValue([{ id: "p1" }] as never);

    await expect(resolveInitialMode()).resolves.toMatchObject({
      mode: "files",
      projects: null,
    });
    expect(listProjectsMock).not.toHaveBeenCalled();
  });

  it("starts in files when nothing is stored and the library is empty", async () => {
    listProjectsMock.mockResolvedValue([]);

    await expect(resolveInitialMode()).resolves.toMatchObject({ mode: "files" });
    expect(listProjectsMock).toHaveBeenCalledTimes(1);
  });

  it("starts in projects when nothing is stored and the library has one", async () => {
    listProjectsMock.mockResolvedValue([{ id: "p1" }] as never);

    await expect(resolveInitialMode()).resolves.toMatchObject({ mode: "projects" });
    expect(listProjectsMock).toHaveBeenCalledTimes(1);
  });

  it("starts in files when the probe fails, and never rejects", async () => {
    listProjectsMock.mockRejectedValue(new Error("network down"));

    // The acceptance is the settled promise: a rejection here would blank the
    // page, because main.tsx awaits it in front of the first render.
    await expect(resolveInitialMode()).resolves.toMatchObject({ mode: "files" });
  });

  it("treats a 401 as an unknown library instead of a fatal error", async () => {
    listProjectsMock.mockRejectedValue(new UnauthorizedError());

    // Nothing is imported from `../lib/toast` or `../lib/auth`: a 401 during
    // boot must not toast, log, or clear the token (that kills the
    // reload-refresh revival). It is simply "no list" -> files, no seed.
    await expect(resolveInitialMode()).resolves.toMatchObject({ mode: "files" });
  });

  it("gives up on a hung probe at the decision timeout", async () => {
    vi.useFakeTimers();
    listProjectsMock.mockReturnValue(new Promise(() => {}));

    const decision = resolveInitialMode();
    await vi.advanceTimersByTimeAsync(MODE_DECISION_TIMEOUT_MS);

    await expect(decision).resolves.toMatchObject({ mode: "files" });
  });

  it("memoises the decision: the probe runs once for repeated calls", async () => {
    listProjectsMock.mockResolvedValue([{ id: "p1" }] as never);

    const first = await resolveInitialMode();
    const second = await resolveInitialMode();

    expect(first).toBe(second);
    expect(first.mode).toBe("projects");
    expect(listProjectsMock).toHaveBeenCalledTimes(1);
  });

  it("hands back the list it decided from", async () => {
    const projects = [{ id: "p1" }, { id: "p2" }] as never;
    listProjectsMock.mockResolvedValue(projects);

    const decision = await resolveInitialMode();

    // The same array instance, so App seeds its state with what the probe
    // already paid for instead of fetching it again.
    expect(decision.mode).toBe("projects");
    expect(decision.projects).toBe(projects);
    expect(listProjectsMock).toHaveBeenCalledTimes(1);
  });

  it("hands back [] for an empty library", async () => {
    listProjectsMock.mockResolvedValue([]);

    const decision = await resolveInitialMode();

    // `[]`, not `null`: an empty library is a real answer, so App must not read
    // the list a second time.
    expect(decision.mode).toBe("files");
    expect(decision.projects).toEqual([]);
    expect(listProjectsMock).toHaveBeenCalledTimes(1);
  });

  it("hands back null on failure", async () => {
    listProjectsMock.mockRejectedValue(new Error("network down"));

    await expect(resolveInitialMode()).resolves.toMatchObject({ projects: null });
  });

  it("hands back null on 401", async () => {
    listProjectsMock.mockRejectedValue(new UnauthorizedError());

    await expect(resolveInitialMode()).resolves.toMatchObject({ projects: null });
  });

  it("hands back null on timeout", async () => {
    vi.useFakeTimers();
    listProjectsMock.mockReturnValue(new Promise(() => {}));

    const decision = resolveInitialMode();
    await vi.advanceTimersByTimeAsync(MODE_DECISION_TIMEOUT_MS);

    await expect(decision).resolves.toMatchObject({ mode: "files", projects: null });
  });
});
