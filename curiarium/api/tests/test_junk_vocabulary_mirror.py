"""Bind the browser's junk mirror to the Python authority (Mf-ls9v / Mf-znu1).

``curiarium/web/src/lib/zipJunk.ts`` is hand-maintained, and it is not only a
mirror: ``curiarium/api/entry_policy.py`` decides what the server may import,
but the browser decides what the import modal pre-ticks AND what a locally
picked zip may upload. A locally picked zip is never listed server-side - its
entries are read in the browser and uploaded as ordinary files through the plain
upload route, so it never meets ``is_junk_entry`` at all. Drift therefore has a
sharp edge: a path this file calls junk and the mirror does not would be
pre-ticked and then, on a locally picked zip, uploaded for real. The mirror is
the only thing stopping that, so deleting it would regress a refusal that has
already shipped.

This module replaces the Makefile gate that used to compare the two files.

Honest residual, because a guard that oversells itself is worse than none:

* it binds the *vocabulary* (the three tables) and the two *test-vector sets*,
  not the matching logic. The two predicates are compared only through their
  verdicts on the vectors;
* ``None -> False`` exists only on the Python side. ``is_junk_entry`` is total
  and answers ``False`` for ``None``; ``isJunkZipPath`` has no ``None`` case and
  reads a missing path through its own ``(path || "")``, which is not the same
  expression as ``if not path: return False``. Any future divergence between
  those two forms is invisible here;
* sets are compared, not order: both files are edited by hand and order is not
  a contract, only membership is.

Stdlib only (``ast``, ``re``, ``pathlib``, ``pytest``) and no import of
``entry_policy``: that module pulls FastAPI and sqlmodel, which a bare host
python running this guard does not need to have.

Host-only, like ``test_auth_docs.py`` and ``test_snapshot_zfs_docs.py``: the API
image builds from ``curiarium/api``, so ``curiarium/web/`` is absent in-container
and this module skips itself. ``Path(__file__).resolve().parents[3]`` *raises*
``IndexError`` there (the build context is shallower than the repository), which
is why the index is guarded rather than only the path's existence.
"""

import ast
import re
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

import pytest

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[3] if len(HERE.parents) > 3 else None
API_ROOT = HERE.parents[1]

POLICY_PATH = API_ROOT / "entry_policy.py"
VECTOR_PATH = API_ROOT / "tests" / "test_zip_junk.py"
MIRROR_PATH: Optional[Path] = (
    REPO_ROOT / "curiarium" / "web" / "src" / "lib" / "zipJunk.ts" if REPO_ROOT is not None else None
)
MIRROR_TEST_PATH: Optional[Path] = (
    REPO_ROOT / "curiarium" / "web" / "src" / "lib" / "zipJunk.test.ts" if REPO_ROOT is not None else None
)

if (
    MIRROR_PATH is None
    or MIRROR_TEST_PATH is None
    or not MIRROR_PATH.exists()
    or not MIRROR_TEST_PATH.exists()
):
    pytest.skip(
        "the zip junk mirror guard is host-only (the API image builds from curiarium/api, so "
        "curiarium/web/src/lib/zipJunk.ts is absent)",
        allow_module_level=True,
    )

TABLE_NAMES = ("JUNK_SEGMENTS", "JUNK_BASENAMES", "JUNK_PREFIXES")
# The Python side spells the two basename/segment tables as frozensets and the
# prefixes as a tuple; the browser spells two as `new Set([...])` and the
# prefixes as a plain array. Membership is what is compared, so both are set().
_SET_DECLARATION = r"const\s+{name}\s*=\s*new\s+Set\(\[(.*?)\]\)"
_ARRAY_DECLARATION = r"const\s+{name}\s*(?::[^=\n]*)?=\s*\[(.*?)\]"
# A vector is a double-quoted literal and nothing else; the body is scanned for
# those literals only, so a comment between vectors cannot be read as data.
_TS_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _decode_ts_string(raw: str) -> str:
    """Decode one double-quoted TypeScript literal body.

    Only ``\\\\`` and ``\\"`` are understood, and anything else fails the test on
    purpose. A vector is data, not code: if a future vector needs ``\\u``, ``\\n``
    or an escaped quote the two sides disagree about, this guard must be updated
    by hand rather than compare two strings that only look equal.
    """
    decoded: List[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != "\\":
            decoded.append(char)
            index += 1
            continue
        index += 1
        if index >= len(raw):
            pytest.fail(f"dangling backslash in a TypeScript vector: {raw!r}")
        escape = raw[index]
        if escape == "\\":
            decoded.append("\\")
        elif escape == '"':
            decoded.append('"')
        else:
            pytest.fail(
                f"unsupported escape \\{escape} in a TypeScript vector ({raw!r}); extend "
                "_decode_ts_string so the guard compares the string the browser actually sees"
            )
        index += 1
    return "".join(decoded)


def _ts_strings(body: str) -> FrozenSet[str]:
    return frozenset(_decode_ts_string(match.group(1)) for match in _TS_STRING.finditer(body))


def _ts_declaration_body(name: str, pattern: str, source: str) -> str:
    match = re.search(pattern.format(name=name), source, re.DOTALL)
    assert match is not None, f"zipJunk.ts no longer declares {name} in the expected form ({pattern})"
    return match.group(1)


def _mirror_table(name: str) -> FrozenSet[str]:
    """One table out of ``zipJunk.ts``, whichever of the two spellings it uses."""
    source = _read(MIRROR_PATH)  # type: ignore[arg-type]
    body = None
    for pattern in (_SET_DECLARATION, _ARRAY_DECLARATION):
        match = re.search(pattern.format(name=name), source, re.DOTALL)
        if match is not None:
            body = match.group(1)
            break
    assert body is not None, f"zipJunk.ts no longer declares {name}"
    return _ts_strings(body)


def _policy_node(node: ast.expr) -> object:
    """The literal a table's right-hand side stands for: ``frozenset(<literal>)`` or a literal."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset":
        assert len(node.args) == 1, "entry_policy's frozenset table no longer takes exactly one literal"
        return ast.literal_eval(node.args[0])
    return ast.literal_eval(node)


def _policy_tables() -> Dict[str, FrozenSet[str]]:
    tree = ast.parse(_read(POLICY_PATH))
    tables: Dict[str, FrozenSet[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in TABLE_NAMES:
                tables[target.id] = frozenset(_policy_node(node.value))  # type: ignore[arg-type]
    return tables


def _vector_pairs() -> List[Tuple[object, object]]:
    """The ``(path, expected)`` table out of ``test_zip_junk.py``, via ``ast``."""
    tree = ast.parse(_read(VECTOR_PATH))
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or len(decorator.args) < 2:
                continue
            names = decorator.args[0]
            if not isinstance(names, ast.Tuple):
                continue
            if ast.literal_eval(names) != ("path", "expected"):
                continue
            return list(ast.literal_eval(decorator.args[1]))
    pytest.fail(f"{VECTOR_PATH.name} no longer has a @pytest.mark.parametrize((\"path\", \"expected\"), [...]) table")


def _policy_verdicts() -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """The Python vectors split into junk and payload paths.

    The ``None`` vector is excluded explicitly: Python's predicate is total and
    answers ``False`` for ``None``, while the mirror has no ``None`` case at all.
    That is the documented residual of this guard, so the exclusion is asserted
    to be live rather than silently shadowing a vector that was retyped.
    """
    junk: set = set()
    payload: set = set()
    none_vectors = 0
    for path, expected in _vector_pairs():
        if path is None:
            none_vectors += 1
            continue
        assert isinstance(path, str), f"unexpected vector path {path!r}"
        (junk if expected is True else payload).add(path)
    assert none_vectors, "the Python vector table no longer carries a None vector, so the exclusion is dead"
    return frozenset(junk), frozenset(payload)


def _mirror_vectors(name: str) -> FrozenSet[str]:
    source = _read(MIRROR_TEST_PATH)  # type: ignore[arg-type]
    return _ts_strings(_ts_declaration_body(name, _ARRAY_DECLARATION, source))


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_the_three_tables_agree(table_name: str) -> None:
    policy_table = _policy_tables().get(table_name)
    assert policy_table is not None, f"entry_policy.py no longer declares {table_name} at module level"

    assert policy_table == _mirror_table(table_name)


def test_the_junk_vector_set_agrees() -> None:
    """Every path Python calls junk is a path the browser calls junk, and vice versa."""
    junk, _ = _policy_verdicts()

    assert junk == _mirror_vectors("JUNK_PATHS")


def test_the_payload_vector_set_agrees() -> None:
    """The other direction: a path either side admits must be admitted on both."""
    _, payload = _policy_verdicts()

    assert payload == _mirror_vectors("PAYLOAD_PATHS")


def test_the_mirror_files_are_the_ones_the_policy_names() -> None:
    """The pointers run both ways, so neither file can be renamed out from under the guard."""
    policy = _read(POLICY_PATH)
    assert "zipJunk.ts" in policy, "entry_policy.py no longer points at its browser mirror"
    assert "test_junk_vocabulary_mirror.py" in policy, "entry_policy.py no longer names this guard"

    mirror = _read(MIRROR_PATH)  # type: ignore[arg-type]
    assert "entry_policy.py" in mirror, "zipJunk.ts no longer points at the Python authority"
    assert "test_junk_vocabulary_mirror.py" in mirror, "zipJunk.ts no longer names this guard"
