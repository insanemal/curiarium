"""One engine, one shim, and the operator's labels holding as tests (Mf-yitm).

`curiarium/scripts/propose_import.py` used to BE the proposal engine: 1213 lines
of measurement and interpretation with no way to reach them from the API except
by importing a script, and no way to assert them except by scraping stdout. The
engine now lives in `curiarium/api/proposal_engine.py` and the script is argv plus
rendering. This module pins that split, because the split is the thing that rots
silently:

* two copies drift by WORDING, which nothing catches. So the first test pins the
  MODULE IDENTITY instead: copy the engine back into the script and this reds on
  identity, not on wording;
* the engine is imported by the API process and run in a host tight loop, so one
  `import fastapi` there would drag the session/HTTP world into a read-only walk
  and make the loop unimportable outside the app. The audit is mechanical: no app
  module, and `pypdf` is the one third-party import, deliberately allow-listed;
* `MODEL_EXTS` is deliberately WIDER than the app's `MODEL_SUFFIXES` - a proposal
  may name a payload no importer accepts - so it is pinned as a containment, not
  as an equality: `check_model_table.py` pins equality for the four copies that
  really must agree, and must not be edited for this one;
* `propose()` shells out to git for a repo's own declaration. A GET handler may
  never do that, so `probe_git=False` is pinned by making the call explode;
* `validate()` prints nothing: it returns rows. A future `print` in the engine is
  invisible to the CLI and fatal to the API, so the silence is asserted.

The six label fixtures are rebuilt as synthetic trees and asserted against the
fixture's OWN values (`expect`, `count`, `arrange_contains`), so fixture drift
reds by name of the key it broke. `import/endorphin` is deliberately NOT built: it
is a real git checkout, which cannot be faked without a subprocess, so it stays
covered by the CLI's own `--validate` against the live library.

Host-only, like `test_wave_plan.py`: the API image builds from `curiarium/api`,
so `curiarium/scripts/` (the shim and the label fixtures) is absent in-container
and this module skips itself there.
"""

import ast
import importlib.util
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import List, Optional, Tuple

import pytest

import library_scan
import proposal_engine

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if not SCRIPTS.is_dir():
    pytest.skip(
        "the proposal engine's CLI shim and label fixtures are host-only (the API image "
        "builds from curiarium/api, so curiarium/scripts/ is absent)",
        allow_module_level=True,
    )

ENGINE = Path(proposal_engine.__file__).resolve()
SHIM_PATH = SCRIPTS / "propose_import.py"
LABELS_PATH = SCRIPTS / "fixtures" / "proposal-labels.json"

# The shapes and names the engine must NOT define in the shim's namespace: each one
# would be a second copy of a rule the two files would then have to keep in step.
SHIM_FORBIDDEN_NAMES = ("propose", "measure", "MODEL_EXTS")
SHIM_FORBIDDEN_FUNCTIONS = ("propose", "measure", "arrange_proposal", "validate")

# Modules the engine may not import, ever: the first two make it unimportable from a
# bare host python, the rest drag the app's session/HTTP world into a read-only walk.
APP_MODULES = (
    "fastapi", "sqlalchemy", "db", "models", "config", "schemas",
    "bundle_service", "file_utils", "library_scan", "zip_service",
)

# The wrapper layout every Vault-shaped fixture is built around, and the one the
# `arrange_contains` label asks for by name.
VAULT_ZIP = "Vault Archive_18.11.2026.zip"
VAULT_WRAPPER = "Vault Archive_18.11.2026"


# --- the shim and the engine are one implementation ------------------------


@pytest.fixture(scope="module")
def shim() -> ModuleType:
    """The CLI shim, executed by path the way a bare host python runs it."""
    spec = importlib.util.spec_from_file_location("propose_import_shim", SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shim_imports_the_one_engine_module(shim: ModuleType) -> None:
    """Identity, not wording: copy the engine back into the script and this reds.

    A second copy of `propose` in the script would drift one wording at a time and
    nothing would notice - the CLI would keep printing plausible proposals while the
    API answered from different rules. The assertions here are structural (which
    module object, which file, which names the shim's namespace holds), so they
    cannot be satisfied by a copy that happens to say the same thing today.
    """
    engine = sys.modules["proposal_engine"]
    assert shim.proposal_engine is engine, (
        "the shim must use the module it bootstrapped, not a second import of its own"
    )
    assert engine is proposal_engine, "the test's engine and the shim's engine must be one object"
    assert Path(engine.__file__).resolve() == ENGINE, (
        f"the shim resolved the engine to {engine.__file__}, not to curiarium/api/proposal_engine.py"
    )
    assert ENGINE == SCRIPTS.parent / "api" / "proposal_engine.py", (
        "the engine must live beside the API it serves, or the API cannot import it"
    )


def test_the_shim_defines_no_rule_of_its_own(shim: ModuleType) -> None:
    """The shim's namespace may hold rendering and argv, nothing else.

    Mutation: re-add `from proposal_engine import propose, measure, MODEL_EXTS` and
    the shim looks identical at the terminal while the names it exposes shadow the
    engine's - the fork starts there, not at the first edited line.
    """
    for name in SHIM_FORBIDDEN_NAMES:
        assert not hasattr(shim, name), (
            f"the shim's namespace defines {name!r}: that name belongs to the engine, and a "
            f"second binding is how the two copies start disagreeing"
        )
    tree = ast.parse(SHIM_PATH.read_text(encoding="utf-8"))
    defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert not defined & set(SHIM_FORBIDDEN_FUNCTIONS), (
        f"the shim defines {sorted(defined & set(SHIM_FORBIDDEN_FUNCTIONS))}: those are engine "
        f"functions, moved out of this file by Mf-yitm, not wrappers to keep here"
    )


def test_the_shim_bootstraps_the_engine_only_through_sys_path() -> None:
    """The one import shape that works from a bare host python, pinned literally.

    The script is not a package member and `curiarium/api` is not on a host
    `sys.path`, so the engine is reachable only by inserting its directory first.
    Mutation: replace the bootstrap with `from . import ...` or a package-relative
    import and the CLI stops starting at all, which no test of the rules would catch.
    """
    source = SHIM_PATH.read_text(encoding="utf-8")
    for fragment in (
        'ENGINE_DIR = Path(__file__).resolve().parents[1] / "api"',
        "if str(ENGINE_DIR) not in sys.path:",
        "sys.path.insert(0, str(ENGINE_DIR))",
        "import proposal_engine",
    ):
        assert fragment in source, f"the sys.path bootstrap lost {fragment!r}"


# --- the engine is app-free, with one allow-listed third-party dep ---------

# The ONE third-party package the engine may import, deliberately: `pypdf` is pure
# Python (no system libraries, so the multi-arch image stays buildable on arm64).
# A second dependency must be an explicit edit here, not an accident.
ALLOWED_THIRD_PARTY = {"pypdf"}


def test_the_engine_imports_no_app_module() -> None:
    """No app import may reach the engine, and `pypdf` is its only third-party one.

    Mutation: `import fastapi` (or `db`, or `library_scan`) makes the engine drag
    the session/HTTP world into a read-only walk and breaks the host tight loop;
    a second third-party dependency slipped in silently would do the same on a
    machine without it. Both are caught mechanically, at every import depth.
    """
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    every = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            every.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise AssertionError("the engine must not import relatively: it has no package")
            every.add((node.module or "").split(".")[0])
    top_level = {
        alias.name.split(".")[0]
        for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in tree.body if isinstance(node, ast.ImportFrom)
    }
    assert every & set(APP_MODULES) == set(), (
        f"the engine imports app modules {sorted(every & set(APP_MODULES))}: they drag the "
        f"session and HTTP world into a read-only walk and break the host tight loop"
    )
    assert top_level & set(APP_MODULES) == set(), (
        f"the engine imports app modules at module level {sorted(top_level & set(APP_MODULES))}"
    )
    third_party = every - set(sys.stdlib_module_names)
    assert third_party <= ALLOWED_THIRD_PARTY, (
        f"the engine imports non-stdlib modules {sorted(third_party)}; the allow-list is "
        f"{sorted(ALLOWED_THIRD_PARTY)}. Add one deliberately, do not slip it in"
    )


def test_the_engine_model_table_is_a_deliberate_superset() -> None:
    """Containment, not equality: the proposal may name a payload no importer accepts.

    The engine answers "what is in this folder", so a Rhino or LightBurn file is real
    payload even though no slicer here opens it - that difference is exactly what the
    proposals exist to expose. `curiarium/scripts/check_model_table.py` pins EQUALITY
    for the four copies that must agree with `file_utils.MODEL_MIME_BY_SUFFIX`; this
    set is not one of them and that gate must not be widened to cover it.
    """
    assert library_scan.MODEL_SUFFIXES <= proposal_engine.MODEL_EXTS, (
        "the engine must accept every suffix the app imports, or a proposal would deny a "
        "payload the importer happily stores"
    )
    wider = proposal_engine.MODEL_EXTS - library_scan.MODEL_SUFFIXES
    for suffix in (".3dm", ".obj"):
        assert suffix in wider, (
            f"{suffix} must stay in the engine's wider table: it is a real payload with no "
            f"importer, which is the case this table is wider for"
        )


# --- synthetic trees for the label fixtures -------------------------------


def write(path: Path, text: str = "") -> None:
    """One file, parents and all: a synthetic tree writes bytes only to measure them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_models(folder: Path, count: int) -> None:
    """`count` model files directly in `folder`."""
    for index in range(count):
        write(folder / f"model{index}.stl")


def write_archive(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "the download this folder came out of")


def build_vault(root: Path) -> Path:
    """The layout of case (a): the zip sits INSIDE the dominant wrapper child.

    Ten models under the wrapper's part folders, three under `Rhino models/`, and the
    manual at the root only - a manual INSIDE the wrapper is a product claim and turns
    the folder into a collection, which is a different measurement.
    """
    vault = root / "Vault"
    wrapper = vault / VAULT_WRAPPER
    for part in range(10):
        write(wrapper / f"part{part}" / "bracket.stl")
    write_archive(wrapper / VAULT_ZIP)
    write_models(vault / "Rhino models", 3)
    write(vault / "Instructions Manual.pdf", "%PDF-1.4\n")
    return vault


def build_own_wrapper(root: Path) -> Path:
    """The layout of case (b): the candidate IS the wrapper, its zip one level up."""
    base = root / "base"
    wrapper = base / VAULT_WRAPPER
    write_models(wrapper / "Rhino models", 12)
    write_archive(base / VAULT_ZIP)
    return wrapper


def build_container(root: Path) -> Path:
    """A drop-zone container: two products and a loose model, all under one folder."""
    container = root / "import" / "Drop"
    write_models(container / "Product A", 2)
    write_models(container / "Product B", 3)
    write(container / "loose.stl")
    return container


def build_series(root: Path) -> Path:
    """`17_ClockworkMonster` beside three numbered Fates End towers: a series."""
    tower = root / "import" / "17_ClockworkMonster"
    write_models(tower / "tower", 5)
    for index, thing in ((1, "Wizard Tower"), (2, "Fighter Tower"), (3, "Rogue Tower")):
        write_models(root / "import" / f"0{index}_Fates End - {thing}", 2)
    return tower


def build_labelled_tree(root: Path) -> None:
    """Every label fixture except `import/endorphin`, as the shape its note describes."""
    # Three marble machines from one creator: each carries its own README and print
    # settings, which is what makes each one a product rather than a part.
    for product in ("Orbital Machine", "Twister Machine", "Cascade Machine"):
        folder = root / "import" / "OutOfMarbles" / product
        write_models(folder, 3)
        write(folder / "README.md", "a Thingiverse export\n")
        write(folder / "print-settings.json", "{}\n")
    # A controller-shell scan beside a foreign product carrying its own vendor id.
    write_models(root / "import" / "xbox360_shell" / "scanned shell", 4)
    write_models(root / "import" / "xbox360_shell" / "Controller shell - 5414731", 2)
    # Two projects imported by hand: one subfolder per file, and the print settings
    # each hand import keeps - the per-file shape alone reads as one project.
    for project in ("Flexi Dragon", "Cable Clips"):
        for index in range(2):
            write_models(root / "Unsorted" / project / f"model{index}", 1)
        write(root / "Unsorted" / project / "print-settings.json", "{}\n")
    build_vault(root)
    # Nine parts, none carrying a document anywhere: the Stagetop caveat.
    for part in range(1, 10):
        write_models(root / "import" / "Stagetop" / f"0{part}_stage", 2)
    # A series tower plus its numbered siblings, so the missing series title is
    # supplied from the names beside it.
    write_models(root / "import" / "17_ClockworkMonster" / "tower", 5)
    for index, thing in ((1, "Wizard Tower"), (2, "Fighter Tower"), (3, "Rogue Tower")):
        write_models(root / "import" / f"0{index}_Fates End - {thing}", 2)


def disk_labels() -> list[dict]:
    """The fixture's labels a synthetic tree can stand in for.

    `import/endorphin` needs a real git checkout (git_state reads the tracked set),
    so it is exercised by the CLI's `--validate` against the live library instead of
    being faked here: a `.git` directory with nothing behind it measures as no repo.
    """
    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    return [label for label in labels if label["key"] != "import/endorphin"]


@pytest.fixture()
def labelled_root(tmp_path: Path) -> Path:
    build_labelled_tree(tmp_path)
    return tmp_path


def chosen_and_arrange(path: Path) -> tuple[str, list[str]]:
    """The engine's answer for one folder, chosen shape and arrangement only."""
    facts = proposal_engine.measure(str(path), path.name)
    options, _reasons, _repo = proposal_engine.propose(facts, ())
    chosen = options[0].shape if options else "leave"
    return chosen, proposal_engine.arrange_proposal(facts, chosen)


# --- the labelled fixtures hold as tests ----------------------------------


def test_every_labelled_fixture_holds_as_a_test(labelled_root: Path) -> None:
    """Six of the seven confirmed labels, asserted against the fixture's own values.

    The fixture is the ground truth (the owner created these projects by hand), so the
    assertions read `expect`, `count` and `arrange_contains` out of it rather than
    repeating literals: a drifted fixture reds here instead of quietly passing.
    Mutation: tighten any rule - treat part names as products, drop the vendor-id
    claim - and the row that rule breaks reds, naming the key it broke.
    """
    labels = disk_labels()
    report = proposal_engine.validate(str(labelled_root), [], [], labels)
    assert [row["key"] for row in report["labels"]] == [label["key"] for label in labels], (
        "validate must return one row per label, in order: a label that silently vanished is a "
        "rule nobody scores"
    )
    rows = {row["key"]: row for row in report["labels"]}
    for label in labels:
        key = label["key"]
        row = rows[key]
        assert row["expect"] == label["expect"], f"{key}: the row must echo the fixture's expectation"
        if "count" in label:
            assert row["count"] == label["count"], (
                f"{key} measured {row['count']} top folders, the fixture confirms {label['count']}"
            )
        assert row["ok"] is True, (
            f"{key} reds: chose {row['shape']} ({row['detail']}), the owner confirmed "
            f"{label['expect']!r}"
        )
        arranged = " | ".join(row["arrange"])
        for needle in label.get("arrange_contains") or []:
            assert needle in arranged, (
                f"{key}: the fixture asks the arrangement to contain {needle!r}, got {arranged!r}"
            )
    assert report["failures"] == 0, (
        f"{report['failures']} label(s) red: {[row['key'] for row in report['labels'] if not row['ok']]}"
    )


# --- the two folder layouts that were provably wrong (Mf-yitm) -------------


def test_a_zip_inside_the_wrapper_child_still_names_the_wrapper(tmp_path: Path) -> None:
    """Case (a): `Vault/` holds the wrapper AND the wrapper holds the zip.

    Before Mf-yitm this printed `none: this path is already a sane folder name`,
    because `Facts.twin_archives()` only scans the candidate's root - so a zip one
    level down was invisible and the whole wrapper went unnoticed. Mutation: revert to
    the root-only scan and this row reds with exactly that before-state.
    """
    vault = build_vault(tmp_path)
    chosen, arrange = chosen_and_arrange(vault)
    assert arrange == [
        f'drop the wrapper "{VAULT_WRAPPER}/" while arranging: its name is the archive\'s, '
        f"not the product's (10 of 13 models inside it)",
        f'file "{VAULT_ZIP}" in "Vault/_source/{VAULT_ZIP}"',
        'keep "Rhino models/" as a part',
        f'move "Vault/{VAULT_WRAPPER}/part0" -> "Vault/part0"',
        f'move "Vault/{VAULT_WRAPPER}/part1" -> "Vault/part1"',
        f'move "Vault/{VAULT_WRAPPER}/part2" -> "Vault/part2"',
        f'move "Vault/{VAULT_WRAPPER}/part3" -> "Vault/part3"',
        f'move "Vault/{VAULT_WRAPPER}/part4" -> "Vault/part4"',
        f'move "Vault/{VAULT_WRAPPER}/part5" -> "Vault/part5"',
        f'move "Vault/{VAULT_WRAPPER}/part6" -> "Vault/part6"',
        f'move "Vault/{VAULT_WRAPPER}/part7" -> "Vault/part7"',
        f'move "Vault/{VAULT_WRAPPER}/part8" -> "Vault/part8"',
        f'move "Vault/{VAULT_WRAPPER}/part9" -> "Vault/part9"',
        f'move "Vault/{VAULT_WRAPPER}/{VAULT_ZIP}" -> "Vault/_source/{VAULT_ZIP}"',
    ], f"expected the wrapper to be dropped and its content moved up, got {arrange!r}"
    assert chosen in proposal_engine.ONE_PROJECT_SHAPES, f"the layout is one project, not {chosen}"
    assert "none: this path is already a sane folder name" not in arrange, (
        "the before-state of this bug: a wrapper was reported as a sane folder name"
    )


def test_the_wrapper_itself_drops_instead_of_being_renamed(tmp_path: Path) -> None:
    """Case (b): the candidate IS the wrapper, its zip one level up.

    Before Mf-yitm this printed only `rename "Vault Archive_18.11.2026" ->
    "Vault Archive 18.11.2026"` - a cosmetic tidy of the packaging, which leaves the
    product unnamed and the zip unfiled. The drop supersedes the rename, and the two
    must never both print. Mutation: revert the branch and this reds with that line.
    """
    wrapper = build_own_wrapper(tmp_path)
    chosen, arrange = chosen_and_arrange(wrapper)
    assert arrange == [
        f'drop the wrapper "{VAULT_WRAPPER}/" while arranging: its name is the archive\'s, '
        f"not the product's (12 models inside it)",
        f'file "{VAULT_ZIP}" in "Vault/_source/{VAULT_ZIP}"',
        'keep "Rhino models/" as a part',
        f'move "{VAULT_WRAPPER}/Rhino models" -> "Vault/Rhino models"',
        f'move "{VAULT_ZIP}" -> "Vault/_source/{VAULT_ZIP}"',
    ], f"expected the candidate to be dropped as the wrapper, got {arrange!r}"
    assert chosen in proposal_engine.ONE_PROJECT_SHAPES
    assert not [line for line in arrange if line.startswith("rename")], (
        "renaming the wrapper is the wrong answer: its name is the archive's, not the product's"
    )
    assert "none: this path is already a sane folder name" not in arrange


def test_the_wrapper_title_is_the_product_not_the_packaging() -> None:
    """`_source/` is filed under the product, so the title must survive its strip."""
    assert proposal_engine.wrapper_title(VAULT_WRAPPER) == "Vault"
    assert proposal_engine.wrapper_title("Vault") == "Vault"
    assert proposal_engine.wrapper_title("Archive Box") == "Archive Box", (
        "a product whose name merely starts with 'Archive' must not be stripped"
    )
    assert proposal_engine.wrapper_title("_Archive 2") == "_Archive 2", (
        "a name the strip would empty falls back to itself: an unnamed project is worse than a "
        "badly named one"
    )


# --- the plan is a move list (Mf-v9eg) -------------------------------------


def test_folder_title_keeps_the_series_prefix() -> None:
    """`17_` is a series index, not clutter: the title keeps it.

    Mutation: strip a leading numeric prefix (the old `rename "17_ClockworkMonster" ->
    "ClockworkMonster"` line) and the owner's 17th Fates End tower loses its place in
    the series - the second of the three contradictory ClockworkMonster lines.
    """
    assert proposal_engine.folder_title("17_ClockworkMonster") == "17_ClockworkMonster"
    assert proposal_engine.folder_title("xbox360_shell") == "xbox360 shell"
    assert proposal_engine.folder_title("Vault Archive_18.11.2026") == "Vault Archive 18.11.2026"
    assert proposal_engine.folder_title("__") == "__", (
        "a name the tidy would empty falls back to itself: an unnamed product is worse"
    )


def test_arrange_moves_hoists_each_model_bearing_child_out_of_a_container(tmp_path: Path) -> None:
    """A split container keeps nothing: each product rises to sit beside it.

    Mutation: leave the children nested (`to` under the container) and every hoisted
    product lands a level deeper than the corpus: `import/Stagetop`,
    `import/17_ClockworkMonster` and `import/endorphin` all sit directly under the drop
    zone, never under a container.
    """
    container = build_container(tmp_path)
    facts = proposal_engine.measure(str(container), "import/Drop")
    assert proposal_engine.arrange_moves(facts, "split-top-folders") == [
        {"from": "import/Drop/Product B", "to": "import/Product B", "kind": "dir", "action": "hoist"},
        {"from": "import/Drop/Product A", "to": "import/Product A", "kind": "dir", "action": "hoist"},
    ], "a loose root model is not a folder to hoist: (root files) must never be a move"
    assert proposal_engine.arrange_moves(facts, "split-model-folders") == [
        {"from": "import/Drop/Product A", "to": "import/Product A", "kind": "dir", "action": "hoist"},
        {"from": "import/Drop/Product B", "to": "import/Product B", "kind": "dir", "action": "hoist"},
    ]


def test_arrange_moves_keeps_the_folder_for_a_project_shape(tmp_path: Path) -> None:
    """`add-to-project`/`git-project` mean the folder stays exactly where it is.

    Mutation: invent a move for these shapes and the folder the operator already owns
    is renamed out from under its own project (the checkout's boundary is the repo).
    """
    write_models(tmp_path / "import" / "Widget" / "part", 2)
    facts = proposal_engine.measure(str(tmp_path / "import" / "Widget"), "import/Widget")
    assert proposal_engine.arrange_moves(facts, "add-to-project") == []
    assert proposal_engine.arrange_moves(facts, "git-project") == []


def test_arrange_moves_files_the_wrapper_archive_under_source(tmp_path: Path) -> None:
    """Case (b): the candidate is the wrapper, so its content rises into the product.

    The two actions here are the discriminating pair for the token mapping: the
    children land in a DIFFERENT, newly named product folder, so they are `move`,
    while the zip is filed as `file-archive`. Mutation: give the children `hoist`
    (the candidate's own name does not survive, so nothing is hoisted) or the zip
    `move` and the UI labels the operation wrongly.

    Mutation: name `_source/` after the wrapper (`Vault Archive_18.11.2026/_source/`)
    and the extracted twin and its own zip disagree about which folder is the product.
    """
    wrapper = build_own_wrapper(tmp_path)
    facts = proposal_engine.measure(str(wrapper), VAULT_WRAPPER)
    assert proposal_engine.arrange_moves(facts, "one-project") == [
        {
            "from": f"{VAULT_WRAPPER}/Rhino models",
            "to": "Vault/Rhino models",
            "kind": "dir",
            "action": "move",
        },
        {
            "from": VAULT_ZIP,
            "to": f"Vault/_source/{VAULT_ZIP}",
            "kind": "file",
            "action": "file-archive",
        },
    ]


def test_arrange_moves_supplies_the_series_title_and_keeps_the_number(tmp_path: Path) -> None:
    """The series move is the only rename, and it keeps `17_`.

    Mutation: render the move from a prefix-stripping title and the folder loses the
    series index; print both a cosmetic rename and the series move and the operator is
    told to rename twice with different answers.
    """
    tower = build_series(tmp_path)
    facts = proposal_engine.measure(str(tower), "import/17_ClockworkMonster")
    assert proposal_engine.series_title(facts) == "17_Fates End - ClockworkMonster"
    assert proposal_engine.series_suggestion(facts) == (
        'rename to "17_Fates End - ClockworkMonster" '
        "(belongs to the Fates End series: 3 siblings)"
    ), "the suggestion text is what callers pin: it must render FROM the title, unchanged"
    moves = proposal_engine.arrange_moves(facts, "one-project")
    assert moves == [{
        "from": "import/17_ClockworkMonster",
        "to": "import/17_Fates End - ClockworkMonster",
        "kind": "dir",
        "action": "rename",
    }], "the series move is the only rename: any other token here mislabels it"
    for move in moves:
        assert re.match(r"^\d{2,}_", os.path.basename(move["to"])), (
            f"{move['to']!r} strips the series number: 17_ is an index, not clutter"
        )


def test_every_move_is_storage_relative(tmp_path: Path) -> None:
    """A move names the storage root's path, never the candidate's or an absolute one.

    Mutation: build `from`/`to` from `facts.path` (or with `..`) and the app applies
    the move against a different tree than the plan measured - the refusal list's
    `outside_root` exists because that is how it starts.
    """
    wrapper = build_own_wrapper(tmp_path / "own")
    container = build_container(tmp_path / "container")
    vault = build_vault(tmp_path / "vault")
    facts_container = proposal_engine.measure(str(container), "import/Drop")
    assert facts_container.rel.startswith("import/"), (
        "this fixture is the nested one: the drop zone is where every move is measured from"
    )
    cases = [
        (proposal_engine.measure(str(wrapper), VAULT_WRAPPER), "one-project"),
        (facts_container, "split-top-folders"),
        (proposal_engine.measure(str(vault), "Vault"), "one-project"),
    ]
    seen = 0
    for facts, shape in cases:
        for move in proposal_engine.arrange_moves(facts, shape):
            seen += 1
            assert move["kind"] in ("dir", "file")
            assert move["action"] in ("hoist", "move", "file-archive", "rename"), (
                f"{move['action']!r} is not an arrange action token"
            )
            for path in (move["from"], move["to"]):
                assert path and not os.path.isabs(path) and not path.startswith("/"), (
                    f"{path!r} is absolute: a move record is storage-relative"
                )
                assert ".." not in path.split("/"), f"{path!r} escapes the storage root"
                assert not path.startswith(str(tmp_path)), (
                    f"{path!r} carries the measured root: the API resolves it against its own"
                )
    assert seen >= 3, "the fixtures must actually produce moves, or this proves nothing"


def test_the_arranged_text_prints_every_move(tmp_path: Path) -> None:
    """Nothing prints that the move list does not back, and no move goes unprinted.

    Mutation: print a move the list does not hold (the old `rename "..." -> "..."` and
    `or keep the sort prefix as-is` lines) and the operator reads a plan the app will
    never apply; keep `(no file moves)` on a split and the text denies its own moves.
    """
    wrapper = build_own_wrapper(tmp_path / "own")
    container = build_container(tmp_path / "container")
    vault = build_vault(tmp_path / "vault")
    tower = build_series(tmp_path / "series")
    facts_container = proposal_engine.measure(str(container), "import/Drop")
    cases = [
        (proposal_engine.measure(str(wrapper), VAULT_WRAPPER), "one-project"),
        (proposal_engine.measure(str(vault), "Vault"), "one-project"),
        (facts_container, "split-top-folders"),
        (facts_container, "split-model-folders"),
        (facts_container, "add-to-project"),
        (facts_container, "git-project"),
        (proposal_engine.measure(str(tower), "import/17_ClockworkMonster"), "one-project"),
    ]
    for facts, shape in cases:
        moves = proposal_engine.arrange_moves(facts, shape)
        lines = proposal_engine.arrange_proposal(facts, shape)
        rendered = "\n".join(lines)
        assert "(no file moves)" not in rendered, f"{shape} still denies its own moves: {rendered}"
        for move in moves:
            assert f'move "{move["from"]}" -> "{move["to"]}"' in lines, (
                f"{shape} plans {move!r} but never prints it: {rendered}"
            )
        printed = [line for line in lines if line.startswith('move "')]
        assert len(printed) == len(moves), (
            f"{shape} printed {len(printed)} move line(s) for {len(moves)} move(s): {rendered}"
        )


def test_arrange_archive_names_one_product_folder(tmp_path: Path) -> None:
    """An archive and its extracted twin must name ONE product folder.

    The zip is the download this folder came out of, so `_source/` and the folder the
    twin is dropped to are the same product. Mutation: drop the `wrapper_title` strip
    and the zip unpacks into `Vault Archive_18.11.2026/` while its twin was arranged
    to `Vault/` - two folders for one product (correction 8).
    """
    vault = build_vault(tmp_path)
    archive = vault / VAULT_WRAPPER / VAULT_ZIP
    facts = proposal_engine.measure_archive(str(archive), f"Vault/{VAULT_WRAPPER}/{VAULT_ZIP}")
    assert proposal_engine.arrange_archive_moves(facts, "archive-project") == [{
        "from": f"Vault/{VAULT_WRAPPER}/{VAULT_ZIP}",
        "to": f"Vault/_source/{VAULT_ZIP}",
        "kind": "file",
    }]
    rendered = "\n".join(proposal_engine.arrange_archive(facts, "archive-project"))
    assert f'"Vault/_source/{VAULT_ZIP}"' in rendered
    assert f'"{VAULT_WRAPPER}/_source/' not in rendered, (
        f"the packaging named the product folder: {rendered}"
    )
    assert proposal_engine.arrange_archive_moves(facts, "archive-split") == [], (
        "only the one-project shape files the archive: a split unpacks several folders"
    )


def test_plan_digest_is_the_same_plan_twice_and_changes_with_any_move() -> None:
    """The plan id is identity: same plan, same id; any edit, a different id.

    Mutation: hash a timestamp, the dict's repr or only the key, and `stale_plan`
    either refuses an unchanged disk or accepts a plan whose moves were rewritten.
    Mutation: fold `action` into the hash and adding a label to a move invalidates
    every plan already on screen.
    """
    moves = [{"from": "import/Drop/Product A", "to": "import/Product A", "kind": "dir"}]
    creates = [{"path": "import/Product A", "exists": True}]
    first = proposal_engine.plan_digest("import/Drop", "split-top-folders", moves, creates)
    assert first == proposal_engine.plan_digest(
        "import/Drop", "split-top-folders", list(moves), list(creates)
    ), "the same plan must hash the same on the second call"
    assert first == proposal_engine.plan_digest(
        "import/Drop", "split-top-folders", [{**moves[0], "action": "hoist"}], creates
    ), "`action` says why the move exists; the digest hashes what it does, so it is ignored"
    assert re.fullmatch(r"[0-9a-f]{64}", first), f"a sha256 hex digest, got {first!r}"
    changed = [
        proposal_engine.plan_digest("import/Drop", "one-project", moves, creates),
        proposal_engine.plan_digest("import/Other", "split-top-folders", moves, creates),
        proposal_engine.plan_digest("import/Drop", "split-top-folders", [], creates),
        proposal_engine.plan_digest("import/Drop", "split-top-folders", [
            {"from": "import/Drop/Product B", "to": "import/Product A", "kind": "dir"},
        ], creates),
        proposal_engine.plan_digest("import/Drop", "split-top-folders", [
            {"from": "import/Drop/Product A", "to": "import/Product B", "kind": "dir"},
        ], creates),
        proposal_engine.plan_digest("import/Drop", "split-top-folders", [
            {"from": "import/Drop/Product A", "to": "import/Product A", "kind": "file"},
        ], creates),
        proposal_engine.plan_digest("import/Drop", "split-top-folders", moves, []),
    ]
    assert len(set(changed)) == len(changed), "two different plans collided"
    assert first not in changed, "an edited plan kept the id of the plan it replaced"


# --- git is behind a switch, and validate() is not a printer ---------------


def test_probe_git_false_never_reaches_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A GET handler may not run a subprocess, so the call must be skippable.

    Mutation: call `git_state` unconditionally and the first request to a proposal
    route forks `git status` on a live mount - and this test, which makes the call
    explode, is the only thing that says so.
    """
    write_models(tmp_path / "Product", 2)
    facts = proposal_engine.measure(str(tmp_path / "Product"), "Product")
    calls: list[str] = []

    def explode(path: str, timeout: float | None = None) -> None:
        calls.append(path)
        raise AssertionError("git_state must not be called when probe_git=False")

    monkeypatch.setattr(proposal_engine, "git_state", explode)
    options, _reasons, repo = proposal_engine.propose(facts, (), probe_git=False)
    assert repo is None, "a skipped probe leaves no repo, not a half-read one"
    assert options, "the interpretations must be unchanged by the switch"
    assert calls == [], "the engine shelled out to git on a request path"
    with pytest.raises(AssertionError, match="probe_git=False"):
        proposal_engine.propose(facts, ())
    assert calls == [str(facts.path)], "the default path must still ask git"


def test_git_timeout_reaches_git_state_and_none_leaves_it_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report's bounded probe is the point of the kwarg (Mf-1agz).

    A hung checkout must cost the report at most `git_timeout` seconds, not
    `GIT_TIMEOUT_SECONDS`. Mutation: drop the kwarg from the `git_state` call (or
    ignore it) and the report is back to a 120 s tail inside a GET; this test is
    what says so.
    """
    write_models(tmp_path / "Product", 2)
    facts = proposal_engine.measure(str(tmp_path / "Product"), "Product")
    seen: list[float | None] = []

    def capture(path: str, timeout: float | None = None):
        seen.append(timeout)
        return None

    monkeypatch.setattr(proposal_engine, "git_state", capture)
    proposal_engine.propose(facts, (), git_timeout=5.0)
    proposal_engine.propose(facts, ())
    assert seen == [5.0, None], f"the bound must reach git_state verbatim: {seen}"


def test_the_rule_set_stamp_is_pinned_so_a_table_edit_cannot_go_unnoticed() -> None:
    """The ONE engine stamps its own tables (Mf-1agz).

    A report row records this stamp, so two reports are comparable by construction or
    explicitly not. Mutation: edit any row-deciding table and this pin reds, which is
    the whole point - the alternative is finding out months later that two artifacts
    were never describing the same rules. An intentional change updates the pin in the
    same commit that changes the table.
    """
    assert proposal_engine.RULE_SET_VERSION == "2"
    assert proposal_engine.RULE_SET_STAMP == proposal_engine.rule_set_stamp()
    assert proposal_engine.RULE_SET_STAMP == (
        "061c9b969c3f5debe357fe8ff96f5e29857a8731fed4d7fb5daf078c37cebc36"
    ), "a rule table changed: update this pin in the same commit, on purpose"
    assert proposal_engine.rule_set() == {
        "version": "2", "stamp": proposal_engine.RULE_SET_STAMP,
    }


def test_a_table_edit_moves_the_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stamp is read from the module NOW, not frozen at import (Mf-1agz).

    Mutation: compute the stamp once into a literal and the pin above becomes the only
    protection - a test that changes one table and re-derives the stamp is what proves
    the table is actually inside it.
    """
    before = proposal_engine.rule_set_stamp()
    monkeypatch.setattr(proposal_engine, "GROUPS_MAX_CHILDREN", 63)
    assert proposal_engine.rule_set_stamp() != before
    monkeypatch.setattr(proposal_engine, "BOILERPLATE_TITLES", set())
    assert proposal_engine.rule_set_stamp() != before


def test_archive_payload_contained_keeps_both_pairing_routes_one_predicate() -> None:
    """THE containment predicate, at the engine, for both routes (Mf-1agz).

    The stem route decides its own wrapper once; the entry-root route is told which one
    to strip, and must never fall back to a second guess. Both are asserted here because
    `bundle_service`'s pairing now calls this function for both, so a divergence between
    the routes would be a pairing that disagrees with its own evidence.
    """
    folder = [("a.stl", 10), ("b.stl", 20), ("notes.txt", 5)]
    # identity, then ONE strip
    assert proposal_engine.archive_payload_contained(folder, folder) is True
    assert proposal_engine.archive_payload_contained(
        [("Wrapper/a.stl", 10), ("Wrapper/b.stl", 20)], folder
    ) is True
    # cruft is never payload
    assert proposal_engine.archive_payload_contained(
        [("__MACOSX/._a.stl", 3), ("a.stl", 10)], folder
    ) is True
    # two strips are refused on purpose
    assert proposal_engine.archive_payload_contained(
        [("Outer/Inner/a.stl", 10)], folder
    ) is False
    # a size mismatch is not containment
    assert proposal_engine.archive_payload_contained([("a.stl", 11)], folder) is False
    # an entry outside a NAMED root is refused, with no second strip
    assert proposal_engine.archive_payload_contained(
        [("Wrapper/a.stl", 10), ("loose.stl", 10)],
        folder + [("a.stl", 10), ("loose.stl", 10)],
        entry_root="Wrapper",
    ) is False
    # the named root route strips exactly that root
    assert proposal_engine.archive_payload_contained(
        [("Wrapper/a.stl", 10), ("Wrapper/b.stl", 20)], folder, entry_root="wrapper"
    ) is True
    # an archive of cruft alone has verified nothing
    assert proposal_engine.archive_payload_contained([("__MACOSX/._a.stl", 3)], folder) is False
    assert proposal_engine.archive_payload_contained([], folder) is False



# --- the pairing vocabulary the engine owns (moved from test_bundle_detect) ---
#
# The renamed extraction this vocabulary exists for: the directory and its archive
# share nothing but an ordinal prefix, so the archive's entry root is the evidence.
CLOCKWORK_ROOT = "17_ClockworkMonster"
#
# Mf-1agz deleted `bundle_detect`; these predicates and their assertions moved to
# the module that owns them, unchanged apart from the name in front of the dot.


def contained(
    archive: List[Tuple[str, int]],
    folder: List[Tuple[str, int]],
    entry_root: Optional[str] = None,
) -> bool:
    """``archive_payload_contained`` from two ``(rel_path, size)`` literals."""
    return proposal_engine.archive_payload_contained(archive, folder, entry_root)


@pytest.mark.parametrize(
    ("token", "display", "url"),
    [
        ("cc-by", "CC BY", "https://creativecommons.org/licenses/by/4.0/"),
        ("CC BY", "CC BY", "https://creativecommons.org/licenses/by/4.0/"),
        ("cc-by-sa", "CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
        ("cc-by-nc", "CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
        ("cc-nc", "CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
        ("cc-by-nc-sa", "CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
        ("cc-nc-sa", "CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
        ("cc-by-nd", "CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
        ("cc-by-nc-nd", "CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
        ("cc-nc-nd", "CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
        ("cc0", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
        ("cc-zero", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
        ("public-domain", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    ],
)
def test_licence_from_token_maps_every_known_token(token: str, display: str, url: str) -> None:
    assert proposal_engine.licence_from_token(token) == (display, url)


def test_licence_from_token_never_invents_a_url() -> None:
    assert proposal_engine.licence_from_token("WTFPL") == ("WTFPL", None)
    assert proposal_engine.licence_from_token(None) == (None, None)


def test_twin_key_casefolds_and_drops_one_trailing_zip() -> None:
    assert proposal_engine.twin_key("Moai_Statue.zip") == "moai_statue"
    assert proposal_engine.twin_key("  MOAI_STATUE.ZIP ") == "moai_statue"
    assert proposal_engine.twin_key("Moai_Statue") == "moai_statue"
    assert proposal_engine.twin_key("archive.zip.zip") == "archive.zip", "only one suffix is removed"


def test_common_entry_root_is_the_one_top_level_directory() -> None:
    paths = [f"{CLOCKWORK_ROOT}/Clockwork_Part{index:02d}.stl" for index in range(3)]

    root = proposal_engine.common_entry_root(paths)

    assert root == CLOCKWORK_ROOT


def test_common_entry_root_carries_the_first_seen_casing() -> None:
    assert proposal_engine.common_entry_root(["17_ClockworkMonster/a.stl", "17_clockworkmonster/b.stl"]) == (
        "17_ClockworkMonster"
    )


def test_common_entry_root_of_mixed_roots_is_none() -> None:
    assert proposal_engine.common_entry_root(["17_Thing/a.stl", "Other Thing/b.stl"]) is None


def test_common_entry_root_of_a_top_level_file_is_none() -> None:
    assert proposal_engine.common_entry_root(["17_Thing/a.stl", "README.txt"]) is None


def test_common_entry_root_of_no_entries_is_none() -> None:
    assert proposal_engine.common_entry_root([]) is None


def test_common_entry_root_ignores_the_macosx_artifact() -> None:
    paths = [
        "__MACOSX/17_Thing/._a.stl",
        "__MACOSX/17_Thing/._preview.jpg",
        "17_Thing/a.stl",
        "17_Thing/preview.jpg",
    ]

    assert proposal_engine.common_entry_root(paths) == "17_Thing"


def test_common_entry_root_of_only_macosx_entries_is_none() -> None:
    assert proposal_engine.common_entry_root(["__MACOSX/17_Thing/._a.stl"]) is None


# Moved to test_file_utils.py with is_generic_container_root (Mf-1agz).


def test_archive_payload_contained_accepts_an_identity_payload() -> None:
    """A flat archive written over the same paths needs no strip at all."""
    archive = [("part.stl", 40), ("preview.jpg", 20)]

    assert contained(archive, [("part.stl", 40), ("preview.jpg", 20)]), "identity: the paths already agree"


def test_archive_payload_contained_strips_one_wrapper() -> None:
    """The archive's own entries name the wrapper; one strip makes them agree."""
    assert contained([("SnVB623/part.stl", 40)], [("part.stl", 40)]), "one common entry root is stripped"


def test_archive_payload_contained_strips_a_generic_wrapper_on_the_stem_route() -> None:
    """``is_generic_container_root`` is the entry-root route's rule, not this one.

    The name only says nothing about WHICH bundle an archive holds, and it is
    consulted where a root is used to NAME a directory. Here nobody names a
    directory with it - the folder is the stem twin already - so ``files/`` is
    just a wrapper like any other and is stripped once.
    """
    assert contained([("files/part.stl", 40)], [("part.stl", 40)]), (
        "a generic root is refused where it names a directory, not where it is stripped"
    )


def test_archive_payload_contained_is_containment_not_equality() -> None:
    """A lived-in directory legitimately holds more than the archive shipped."""
    archive = [("SnVB623/part.stl", 40)]
    folder = [("part.stl", 40), ("spare.stl", 41), ("render.png", 20)]

    assert contained(archive, folder), "one extra file on disk does not unpair a verified archive"


def test_archive_payload_contained_refuses_two_strips() -> None:
    """The wrapper decision is made once; a wrapper's wrapper is not stripped."""
    archive = [("outer/files/part.stl", 40)]

    assert contained(archive, [("part.stl", 40)]) is False, "two strips would pair a nested thing with anything"
    assert contained(archive, [("files/part.stl", 40)]), "exactly one strip is the rule"


def test_archive_payload_contained_refuses_a_size_mismatch() -> None:
    """Declared size is header data; a lie in it makes the pairing wrong, so refuse."""
    assert contained([("SnVB623/part.stl", 40)], [("part.stl", 41)]) is False


def test_archive_payload_contained_refuses_a_name_mismatch() -> None:
    assert contained([("SnVB623/part.stl", 40)], [("other.stl", 40)]) is False


def test_archive_payload_contained_ignores_macosx_cruft() -> None:
    """AppleDouble entries are not payload, and cruft alone has verified nothing."""
    assert contained([("__MACOSX/._part.stl", 5), ("part.stl", 40)], [("part.stl", 40)]), (
        "the cruft is dropped before the comparison"
    )
    assert contained([("__MACOSX/._part.stl", 5)], [("part.stl", 40)]) is False


def test_archive_payload_contained_without_one_common_root_refuses() -> None:
    """No single wrapper, and no identity, means there is no strip to make."""
    assert contained([("A/part.stl", 40), ("B/other.stl", 41)], [("part.stl", 40)]) is False, (
        "two wrappers: there is no single entry root"
    )
    assert contained([("part.stl", 40), ("A/other.stl", 41)], [("part.stl", 40)]) is False, (
        "a top-level file beside a wrapped one is no single root either"
    )


def test_archive_payload_contained_with_an_entry_root_is_the_strict_strip() -> None:
    """The named root is stripped without any identity shortcut."""
    assert contained([("SnVB623/part.stl", 40)], [("part.stl", 40)], entry_root="SnVB623"), (
        "every entry sits under the named root"
    )
    assert contained(
        [("SnVB623/part.stl", 40), ("Other/x.stl", 1)], [("part.stl", 40)], entry_root="SnVB623"
    ) is False, "an entry outside the root is not this archive's payload"
    assert contained([("part.stl", 40)], [("part.stl", 40)], entry_root="SnVB623") is False, (
        "no identity shortcut on the entry-root route: a flat archive must not widen Mf-6b82"
    )


def test_validate_returns_rows_and_prints_nothing(labelled_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The engine answers, the shim renders - and the split is observable.

    Mutation: put the `print`s back in `validate()` and the CLI still looks right while
    the API silently writes to a request log, so the silence is asserted here and the
    rendering is asserted on the shim.
    """
    labels = disk_labels()
    report = proposal_engine.validate(str(labelled_root), [], [], labels)
    for key in ("agree", "disagree", "missing", "archives", "archive_scored", "labels", "failures"):
        assert key in report, f"the report must carry {key!r}: the CLI and the API both read it"
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "", (
        f"validate() printed {captured.out + captured.err!r}: rendering belongs to the shim"
    )
    assert report["failures"] == 0


def test_the_shim_renders_the_report_it_is_given(shim: ModuleType, labelled_root: Path) -> None:
    """The printed `--validate` text is the shim's job, and it marks each label."""
    labels = disk_labels()
    report = proposal_engine.validate(str(labelled_root), [], [], labels)
    rendered = shim.render_validation(report)
    assert "confirmed split labels:" in rendered
    assert rendered.count("  OK ") == len(labels), (
        f"every confirmed label must print an OK row, got:\n{rendered}"
    )
    assert "BAD " not in rendered, f"a label red:\n{rendered}"
