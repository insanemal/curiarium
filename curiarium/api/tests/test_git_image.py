"""The API image ships git, asserted IN the image where it used to be silently skipped.

The proposal engine's git probe is what keeps a real checkout from being split, so
the capability is a runtime feature and not a convenience: remove `git` from the
Dockerfile's apt line and `test_the_image_ships_git` reds here instead of a feature
quietly disabling itself. The synthetic-checkout tests exercise the same checkout
shapes the matrix uses, but inside the image, where the old skip would have hidden
the bug Mf-ky16 was filed for.

The image's WORKDIR is `/app`, so `/app/Dockerfile` is the discriminator: the image
is /app, and a skip there would be the exact pathology this ticket kills. On a
developer host the tests skip; on the image they must pass.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_proposal_engine_matrix import facts_for, git, new_repo, write, write_bytes, write_models

import bundle_service
import proposal_engine

IN_IMAGE = os.path.exists("/app/Dockerfile")
IN_IMAGE_REQUIRED = pytest.mark.skipif(
    not IN_IMAGE, reason="only the API image can prove the image ships git (Mf-ky16)"
)


@IN_IMAGE_REQUIRED
def test_the_image_ships_git() -> None:
    """The binary exists on PATH where the probe runs.

    Mutation: remove `git` from the Dockerfile's apt line and the engine's probe is
    silently dead in production again - this reds instead of a feature disappearing.
    """
    assert shutil.which("git") is not None, (
        "the API image must ship git: the proposal engine's git probe is silently dead without it (Mf-ky16)"
    )


def build_endorphin_checkout(path: Path) -> Path:
    """A tracked payload plus untracked ingredient folders, exactly like endorphin.

    The models are committed, so git declares the repo's own boundary; each
    ingredient is untracked, carries its own README + LICENSE and a matching `.zip`
    sibling, and is never committed. This is the shape whose structural reading is a
    split but whose git reading is one project.
    """
    repo = new_repo(path)
    write_models(repo, 4)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    for name in ("Ingredient One", "Ingredient Two", "Ingredient Three"):
        write(repo / name / "README.md", f"# {name}\n")
        write(repo / name / "LICENSE", "CC BY-NC-SA\n")
        write_models(repo / name, 2)
        write_bytes(repo / f"{name}.zip", b"PK\x03\x04")
    return repo


@IN_IMAGE_REQUIRED
def test_endorphin_shape_reads_as_one_project_in_the_image(tmp_path: Path) -> None:
    """A real checkout reads as `git-project` in the image, note-free and creatable.

    Mutation: let the image lack git and this row falls to the structural split that
    Mf-ky16 was filed for; `_match_row` must carry no note for a boundary git could
    read.
    """
    repo = build_endorphin_checkout(tmp_path / "endorphin")
    facts = facts_for(repo)

    state = proposal_engine.git_state(str(repo))
    assert isinstance(state, dict)
    assert state["git_error"] is None
    assert any(name.endswith(".stl") for name in state["tracked"]), state["tracked"]

    options, _reasons, _repo = proposal_engine.propose(facts, ())
    assert options[0].shape == "git-project"
    assert options[0].confidence == "high"

    row = bundle_service._match_row(
        options[0],
        bundle_service.BundleCandidate(
            key="import/endorphin",
            rel="import/endorphin",
            facts=facts,
            options=tuple(options),
            is_archive=False,
        ),
    )
    assert row["note"] is None
    assert row["creatable"] is True


@IN_IMAGE_REQUIRED
def test_an_unreadable_boundary_is_one_project_never_a_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same checkout with an unreadable probe is UNKNOWN, never a split.

    Mutation: let a missing binary fall back to the structural guess and a real
    checkout is split into 8 projects in the shipping image - the exact bug Mf-ky16
    was filed for. The fixture is built BEFORE the patch because building it needs a
    real binary; the probe is the only thing the patch makes impossible.
    """
    repo = build_endorphin_checkout(tmp_path / "endorphin")
    facts = facts_for(repo)

    def raiser(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", raiser)

    options, _reasons, repo_state = proposal_engine.propose(facts, ())
    assert repo_state is not None and repo_state["git_error"] == proposal_engine.GIT_NOT_AVAILABLE
    assert options[0].shape == "one-project"
    assert {option.shape for option in options} & {"split-top-folders", "split-model-folders"} == set()
    assert "could not read it" in options[0].note
    assert "not available" in options[0].note
