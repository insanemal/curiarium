"""The API image must be a function of the commit, not of the machine that built it.

A working tree's `.venv` and `.ruff_cache` are local state, and `/data` is the
runtime data directory the image creates for itself, so none of them are build
inputs. When an entry is dropped from `.dockerignore` the same commit produces a
differently-shaped image on two machines - a virtualenv carrying the builder's host
path in `pyvenv.cfg` and its console-script shebangs, a linter cache, or somebody's
runtime data copied in ahead of what the image would generate. That is the build
correctness bug Mf-uwqr is about, and it is what this module pins.

The static half runs everywhere and asserts only that the entries exist: it is a
pin, not a formatter, so beyond dropping comments and surrounding whitespace it
imposes no layout on the file. The image half is gated on the lint/test image, where
the entries' consequences are observable; the gate is explained on `IN_TEST_IMAGE`.
"""

import os
import shutil
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]

# Docker matches these patterns against the build-context root, so the entries are
# spelled as the API context sees them; `/data` and `data` mean the same thing here.
REQUIRED_ENTRIES = frozenset({".venv", ".ruff_cache", "/data"})

# Positive control: a truncated or emptied ignore file must not read as a pass.
PRE_EXISTING_ENTRIES = frozenset({"__pycache__", "*.db", "storage", ".env", ".pytest_cache"})

# Both markers are needed. `ruff` ships only in the `testdeps` stage, so it is on PATH
# in the lint/test images but never in `runtime` (which is `FROM base`). That matters
# because `runtime` additionally runs `mkdir -p /app/data`, so `/app/data` exists there
# by construction and an absence assertion about it cannot run in that stage.
#
# `.ruff_cache` is deliberately NOT asserted here even though it is ignored: the lint
# stage runs `ruff check .` after `COPY . .`, so ruff recreates the directory in the
# image whatever the ignore file says (measured). The entry still matters - `runtime`
# is `FROM base` and never runs ruff, so without it a builder's cache is baked into
# the shipped image - but its consequence is not observable from the test image.
IN_TEST_IMAGE = os.path.exists("/app/main.py") and shutil.which("ruff") is not None

IN_TEST_IMAGE_REQUIRED = pytest.mark.skipif(
    not IN_TEST_IMAGE,
    reason="only the API lint/test image can prove the ignores took effect (Mf-uwqr)",
)


def dockerignore_entries() -> set[str]:
    """The ignore file's entries, without comments, blank lines or trailing slashes."""
    entries: set[str] = set()
    for raw in (API_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line.rstrip("/"))
    return entries


def test_the_dockerignore_declares_the_build_inputs() -> None:
    """The entries that keep builder state out of the image are still declared.

    Mutation: delete `.venv`, `.ruff_cache` or `/data` from `.dockerignore` and a
    builder's virtualenv, linter cache or runtime data dir is copied into the image
    again, so the same commit builds differently per machine (Mf-uwqr).
    """
    entries = dockerignore_entries()
    missing = REQUIRED_ENTRIES - entries
    assert not missing, (
        f"curiarium/api/.dockerignore must keep {sorted(missing)} or the image depends on the builder (Mf-uwqr); "
        f"it declares {sorted(entries)}"
    )
    lost = PRE_EXISTING_ENTRIES - entries
    assert not lost, f"curiarium/api/.dockerignore lost pre-existing entries {sorted(lost)} (Mf-uwqr)"


@IN_TEST_IMAGE_REQUIRED
def test_the_test_image_contains_no_builder_state() -> None:
    """In the image the ignored paths are absent, and the tree itself did arrive.

    Mutation: drop an entry from `.dockerignore` and the matching path reappears
    under `/app`, so the shipped image carries a builder's local state again
    (Mf-uwqr). The positive controls make an empty or failed `COPY` red while the
    discriminator - `/app/.venv` - is the reason the ticket exists.

    Ruff is not the discriminator and must not be used as one: its default excludes
    hide a copied virtualenv, so `RUN ruff check .` passes either way (Mf-uwqr).
    """
    assert Path("/app/main.py").exists(), "the API image must contain /app/main.py (Mf-uwqr)"
    assert Path("/app/.dockerignore").exists(), (
        "the ignore file itself must still be copied into the image: it is not self-excluded (Mf-uwqr)"
    )
    assert Path("/app/tests/test_api_smoke.py").exists(), (
        "the image must carry the source tree, so an empty or failed COPY cannot read as a pass (Mf-uwqr)"
    )
    assert not Path("/app/.venv").exists(), (
        "/app/.venv must not exist: a builder's virtualenv is not a build input, and copying it bakes the "
        "builder's host paths and console-script shebangs into the image (Mf-uwqr)"
    )
    assert not Path("/app/data").exists(), (
        "/app/data must not exist: this image never creates it, so its presence means host runtime data was "
        "copied in ahead of what the image would generate (Mf-uwqr)"
    )
