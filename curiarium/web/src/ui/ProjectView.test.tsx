// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createRef,
  useCallback,
  useState,
  type ComponentProps,
  type ReactElement,
  type RefObject,
} from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import type { Project } from "../lib/api";

/** The props the landing page is contractually handed by `ProjectView`. */
type LandingProps = {
  canContribute: boolean;
  canDelete: boolean;
  project: Project;
  assetsVersion: number;
  onProjectUpdated: (project: Project) => void;
  onProjectDeleted: (id: string) => void;
  onAssetsChanged: () => void;
  onUnauthorized?: () => void;
  scrollElementRef?: RefObject<HTMLElement | null>;
};

vi.mock("../lib/api", () => ({
  fileUrl: (rel: string) => rel,
  createProject: vi.fn(),
  UnauthorizedError: class UnauthorizedError extends Error {},
}));

// The landing page is its own component: stub it to a marker plus the props it
// received, so the tests can prove `ProjectView` forwarded every callback.
// `withDialog` lets a test nest a modal, the way a delete ConfirmDialog does in
// the real page.
const landingState = vi.hoisted(() => ({
  withDialog: false,
  received: null as LandingProps | null,
}));

vi.mock("./ProjectLanding", () => ({
  default: (props: LandingProps) => {
    landingState.received = props;
    return (
      <div data-testid="project-landing">
        landing:{props.project.id}
        {landingState.withDialog && (
          <div role="dialog" aria-label="Delete file?">
            <button type="button">Delete</button>
          </div>
        )}
      </div>
    );
  },
}));

import ProjectView from "./ProjectView";
import { createProject, UnauthorizedError } from "../lib/api";
import { getToasts, resetToasts } from "../lib/toast";

const createProjectMock = vi.mocked(createProject);

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
    asset_count: 3,
    metadata_json: {},
    total_size: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

const projects: Project[] = [
  makeProject({ id: "p1", name: "Hydra" }),
  makeProject({ id: "p2", name: "Solo", description: null, creator: null, asset_count: 1 }),
];

type Props = ComponentProps<typeof ProjectView>;

function viewProps(overrides: Partial<Props> = {}): Props {
  return {
    canContribute: true,
    canDelete: true,
    projects,
    selectedProjectId: null,
    onOpenProject: vi.fn(),
    onCloseProject: vi.fn(),
    onProjectCreated: vi.fn(),
    onProjectUpdated: vi.fn(),
    onProjectDeleted: vi.fn(),
    onAssetsChanged: vi.fn(),
    assetsVersion: 0,
    theme: "dark",
    scrollElementRef: createRef<HTMLElement>(),
    ...overrides,
  };
}

/**
 * `ProjectView` under an owner that really mutates its list, so a create,
 * delete or vanished id re-renders the way `App` does. A bare render with a
 * spy cannot prove the notice or the focus: nothing would change on screen.
 */
function Harness({
  initial,
  openId = null,
  canContribute = true,
  onCloseProject,
  onProjectDeleted,
}: {
  initial: Project[];
  openId?: string | null;
  canContribute?: boolean;
  /** Parent-side observers, the way `App`'s own handlers would be spied on. */
  onCloseProject?: () => void;
  onProjectDeleted?: (id: string) => void;
}): ReactElement {
  const [rows, setRows] = useState<Project[]>(initial);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(openId);

  const close = useCallback(() => {
    onCloseProject?.();
    setSelectedProjectId(null);
  }, [onCloseProject]);

  // Delete closes in the same update that drops the row, exactly like
  // `App.handleProjectDeleted`: the landing that held focus is gone either way.
  const remove = useCallback(
    (id: string) => {
      onProjectDeleted?.(id);
      setRows(list => list.filter(project => project.id !== id));
      setSelectedProjectId(null);
    },
    [onProjectDeleted],
  );

  const append = useCallback((project: Project) => {
    setRows(list => [...list, project]);
  }, []);

  return (
    <ProjectView
      canContribute={canContribute}
      canDelete
      projects={rows}
      selectedProjectId={selectedProjectId}
      onOpenProject={setSelectedProjectId}
      onCloseProject={close}
      onProjectCreated={append}
      onProjectUpdated={() => undefined}
      onProjectDeleted={remove}
      onAssetsChanged={() => undefined}
      assetsVersion={0}
      theme="dark"
    />
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  resetToasts();
});

afterEach(() => {
  landingState.withDialog = false;
  landingState.received = null;
  cleanup();
});

describe("ProjectView", () => {
  it("forwards the scroll container ref to the landing grid", () => {
    const scrollElementRef = createRef<HTMLElement>();
    render(
      <ProjectView
        {...viewProps({ selectedProjectId: "p1", scrollElementRef })}
      />,
    );

    expect(landingState.received?.scrollElementRef).toBe(scrollElementRef);
  });

  it("shows a loading line while the project list is null", () => {
    render(<ProjectView {...viewProps({ projects: null })} />);
    expect(screen.getByText("Loading projects…")).toBeTruthy();
  });

  it("shows neutral empty copy when there are no projects", () => {
    render(<ProjectView {...viewProps({ projects: [] })} />);

    expect(screen.getByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(screen.getByText("No projects yet.")).toBeTruthy();
    expect(
      screen.getByText("Projects are collections of files from any folder."),
    ).toBeTruthy();
  });

  it("renders project cards and opens one on click", () => {
    const onOpenProject = vi.fn();
    render(<ProjectView {...viewProps({ onOpenProject })} />);

    expect(screen.getByText("Hydra")).toBeTruthy();
    expect(screen.getByText("A multi-part print")).toBeTruthy();
    expect(screen.getByText("3 files")).toBeTruthy();
    expect(screen.getByText("By Ada")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open project Solo" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Open project Hydra" }));
    expect(onOpenProject).toHaveBeenCalledWith("p1");
  });

  it("shows each card's licence, including the explicit unknown state", () => {
    render(
      <ProjectView
        {...viewProps({
          projects: [
            makeProject({ id: "p1", name: "Hydra", licence: "CC-BY-SA" }),
            makeProject({ id: "p2", name: "Solo", creator: null }),
          ],
        })}
      />,
    );

    const licensed = screen.getByRole("button", { name: "Open project Hydra" });
    expect(within(licensed).getByText("CC-BY-SA")).toBeTruthy();
    expect(within(licensed).queryByText("Licence unknown")).toBeNull();

    const unlicensed = screen.getByRole("button", { name: "Open project Solo" });
    expect(within(unlicensed).getByText("Licence unknown")).toBeTruthy();
    expect(within(unlicensed).queryByText("CC-BY-SA")).toBeNull();
  });

  it("shows the source host on a card without nesting an anchor in the button", () => {
    render(
      <ProjectView
        {...viewProps({
          projects: [
            makeProject({
              id: "p1",
              name: "Hydra",
              source_url: "https://www.printables.com/model/123-hydra",
            }),
          ],
        })}
      />,
    );

    const card = screen.getByRole("button", { name: "Open project Hydra" });
    const chip = card.querySelector("[data-source-chip]");
    expect(chip?.textContent).toBe("Printables");
    expect(chip?.getAttribute("title")).toBe("https://www.printables.com/model/123-hydra");
    // The card must stay a single interactive element.
    expect(within(card).queryByRole("link")).toBeNull();
    expect(card.querySelector("a")).toBeNull();
  });

  it("renders the landing page for the open project", () => {
    render(
      <ProjectView {...viewProps({ selectedProjectId: "p1", assetsVersion: 7 })} />,
    );

    expect(screen.getByTestId("project-landing").textContent).toBe("landing:p1");
    expect(landingState.received?.project.id).toBe("p1");
    expect(landingState.received?.assetsVersion).toBe(7);
    expect(screen.queryByRole("heading", { name: "Projects" })).toBeNull();
  });

  it("forwards the landing page's callbacks to its own props", () => {
    const onProjectUpdated = vi.fn();
    const onProjectDeleted = vi.fn();
    const onAssetsChanged = vi.fn();
    const onUnauthorized = vi.fn();
    render(
      <ProjectView
        {...viewProps({
          selectedProjectId: "p1",
          onProjectUpdated,
          onProjectDeleted,
          onAssetsChanged,
          onUnauthorized,
        })}
      />,
    );

    const landing = landingState.received;
    if (!landing) throw new Error("landing page did not render");

    const renamed = makeProject({ name: "Hydra v2" });
    landing.onProjectUpdated(renamed);
    expect(onProjectUpdated).toHaveBeenCalledWith(renamed);

    landing.onProjectDeleted("p1");
    expect(onProjectDeleted).toHaveBeenCalledWith("p1");

    landing.onAssetsChanged();
    expect(onAssetsChanged).toHaveBeenCalledTimes(1);

    expect(landing.onUnauthorized).toBe(onUnauthorized);
  });

  it("forwards the contribute and delete capabilities to the landing page for each role", () => {
    const { rerender } = render(
      <ProjectView {...viewProps({ selectedProjectId: "p1" })} />,
    );
    expect(landingState.received?.canContribute).toBe(true);
    expect(landingState.received?.canDelete).toBe(true);

    rerender(
      <ProjectView
        {...viewProps({ canContribute: false, canDelete: false, selectedProjectId: "p1" })}
      />,
    );
    expect(landingState.received?.canContribute).toBe(false);
    expect(landingState.received?.canDelete).toBe(false);
  });

  it("closes via the Back button", () => {
    const onCloseProject = vi.fn();
    render(
      <ProjectView {...viewProps({ selectedProjectId: "p1", onCloseProject })} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Back to projects" }));
    expect(onCloseProject).toHaveBeenCalledTimes(1);
  });

  it("closes via Escape", () => {
    const onCloseProject = vi.fn();
    render(
      <ProjectView {...viewProps({ selectedProjectId: "p1", onCloseProject })} />,
    );

    fireEvent.keyDown(screen.getByRole("button", { name: "Back to projects" }), {
      key: "Escape",
    });
    expect(onCloseProject).toHaveBeenCalledTimes(1);
  });

  it("ignores Escape while the project list is showing", () => {
    const onCloseProject = vi.fn();
    render(<ProjectView {...viewProps({ onCloseProject })} />);

    fireEvent.keyDown(screen.getByRole("heading", { name: "Projects" }), { key: "Escape" });
    expect(onCloseProject).not.toHaveBeenCalled();
  });

  it("ignores Escape while a dialog inside the project is open", () => {
    const onCloseProject = vi.fn();
    landingState.withDialog = true;
    render(
      <ProjectView {...viewProps({ selectedProjectId: "p1", onCloseProject })} />,
    );

    fireEvent.keyDown(screen.getByRole("button", { name: "Delete" }), { key: "Escape" });

    expect(onCloseProject).not.toHaveBeenCalled();
  });

  it("ignores Escape a child already handled", () => {
    const onCloseProject = vi.fn();
    render(
      <ProjectView {...viewProps({ selectedProjectId: "p1", onCloseProject })} />,
    );

    const back = screen.getByRole("button", { name: "Back to projects" });
    // A nested handler that consumed Escape prevents the default, which is how
    // a child (e.g. a closing popover) signals it owned the key.
    const handler = (event: KeyboardEvent) => event.preventDefault();
    back.addEventListener("keydown", handler);
    fireEvent.keyDown(back, { key: "Escape" });

    expect(onCloseProject).not.toHaveBeenCalled();
  });

  it("moves focus to Back on open and back to the originating card on close", () => {
    const { rerender } = render(<ProjectView {...viewProps()} />);
    const card = screen.getByRole("button", { name: "Open project Hydra" });
    card.focus();
    expect(document.activeElement).toBe(card);

    rerender(<ProjectView {...viewProps({ selectedProjectId: "p1" })} />);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Back to projects" }),
    );

    rerender(<ProjectView {...viewProps({ selectedProjectId: null })} />);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Open project Hydra" }),
    );
  });

  it("Escape also returns focus to the originating card", () => {
    function Harness() {
      const [selected, setSelected] = useState<string | null>("p1");
      return (
        <ProjectView
          {...viewProps({
            selectedProjectId: selected,
            onCloseProject: () => setSelected(null),
          })}
        />
      );
    }

    render(<Harness />);
    const back = screen.getByRole("button", { name: "Back to projects" });
    expect(document.activeElement).toBe(back);

    fireEvent.keyDown(back, { key: "Escape" });

    expect(
      screen.getByRole("button", { name: "Open project Hydra" }),
    ).toBe(document.activeElement);
  });

  it("filters the cards by name as the operator types", () => {
    render(<ProjectView {...viewProps()} />);

    fireEvent.change(screen.getByRole("searchbox", { name: "Search projects" }), {
      target: { value: "solo" },
    });

    expect(screen.getByRole("button", { name: "Open project Solo" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open project Hydra" })).toBeNull();
    // The count must speak about the list as well as the filter: "1 project"
    // alone would leave the operator unsure whether a search removed Hydra.
    expect(screen.getByText("1 of 2 projects")).toBeTruthy();
  });

  it("matches a description or a creator, case-insensitively", () => {
    render(<ProjectView {...viewProps()} />);
    const search = screen.getByRole("searchbox", { name: "Search projects" });

    // Description only: Hydra's description is "A multi-part print".
    fireEvent.change(search, { target: { value: "multi-part" } });
    expect(screen.getByRole("button", { name: "Open project Hydra" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open project Solo" })).toBeNull();

    // Creator only: Hydra was made by "Ada", and the stored casing differs.
    fireEvent.change(search, { target: { value: "ada" } });
    expect(screen.getByRole("button", { name: "Open project Hydra" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open project Solo" })).toBeNull();
  });

  it("restores the whole list from the clear button", () => {
    render(<ProjectView {...viewProps()} />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search projects" }), {
      target: { value: "solo" },
    });
    expect(screen.queryByRole("button", { name: "Open project Hydra" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Clear project search" }));

    expect(screen.getByRole("button", { name: "Open project Hydra" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open project Solo" })).toBeTruthy();
    expect(screen.getByText("2 projects")).toBeTruthy();
    // An empty list under a full search box would contradict itself.
    expect(
      (screen.getByRole("searchbox", { name: "Search projects" }) as HTMLInputElement).value,
    ).toBe("");
  });

  it("says a search matched nothing instead of saying the vault is empty", () => {
    render(<ProjectView {...viewProps()} />);

    fireEvent.change(screen.getByRole("searchbox", { name: "Search projects" }), {
      target: { value: "zzz" },
    });

    expect(screen.getByText("No projects match “zzz”.")).toBeTruthy();
    // "No projects yet." would tell an operator with two projects that their
    // vault is empty.
    expect(screen.queryByText("No projects yet.")).toBeNull();
  });

  it("re-filters the current list when the list itself changes under a live query", () => {
    const { rerender } = render(<ProjectView {...viewProps()} />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search projects" }), {
      target: { value: "solo" },
    });
    expect(screen.getByText("1 of 2 projects")).toBeTruthy();

    // The filter is derived on every render, not a snapshot taken when the
    // query was typed: a row a refresh added must be filtered too.
    rerender(
      <ProjectView
        {...viewProps({
          projects: [
            ...projects,
            makeProject({ id: "p3", name: "Solo two", description: null, creator: null }),
          ],
        })}
      />,
    );

    expect(screen.getByText("2 of 3 projects")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open project Solo two" })).toBeTruthy();
  });

  it("hides the create entry from a role that cannot contribute, without losing search or open", () => {
    const onOpenProject = vi.fn();
    render(<ProjectView {...viewProps({ canContribute: false, onOpenProject })} />);

    // The gate is the point: offering a control the server would reject makes
    // the role look broken instead of restricted.
    expect(screen.queryByRole("button", { name: "New project" })).toBeNull();
    expect(screen.queryByRole("form", { name: "New project" })).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search projects" }), {
      target: { value: "solo" },
    });
    expect(screen.queryByRole("button", { name: "Open project Hydra" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Open project Solo" }));
    expect(onOpenProject).toHaveBeenCalledWith("p2");
  });

  it("opens the create form on the name field and holds the submit until a name is typed", () => {
    render(<ProjectView {...viewProps()} />);

    fireEvent.click(screen.getByRole("button", { name: "New project" }));

    expect(screen.getByRole("form", { name: "New project" })).toBeTruthy();
    const name = screen.getByRole("textbox", { name: "Project name" });
    // The form appears where the button was, so focus has to follow it in.
    expect(document.activeElement).toBe(name);

    const submit = () =>
      screen.getByRole("button", { name: "Create project" }) as HTMLButtonElement;
    expect(submit().disabled).toBe(true);

    // Whitespace is not a name: the request would be rejected server-side.
    fireEvent.change(name, { target: { value: "   " } });
    expect(submit().disabled).toBe(true);

    fireEvent.change(name, { target: { value: "Fresh" } });
    expect(submit().disabled).toBe(false);
  });

  it("hands a created project to its parent, closes the form and focuses the new card", async () => {
    createProjectMock.mockResolvedValueOnce(
      makeProject({ id: "p3", name: "Fresh", description: null, creator: null }),
    );

    render(<Harness initial={projects} />);
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), {
      target: { value: "Fresh" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    // The card can only exist if the row reached the parent's list: this view
    // never keeps a list of its own.
    const fresh = await screen.findByRole("button", { name: "Open project Fresh" });
    expect(createProjectMock).toHaveBeenCalledTimes(1);
    expect(createProjectMock).toHaveBeenCalledWith({ name: "Fresh" });
    expect(screen.queryByRole("form", { name: "New project" })).toBeNull();
    expect(
      (screen.getByRole("searchbox", { name: "Search projects" }) as HTMLInputElement).value,
    ).toBe("");
    // Landing on the card just made, not back at the top of the pane.
    await waitFor(() => expect(document.activeElement).toBe(fresh));
  });

  it("clears an active search so a created card is actually painted", async () => {
    createProjectMock.mockResolvedValueOnce(
      makeProject({ id: "p3", name: "Fresh", description: null, creator: null }),
    );

    render(<Harness initial={projects} />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search projects" }), {
      target: { value: "solo" },
    });
    expect(screen.queryByRole("button", { name: "Open project Hydra" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), {
      target: { value: "Fresh" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    // "Fresh" does not match "solo": without the reset the new card would be
    // filtered out and the focus target would not exist at all.
    expect(await screen.findByRole("button", { name: "Open project Fresh" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open project Hydra" })).toBeTruthy();
  });

  it("treats a 401 from create as a session expiry, not as a form error", async () => {
    const onUnauthorized = vi.fn();
    const onProjectCreated = vi.fn();
    const toastsBefore = getToasts().length;
    createProjectMock.mockRejectedValueOnce(new UnauthorizedError());

    render(<ProjectView {...viewProps({ onUnauthorized, onProjectCreated })} />);
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), {
      target: { value: "Fresh" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(onProjectCreated).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Open project Fresh" })).toBeNull();
    // An expired session is not the operator's mistake: a "could not create"
    // toast here would blame them for a re-login.
    expect(getToasts()).toHaveLength(toastsBefore);
  });

  it("keeps the typed name and the open form when a create fails for another reason", async () => {
    createProjectMock.mockRejectedValueOnce(new Error("Project creation failed"));

    render(<ProjectView {...viewProps()} />);
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), {
      target: { value: "Fresh" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(getToasts()).toHaveLength(1));
    expect(getToasts()[0].tone).toBe("error");
    // The server's sentence, not a rewritten one: it names the actual cause.
    expect(getToasts()[0].message).toBe("Project creation failed");
    expect(screen.getByRole("form", { name: "New project" })).toBeTruthy();
    expect(
      (screen.getByRole("textbox", { name: "Project name" }) as HTMLInputElement).value,
    ).toBe("Fresh");
  });

  it("cancels the create form on Escape without sending a request", () => {
    render(<ProjectView {...viewProps()} />);
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), {
      target: { value: "Fresh" },
    });

    fireEvent.keyDown(screen.getByRole("form", { name: "New project" }), { key: "Escape" });

    expect(screen.queryByRole("form", { name: "New project" })).toBeNull();
    expect(createProjectMock).not.toHaveBeenCalled();

    // A discarded draft must not reappear: the next create starts clean.
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    expect(
      (screen.getByRole("textbox", { name: "Project name" }) as HTMLInputElement).value,
    ).toBe("");
  });

  it("names the open project in a breadcrumb above the Back control", () => {
    render(<ProjectView {...viewProps({ selectedProjectId: "p1" })} />);

    const breadcrumb = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(breadcrumb).getByText("Projects")).toBeTruthy();
    // The separator is decoration only, but it still has to be in the trail or
    // the two crumbs read as one phrase.
    expect(within(breadcrumb).getByText("/").getAttribute("aria-hidden")).toBe("true");
    // `aria-current` is what tells a screen reader this crumb is the page.
    expect(within(breadcrumb).getByText("Hydra").getAttribute("aria-current")).toBe("page");

    // Trail, then the way out, then the page: reversing it would put the escape
    // hatch above the thing it escapes from.
    const back = screen.getByRole("button", { name: "Back to projects" });
    expect(
      breadcrumb.compareDocumentPosition(back) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      back.compareDocumentPosition(screen.getByTestId("project-landing")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("shows no breadcrumb and no current page on the project index", () => {
    render(<ProjectView {...viewProps()} />);

    expect(screen.getByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
    // `aria-current="page"` with no page open would announce a destination the
    // operator is not on.
    expect(document.querySelector('[aria-current="page"]')).toBeNull();
  });

  it("lands on the index with an announced notice after a delete", () => {
    const onProjectDeleted = vi.fn();
    render(<Harness initial={projects} openId="p1" onProjectDeleted={onProjectDeleted} />);

    const landing = landingState.received;
    if (!landing) throw new Error("landing page did not render");
    // Delete is the landing page's own path; this view only relays it, and the
    // owner drops the row and the open id in one update, like App does.
    act(() => {
      landing.onProjectDeleted("p1");
    });

    expect(onProjectDeleted).toHaveBeenCalledTimes(1);
    expect(onProjectDeleted).toHaveBeenCalledWith("p1");
    expect(screen.queryByTestId("project-landing")).toBeNull();
    expect(screen.getByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open project Solo" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open project Hydra" })).toBeNull();

    const notice = screen.getByRole("status");
    // Naming the project is the whole value of the notice: the row it would
    // otherwise point at is already gone from the list.
    expect(notice.textContent).toBe("Deleted project “Hydra”.");
    // The button that was clicked unmounted with the landing, so without this
    // focus a keyboard user restarts at the top of the document.
    expect(document.activeElement).toBe(notice);
  });

  it("closes a project that vanished from the refreshed list, and says so", () => {
    const onCloseProject = vi.fn();
    render(
      <Harness initial={[projects[1]]} openId="p1" onCloseProject={onCloseProject} />,
    );

    expect(onCloseProject).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("project-landing")).toBeNull();
    expect(screen.getByRole("heading", { name: "Projects" })).toBeTruthy();

    const notice = screen.getByRole("status");
    // A silent close would look like a crash: the project simply disappeared.
    expect(notice.textContent).toBe("That project no longer exists.");
    expect(document.activeElement).toBe(notice);
  });
});
