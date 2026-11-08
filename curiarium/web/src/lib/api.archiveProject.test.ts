// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { UnauthorizedError } from "./api";
import { createProjectFromExtract, type ArchiveProjectResult } from "./api/imports";

type MockResponse = {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
  headers: Headers;
};

type MockFetch = ReturnType<typeof vi.fn<(input: string, init?: RequestInit) => Promise<MockResponse>>>;

function respond(
  payload: unknown,
  init: { ok?: boolean; status?: number; body?: string } = {},
): MockResponse {
  const body = init.body ?? JSON.stringify(payload);
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => payload,
    text: async () => body,
    headers: new Headers(),
  };
}

function installFetch(response: MockResponse): MockFetch {
  const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<MockResponse>>(
    async () => response,
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function onlyCall(fetchMock: MockFetch): { url: string; init: RequestInit } {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0];
  if (!init) throw new Error("fetch was called without an init object");
  return { url, init };
}

/** One created verdict, in the shape `ArchiveProjectOut` actually serialises:
 *  the `ProjectOut` is the full row, not a summary the client invents. */
function created(overrides: Partial<ArchiveProjectResult> = {}): ArchiveProjectResult {
  return {
    status: "created",
    archive: "import/01_Fates End - Wizard Tower.zip",
    twin: "import/01_Fates End - Wizard Tower",
    planned: 21,
    resolved: 21,
    failed: [],
    project: {
      id: "p1",
      name: "01_Fates End - Wizard Tower",
      description: null,
      creator: null,
      source_url: null,
      licence: null,
      licence_url: null,
      origin: "own",
      cover_asset_id: "a1",
      cover_thumb_url: "/thumb/a1.jpg?v=1",
      asset_count: 21,
      total_size: 4096,
      metadata_json: {
        _archive_project: {
          archive: "import/01_Fates End - Wizard Tower.zip",
          twin: "import/01_Fates End - Wizard Tower",
          planned: 21,
          resolved: 21,
          fingerprint: "0123456789abcdef",
          common_root: "01_Fates End - Wizard Tower",
          groups: [{ name: "01_Wizard", count: 10 }],
        },
      },
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    },
    ...overrides,
  };
}

describe("createProjectFromExtract API client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts one create for the archive and parses the verdict", async () => {
    const fetchMock = installFetch(respond(created()));

    const result = await createProjectFromExtract({
      path: "import/01_Fates End - Wizard Tower.zip",
      name: "Wizard v5.4",
    });

    expect(result.status).toBe("created");
    expect(result.planned).toBe(21);
    expect(result.project?.asset_count).toBe(21);
    const { url, init } = onlyCall(fetchMock);
    expect(url.endsWith("/import/archive/project")).toBe(true);
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({
      path: "import/01_Fates End - Wizard Tower.zip",
      name: "Wizard v5.4",
    });
  });

  it("sends the whole archive when no entry list is given", async () => {
    const fetchMock = installFetch(respond(created()));

    await createProjectFromExtract({ path: "Moai.zip" });

    const { init } = onlyCall(fetchMock);
    // No `entries` key at all: `None` on the server means every member, and a
    // literal `null` in the body would say the same thing less clearly.
    expect(JSON.parse(String(init.body))).toEqual({ path: "Moai.zip" });
  });

  it("passes an entry list through when the caller has one", async () => {
    const fetchMock = installFetch(respond(created()));

    await createProjectFromExtract({ path: "Moai.zip", entries: ["Moai/README.txt", "Moai/Moai.stl"] });

    const { init } = onlyCall(fetchMock);
    expect(JSON.parse(String(init.body))).toEqual({
      path: "Moai.zip",
      entries: ["Moai/README.txt", "Moai/Moai.stl"],
    });
  });

  it("parses a refusal verdict as a 200 answer, not an error", async () => {
    installFetch(
      respond(
        created({
          status: "partial",
          resolved: 15,
          failed: ["Moai/images/preview.png", "Moai/nested/accessories.zip"],
          project: null,
        }),
      ),
    );

    const result = await createProjectFromExtract({ path: "Moai.zip" });

    expect(result.status).toBe("partial");
    expect(result.planned).toBe(21);
    expect(result.resolved).toBe(15);
    expect(result.failed).toEqual(["Moai/images/preview.png", "Moai/nested/accessories.zip"]);
    expect(result.project).toBeNull();
  });

  it("carries the twin directory a zero-resolution verdict points the operator at", async () => {
    installFetch(
      respond(
        created({ status: "nothing_resolved", planned: 21, resolved: 0, failed: ["Moai/Moai.stl"], project: null }),
      ),
    );

    const result = await createProjectFromExtract({ path: "Moai.zip" });

    expect(result.status).toBe("nothing_resolved");
    expect(result.twin).toBe("import/01_Fates End - Wizard Tower");
  });

  it("surfaces the server detail on a 409 name collision", async () => {
    installFetch(
      respond(null, {
        ok: false,
        status: 409,
        body: JSON.stringify({ detail: "Project name already exists" }),
      }),
    );

    const error = await createProjectFromExtract({ path: "Moai.zip", name: "Moai" }).catch((err: unknown) => err);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toBe("Project name already exists");
  });

  it("falls back to a named failure when the error body says nothing", async () => {
    installFetch(respond(null, { ok: false, status: 500, body: "" }));

    const error = await createProjectFromExtract({ path: "Moai.zip" }).catch((err: unknown) => err);
    expect((error as Error).message).toBe("Project creation failed");
  });

  it("keeps a non-JSON error body, which is what a proxy in front of the API sends", async () => {
    installFetch(
      respond(null, { ok: false, status: 502, body: "<!doctype html><h1>502 Bad Gateway</h1>" }),
    );

    const error = await createProjectFromExtract({ path: "Moai.zip" }).catch((err: unknown) => err);
    expect((error as Error).message).toBe("<!doctype html><h1>502 Bad Gateway</h1>");
  });

  it("throws UnauthorizedError on 401", async () => {
    installFetch(respond(null, { ok: false, status: 401, body: "" }));

    await expect(createProjectFromExtract({ path: "Moai.zip" })).rejects.toBeInstanceOf(UnauthorizedError);
  });
});
