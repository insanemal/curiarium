// @vitest-environment jsdom
//
// Acceptance test for `Mf-8a4e`: the project surfaces that render a
// browser-loaded `fileUrl()` (`ProjectLanding`'s hero cover, gallery thumbs and
// gallery anchors, the member grid's card thumbs, `ProjectView`'s card covers
// and `ImagePickerDialog`'s insert thumbs) must keep their grants warm. ADR 0005
// decision 10: a grant lives 60 s, so a surface with no keeper goes blank a
// minute after mount.
//
// The grant machinery is the real thing — `mintFileGrants` from
// `../lib/api/grants` installs itself as the minter on import, exactly as it
// does in the app — and only the transport (`fetch`) is stubbed. Nothing here
// mocks `../lib/api` or `../lib/fileGrants`, so the production path
// (`useFileGrants` -> `primeGrants` -> `POST /file-grants` -> `fileUrl`) runs
// end to end.
//
// The `(a)` assertions — "a further POST /file-grants batch after the mount
// baseline contains this rel" — are the proof obligation: they are what dies
// on the pre-fix tree, where the element exists but nothing re-mints it.
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { type ComponentProps } from "react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Asset, Project } from "../lib/api";
import { mintFileGrants } from "../lib/api/grants";
import {
  configureGrantMinter,
  grantedFileUrl,
  resetFileGrants,
} from "../lib/fileGrants";

import ImagePickerDialog from "./project-editor/ImagePickerDialog";
import ProjectLanding from "./ProjectLanding";
import ProjectView from "./ProjectView";

/** The server's `FILE_GRANT_TTL_SECONDS`; a grant is dead after this long. */
const GRANT_TTL_SECONDS = 60;

/** Everything `POST /file-grants` was asked for, one entry per request. */
let mintRequests: string[][] = [];

/** Every minted URL gets a fresh `sig`, so a re-mint is distinguishable. */
let sigCounter = 0;

/** When true, `POST /file-grants` answers 500 and nothing is cached. */
let failMint = false;

/** The members `ProjectLanding`'s mount fetch returns for this test. */
let projectMembers: Asset[] = [];

/** The `sig` query value of a rendered URL, or null when it carries none. */
function sigOf(url: string | null | undefined): string | null {
  if (!url) return null;
  const match = /[?&]sig=([^&]+)/.exec(url);
  return match ? match[1] : null;
}

/** A `Response`-shaped object: a real one is awkward to build under fake timers. */
function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(headers),
    json: async () => body,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  };
}

/**
 * The stub answers the two endpoints these surfaces touch. `POST
 * /file-grants` records what was asked for; `GET /project/<id>/assets`
 * answers the landing's member fetch with the raw items array, because
 * `listProjectAssets` itself primes every member rel before first render.
 */
function installFetchStub(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      const path = url.split("?")[0];
      const method = (init?.method ?? "GET").toUpperCase();

      if (method === "POST" && path.endsWith("/file-grants")) {
        const body = JSON.parse(String(init?.body ?? "{}")) as { paths?: string[] };
        const paths = Array.isArray(body.paths) ? body.paths : [];
        mintRequests.push([...paths]);
        if (failMint) return jsonResponse({ detail: "boom" }, 500);
        return jsonResponse({
          grants: paths.map(rel => ({
            path: rel,
            url: `${rel}${rel.includes("?") ? "&" : "?"}sig=${sigCounter++}`,
            expires_at: Math.floor(Date.now() / 1000) + GRANT_TTL_SECONDS,
          })),
        });
      }
      if (method === "GET" && /\/project\/[^/]+\/assets$/.test(path)) {
        return jsonResponse(projectMembers, 200, { "X-Has-More": "false" });
      }
      // The project-scoped grid fetches its facets alongside the listing.
      if (method === "GET" && (path.endsWith("/tags") || path.endsWith("/creators"))) {
        return jsonResponse([], 200);
      }
      return jsonResponse({});
    }),
  );
}

/**
 * Drains the mint -> cache -> repaint chain. RTL's `waitFor`/`findBy*` poll on
 * timers and would hang under fake timers, so the tests flush microtasks
 * explicitly instead.
 */
async function flush(): Promise<void> {
  await act(async () => {
    for (let round = 0; round < 20; round += 1) {
      await Promise.resolve();
    }
  });
}

/** Advances the fake clock past the grant TTL, letting every armed timer fire. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await flush();
}

/** Every path any mint request asked for from `baseline` onwards, flattened. */
function mintsSince(baseline: number): string[] {
  return mintRequests.slice(baseline).flat();
}

function imgSrc(img: Element | null): string {
  if (!img) throw new Error("expected an <img>");
  return img.getAttribute("src") ?? "";
}

function requireImage(img: Element | null): HTMLImageElement {
  if (!(img instanceof HTMLImageElement)) throw new Error("expected an <img>");
  return img;
}

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: "p1",
    name: "Hydra",
    description: "A multi-part print",
    creator: "Ada",
    source_url: null,
    licence: null,
    licence_url: null,
    origin: "local",
    cover_asset_id: null,
    cover_thumb_url: null,
    asset_count: 4,
    metadata_json: {},
    total_size: 5120,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

function makeAsset(overrides: Partial<Asset> = {}): Asset {
  return {
    id: "a1",
    name: "Body",
    filename: "body.stl",
    mime: "model/stl",
    size: 2048,
    tags: [],
    url: "/file/a1/body.stl",
    thumb_url: "/thumb/a1.jpg",
    supporting_file_count: 0,
    ...overrides,
  };
}

/**
 * Two image members (one with no thumbnail, so the `thumb_url || url` fallback
 * is exercised) plus a model member and a file member whose row has a thumb and
 * a plain placeholder respectively.
 */
const MEMBERS: Asset[] = [
  makeAsset({
    id: "img1",
    name: "Photo one",
    filename: "photo-one.jpg",
    mime: "image/jpeg",
    size: 1024,
    url: "/file/img1/photo-one.jpg",
    thumb_url: "/thumb/img1.jpg",
  }),
  makeAsset({
    id: "img2",
    name: "Photo two",
    filename: "photo-two.png",
    mime: "image/png",
    size: 1024,
    url: "/file/img2/photo-two.png",
    thumb_url: null,
  }),
  makeAsset({
    id: "model1",
    name: "Body",
    filename: "body.stl",
    mime: "model/stl",
    size: 4096,
    url: "/file/model1/body.stl",
    thumb_url: "/thumb/model1.jpg",
  }),
  makeAsset({
    id: "doc1",
    name: "Notes",
    filename: "notes.txt",
    mime: "text/plain",
    size: 256,
    url: "/file/doc1/notes.txt",
    thumb_url: null,
  }),
];

const HERO_PROJECT = makeProject({
  id: "p1",
  name: "Hydra",
  cover_asset_id: "img1",
  cover_thumb_url: "/thumb/cover.jpg",
});

const HERO_COVER_REL = "/thumb/cover.jpg";

type LandingProps = ComponentProps<typeof ProjectLanding>;

function landingProps(overrides: Partial<LandingProps> = {}): LandingProps {
  return {
    canContribute: true,
    canDelete: true,
    project: HERO_PROJECT,
    assetsVersion: 0,
    onProjectUpdated: vi.fn(),
    onProjectDeleted: vi.fn(),
    onAssetsChanged: vi.fn(),
    theme: "dark",
    ...overrides,
  };
}

function heroCover(): HTMLImageElement {
  return screen.getByRole("img", { name: "Hydra cover" }) as HTMLImageElement;
}

function galleryImage(name: string): HTMLImageElement {
  return screen.getByRole("img", { name }) as HTMLImageElement;
}

function galleryHref(name: string): string {
  const anchor = galleryImage(name).closest("a");
  if (!anchor) throw new Error(`expected the gallery anchor around "${name}"`);
  return anchor.getAttribute("href") ?? "";
}

/** A project grid card's thumbnail, found by the card's asset id. */
function gridCardThumb(assetId: string): HTMLImageElement {
  return requireImage(document.querySelector(`[data-asset-id="${assetId}"] img`));
}

const COVER_PROJECTS: Project[] = [
  makeProject({ id: "p1", name: "Hydra", cover_thumb_url: "/thumb/p1.jpg" }),
  makeProject({ id: "p2", name: "Solo", cover_thumb_url: "/thumb/p2.jpg" }),
  makeProject({ id: "p3", name: "Lamp", cover_thumb_url: "/thumb/p3.jpg" }),
];

const COVER_RELS = ["/thumb/p1.jpg", "/thumb/p2.jpg", "/thumb/p3.jpg"];

type ViewProps = ComponentProps<typeof ProjectView>;

function viewProps(overrides: Partial<ViewProps> = {}): ViewProps {
  return {
    canContribute: true,
    canDelete: true,
    projects: COVER_PROJECTS,
    selectedProjectId: null,
    onOpenProject: vi.fn(),
    onCloseProject: vi.fn(),
    onProjectCreated: vi.fn(),
    onProjectUpdated: vi.fn(),
    onProjectDeleted: vi.fn(),
    onAssetsChanged: vi.fn(),
    assetsVersion: 0,
    theme: "dark",
    ...overrides,
  };
}

function cardImage(projectName: string): HTMLImageElement {
  const card = screen.getByRole("button", { name: `Open project ${projectName}` });
  return requireImage(card.querySelector("img"));
}


const PICKER_IMAGES: Asset[] = [
  makeAsset({
    id: "pick1",
    name: "Insert me",
    filename: "insert-me.jpg",
    mime: "image/jpeg",
    size: 1024,
    url: "/file/pick1/insert-me.jpg",
    thumb_url: "/thumb/pick1.jpg",
  }),
  makeAsset({
    id: "pick2",
    name: "No thumb",
    filename: "no-thumb.png",
    mime: "image/png",
    size: 1024,
    url: "/file/pick2/no-thumb.png",
    thumb_url: null,
  }),
];

beforeEach(() => {
  vi.useFakeTimers();
  mintRequests = [];
  sigCounter = 0;
  failMint = false;
  projectMembers = MEMBERS;
  resetFileGrants();
  configureGrantMinter(mintFileGrants);
  installFetchStub();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

afterAll(() => {
  configureGrantMinter(null);
  resetFileGrants();
});

describe("project file grants", () => {
  it("the ProjectLanding hero cover re-mints past the TTL and paints a fresh sig on the next render", async () => {
    // The cover rel is not in the member list, so the mount fetch cannot prime
    // it: only the surface's own keeper can. That makes it the cleanest proof
    // that a keeper exists.
    const props = landingProps();
    const view = render(<ProjectLanding {...props} />);
    await flush();

    const baseline = mintRequests.length;
    const mountSrc = imgSrc(heroCover());
    const mountSig = sigOf(mountSrc);

    await advance(61_000);

    expect(
      mintsSince(baseline),
      "ProjectLanding hero cover (Mf-8a4e): no POST /file-grants after the mount baseline carried /thumb/cover.jpg, so the hero still calls fileUrl() with no keeper and goes blank after 60 s",
    ).toContain(HERO_COVER_REL);
    expect(grantedFileUrl(HERO_COVER_REL)).not.toBeNull();

    // Silent variant: the mounted <img> keeps the src it painted with.
    expect(imgSrc(heroCover())).toBe(mountSrc);

    // (d) a parent re-render reads the warm cache and paints the new grant.
    view.rerender(<ProjectLanding {...props} />);
    await flush();
    const rerenderedSig = sigOf(imgSrc(heroCover()));
    expect(
      rerenderedSig,
      "ProjectLanding hero cover (Mf-8a4e): a re-render after the renewal must yield a signed src",
    ).not.toBeNull();
    expect(rerenderedSig).not.toBe(mountSig);
  });

  it("the gallery thumbnail re-mints past the TTL and keeps its silent src byte-identical while the anchor repaints", async () => {
    render(<ProjectLanding {...landingProps()} />);
    await flush();

    const baseline = mintRequests.length;
    const mountSrc = imgSrc(galleryImage("Photo one"));
    const mountHrefSig = sigOf(galleryHref("Photo one"));

    await advance(61_000);

    expect(
      mintsSince(baseline),
      "ProjectLanding gallery <img> (Mf-8a4e): no POST /file-grants after the mount baseline carried /thumb/img1.jpg, so the gallery thumbnail goes blank after 60 s",
    ).toContain("/thumb/img1.jpg");
    expect(grantedFileUrl("/thumb/img1.jpg")).not.toBeNull();

    // The <img> is silent: same element, byte-identical src. Its live sibling
    // anchor is what repaints, because it holds the URL string itself.
    expect(imgSrc(galleryImage("Photo one"))).toBe(mountSrc);
    const liveHrefSig = sigOf(galleryHref("Photo one"));
    expect(liveHrefSig).not.toBeNull();
    expect(liveHrefSig).not.toBe(mountHrefSig);
  });

  it("the gallery anchor's href re-mints past the TTL and holds a live sig different from its mount sig", async () => {
    const props = landingProps();
    const view = render(<ProjectLanding {...props} />);
    await flush();

    const baseline = mintRequests.length;
    const mountHrefSig = sigOf(galleryHref("Photo one"));

    await advance(61_000);

    expect(
      mintsSince(baseline),
      "ProjectLanding gallery <a href> (Mf-8a4e): no POST /file-grants after the mount baseline carried /file/img1/photo-one.jpg, so the link would 401 after 60 s",
    ).toContain("/file/img1/photo-one.jpg");
    expect(grantedFileUrl("/file/img1/photo-one.jpg")).not.toBeNull();

    const liveSig = sigOf(galleryHref("Photo one"));
    expect(liveSig).not.toBeNull();
    expect(liveSig).not.toBe(mountHrefSig);

    // (d) a re-render keeps a signed href, still different from the mount one.
    view.rerender(<ProjectLanding {...props} />);
    await flush();
    const rerenderedSig = sigOf(galleryHref("Photo one"));
    expect(rerenderedSig).not.toBeNull();
    expect(rerenderedSig).not.toBe(mountHrefSig);
  });

  it("an image with no thumb_url falls back to asset.url for both the keeper and the <img src>", async () => {
    render(<ProjectLanding {...landingProps()} />);
    await flush();

    const baseline = mintRequests.length;
    const img = galleryImage("Photo two");
    const mountSrc = imgSrc(img);
    // The same `thumb_url || url` expression has to feed the hook and the
    // <img>, or the keeper never learns this rel and the fallback goes stale.
    expect(mountSrc).toContain("/file/img2/photo-two.png");

    await advance(61_000);

    expect(
      mintsSince(baseline),
      "ProjectLanding thumb_url:null gallery image (Mf-8a4e): the keeper must use the same `thumb_url || url` fallback as the <img src>, so /file/img2/photo-two.png is re-minted",
    ).toContain("/file/img2/photo-two.png");
    expect(grantedFileUrl("/file/img2/photo-two.png")).not.toBeNull();
    expect(sigOf(mountSrc)).not.toBeNull();
    expect(imgSrc(galleryImage("Photo two"))).toBe(mountSrc);
  });

  it("a project grid card re-mints its thumbnail past the TTL and keeps its silent src", async () => {
    render(<ProjectLanding {...landingProps()} />);
    await flush();

    const baseline = mintRequests.length;
    const mountSrc = imgSrc(gridCardThumb("model1"));

    await advance(61_000);

    expect(
      mintsSince(baseline),
      "ProjectLanding grid card thumb (Mf-8a4e): no POST /file-grants after the mount baseline carried /thumb/model1.jpg, so the grid card goes blank after 60 s",
    ).toContain("/thumb/model1.jpg");
    expect(grantedFileUrl("/thumb/model1.jpg")).not.toBeNull();
    expect(imgSrc(gridCardThumb("model1"))).toBe(mountSrc);

    // (d) a card re-render paints the refreshed grant. AssetCard is memoised,
    // so the honest trigger is the card's own interaction, not a parent pass.
    const card = screen.getByText("Body").closest("[data-asset-id]");
    if (!card) throw new Error("expected the model card");
    fireEvent.click(within(card as HTMLElement).getByRole("checkbox", { name: "Select" }));
    await flush();
    const rerenderedSig = sigOf(imgSrc(gridCardThumb("model1")));
    expect(rerenderedSig).not.toBeNull();
    expect(rerenderedSig).not.toBe(sigOf(mountSrc));
  });

  it("the ProjectView card grid renews every cover in exactly one batch past the TTL", async () => {
    const props = viewProps();
    const view = render(<ProjectView {...props} />);
    await flush();

    const baseline = mintRequests.length;
    const mountSrc = imgSrc(cardImage("Hydra"));

    await advance(61_000);

    const renewal = mintRequests.slice(baseline);
    expect(
      renewal.length,
      "ProjectView card grid (Mf-8a4e): one list-level keeper must renew all three covers in exactly one POST /file-grants batch; none was sent",
    ).toBe(1);
    expect([...renewal[0]].sort()).toEqual([...COVER_RELS].sort());
    for (const rel of COVER_RELS) {
      expect(grantedFileUrl(rel)).not.toBeNull();
    }

    // Silent: the mounted card keeps its src.
    expect(imgSrc(cardImage("Hydra"))).toBe(mountSrc);

    // (d) a re-render paints the refreshed grant.
    view.rerender(<ProjectView {...props} />);
    await flush();
    const rerenderedSig = sigOf(imgSrc(cardImage("Hydra")));
    expect(rerenderedSig).not.toBeNull();
    expect(rerenderedSig).not.toBe(sigOf(mountSrc));
  });

  it("mints no other project's cover while a project landing is open", async () => {
    render(<ProjectView {...viewProps({ selectedProjectId: "p1" })} />);
    await flush();

    expect(screen.getByRole("heading", { name: "Hydra" })).toBeTruthy();
    const minted = mintRequests.flat();
    expect(minted).not.toContain("/thumb/p2.jpg");
    expect(minted).not.toContain("/thumb/p3.jpg");
  });

  it("the ImagePickerDialog renews every thumb, including a thumb_url:null image that falls back to asset.url", async () => {
    const view = render(
      <ImagePickerDialog images={PICKER_IMAGES} onInsert={vi.fn()} onClose={vi.fn()} />,
    );
    await flush();

    const baseline = mintRequests.length;
    const mountSrcs = Array.from(view.container.querySelectorAll("img")).map(imgSrc);
    expect(mountSrcs).toHaveLength(2);

    await advance(61_000);

    const renewal = mintsSince(baseline);
    expect(
      renewal,
      "ImagePickerDialog (Mf-8a4e): no POST /file-grants after the mount baseline carried /thumb/pick1.jpg, so the insert thumb goes blank after 60 s",
    ).toContain("/thumb/pick1.jpg");
    expect(
      renewal,
      "ImagePickerDialog thumb_url:null image (Mf-8a4e): the keeper must use the same `thumb_url || url` fallback as the <img src>, so /file/pick2/no-thumb.png is re-minted",
    ).toContain("/file/pick2/no-thumb.png");
    expect(grantedFileUrl("/thumb/pick1.jpg")).not.toBeNull();
    expect(grantedFileUrl("/file/pick2/no-thumb.png")).not.toBeNull();

    expect(Array.from(view.container.querySelectorAll("img")).map(imgSrc)).toEqual(mountSrcs);

    // (d) a re-render paints the refreshed grant.
    view.rerender(
      <ImagePickerDialog images={PICKER_IMAGES} onInsert={vi.fn()} onClose={vi.fn()} />,
    );
    await flush();
    const rerenderedSig = sigOf(imgSrc(view.container.querySelector("img")));
    expect(rerenderedSig).not.toBeNull();
    expect(rerenderedSig).not.toBe(sigOf(mountSrcs[0]));
  });

  it("a failed mint leaves the ProjectView card grid with the plain unsigned URL, no gate and no throw, and a remount signs it", async () => {
    failMint = true;
    const props = viewProps();
    const first = render(<ProjectView {...props} />);
    await flush();

    const unsignedSrc = imgSrc(cardImage("Hydra"));
    expect(unsignedSrc).toContain("/thumb/p1.jpg");
    expect(sigOf(unsignedSrc)).toBeNull();
    // ADR 0005 decision 10: a failed first mint leaves the plain URL; only the
    // two preview dialogs gate. Nothing may replace a card with a placeholder.
    expect(screen.queryByText(/Preparing/i)).toBeNull();
    expect(screen.getByRole("button", { name: "Open project Hydra" })).toBeTruthy();

    failMint = false;
    first.unmount();
    render(<ProjectView {...props} />);
    await flush();

    expect(
      sigOf(imgSrc(cardImage("Hydra"))),
      "ProjectView card grid (Mf-8a4e): after the mint recovers, a remount must paint a signed cover",
    ).not.toBeNull();
  });

  it("a failed mint leaves the ProjectLanding hero with the plain unsigned URL, no gate and no throw, and a remount signs it", async () => {
    failMint = true;
    const props = landingProps();
    const first = render(<ProjectLanding {...props} />);
    await flush();

    const unsignedSrc = imgSrc(heroCover());
    expect(unsignedSrc).toContain(HERO_COVER_REL);
    expect(sigOf(unsignedSrc)).toBeNull();
    expect(screen.queryByText(/Preparing/i)).toBeNull();

    failMint = false;
    first.unmount();
    render(<ProjectLanding {...props} />);
    await flush();

    expect(
      sigOf(imgSrc(heroCover())),
      "ProjectLanding hero cover (Mf-8a4e): after the mint recovers, a remount must paint a signed cover",
    ).not.toBeNull();
  });
});
