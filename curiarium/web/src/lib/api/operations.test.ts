// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  UnauthorizedError,
  getApiBase,
  getOperation,
  planOperation,
  submitOperation,
  type OperationReview,
  undoOperation,
} from "./index";
// `index.ts` is outside this ticket's surface, so the new symbols are read from
// their domain module - the same module every other production import uses.
import { cancelOperation, listOperations } from "./operations";

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

const review: OperationReview = {
  workflow: "split-and-create",
  target: "Widget",
  moves: 2,
  creates: 2,
  adopts: 0,
  deletes: 0,
  members: 7,
  groups: [],
  by_role: { model: 7, image: 0, text: 0, archive: 0, other: 0 },
  cover: null,
  excluded: [],
  existing_project_id: null,
  existing_project_name: null,
  move_list: [
    { source: "Widget/a", target: "a", kind: "dir", action: "hoist" },
    { source: "Widget/b", target: "b", kind: "dir", action: "hoist" },
  ],
  projects: [
    { name: "a", root: "a", members: 4 },
    { name: "b", root: "b", members: 3 },
  ],
};

const plan = {
  plan_id: "plan-1.digest",
  workflow: "split-and-create",
  target: "Widget",
  steps: [{ index: 0, op: "move", summary: "move 2 top folder(s) out of 'Widget'" }],
  review,
};

const operation = {
  id: "op-1",
  workflow: "split-and-create",
  target: "Widget",
  status: "queued",
  submitted_by: "u1",
  created_at: "2026-01-01T00:00:00Z",
  started_at: null,
  finished_at: null,
  error: null,
  undo_state: "none",
  superseded_by: null,
  steps: [],
};

describe("operations API client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the workflow, target and params to /operations/plan", async () => {
    // Catches a client that builds its own steps locally instead of asking the
    // server for a plan, and one that drops the params the planner needs.
    const fetchMock = installFetch(respond(plan));

    await expect(
      planOperation({ workflow: "split-and-create", target: "Widget", params: { shape: "split" } }),
    ).resolves.toEqual(plan);

    const { url, init } = onlyCall(fetchMock);
    expect(url).toBe(`${getApiBase()}/operations/plan`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      workflow: "split-and-create",
      target: "Widget",
      params: { shape: "split" },
    });
  });

  it("posts only plan_id and idempotency_key to /operations", async () => {
    // Catches a client that leaks a hand-built step list into the submit body:
    // the server must be the only author of steps, so the parsed body's keys are
    // exactly these two.
    const fetchMock = installFetch(respond(operation));

    await expect(submitOperation({ planId: "plan-1.digest", idempotencyKey: "key-1" })).resolves.toEqual(
      operation,
    );

    const { url, init } = onlyCall(fetchMock);
    expect(url).toBe(`${getApiBase()}/operations`);
    expect(init.method).toBe("POST");
    const body = JSON.parse(String(init.body));
    expect(Object.keys(body).sort()).toEqual(["idempotency_key", "plan_id"]);
    expect(body).toEqual({ plan_id: "plan-1.digest", idempotency_key: "key-1" });
  });

  it("gets one operation at its url-encoded path", async () => {
    // Catches an id interpolated raw into the path, which an id with a slash or
    // a space would turn into a different route.
    const fetchMock = installFetch(respond(operation));

    await expect(getOperation("op/1")).resolves.toEqual(operation);

    const { url, init } = onlyCall(fetchMock);
    expect(url).toBe(`${getApiBase()}/operations/op%2F1`);
    expect(init.method ?? "GET").toBe("GET");
  });

  it("posts the undo with no body and no Content-Type", async () => {
    // Catches a body on a route whose id in the path is the whole argument, and a
    // Content-Type that would claim a body that is not there.
    const undone = {
      operation_id: "op-1",
      undone: true,
      project_id: "p1",
      project_name: "a",
      moves: 2,
      projects_removed: ["p2"],
      members_removed: null,
    };
    const fetchMock = installFetch(respond(undone));

    await expect(undoOperation("op-1")).resolves.toEqual(undone);

    const { url, init } = onlyCall(fetchMock);
    expect(url).toBe(`${getApiBase()}/operations/op-1/undo`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
    const headers = new Headers(init.headers);
    expect(headers.get("Content-Type")).toBeNull();
  });

  it("throws UnauthorizedError on a 401 from submit", async () => {
    // Catches a submit that swallows the auth failure and reports the generic
    // fallback instead, so the app never re-authenticates.
    installFetch(respond(null, { ok: false, status: 401, body: "" }));

    await expect(
      submitOperation({ planId: "plan-1.digest", idempotencyKey: "key-1" }),
    ).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("surfaces the server's detail on a 409 from undo", async () => {
    // Catches an undo that replaces the server's sentence with its own fallback:
    // the operator has to read why the refusal happened, not a generic failure.
    installFetch(
      respond(null, {
        ok: false,
        status: 409,
        body: JSON.stringify({ detail: "the operation has already been undone" }),
      }),
    );

    const error = await undoOperation("op-1").catch((err: unknown) => err);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toBe("the operation has already been undone");
  });

  it("posts the cancel with no body and no Content-Type", async () => {
    // A cancel names its operation in the path, so a body would be a claim the
    // route did not ask for.
    const cancelling = { ...operation, status: "cancelling" };
    const fetchMock = installFetch(respond(cancelling));

    await expect(cancelOperation("op-1")).resolves.toEqual(cancelling);

    const { url, init } = onlyCall(fetchMock);
    expect(url).toBe(`${getApiBase()}/operations/op-1/cancel`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
    const headers = new Headers(init.headers);
    expect(headers.get("Content-Type")).toBeNull();
  });

  it("throws UnauthorizedError on a 401 from cancel", async () => {
    installFetch(respond(null, { ok: false, status: 401, body: "" }));

    await expect(cancelOperation("op-1")).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("surfaces the server's detail on a 409 from cancel", async () => {
    // The server refuses a finished or never-queued operation for a reason the
    // operator has to read, so its sentence is shown verbatim.
    installFetch(
      respond(null, {
        ok: false,
        status: 409,
        body: JSON.stringify({ detail: "the operation has already finished" }),
      }),
    );

    const error = await cancelOperation("op-1").catch((err: unknown) => err);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toBe("the operation has already finished");
  });

  it("passes target and submitted_by to the query, including an empty target", async () => {
    // `""` is the library root, a legal target: `!= null` keeps it in the query
    // rather than dropping it and widening the read to every target.
    const fetchMock = installFetch(respond([]));

    await listOperations({ target: "", submitted_by: "u1" });

    const { url } = onlyCall(fetchMock);
    const query = new URLSearchParams(url.split("?")[1] ?? "");
    expect(query.has("target")).toBe(true);
    expect(query.get("target")).toBe("");
    expect(query.get("submitted_by")).toBe("u1");
  });

  it("omits target and submitted_by when they are undefined", async () => {
    const fetchMock = installFetch(respond([]));

    await listOperations({ workflow: "scan" });

    const { url } = onlyCall(fetchMock);
    expect(url).toBe(`${getApiBase()}/operations?workflow=scan`);
  });
});
