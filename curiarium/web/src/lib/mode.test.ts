// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MODE_STORAGE_KEY,
  type StoredModeState,
  decideMode,
  loadMode,
  readStoredModeState,
  saveMode,
} from "./mode";

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
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("library mode persistence", () => {
  it("defaults to files when nothing is stored", () => {
    expect(loadMode()).toBe("files");
  });

  it("round-trips both modes", () => {
    saveMode("projects");
    expect(loadMode()).toBe("projects");

    saveMode("files");
    expect(loadMode()).toBe("files");
  });

  it("ignores a stored value it does not recognise", () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "grid");
    expect(loadMode()).toBe("files");
  });

  it("falls back to files when storage reads throw", () => {
    vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    expect(loadMode()).toBe("files");
  });

  it("swallows storage write failures", () => {
    vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => saveMode("projects")).not.toThrow();
  });
});

describe("readStoredModeState", () => {
  it("reports absent when nothing is stored", () => {
    expect(readStoredModeState()).toEqual({ kind: "absent" });
  });

  it("reports each recognised mode", () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "files");
    expect(readStoredModeState()).toEqual({ kind: "mode", mode: "files" });

    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");
    expect(readStoredModeState()).toEqual({ kind: "mode", mode: "projects" });
  });

  it("reports invalid for anything it does not recognise", () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "grid");
    expect(readStoredModeState()).toEqual({ kind: "invalid" });
  });

  it("reports unreadable when storage reads throw", () => {
    vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    expect(readStoredModeState()).toEqual({ kind: "unreadable" });
  });
});

describe("decideMode", () => {
  const absent: StoredModeState = { kind: "absent" };
  const invalid: StoredModeState = { kind: "invalid" };
  const unreadable: StoredModeState = { kind: "unreadable" };

  it("derives files for an absent mode with an unknown count", () => {
    expect(decideMode(absent, null)).toBe("files");
  });

  it("derives files for an absent mode and an empty library", () => {
    expect(decideMode(absent, 0)).toBe("files");
  });

  it("derives projects for an absent mode and a non-empty library", () => {
    expect(decideMode(absent, 3)).toBe("projects");
  });

  it("lets a stored files choice win over a non-empty library", () => {
    expect(decideMode({ kind: "mode", mode: "files" }, 3)).toBe("files");
  });

  it("lets a stored projects choice win over an empty library", () => {
    expect(decideMode({ kind: "mode", mode: "projects" }, 0)).toBe("projects");
  });

  it("never derives from an invalid stored value", () => {
    expect(decideMode(invalid, null)).toBe("files");
    expect(decideMode(invalid, 3)).toBe("files");
  });

  it("never derives from unreadable storage", () => {
    expect(decideMode(unreadable, 3)).toBe("files");
  });
});
