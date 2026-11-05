// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { enrichBundles } from "./api/bundles";

type MockResponse = {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
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

function installFetch(): MockFetch {
  const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<MockResponse>>(async () => ({
    ok: true,
    status: 200,
    json: async () => emptyReport,
    headers: new Headers(),
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function bodyOf(init: RequestInit): Record<string, unknown> {
  if (typeof init.body !== "string") throw new Error("expected a JSON request body");
  return JSON.parse(init.body) as Record<string, unknown>;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("enrichBundles", () => {
  it("sends the saved cookie trimmed in the request body", async () => {
    const fetchMock = installFetch();

    await enrichBundles(["Moai statue"], { thingiverseCookie: " session=fake-cookie\n" });

    const [url, init] = fetchMock.mock.calls[0];
    const parsed = bodyOf(init ?? {});
    // The leading space and the trailing newline a copied Cookie header carries
    // are gone: `ImportCookiesCard` commits the paste untrimmed.
    expect(parsed.thingiverse_cookie).toBe("session=fake-cookie");
    // The JSON body is the cookie's only transport: a cookie in a URL would land
    // in access logs and browser history alike, and one in a header would bypass
    // the server's own resolution. `authHeaders` returns a `Headers`, which
    // stringifies to `{}`, so the values are read rather than serialised - and
    // every value is checked, not just the `cookie` name. (`forEach` rather than
    // spread: this tsconfig has no `DOM.Iterable`, so `Headers` is not iterable.)
    expect(String(url)).not.toContain("session=fake-cookie");
    const headerValues: string[] = [];
    new Headers(init?.headers).forEach(value => headerValues.push(value));
    expect(headerValues.some(value => value.includes("session=fake-cookie"))).toBe(false);
    expect(parsed.refresh).toBe(false);
  });

  it("omits the cookie entirely when none is saved", async () => {
    // Pinned at the fetch body because `toHaveBeenCalledWith` uses `toEqual`,
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

      const parsed = bodyOf(fetchMock.mock.calls[0][1] ?? {});
      expect("thingiverse_cookie" in parsed).toBe(false);
      expect(parsed.refresh).toBe(false);
    }
  });
});
