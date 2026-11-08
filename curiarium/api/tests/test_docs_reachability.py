"""Guard the repository's markdown against links that no longer reach anything.

Twenty-three links across the docs pointed at paths that do not exist, two were
written as `file://` URIs (which GitHub reads as repository-relative paths and
404s), and several documents were reachable from nowhere at all. This module is
the guard that keeps that from happening again: every tracked `.md` is walked
from the root README, every relative link must land on a path that exists, and
no link may use a URI scheme GitHub cannot resolve.

One rule is worth stating up front, because it is the difference between an
allowlist and a hiding place: **an allowlist entry exempts a file from the
reachability test only.** The link-resolution test and the scheme test scan
every tracked `.md`, allowlisted files included, so the allowlist can never be
used to smuggle a broken link past the guard.

Non-goals, stated so a later reader does not assume more coverage than exists:

* Heading anchors and fragments are NOT validated. A `#fragment` is stripped off
  the target and discarded; the file it names still has to exist.
* Only relative links are resolved. External `http`/`https`/`mailto`/`tel`
  targets are opaque, so a dead external URL is not caught here.
* HTML `<img src>` is existence-checked only, never recursed: an image is a leaf,
  it cannot carry documentation.

Pure stdlib plus pytest with no imports from the API, consistent with the other
doc guards. The guarded files live above the API directory, so the module skips
itself in the container, where `curiarium/api` is the whole build context.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
CURIARIUM_ROOT = API_ROOT.parent
REPO_ROOT = CURIARIUM_ROOT.parent
README_PATH = REPO_ROOT / "README.md"
DOCS_DIR = CURIARIUM_ROOT / "docs"

# The whole module is host-only: it walks the repository's markdown from the root
# README, and the API image builds from curiarium/api (which becomes /app), so
# README.md and curiarium/docs/ are both absent there. Requiring BOTH to be
# missing keeps a half-deleted docs tree failing loudly on a real checkout
# instead of skipping.
if not README_PATH.exists() and not DOCS_DIR.is_dir():
    pytest.skip(
        "docs reachability guard is host-only (the API image builds from curiarium/api, so "
        "the repo README.md and curiarium/docs/ are absent)",
        allow_module_level=True,
    )

# Schemes a browser or mail client resolves without a repository path behind them.
ALLOWED_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# Allowlist: exempts REACHABILITY ONLY, never link resolution (see the rule in the
# module docstring and in `test_the_allowlist_is_minimal_and_justified`).
ORPHAN_ALLOWLIST = {
    "curiarium/api/tests/fixtures/prepared/README.md": (
        "fixture-internal: it documents the exact bytes the prepared-print parser tests "
        "expect from the checked-in fixture files, not the product; linking it from the "
        "product docs would advertise test data as documentation"
    ),
}

# The only allowlist entry this module accepts; a second one is a decision to make
# deliberately, not a diff to slip past review.
EXPECTED_ALLOWLIST = {"curiarium/api/tests/fixtures/prepared/README.md"}
MINIMUM_REASON_LENGTH = 40

# The document roots the root README's map has to name, one link each. A directory
# entry (`curiarium/docs/adr`) is enough: the map points at a root, the root's own
# README lists its files.
CANONICAL_DOC_ROOTS = (
    "curiarium/docs/README.md",
    "curiarium/docs/ARCHITECTURE_RUNBOOK.md",
    "curiarium/docs/architecture/CONFIGURATION.md",
    "curiarium/docs/architecture/REVERSE_PROXY.md",
    "curiarium/docs/DEVELOPMENT.md",
    "curiarium/docs/adr",
    "curiarium/docs/guides/truenas.md",
    "docs/PERMISSIONS.md",
    "docs/AUDIT.md",
    "docs/TICKET_WORKFLOW.md",
    "docs/images/README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "curiarium/docker-compose.deploy.yml",
    "curiarium/docker-deploy.yml",
    "curiarium/api/.env.example",
    "curiarium/slicer-bridge/README.md",
    "curiarium/snapshot-sidecar/README.md",
    "curiarium/docs/archive",
    "curiarium/docs/archive/evidence/gate-0/BASELINE.md",
    "curiarium/docs/archive/evidence/gate-0/RESULTS.md",
)

DOCUMENTATION_MAP_HEADING = "## Documentation map"
DOCUMENTATION_MAP_LINK = "(#documentation-map)"
QUICK_START_HEADING = "## Quick start"
GUIDES_HEADING = "## Guides"

# `^ {0,3}(`{3,}|~{3,})`: an opening fence may be indented up to three spaces, and
# is a run of at least three backticks or tildes.
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

# Attribute-order agnostic: `src` is not the first attribute in `<img width="520"
# alt="Curiarium" src="docs/brand/curiarium-lockup.png" />`.
IMAGE_SRC_RE = re.compile(r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE | re.DOTALL)

LINK_START = "]("


class LinkResolutionError(Exception):
    """A link target cannot be resolved to a path in this repository."""


def _blank(text: str) -> str:
    """Spaces of the same length, keeping newlines so line numbers still map."""
    return "".join("\n" if char == "\n" else " " for char in text)


def _closing_fence(line: str, fence_char: str, fence_length: int) -> bool:
    """Whether `line` closes a fence opened by `fence_length` `fence_char`s."""
    stripped = line.strip()
    if len(stripped) < fence_length:
        return False
    return stripped[0] == fence_char and set(stripped) == {fence_char}


def _mask_fences(text: str) -> str:
    """Blank every fenced code block, from its opening line to its closing line."""
    out: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        if fence_char is None:
            opener = FENCE_OPEN_RE.match(line)
            if opener is None:
                out.append(line)
                continue
            run = opener.group(1)
            fence_char = run[0]
            fence_length = len(run)
            out.append(_blank(line))
            continue
        out.append(_blank(line))
        if _closing_fence(line, fence_char, fence_length):
            fence_char = None
            fence_length = 0
    return "".join(out)


def _closing_backtick_run(text: str, start: int, run: int) -> int | None:
    """The start index of the next run of exactly `run` backticks, if any."""
    index = start
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        begin = index
        while index < len(text) and text[index] == "`":
            index += 1
        if index - begin == run:
            return begin
    return None


def _mask_inline_spans(text: str) -> str:
    """Blank every inline code span, delimiters and content alike."""
    out = list(text)
    index = 0
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        opening = index
        while index < len(text) and text[index] == "`":
            index += 1
        run = index - opening
        closing = _closing_backtick_run(text, index, run)
        if closing is None:
            # An unmatched run is prose, not code; leave it alone.
            continue
        for position in range(opening, closing + run):
            if out[position] != "\n":
                out[position] = " "
        index = closing + run
    return "".join(out)


def _mask_code(text: str) -> str:
    """Return `text` with code blanked out, same length and same line breaks.

    Fences go first: a fenced block is not prose, so backticks inside it must not
    be paired as inline spans. Blanking rather than deleting keeps every offset,
    so a link that survives masking still reports its real line number.
    """
    return _mask_inline_spans(_mask_fences(text))


def _closing_paren(text: str, start: int) -> int | None:
    """The index of the `)` closing a target opened at `start`, counting depth."""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return index
            depth -= 1
    return None


def _clean_target(raw: str) -> str:
    """Drop the optional `<...>` wrapper and the optional `"title"` / `'title'`."""
    target = raw.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    for separator in (' "', " '"):
        cut = target.find(separator)
        if cut != -1:
            target = target[:cut]
    return target.strip()


def _link_targets(text: str) -> list[tuple[str, int]]:
    """Every `](target)` in `text`, with the 1-based line the target starts on.

    The scan counts parentheses instead of matching `\\(([^)]+)\\)`. A naive
    regex truncates `archive/Curiarium_v5.1_Competitive_Strategy(1).md` to
    `..._Strategy(1`, which manufactures a false broken link *and* a false orphan
    for the file the truncated target was supposed to name - exactly how the
    audit behind this ticket miscounted.
    """
    targets: list[tuple[str, int]] = []
    index = 0
    while True:
        opener = text.find(LINK_START, index)
        if opener == -1:
            return targets
        start = opener + len(LINK_START)
        end = _closing_paren(text, start)
        if end is None:
            index = start
            continue
        targets.append((_clean_target(text[start:end]), text.count("\n", 0, start) + 1))
        index = end + 1


def _is_inside_repo(path: Path) -> bool:
    """Whether `path` is REPO_ROOT or below it."""
    try:
        path.relative_to(REPO_ROOT)
    except ValueError:
        return False
    return True


def _resolve(target: str, containing: Path) -> Path | None:
    """The on-disk path a raw link target names, or None if it names no path.

    A non-allowed URI scheme is rejected on the scheme itself, never by stripping
    it and testing the resulting path: `urlparse("file://../docs/brand/")` yields
    a path-ish value, and `file://curiarium/snapshot-sidecar/README.md` names a
    file that genuinely exists, so a scheme-stripping implementation would pass
    links that GitHub can never resolve.
    """
    path = target.strip()
    if not path or path.startswith("#"):
        return None

    scheme_match = SCHEME_RE.match(path)
    if scheme_match is not None:
        scheme = scheme_match.group(0)[:-1].lower()
        if scheme in ALLOWED_SCHEMES:
            return None
        raise LinkResolutionError(f"unsupported URI scheme {scheme!r} in link target {target!r}")

    # The fragment is cut before unquoting, so a `%23` in a filename is not
    # mistaken for the start of a fragment.
    path = path.split("#", 1)[0]
    path = unquote(path)
    if not path:
        return None

    base = REPO_ROOT if path.startswith("/") else containing.parent
    resolved = Path(os.path.normpath(base / path.lstrip("/")))
    if not _is_inside_repo(resolved):
        raise LinkResolutionError(f"link target {target!r} escapes the repository: {resolved}")
    return resolved


def _git_paths(*patterns: str) -> list[str]:
    """Repo-relative paths git tracks that match `patterns`, sorted.

    Git failing is a hard error: this must never skip, and it must never fall
    back to `Path.rglob`, which would pick up `.lanes/`, `.plans/` and
    `node_modules/` and quietly change what "tracked" means.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *patterns],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(name for name in result.stdout.decode("utf-8").split("\0") if name)


def _tracked_markdown() -> list[str]:
    """Repo-relative paths of the tracked markdown files."""
    return _git_paths("*.md")


def _relative(path: Path) -> str:
    """The repo-relative posix form of a resolved path."""
    return path.relative_to(REPO_ROOT).as_posix()


def _links_in(relative_path: str) -> list[tuple[str, int]]:
    """The link targets of a tracked document, with code masked out first."""
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    return _link_targets(_mask_code(text))


def _reachable_documents() -> set[str]:
    """The `.md` files reachable from the root README by following links.

    `visited` is explicit and load-bearing: the repaired graph has cycles
    (ARCHITECTURE_RUNBOOK.md <-> archive/ROADMAP.md, and <-> archive/Curiarium_v5.1
    _Competitive_Strategy(1).md). Directory links and non-`.md` links are only
    existence-checked, never recursed - a directory link must not adopt every
    document beneath it, or the map could point at a folder and call it done.
    """
    tracked = set(_tracked_markdown())
    visited = {"README.md"}
    queue = ["README.md"]
    while queue:
        current = queue.pop()
        for target, _line in _links_in(current):
            try:
                resolved = _resolve(target, REPO_ROOT / current)
            except LinkResolutionError:
                continue
            if resolved is None:
                continue
            relative = _relative(resolved)
            if relative in visited or relative not in tracked or not relative.endswith(".md"):
                continue
            visited.add(relative)
            queue.append(relative)
    return visited


def _section_lines(lines: list[str], heading: str) -> list[str]:
    """The lines of the `heading` section, heading included, subheadings too."""
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    assert start is not None, f"README.md has no {heading!r} section"
    section: list[str] = []
    for line in lines[start:]:
        if section and line.startswith("## "):
            break
        section.append(line)
    return section


def _readme_section(heading: str) -> list[tuple[str, int]]:
    """The masked link targets of the `heading` section of the root README."""
    lines = README_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    return _link_targets(_mask_code("".join(_section_lines(lines, heading))))


def _heading_index(lines: list[str], heading: str) -> int:
    """The index of the `heading` line of the root README."""
    index = next((position for position, line in enumerate(lines) if line.strip() == heading), None)
    assert index is not None, f"README.md has no {heading!r} section"
    return index


def _format_problems(problems: list[str]) -> str:
    """One bullet per problem, so a single failing run reports the whole set."""
    return "\n  - ".join(problems)


def test_every_tracked_document_is_reachable_from_the_readme() -> None:
    """No document may sit on disk that the documentation map cannot lead to.

    Every orphan is aggregated into one message: a one-document-per-run failure
    turns a six-file cleanup into six verify cycles. The allowlist is checked in
    the other direction too - an entry for a file that no longer exists is a
    stale promise, and it would otherwise keep suppressing nothing forever.
    """
    tracked = set(_tracked_markdown())
    reachable = _reachable_documents()
    allowlisted = set(ORPHAN_ALLOWLIST)

    problems = [
        f"{path} is tracked but no document links to it from README.md"
        for path in sorted(tracked - reachable - allowlisted)
    ]
    problems += [
        f"{path} is in ORPHAN_ALLOWLIST but is not a tracked .md file; the entry is stale"
        for path in sorted(allowlisted - tracked)
    ]

    assert not problems, (
        f"the reachability guard found {len(problems)} problem(s):\n  - " + _format_problems(problems)
    )


def test_every_relative_link_resolves_on_disk() -> None:
    """Every relative link in every tracked document must land on something.

    This scans ALL tracked markdown, not just the reachable set: restricting it
    to reachable files would make the allowlist a smuggling route, where unlinking
    a file also silently stops checking the links inside it. An external scheme is
    not a failure, but `LinkResolutionError` still is, and its message names the
    offending scheme so the report says which kind of problem it found.
    """
    problems: list[str] = []
    for relative_path in _tracked_markdown():
        containing = REPO_ROOT / relative_path
        for target, line in _links_in(relative_path):
            try:
                resolved = _resolve(target, containing)
            except LinkResolutionError as error:
                problems.append(f"{relative_path}:{line}  {target}  ({error})")
                continue
            if resolved is None:
                continue
            if not resolved.exists():
                problems.append(f"{relative_path}:{line}  {target} -> {_relative(resolved)} does not exist")

    assert not problems, (
        f"{len(problems)} link(s) do not resolve on disk:\n  - " + _format_problems(problems)
    )


def test_no_link_uses_a_file_scheme() -> None:
    """`file://` links do not resolve on GitHub; only web and contact schemes do.

    GitHub renders a `file://` target as a repository path it cannot find, so the
    link 404s for every reader while looking fine in a local editor that opens
    local files. The fix is always a repository-relative link, never a scheme
    whitelist addition.
    """
    problems: list[str] = []
    for relative_path in _tracked_markdown():
        for target, line in _links_in(relative_path):
            scheme_match = SCHEME_RE.match(target.strip())
            if scheme_match is None:
                continue
            scheme = scheme_match.group(0)[:-1].lower()
            if scheme in ALLOWED_SCHEMES:
                continue
            problems.append(f"{relative_path}:{line}  {target}  (scheme {scheme!r})")

    assert not problems, (
        "file:// links do not resolve on GitHub (a file: target is read as a repository path "
        "and 404s); use a repository-relative link instead:\n  - " + _format_problems(problems)
    )


def test_file_scheme_is_rejected_even_when_the_path_exists() -> None:
    """A `file://` link to a real file is still broken, and must be rejected as one.

    The snapshot sidecar's README is tracked and on disk, so a resolver that
    stripped the scheme and tested the path would resolve this link happily and
    pass. Rejection therefore has to happen on the scheme, which is what the
    message asserts: the path existing cannot be the reason this passes.
    """
    relative = "curiarium/snapshot-sidecar/README.md"
    assert (REPO_ROOT / relative).is_file(), f"{relative} is missing, so this test proves nothing"
    assert relative in _tracked_markdown(), f"{relative} is not tracked, so this test proves nothing"

    with pytest.raises(LinkResolutionError) as excinfo:
        _resolve("file://" + relative, README_PATH)

    message = str(excinfo.value)
    assert "scheme" in message, f"the rejection does not name the problem as a scheme: {message}"
    assert "file" in message, f"the rejection does not name the offending scheme: {message}"


def test_balanced_parentheses_in_a_link_target_are_preserved() -> None:
    """A parenthesised filename is one target, not a truncated one plus junk.

    `archive/Curiarium_v5.1_Competitive_Strategy(1).md` is a real filename. A
    regex that stops at the first `)` reports `..._Strategy(1`, which is both a
    broken link and an orphan for the real file - the audit error this guard
    exists to prevent.
    """
    text = "see [strategy](archive/Curiarium_v5.1_Competitive_Strategy(1).md) and [x](../a.md)\n"

    targets = _link_targets(text)

    assert [target for target, _line in targets] == [
        "archive/Curiarium_v5.1_Competitive_Strategy(1).md",
        "../a.md",
    ], f"the parenthesised target was truncated: {targets}"
    assert [line for _target, line in targets] == [1, 1], f"line numbers are wrong: {targets}"


def test_code_fences_and_inline_code_spans_are_ignored() -> None:
    """Code is an example, not a link; masking it must not hide the prose either.

    The positive control is the point of the test: `[real](../present.md)` sits
    after the closing fence and must still be detected, so masking is a fence
    filter and not a loophole that blanks whatever is inconvenient.

    The real-data case this protects is `docs/images/README.md:70`, an example
    inside a ```markdown fence whose target is deliberately absent - the
    screenshots are disclosed as missing. A fence-unaware guard would demand a
    bogus "fix" for a file that must have no allowlist entry.
    """
    text = (
        "```markdown\n"
        "[a](../missing-a.png)\n"
        "```\n"
        "\n"
        "~~~\n"
        "[b](../missing-b.png)\n"
        "~~~\n"
        "\n"
        "Inline `[c](../missing-c.md)` span.\n"
        "\n"
        "[real](../present.md)\n"
    )

    masked = _mask_code(text)
    targets = _link_targets(masked)
    found = [target for target, _line in targets]

    assert len(masked) == len(text), "masking must preserve length so line numbers still map"
    assert "../missing-a.png" not in found, f"a backtick fence leaked a link: {found}"
    assert "../missing-b.png" not in found, f"a tilde fence leaked a link: {found}"
    assert "../missing-c.md" not in found, f"an inline code span leaked a link: {found}"
    assert found == ["../present.md"], f"the link after the closing fence was lost: {found}"
    assert [line for _target, line in targets] == [11], f"the positive control's line is wrong: {targets}"


def test_the_parenthesised_strategy_document_is_reachable() -> None:
    """The parenthesised strategy doc is reached by the link that names it.

    Both directions are asserted, because reachability alone is not enough: the
    file could be reachable through a directory link or another document that
    happens to mention it. The carrier row is what proves the scanner kept the
    `(1)` whole, and the truncated form is asserted absent everywhere so a
    regex-style truncation cannot reappear in some other file.
    """
    relative = "curiarium/docs/archive/Curiarium_v5.1_Competitive_Strategy(1).md"
    carrier = "curiarium/docs/README.md"

    assert relative in _tracked_markdown(), f"{relative} is not a tracked file"
    assert relative in _reachable_documents(), f"{relative} is tracked but unreachable from README.md"

    carried = {_resolve(target, REPO_ROOT / carrier) for target, _line in _links_in(carrier)}
    assert REPO_ROOT / relative in carried, f"{carrier} has no link that resolves to {relative}: {sorted(carried)}"

    # A target that carries the stem but not the closing `)` of its `(1)` is a
    # target truncated at the parenthesised filename.
    stem = "Curiarium_v5.1_Competitive_Strategy(1"
    offenders = [
        f"{relative_path}:{line}  {target}"
        for relative_path in _tracked_markdown()
        for target, line in _links_in(relative_path)
        if stem in target and stem + ")" not in target
    ]
    assert not offenders, "a link target is truncated at the parenthesised filename:\n  - " + _format_problems(offenders)


def test_the_allowlist_is_minimal_and_justified() -> None:
    """One entry, one reason, one real file - or this guard is being gamed.

    The rule, restated where someone editing the allowlist will read it: an entry
    exempts reachability ONLY. Link resolution and scheme checking still scan the
    entry's file, so an allowlisted document with a broken link still fails.
    """
    tracked = set(_tracked_markdown())
    problems: list[str] = []

    if set(ORPHAN_ALLOWLIST) != EXPECTED_ALLOWLIST:
        problems.append(f"ORPHAN_ALLOWLIST is {sorted(ORPHAN_ALLOWLIST)}, expected {sorted(EXPECTED_ALLOWLIST)}")

    for path, reason in sorted(ORPHAN_ALLOWLIST.items()):
        if not isinstance(reason, str) or len(reason.strip()) < MINIMUM_REASON_LENGTH:
            problems.append(f"{path} has no usable justification (need {MINIMUM_REASON_LENGTH}+ characters): {reason!r}")
        if path not in tracked:
            problems.append(f"{path} is allowlisted but is not a tracked .md file")
        elif not (REPO_ROOT / path).is_file():
            problems.append(f"{path} is allowlisted but is missing from the working tree")

    assert not problems, "the orphan allowlist is not minimal and justified:\n  - " + _format_problems(problems)


def test_readme_documentation_map_covers_every_doc_root() -> None:
    """The root README's map must actually list every document root.

    It has to be a bullet list: a table cannot take a one-line addition, which is
    the whole argument for writing the map as a list, so a `|` row in the section
    means the map has regressed to the shape it replaced. Placement matters too -
    the map is useless below "Guides", where a reader looking for a document has
    already scrolled past it.
    """
    lines = README_PATH.read_text(encoding="utf-8").splitlines()
    section = _section_lines(lines, DOCUMENTATION_MAP_HEADING)
    map_index = _heading_index(lines, DOCUMENTATION_MAP_HEADING)
    problems: list[str] = []

    table_rows = [line for line in section if line.lstrip().startswith("|")]
    if table_rows:
        problems.append(f"the map is a table, not a bullet list: {table_rows}")

    quick_start = _heading_index(lines, QUICK_START_HEADING)
    guides = _heading_index(lines, GUIDES_HEADING)
    if not quick_start < map_index < guides:
        problems.append(
            f"the map is at line {map_index + 1}; it belongs after {QUICK_START_HEADING!r} "
            f"(line {quick_start + 1}) and before {GUIDES_HEADING!r} (line {guides + 1})"
        )

    first_section = next((index for index, line in enumerate(lines) if line.startswith("## ")), len(lines))
    intro = "\n".join(lines[:first_section])
    if DOCUMENTATION_MAP_LINK not in intro:
        problems.append(f"the README intro does not link to the map with {DOCUMENTATION_MAP_LINK}")

    listed: set[str] = set()
    for target, _line in _readme_section(DOCUMENTATION_MAP_HEADING):
        try:
            resolved = _resolve(target, README_PATH)
        except LinkResolutionError as error:
            problems.append(f"the map links {target!r}, which does not resolve: {error}")
            continue
        if resolved is not None:
            listed.add(os.path.normpath(_relative(resolved)))

    missing = [root for root in CANONICAL_DOC_ROOTS if os.path.normpath(root) not in listed]
    if missing:
        problems.append(f"the map does not link these document roots: {missing} (linked: {sorted(listed)})")

    assert not problems, "the root README's documentation map is incomplete:\n  - " + _format_problems(problems)


def test_relative_html_image_sources_resolve() -> None:
    """An `<img src>` that points nowhere is a broken picture nobody notices.

    The regex is attribute-order agnostic on purpose: the root README's own badge
    image is `<img width="520" alt="Curiarium" src="docs/brand/curiarium-lockup.png"
    />`, so a guard that assumed `src` came first would check nothing at all.
    Images are existence-checked only and never recursed - an image cannot carry
    documentation.
    """
    lockup = "docs/brand/curiarium-lockup.png"
    sources: set[str] = set()
    problems: list[str] = []

    for relative_path in _tracked_markdown():
        text = _mask_code((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
        for match in IMAGE_SRC_RE.finditer(text):
            source = match.group(1).strip()
            if not source or SCHEME_RE.match(source) or source.startswith("//"):
                continue
            line = text.count("\n", 0, match.start()) + 1
            try:
                resolved = _resolve(source, REPO_ROOT / relative_path)
            except LinkResolutionError as error:
                problems.append(f"{relative_path}:{line}  {source}  ({error})")
                continue
            if resolved is None:
                continue
            resolved_relative = os.path.normpath(_relative(resolved))
            sources.add(resolved_relative)
            if not resolved.exists():
                problems.append(f"{relative_path}:{line}  {source} -> {resolved_relative} does not exist")

    assert os.path.normpath(lockup) in sources, f"the root README's lockup image was not found by the scan: {sorted(sources)}"
    assert (REPO_ROOT / lockup).is_file(), f"{lockup} is missing from the working tree"
    assert _git_paths(lockup), f"{lockup} is not tracked by git"
    assert not problems, "relative <img src> targets do not resolve:\n  - " + _format_problems(problems)
