"""The proposal engine's matrix: repo shapes, and the renamed-twin archetypes.

Split from `test_proposal_engine_facts.py` so that file stays under the size the
lane brief allows. `git_state` is a read-only probe whose job is to survive every
shape a checkout can take on a real mount, so each test names the MUTATION that
would red it. All fixtures are temp repos; nothing here touches the live library.

The second half is the RENAMED-TWIN matrix: each measured archetype is built twice,
once under its real product name and once under a name that shares no token with
it, and the engine's whole answer - shape, score, confidence, group tree and
arrange moves - must agree once the product tokens are canonicalised. That is the
behavioural proof that the engine contains no project-specific literal; the
`_code_only` test is the static one.
"""

import io
import os
import shutil
import subprocess
import tokenize
import zipfile
from pathlib import Path

import pytest

import proposal_engine

# The API test image is deliberately slim and ships no git; the engine is designed
# to degrade to `git_state() is None` there. Tests that need a real checkout are
# host-only, exactly like `test_wave_plan.py`'s serialiser-path case.
HAVE_GIT = shutil.which("git") is not None
GIT_REQUIRED = pytest.mark.skipif(not HAVE_GIT, reason="git is required to exercise git_state")


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_models(folder: Path, count: int, suffix: str = ".stl") -> None:
    for index in range(count):
        write_bytes(folder / f"model{index}{suffix}", b"solid\n")


def write_pdf(path: Path, text: str) -> None:
    """A minimal but valid single-page PDF whose page text is `text`.

    Hand-written and uncompressed, so `pypdf` opens and extracts it and no checked-in
    fixture file is needed. The point is the DOCUMENT leg: a `b"%PDF-1.4\n"` stub
    satisfies the suffix check but has nothing to read, so it would exercise the path
    without exercising the parse.
    """
    lines = text.splitlines() or [""]
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
    ops = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({line}) Tj 0 -16 Td" for line in escaped) + " ET"
    stream = ops.encode("latin-1")
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    }
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for number in range(1, 6):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
            + str(xref).encode() + b"\n%%EOF\n")
    write_bytes(path, bytes(out))


def git(cwd: Path, *args: str) -> None:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com",
    )
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True, env=env)


def new_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "test")
    return path


def commit_all(repo: Path, message: str = "init") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def facts_for(folder: Path) -> proposal_engine.Facts:
    return proposal_engine.measure(str(folder), folder.name)


# --- git, hardened --------------------------------------------------------

@GIT_REQUIRED
def test_git_reads_a_normal_repo(tmp_path: Path) -> None:
    """A `.git` DIRECTORY is a repo; tracked paths are candidate-relative.

    Mutation: rely on `.git` being a directory's marker only and the worktree case
    below breaks; leave paths repo-root-relative and a nested candidate reads the
    wrong folder.
    """
    repo = new_repo(tmp_path / "repo")
    write_bytes(repo / "model.stl", b"solid\n")
    commit_all(repo)
    state = proposal_engine.git_state(str(repo))
    assert state["branch"] == "main"
    assert state["detached"] is False
    assert "model.stl" in state["tracked"]
    assert state["toplevel"] == str(repo.resolve())
    assert state["truncated"] is False


@GIT_REQUIRED
def test_git_reads_a_dot_git_file_worktree(tmp_path: Path) -> None:
    """`.git` may be a FILE (linked worktree, submodule): `os.path.exists` covers it.

    Mutation: test `.git` with `isdir` and a linked worktree reads as no repo at all.
    """
    main = new_repo(tmp_path / "main")
    write_bytes(main / "model.stl", b"solid\n")
    commit_all(main)
    worktree = tmp_path / "worktree"
    git(main, "worktree", "add", "-q", "-b", "linked", str(worktree))
    assert (worktree / ".git").is_file(), "the fixture must build the file case"
    state = proposal_engine.git_state(str(worktree))
    assert state is not None
    assert state["branch"] == "linked"


@GIT_REQUIRED
def test_git_reports_a_detached_head_without_inventing_a_branch(tmp_path: Path) -> None:
    """Detached HEAD is `branch: None, detached: True` plus a short sha.

    Mutation: pass `rev-parse --abbrev-ref` through and the operator is told the
    branch is literally `HEAD`, which is not a branch.
    """
    repo = new_repo(tmp_path / "repo")
    write_bytes(repo / "model.stl", b"solid\n")
    commit_all(repo)
    git(repo, "checkout", "-q", "--detach")
    state = proposal_engine.git_state(str(repo))
    assert state["branch"] is None
    assert state["detached"] is True
    assert state["sha"] and state["sha"] != "HEAD"


@GIT_REQUIRED
def test_git_nested_paths_resolve_to_the_candidate(tmp_path: Path) -> None:
    """A candidate inside another repo reads ITS files, not the parent's.

    Mutation: return `ls-files` paths unchanged and a nested checkout shows the
    parent's tracked set - the bug this rebase exists to prevent.
    """
    parent = new_repo(tmp_path / "parent")
    write_bytes(parent / "parent.stl", b"solid\n")
    commit_all(parent)
    nested = new_repo(parent / "nested")
    write_bytes(nested / "inner.stl", b"solid\n")
    commit_all(nested)
    state = proposal_engine.git_state(str(nested))
    assert "inner.stl" in state["tracked"]
    assert "parent.stl" not in state["tracked"]
    assert proposal_engine._resolve_to_candidate(
        "nested/inner.stl", str(parent / "nested"), str(parent)
    ) == "inner.stl"
    assert proposal_engine._resolve_to_candidate(
        "parent.stl", str(parent / "nested"), str(parent)
    ) is None


@GIT_REQUIRED
def test_a_repo_with_no_models_does_not_offer_git_project(tmp_path: Path) -> None:
    """A checkout holding no model must not recommend importing nothing.

    Mutation: always offer `git-project` at score 95 and an empty repo becomes the
    strongest recommendation in the library.
    """
    repo = new_repo(tmp_path / "empty")
    write(repo / "README.md", "only documentation\n")
    commit_all(repo)
    facts = facts_for(repo)
    options, reasons, _repo = proposal_engine.propose(facts, ())
    assert "git-project" not in {option.shape for option in options}
    assert any("no model" in reason for reason in reasons), reasons


def test_git_absent_returns_none_without_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing git binary must not fail a proposal.

    Mutation: let `FileNotFoundError` escape and the whole measurement dies on a
    host without git, which is a legal host for the CLI.
    """
    folder = tmp_path / "pretend"
    (folder / ".git").mkdir(parents=True)

    def absent(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", absent)
    assert proposal_engine.git_state(str(folder)) is None


@GIT_REQUIRED
def test_untracked_products_survive_and_a_zip_twin_is_reported(tmp_path: Path) -> None:
    """A `--porcelain -z` untracked folder and its sibling `.zip` pair as a twin.

    Mutation: parse the quoted non-`-z` status and a name with a space is mangled;
    forget the `<dir>.zip` check and the peel reports a half product.
    """
    repo = new_repo(tmp_path / "repo")
    write_bytes(repo / "tracked.stl", b"solid\n")
    commit_all(repo)
    write_models(repo / "New Product", 1)
    with zipfile.ZipFile(repo / "New Product.zip", "w") as archive:
        archive.writestr("New Product/model0.stl", b"solid\n")
    facts = facts_for(repo)
    state = proposal_engine.git_state(str(repo))
    peel = proposal_engine.peel_proposals(facts, state)
    entry = next(item for item in peel if item["name"] == "New Product")
    assert entry["kind"] == "folder"
    assert entry["twin"] is True, peel


@GIT_REQUIRED
def test_git_output_is_bounded_and_says_so(tmp_path: Path) -> None:
    """A cap that bites sets `truncated: True` instead of returning a silent prefix.

    Mutation: slice the listing without the flag and the report prints a partial
    tracked set as if it were the whole checkout.
    """
    repo = new_repo(tmp_path / "big")
    for index in range(6):
        write_bytes(repo / f"model{index}.stl", b"solid\n")
    commit_all(repo)
    state = proposal_engine.git_state(str(repo), max_tracked=2, max_untracked=1)
    assert len(state["tracked"]) <= 2
    assert state["truncated"] is True


# --- renamed twins: one archetype, two names, one answer ------------------
#
# The engine claims to read a SHAPE, not a catalogue of the library's names. Each
# builder below reproduces one measured archetype TWICE: once with its real product
# name (`twin=False`) and once with a name sharing no token with it (`twin=True`).
# Only the PRODUCT name changes - structural child names (`Wave 1`, `parts`,
# `Models`) stay, because they are the engine's generic vocabulary. The comparison
# test then asserts the two answers agree once the product tokens are canonicalised,
# and `test_the_engine_code_names_no_project` asserts the same thing statically.

# The product names the corpus taught this engine to recognise. Prose may name them
# (a docstring explaining a rule); CODE may not - the scan test below is the proof.
PRODUCT_WORDS = (
    "vault", "stagetop", "outofmarbles", "endorphin", "elecrow",
    "clockworkmonster", "fates end", "flexi rex", "two wheeler",
    "marble machine", "repo dump",
)


def zip_folder(zip_path: Path, folder: Path, arc_root: str) -> None:
    """Zip `folder` with every entry under `arc_root/`, preserving size exactly."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, f"{arc_root}/{path.relative_to(folder).as_posix()}")


def zip_bytes(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def group_shape(nodes):
    """The group tree WITHOUT names: nesting, counts, and the two flags per node."""
    return [
        (node["models"], node["foreign"], node["separable"], group_shape(node["children"]))
        for node in nodes
    ]


def canonical(text: str, pairs) -> str:
    """Replace each real/twin token with one placeholder, longest token first.

    Longest first because a product name can CONTAIN a shorter one
    (`Plain Gadget` inside `Plain Gadget-model_files`); the reverse order would
    leave a real token half-replaced and the two answers would compare unequal.
    """
    ordered = sorted(pairs, key=lambda pair: -max(len(pair[0]), len(pair[1])))
    for index, (real, twin) in enumerate(ordered):
        text = text.replace(real, f"<t{index}>").replace(twin, f"<t{index}>")
    return text


def canonical_moves(moves, pairs):
    """A move list with both ends of every move canonicalised."""
    return [(canonical(move["from"], pairs), canonical(move["to"], pairs)) for move in moves]


def folder_answer(path: Path, *, probe_git: bool = False) -> dict:
    """Every part of a folder proposal, with the product names left in the moves."""
    facts = proposal_engine.measure(str(path), path.name)
    options, _reasons, _repo = proposal_engine.propose(facts, (), probe_git=probe_git)
    chosen = options[0].shape
    return {
        "recommended": chosen,
        "top_score": options[0].score,
        "shapes": [(o.shape, o.score) for o in options],
        "confidence": [o.confidence for o in options],
        "groups": group_shape(facts.groups),
        "twin_verified": facts.twin["verified"],
        "cover": facts.cover["reason"],
        "arrange": proposal_engine.arrange_moves(facts, chosen),
        "counts": (facts.models, facts.images, facts.docs, facts.archives),
    }


def archive_answer(path: Path) -> dict:
    """Every part of an archive proposal, with the product names left in the moves."""
    facts = proposal_engine.measure_archive(str(path), path.name)
    options, _reasons = proposal_engine.propose_archive(facts)
    chosen = options[0].shape
    return {
        "recommended": chosen,
        "top_score": options[0].score,
        "shapes": [(o.shape, o.score) for o in options],
        "confidence": [o.confidence for o in options],
        "groups": [],
        "twin_verified": False,
        "cover": "none",
        "arrange": proposal_engine.arrange_archive_moves(facts, chosen),
        "counts": (facts.models, facts.images, facts.docs, facts.others),
        "nested": len(facts.nested),
        "wrapped": facts.common_root() is not None,
    }


def comparable(answer: dict, pairs) -> dict:
    """The answer with product tokens canonicalised - the form two names must share."""
    return {**answer, "arrange": canonical_moves(answer["arrange"], pairs)}


def build_deep_catalogue(base: Path, twin: bool) -> Path:
    """Mirror `Vault/`: ONE dated archive wrapper holding nearly all the models.

    The wrapper child holds 781 of 781 models, a manual sits at the wrapper's OWN
    root but claims no product, three ordinal families fan out into size families and
    part folders five levels deep, and the wrapper's own `.zip` sits beside it so
    twin verification pairs them and the wrapper is what gets dropped.
    """
    brand = "WidgetWorks" if twin else "Vault"
    root = base / brand
    wrapper = root / f"{brand} Archive_18.11.2026"
    write_bytes(wrapper / "Instructions Manual.pdf", b"%PDF-1.4\n")
    write_models(wrapper / "Rhino models", 1)
    sizes = ("S", "M", "L", "XL")
    parts = ("Facades", "Boxes", "Roofs", "Modules", "Runners")
    for family in ("01. DRAWERS", "02. SUSTAINERS", "03. MODIFICATIONS"):
        for number, size in enumerate(sizes, start=1):
            for part in parts:
                write_models(wrapper / family / f"0{number}. {size} Families" / f"{size}3" / part, 13)
    zip_folder(root / f"{brand} Archive_18.11.2026.zip", wrapper, wrapper.name)
    return root


def build_parts_product(base: Path, twin: bool) -> Path:
    """Mirror `Stagetop/`: waves of part folders, each wave with its own twin zip.

    Nine part folders read as parts by name, no document anywhere, and three wave
    archives whose stems match their folders - provenance, not products.
    """
    brand = "Some Other Wave Project" if twin else "Stagetop"
    root = base / brand
    for wave in ("Wave 1", "Wave 2", "Wave 3"):
        for part in ("Rails", "Legs", "Core"):
            write_models(root / wave / part, 3)
        zip_folder(root / f"{wave}.zip", root / wave, wave)
    return root


def build_multi_product(base: Path, twin: bool) -> Path:
    """The `OutOfMarbles` collection: three children that each claim to be a product.

    The collection root carries no document of its own; each of its three children
    holds models below AND a Printables-style attribution PDF at its OWN root. Every
    child therefore claims to be a product, and only the per-child document can be
    deciding the split - the root itself looks like nothing.

    The real witness is `import/OutOfMarbles`, whose `...-model_files` children carry
    a Printables attribution PDF at their own root; the live engine's reason line is
    "2 of 3 top folders carry their own metadata". In the live tree only TWO of the
    three children carry that PDF - the third keeps its documentation inside its
    nested product folder - while this fixture gives all three one, so the archetype
    is exercised at its strongest. That count is a fixture choice, not a corpus fact.

    The live row is `split into 3 projects` at 60/medium, and a `.pdf` at a child's
    own root is the same `children_with_own_meta` predicate as a README, which is why
    this is assertion-equivalent to what it replaced. The README/stem arm of that
    same predicate is already exercised elsewhere (`build_git_dump`'s README-bearing
    child and the facts tests), so no README is added here: keeping both arms in this
    row would make them indistinguishable.
    """
    brand = "Deep Catalogue Co" if twin else "OutOfMarbles"
    root = base / brand
    tail = "acc449f1-8f89-47a2-867a-7d9eb630e9f7"
    for vendor, child in (("326261", "Product Alpha"), ("326262", "Product Beta"),
                          ("326263", "Product Gamma")):
        slug = child.lower().replace(" ", "-")
        write_models(root / child / "parts", 3)
        write_pdf(root / child / f"{vendor}-{slug}-{tail}.pdf",
                  f"{child} by Ada\nLicensed under CC BY\n")
    return root


def build_git_dump(base: Path, twin: bool) -> Path:
    """Mirror `endorphin/`: a real checkout whose tracked models are the project.

    The working tree declares the boundary: five tracked models plus a README, and
    three untracked entries that peel off as materials.
    """
    brand = "Repo Dump Ltd" if twin else "endorphin"
    repo = new_repo(base / brand)
    write_models(repo / "models", 5)
    write(repo / "README.md", "the project\n")
    commit_all(repo)
    write_models(repo / "Product One", 2)
    write(repo / "Product One" / "README.txt", "Product One by Ada\nLicensed under cc_by\n")
    write_models(repo / "Product Two", 3)
    write_models(repo / "CAD", 1)
    return repo


def build_flat_product(base: Path, twin: bool) -> Path:
    """A GENERIC flat vendor export: three part folders and a README at the root.

    The three model-bearing part folders sit directly in the candidate and a vendor
    `README.txt` sits beside them at the root, so this is one product; the download's
    packaging tail (`-model_files`) is the only thing the plan renames away, and
    `arrange` moves the folder to `elecrow-7-inch-screen-console`.

    This is a generic archetype, NOT a mirror of the live elecrow row. That row, the
    real `elecrow-7-inch-screen-console-model_files`, keeps its document as a PDF in
    a FOURTH child (`..._580464-...pdf`) and has no document at the root at all, so
    it scores `one project 40 [low]` - measured by `propose_import.py` over the live
    tree, not by this fixture. This fixture pins 50 because its root README adds the
    document leg the live row lacks.
    """
    name = "Plain Gadget-model_files" if twin else "elecrow-7-inch-screen-console-model_files"
    root = base / name
    write_models(root / "body-a", 1)
    write_models(root / "body-b", 1)
    write_models(root / "screenmount", 1)
    write(root / "README.txt", "Created by Thingiverse user Ada\nLicensed under cc_by\n")
    return root


def build_archive_wrapper(base: Path, twin: bool) -> Path:
    """Mirror `01_Fates End - Wizard Tower.zip`: a download that wraps one product.

    One inner folder holds the models, an image and a manual, so the archive wraps
    exactly one product and offers one reading.
    """
    stem = "27_Frost Giant - Keep" if twin else "01_Fates End - Wizard Tower"
    source = base / "src" / f"{stem}_inner"
    for index in range(1, 6):
        write(source / "Models" / f"part{index}.stl", "solid\n")
    write_bytes(source / "images" / "cover.png", b"\x89PNG")
    write_bytes(source / "Manual.pdf", b"%PDF-1.4\n")
    zip_folder(base / f"{stem}.zip", source, source.name)
    return base / f"{stem}.zip"


def build_zip_of_zips(base: Path, twin: bool, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mirror the opaque `Vault ... .zip`: junk, one readable inner, one too large.

    The nested-inspection cap is monkeypatched to 1 KiB instead of writing a 64 MiB
    entry - `inspect_nested` reads the same branch either way, so this is the same
    code path at a size a test can afford.
    """
    monkeypatch.setattr(proposal_engine, "NESTED_INSPECT_MAX", 1024)
    stem = "Nova Bundle" if twin else "Atlas Bundle"
    inner_a = zip_bytes({"A/model1.stl": b"solid\n", "A/model2.stl": b"solid\n"})
    inner_b = zip_bytes({"B/blob.bin": b"x" * 4096})
    entries = {
        "__MACOSX/._Bundle": b"junk",
        "Bundle/.DS_Store": b"junk",
        "Bundle/notes.txt": b"notes\n",
        "Bundle/set-a.zip": inner_a,
        "Bundle/set-b.zip": inner_b,
    }
    write_bytes(base / f"{stem}.zip", zip_bytes(entries))
    return base / f"{stem}.zip"


def build_series(base: Path, twin: bool) -> Path:
    """Mirror `17_ClockworkMonster/`: a numbered folder missing its series title.

    Three numbered siblings share a title prefix and the candidate is numbered but
    untitled, so the series name is the rename - and the number stays. The series is
    read off the candidate's OWN parent, so each name gets a parent of its own: one
    shared directory would mix the two sibling sets and neither would be a series.
    """
    parent = base / ("twin" if twin else "real")
    candidate = "42_Gizmo" if twin else "17_ClockworkMonster"
    siblings = (
        ("01_Launchpad - Alpha", "02_Launchpad - Beta", "03_Launchpad - Gamma")
        if twin else
        ("01_Fates End - Wizard Tower", "02_Fates End - Fighter Tower", "03_Fates End - Paladin Tower")
    )
    for sibling in siblings:
        write_models(parent / sibling, 1)
    write_models(parent / candidate, 12)
    return parent / candidate


REAL_WRAPPER = "01_Fates End - Wizard Tower"
TWIN_WRAPPER = "27_Frost Giant - Keep"

ARCHETYPES = [
    pytest.param(
        "deep_catalogue", build_deep_catalogue, "folder", "one-project",
        [("Vault Archive_18.11.2026", "WidgetWorks Archive_18.11.2026"),
         ("Vault", "WidgetWorks")],
        id="deep-catalogue",
    ),
    pytest.param(
        "parts_product", build_parts_product, "folder", "one-project",
        [("Stagetop", "Some Other Wave Project")],
        id="parts-product",
    ),
    pytest.param(
        "multi_product", build_multi_product, "folder", "split-top-folders",
        [("OutOfMarbles", "Deep Catalogue Co")],
        id="multi-product",
    ),
    pytest.param(
        "git_dump", build_git_dump, "folder", "git-project",
        [("endorphin", "Repo Dump Ltd")],
        id="git-dump",
        marks=pytest.mark.skipif(not HAVE_GIT, reason="git is required to build the checkout"),
    ),
    pytest.param(
        "flat_product", build_flat_product, "folder", "one-project",
        [("elecrow-7-inch-screen-console-model_files", "Plain Gadget-model_files"),
         ("elecrow-7-inch-screen-console", "Plain Gadget")],
        id="flat-product",
    ),
    pytest.param(
        "archive_wrapper", build_archive_wrapper, "archive", "archive-project",
        # The rendered title is `wrapper_title(product_title(stem))`, which collapses
        # `_` and ` - ` into single spaces: it needs a pair of its own or the two
        # `to` paths never compare equal.
        [(f"{REAL_WRAPPER}.zip", f"{TWIN_WRAPPER}.zip"),
         (REAL_WRAPPER, TWIN_WRAPPER),
         ("01 Fates End Wizard Tower", "27 Frost Giant Keep")],
        id="archive-wrapper",
    ),
    pytest.param(
        "zip_of_zips", build_zip_of_zips, "archive", "archive-project",
        [("Atlas Bundle", "Nova Bundle")],
        id="zip-of-zips",
    ),
    pytest.param(
        "series", build_series, "folder", "one-project",
        [("17_ClockworkMonster", "42_Gizmo"),
         ("17_Fates End - ClockworkMonster", "42_Launchpad - Gizmo"),
         ("Fates End", "Launchpad"),
         ("ClockworkMonster", "Gizmo")],
        id="series",
    ),
]

# What the engine measures for the REAL shape: the band, the flags and the score.
# Pinned so a fixture that stopped mirroring its archetype reds here instead of
# quietly comparing two equally-wrong trees. `multi_product`'s split score is left
# to the equality check - only its shape is the corpus fact. `flat_product`'s
# confidence is deliberately not pinned either: the pinned 50 is the generic
# archetype's band (high, earned by its root document), whereas the live corpus row
# is 40 at `low`; pinning a band here would pin the fixture's story rather than the
# corpus fact, and the real-vs-twin equality still covers the row.
MEASURED = {
    "deep_catalogue": {"top_score": 75, "confidence": "medium", "twin_verified": True},
    "parts_product": {"top_score": 85, "confidence": "medium"},
    "git_dump": {"top_score": 95, "confidence": "high", "counts": (11, 0, 2, 0)},
    "flat_product": {"top_score": 50, "counts": (3, 0, 1, 0)},
    "archive_wrapper": {"top_score": 70, "counts": (5, 1, 1, 0)},
    "zip_of_zips": {"nested": 2, "wrapped": True},
    "series": {"top_score": 45, "counts": (12, 0, 0, 0)},
}


def _build(build, base: Path, twin: bool, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Call a builder; only the zip-of-zips fixture needs the cap monkeypatched."""
    if build is build_zip_of_zips:
        return build(base, twin, monkeypatch)
    return build(base, twin)


@pytest.mark.parametrize("name,build,kind,expected,pairs", ARCHETYPES)
def test_a_renamed_twin_gets_the_same_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name, build, kind, expected, pairs
) -> None:
    """Renaming a shape changes nothing: shapes, scores, confidence, tree and moves agree.

    Mutation: special-case any project name (or a name-derived token) anywhere in the
    engine and the twin's recommended shape, score, group flags or arrange moves
    diverge from the real one. Each builder mirrors one measured archetype; the twin
    shares no product token with it, so only the shape can be doing the deciding.
    """
    path_real = _build(build, tmp_path / "real", False, monkeypatch)
    path_twin = _build(build, tmp_path / "twin", True, monkeypatch)
    if kind == "archive":
        real, twin_answer = archive_answer(path_real), archive_answer(path_twin)
    else:
        probe = name == "git_dump"
        real = folder_answer(path_real, probe_git=probe)
        twin_answer = folder_answer(path_twin, probe_git=probe)
    assert comparable(real, pairs) == comparable(twin_answer, pairs), (real, twin_answer)
    assert real["recommended"] == expected
    # `confidence` is per option; a pinned band always means the recommendation's.
    summary = {**real, "confidence": real["confidence"][0]}
    for key, value in MEASURED.get(name, {}).items():
        assert summary[key] == value, (key, summary[key], value)
    # The pairs must capture EVERY product token, or the equality above could pass on
    # two names that were never compared: no real word may survive canonicalisation.
    for move in real["arrange"] + twin_answer["arrange"]:
        text = canonical(f'{move["from"]} {move["to"]}', pairs).casefold()
        survivors = [word for word in PRODUCT_WORDS if word in text]
        assert not survivors, (survivors, move)


# --- the static half ------------------------------------------------------

def _code_only(source: str) -> str:
    """Source with comments and DOCSTRINGS removed; other string literals stay.

    A docstring is a bare string statement - the only thing on its logical line - so
    `tokenize` can tell it from a literal the code actually compares against. That
    distinction is the whole point: prose may name the corpus, a comparison may not.
    """
    tokens = [
        token for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in {
            tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
            tokenize.ENCODING, tokenize.ENDMARKER,
        }
    ]
    pieces = []
    for index, token in enumerate(tokens):
        if token.type == tokenize.NEWLINE:
            continue
        starts_line = index == 0 or tokens[index - 1].type == tokenize.NEWLINE
        ends_line = index == len(tokens) - 1 or tokens[index + 1].type == tokenize.NEWLINE
        if token.type == tokenize.STRING and starts_line and ends_line:
            continue
        pieces.append(token.string)
    return " ".join(pieces).casefold()


def test_the_engine_code_names_no_project() -> None:
    """No product name appears in the engine's executable code, only in prose.

    Mutation: add a project-specific branch, constant or string literal to
    `proposal_engine.py` and this reds. Comments and docstrings may name the corpus
    (they explain a rule); code may not depend on it.
    """
    source = Path(proposal_engine.__file__).read_text(encoding="utf-8")
    code = _code_only(source)
    found = [word for word in PRODUCT_WORDS if word in code]
    assert not found, f"project names found in engine code: {found}"
