// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  UnauthorizedError,
  getApiBase,
  getOperation,
  planOperation,
  splitActionLabel,
  submitOperation,
  type OperationReview,
  undoOperation,
} from "./index";
// `index.ts` is outside this ticket's surface, so the new symbol is read from
// its domain module - the same module every other production import uses.
import { adoptActionLabel } from "./operations";

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

  it("labels the split action from the plan's own project count", () => {
    // Catches a hard-coded count, a singular drawn from the plural string, and a
    // review with no projects promising nothing at all.
    expect(splitActionLabel({ ...review, projects: [] })).toBe("Split into 1 project");
    expect(splitActionLabel({ ...review, projects: [{ name: "a", root: "a", members: 1 }] })).toBe(
      "Split into 1 project",
    );
    expect(splitActionLabel(review)).toBe("Split into 2 projects");
    expect(
      splitActionLabel({
        ...review,
        projects: [
          { name: "a", root: "a", members: 1 },
          { name: "b", root: "b", members: 1 },
          { name: "c", root: "c", members: 1 },
        ],
      }),
    ).toBe("Split into 3 projects");
  });

  it("labels the adopt action from the plan's own file and group counts", () => {
    // Catches a label named for the subsystem rather than the outcome, a
    // singular drawn from the plural string, and the `""` root bucket counted
    // as a group: that bucket is the folder's own files, and printing it would
    // promise a group the operator cannot see.
    const namedGroups = (count: number): OperationReview["groups"] =>
      Array.from({ length: count }, (_, index) => ({
        name: `Group ${index + 1}`,
        members: 19,
        models: 19,
      }));

    expect(
      adoptActionLabel({
        ...review,
        members: 171,
        groups: namedGroups(9),
        by_role: { model: 171, image: 0, text: 0, archive: 0, other: 0 },
      }),
    ).toBe("Add 171 files in 9 groups");
    expect(
      adoptActionLabel({
        ...review,
        members: 1,
        groups: [{ name: "Hydra", members: 1, models: 1 }],
        by_role: { model: 1, image: 0, text: 0, archive: 0, other: 0 },
      }),
    ).toBe("Add 1 file in 1 group");
    expect(
      adoptActionLabel({
        ...review,
        members: 171,
        groups: [],
        by_role: { model: 171, image: 0, text: 0, archive: 0, other: 0 },
      }),
    ).toBe("Add 171 files");
    // The `""` bucket rides with a real group here, so the test proves it is
    // skipped rather than proving an empty list stays empty.
    expect(
      adoptActionLabel({
        ...review,
        members: 3,
        groups: [
          { name: "", members: 2, models: 2 },
          { name: "Core", members: 1, models: 1 },
        ],
        by_role: { model: 3, image: 0, text: 0, archive: 0, other: 0 },
      }),
    ).toBe("Add 3 files in 1 group");
  });
});
