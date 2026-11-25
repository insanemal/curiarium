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
* the engine is imported by the API process, so one `import fastapi` there would
  make the offline tight loop (a bare host python against a live mount)
  unimportable. The audit is mechanical, not a comment;
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
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

import bundle_detect
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
    "bundle_detect", "bundle_service", "file_utils", "zip_service",
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


# --- the engine is stdlib-only and app-free -------------------------------


def test_the_engine_imports_nothing_but_the_stdlib() -> None:
    """No app import may reach the engine: it is imported by a bare host python.

    Mutation: `import fastapi` (or `db`, or `bundle_detect`) at the top of
    `proposal_engine.py` makes the offline tight loop - the whole point of the tool -
    fail to start on a machine that has no app dependencies installed, and the API
    keeps working, so nothing else notices.
    """
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    top_level = {
        alias.name.split(".")[0]
        for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in tree.body if isinstance(node, ast.ImportFrom)
    }
    assert top_level == {"os", "re", "typing"}, (
        f"the engine's top-level imports are {sorted(top_level)}: stdlib-only was pinned to "
        f"os, re and typing, and subprocess/zipfile/io belong inside the one function using them"
    )
    every = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            every.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise AssertionError("the engine must not import relatively: it has no package")
            every.add((node.module or "").split(".")[0])
    assert every <= set(sys.stdlib_module_names), (
        f"the engine imports non-stdlib modules {sorted(every - set(sys.stdlib_module_names))}"
    )
    assert every & set(APP_MODULES) == set(), (
        f"the engine imports app modules {sorted(every & set(APP_MODULES))}: they drag the "
        f"session and HTTP world into a read-only walk and break the host tight loop"
    )


def test_the_engine_model_table_is_a_deliberate_superset() -> None:
    """Containment, not equality: the proposal may name a payload no importer accepts.

    The engine answers "what is in this folder", so a Rhino or LightBurn file is real
    payload even though no slicer here opens it - that difference is exactly what the
    proposals exist to expose. `curiarium/scripts/check_model_table.py` pins EQUALITY
    for the four copies that must agree with `file_utils.MODEL_MIME_BY_SUFFIX`; this
    set is not one of them and that gate must not be widened to cover it.
    """
    assert bundle_detect.MODEL_SUFFIXES <= proposal_engine.MODEL_EXTS, (
        "the engine must accept every suffix the app imports, or a proposal would deny a "
        "payload the importer happily stores"
    )
    wider = proposal_engine.MODEL_EXTS - bundle_detect.MODEL_SUFFIXES
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
    ], f"expected the wrapper to be dropped and the zip filed, got {arrange!r}"
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

    def explode(path: str) -> None:
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
