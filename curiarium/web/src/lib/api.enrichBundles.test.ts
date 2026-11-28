// @vitest-environment jsdom
//
// `enrichBundles` is the operations adapter now: the server deleted `POST
// /bundles/enrich`, so one call plans the `enrich` workflow, submits that plan
// and polls the row the runner writes until it leaves a terminal status. Every
// assertion below is on the WIRE - the plan body, the submit body and the reads -
// because the contract this function owes its callers is exactly the deleted
// route's: the plan carries the keys, the refresh flag and the cookie, and the
// return value is the step's result untouched.
import { afterEach, describe, expect, it, vi } from "vitest";
import { enrichBundles } from "./api/bundles";
import { UnauthorizedError } from "./api/http";
import type { Operation, OperationPlan, OperationStep } from "./api/operations";

type MockResponse = {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
  headers: Headers;
};

type MockFetch = ReturnType<typeof vi.fn<(input: string, init?: RequestInit) => Promise<MockResponse>>>;

/** An enrichment answer with every count at zero: the body is what is under test. */
const emptyReport = {
  fetched: 0,
  cached: 0,
  refused: 0,
  unavailable: 0,
  unknown: 0,
  results: [],
};

/** The plan the server issues for an `enrich` request. Only `plan_id` is read by
 *  the adapter; the rest is what the server sends. */
const plan: OperationPlan = {
  plan_id: "plan-enrich.1",
  workflow: "enrich",
  target: "",
  steps: [{ index: 0, op: "enrich", summary: "Read the source page for 1 bundle" }],
  review: {
    workflow: "enrich",
    target: "",
    moves: 0,
    creates: 0,
    adopts: 0,
    deletes: 0,
    members: 0,
    groups: [],
    cover: null,
    excluded: [],
    existing_project_id: null,
    existing_project_name: null,
    move_list: [],
    projects: [],
  },
};

/** One `enrich` operation row; the step's `result` is the deleted route's body. */
function enrichRun(step: Partial<OperationStep>, overrides: Partial<Operation> = {}): Operation {
  return {
    id: "op-1",
    workflow: "enrich",
    target: "",
    status: "queued",
    submitted_by: "operator",
    created_at: "2026-11-26T00:00:00Z",
    started_at: null,
    finished_at: null,
    error: null,
    undo_state: "none",
    superseded_by: null,
    steps: [
      {
        index: 0,
        op: "enrich",
        status: "pending",
        result: null,
        error: null,
        started_at: null,
        finished_at: null,
        ...step,
      },
    ],
    ...overrides,
  };
}

/** A finished run whose step carries `result`: the shape a caller gets back. */
function doneRun(result: unknown): Operation {
  return enrichRun({ status: "done", result: result as Record<string, unknown> }, { status: "done" });
}

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

/** The three calls the adapter makes, each answered by the endpoint it names.
 *  The defaults describe the happy path: a plan, a submit that answers a QUEUED
 *  row (the 202 the server sends) and one read that finds it done. */
function installFetch(
  answers: { plan?: MockResponse; submit?: MockResponse; reads?: MockResponse[] } = {},
): MockFetch {
  let reads = 0;
  const rows = answers.reads ?? [respond(doneRun(emptyReport))];
  const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<MockResponse>>(
    async input => {
      const url = String(input);
      if (url.endsWith("/operations/plan")) return answers.plan ?? respond(plan);
      if (url.endsWith("/operations")) return answers.submit ?? respond(enrichRun({}));
      const next = rows[Math.min(reads, rows.length - 1)];
      reads += 1;
      return next;
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function bodyOf(call: [string, RequestInit?]): Record<string, unknown> {
  const init = call[1];
  if (typeof init?.body !== "string") throw new Error("expected a JSON request body");
  return JSON.parse(init.body) as Record<string, unknown>;
}

function urlOf(call: [string, RequestInit?]): string {
  // The origin is a runtime setting, so only the path is comparable across tests.
  return new URL(String(call[0]), "http://localhost").pathname;
}

function callsTo(fetchMock: MockFetch, suffix: string): [string, RequestInit?][] {
  return fetchMock.mock.calls.filter(call => urlOf(call) === suffix);
}

function planBodyOf(fetchMock: MockFetch): Record<string, unknown> {
  const [planCall] = callsTo(fetchMock, "/operations/plan");
  if (!planCall) throw new Error("the plan was never requested");
  return bodyOf(planCall);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("enrichBundles", () => {
  it("plans the enrich workflow with the keys and the refresh flag", async () => {
    const fetchMock = installFetch();

    const response = await enrichBundles(["Moai statue", "Widget"]);

    // The plan is the whole contract with the planner: the workflow name, an
    // EMPTY target (enrichment names its subjects in `params`, not in the
    // target), and the keys exactly as handed over.
    expect(planBodyOf(fetchMock)).toEqual({
      workflow: "enrich",
      target: "",
      params: { keys: ["Moai statue", "Widget"], refresh: false },
    });
    expect(response).toEqual(emptyReport);
  });

  it("submits the plan's id with one idempotency key", async () => {
    const fetchMock = installFetch();

    await enrichBundles(["Moai statue"]);

    const submit = bodyOf(callsTo(fetchMock, "/operations")[0]);
    expect(submit).toEqual({ plan_id: "plan-enrich.1", idempotency_key: expect.any(String) });
    expect(submit.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("sends the saved cookie trimmed in the plan body", async () => {
    const fetchMock = installFetch();

    await enrichBundles(["Moai statue"], { thingiverseCookie: " session=fake-cookie\n" });

    const params = planBodyOf(fetchMock).params as Record<string, unknown>;
    // The leading space and the trailing newline a copied Cookie header carries
    // are gone: `ImportCookiesCard` commits the paste untrimmed.
    expect(params.thingiverse_cookie).toBe("session=fake-cookie");
    // The JSON plan body is the cookie's only transport: a cookie in a URL would
    // land in access logs and browser history alike, and one in a header would
    // bypass the server's own resolution and the redaction of the row it stores.
    // Every request's URL is checked, and every header value with it. (`forEach`
    // rather than spread: this tsconfig has no `DOM.Iterable`, so `Headers` is
    // not iterable.)
    const headerValues: string[] = [];
    for (const [url, init] of fetchMock.mock.calls) {
      expect(urlOf([url, init])).not.toContain("session=fake-cookie");
      new Headers(init?.headers).forEach(value => headerValues.push(value));
    }
    expect(headerValues.some(value => value.includes("session=fake-cookie"))).toBe(false);
    expect(params.refresh).toBe(false);
  });

  it("omits the cookie entirely when none is saved", async () => {
    // Pinned at the wire body because `toHaveBeenCalledWith` uses `toEqual`,
    // where `""`, an absent key and `undefined` are indistinguishable - only the
    // serialised body can tell "sent an empty cookie" from "sent none".
    const cases: Array<{ option: { thingiverseCookie?: string } }> = [
      { option: { thingiverseCookie: "" } },
      { option: { thingiverseCookie: "   " } },
      { option: {} },
    ];

    for (const { option } of cases) {
      const fetchMock = installFetch();
      await enrichBundles(["Moai statue"], option);

      const params = planBodyOf(fetchMock).params as Record<string, unknown>;
      expect("thingiverse_cookie" in params).toBe(false);
      expect(params.refresh).toBe(false);
    }
  });

  it("asks for a refresh in the params, where the server reads it", async () => {
    const fetchMock = installFetch();

    await enrichBundles(["Moai statue"], { refresh: true });

    expect(planBodyOf(fetchMock).params).toEqual({ keys: ["Moai statue"], refresh: true });
  });

  it("returns a done operation's step result verbatim", async () => {
    const stepResult = {
      fetched: 1,
      cached: 1,
      refused: 0,
      unavailable: 0,
      unknown: 1,
      results: [
        {
          key: "Moai statue",
          status: "fetched",
          source_url: "https://www.thingiverse.com/thing:2493386",
          creator: "moai_maker",
          licence: "CC BY-NC",
          licence_url: "https://creativecommons.org/licenses/by-nc/4.0/",
          filled: ["creator", "licence"],
          fetched_at: "2026-11-26T00:00:00Z",
          reason: null,
        },
      ],
    };
    installFetch({ submit: respond(doneRun(stepResult)) });

    const response = await enrichBundles(["Moai statue"]);

    // Identity, not equality: the adapter hands the step's own report back
    // untouched, so nothing here can quietly re-count or re-order the server's
    // rows. `toEqual` would pass on a rebuilt body; `toBe` would not.
    expect(response).toBe(stepResult);
  });

  it("polls a queued operation until the runner leaves a terminal status", async () => {
    const running = enrichRun({ status: "running" }, { status: "running" });
    const fetchMock = installFetch({
      submit: respond(enrichRun({})),
      reads: [respond(running), respond(doneRun(emptyReport))],
    });

    const response = await enrichBundles(["Moai statue"]);

    expect(response).toEqual(emptyReport);
    // Queued is not terminal, so the submit's own row is read and re-read.
    expect(callsTo(fetchMock, "/operations/op-1")).toHaveLength(2);
  });

  it("surfaces a failed operation's sentence rather than an empty report", async () => {
    installFetch({
      submit: respond(
        enrichRun(
          { status: "failed", error: "the vendor page timed out" },
          { status: "failed", error: null },
        ),
      ),
    });

    // A VENDOR refusal is a `refused` row inside a done report, so it never
    // reaches this branch; a failed run produced no report at all, and it rejects
    // with the server's own sentence - exactly as the deleted route's non-2xx
    // answer did.
    await expect(enrichBundles(["Moai statue"])).rejects.toThrow("the vendor page timed out");
  });

  it("lets the server refuse a request over its key cap", async () => {
    const keys = Array.from({ length: 26 }, (_, index) => `bundle-${index}`);
    const refusal = JSON.stringify({ detail: "enrich at most 25 bundles at a time" });
    const fetchMock = installFetch({
      plan: respond({ detail: "enrich at most 25 bundles at a time" }, {
        ok: false,
        status: 400,
        body: refusal,
      }),
    });

    await expect(enrichBundles(keys)).rejects.toThrow("enrich at most 25 bundles at a time");

    // The cap is the SERVER's rule, so the client sends all 26 and lets the
    // refusal speak: a silent client-side truncation would drop keys the
    // operator selected.
    expect((planBodyOf(fetchMock).params as Record<string, unknown>).keys).toHaveLength(26);
    // Nothing is queued for a plan the server refused.
    expect(callsTo(fetchMock, "/operations")).toHaveLength(0);
  });

  it("turns a 401 on the plan into UnauthorizedError", async () => {
    installFetch({ plan: respond({ detail: "Unauthorized" }, { ok: false, status: 401 }) });

    await expect(enrichBundles(["Moai statue"])).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("turns a 401 on the poll into UnauthorizedError", async () => {
    installFetch({
      submit: respond(enrichRun({})),
      reads: [respond({ detail: "Unauthorized" }, { ok: false, status: 401 })],
    });

    await expect(enrichBundles(["Moai statue"])).rejects.toBeInstanceOf(UnauthorizedError);
  });
});
