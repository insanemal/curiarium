"""Guard publish.yml's published-identity check against being quietly undone.

The publish job stamps ``org.opencontainers.image.revision`` from the runner
environment and then reads it back out of GHCR, by digest, for every leg. Each of
those facts rests on a detail a plain YAML edit can erase without anything else
noticing: metadata-action gaining a ``context:``, the check sliding from the pushed
digest to a moving tag, the label read dropping off the per-platform ``.Image`` map
onto the annotation-less index, the platform-count guard loosening or drifting away
from the build step's ``platforms:``, the step gaining ``continue-on-error``. Every
one of them is asserted here as structure, so the workflow keeps saying what was
actually measured against the live registry.

Host-only, like ``test_auth_docs.py``: the workflow lives above the API directory,
so this module skips itself in the container, where ``curiarium/api`` is the whole
build context. The skip keys on publish.yml's own absence - a guard for CI hidden
behind somebody else's doc file disappears the day the docs get tidied.
"""

import re
import types
from pathlib import Path

import pytest
import yaml

# The repository root is outside this module's build context, so the index is guarded: the
# API image builds from ``curiarium/api``, where this file sits at ``/app/tests/...`` and
# ``parents`` is only three deep, so a bare ``parents[3]`` is a collection ERROR there rather
# than the skip the guardrail asks for (AGENTS.md guardrail 3). The host, where verify-fast
# runs, is five deep and cannot see the difference - hence the container-depth test below.
HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[3] if len(HERE.parents) > 3 else None
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml" if REPO_ROOT is not None else None

if PUBLISH_WORKFLOW_PATH is None or not PUBLISH_WORKFLOW_PATH.exists():
    pytest.skip(
        "the publish identity guard is host-only (the API image builds from curiarium/api, so the "
        "repository root and .github/workflows/publish.yml are absent)",
        allow_module_level=True,
    )

VERIFY_STEP_NAME = "Verify published identity"
REVISION_LABEL = "org.opencontainers.image.revision"
# The platform set the verification was written against. The build step's `platforms:`
# is the real source of truth - this pin is what makes an unaccompanied change to it
# fail loudly instead of silently retargeting the count guard asserted below.
EXPECTED_PLATFORMS = ("linux/amd64", "linux/arm64")
# The one guard in the step that demands an exact number of revision values.
REVISION_COUNT_GUARD = re.compile(r"-ne\s+(\d+)")
# Written as chr() so the character cannot be lost to an editor's escaping: the step's
# per-platform separator has to be a trailing comma, never this character plus an n.
BACKSLASH = chr(92)
# The triggering commit, as the runner exports it. metadata-action reads the same
# value out of process.env.GITHUB_SHA, which is the whole reason a revision label
# can be trusted without spelling it out as a build label.
TRIGGERING_COMMIT = "${{ github.sha }}"
# What each guard actually compares. Token-presence assertions (``git rev-parse HEAD`` appears,
# ``::error::`` appears) stay green when the comparison beside them is deleted or weakened to a
# prefix match, which is how a check that cannot fail gets shipped.
COMPARISONS = (
    '[ "$checked_out" != "$EXPECTED_REVISION" ]',
    '[ "$revision" != "$published" ]',
    '[ "$published" != "$EXPECTED_REVISION" ]',
    "}},{{ end }}",
    "IFS=',' read -r -a",
)


def workflow() -> dict:
    assert PUBLISH_WORKFLOW_PATH is not None
    return yaml.safe_load(PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8"))


def job() -> dict:
    return workflow()["jobs"]["images"]


def steps() -> list[dict]:
    return job()["steps"]


def step(name: str) -> dict:
    found = [candidate for candidate in steps() if candidate.get("name") == name]
    assert found, f"publish.yml has no step named {name!r}"
    return found[0]


def script() -> str:
    """The step's commands, without the comments - which quote the bad forms they warn about."""
    return "\n".join(line for line in step(VERIFY_STEP_NAME)["run"].splitlines() if not line.lstrip().startswith("#"))


def step_using(prefix: str) -> dict:
    found = [candidate for candidate in steps() if str(candidate.get("uses", "")).startswith(prefix)]
    assert found, f"publish.yml has no step using {prefix!r}"
    return found[0]


def index_of(target: dict) -> int:
    return next(position for position, candidate in enumerate(steps()) if candidate == target)


def revision_count_guard(step_script: str) -> int:
    """How many revision values the step demands, as written in the step itself.

    Read out of the script rather than hardcoded twice, so the platform matrix has
    something to be checked against instead of a number that can drift on its own.
    """
    guards = REVISION_COUNT_GUARD.findall(step_script)
    assert len(guards) == 1, (
        f"{VERIFY_STEP_NAME} must demand one exact number of published revision values so the build "
        f"matrix has something single to be checked against; found the guards {guards}"
    )
    return int(guards[0])


def failure_text(step_script: str) -> str:
    """Only the ``::error::`` lines of the step.

    The step summary names the platforms and the revision too, so an assertion over
    the whole script is satisfiable by prose that gates nothing - which is exactly
    when a reader needs the values spelled out in the message that comes with the red.
    """
    return "\n".join(line for line in step_script.splitlines() if "::error::" in line)


def build_with() -> dict:
    """The push step's ``with:``, defaulted so a removed ``with:`` is a message, not a KeyError."""
    return step_using("docker/build-push-action").get("with") or {}


def metadata_with() -> dict:
    """Likewise for the step that derives the labels the check reads back."""
    return step_using("docker/metadata-action").get("with") or {}


def derived_labels() -> str:
    """The labels the workflow tells metadata-action to stamp, as written."""
    with_args = metadata_with()
    assert "labels" in with_args, "docker/metadata-action is no longer told to stamp any labels at all"
    return str(with_args["labels"])


def test_the_module_skips_rather_than_raises_at_container_depth() -> None:
    """The container's build context is shallower than the repository (AGENTS.md guardrail 3).

    In the API image's test stage this module lives at /app/tests/..., whose parents list is
    three deep, so a bare parents[3] is a collection ERROR there rather than the skip the
    guardrail asks for - and the host, where verify-fast runs, is five deep and cannot see it.
    """
    module = types.ModuleType("publish_identity_at_container_depth")
    module.__file__ = "/app/tests/test_publish_identity.py"
    source = Path(__file__).resolve().read_text(encoding="utf-8")

    with pytest.raises(pytest.skip.Exception):
        exec(compile(source, str(module.__file__), "exec"), module.__dict__)


def test_a_step_verifies_the_published_identity() -> None:
    """The leg has to end by reading the image back, not by printing a summary."""
    verifier = step(VERIFY_STEP_NAME)

    assert "run" in verifier, f"{VERIFY_STEP_NAME} must be a script step, not an action"
    assert "uses" not in verifier, f"{VERIFY_STEP_NAME} must not delegate the check to an action"


def test_the_verification_runs_after_the_push() -> None:
    """Nothing can be verified before it exists.

    The step also has to sit after `Derive tags and labels`, since it quotes the
    tags that leg derived, but the ordering that matters is against the push.
    """
    pushed = step_using("docker/build-push-action")
    verifier = step(VERIFY_STEP_NAME)

    assert "id" in pushed, "the push step lost its id, so the digest the check needs has no source"
    assert index_of(verifier) > index_of(pushed), (
        f"{VERIFY_STEP_NAME} must run after docker/build-push-action, not before the images exist"
    )


def test_the_verification_is_wired_to_the_triggering_commit() -> None:
    """One source for the expected revision, and it is the run's own head SHA."""
    env = step(VERIFY_STEP_NAME).get("env") or {}

    assert env.get("EXPECTED_REVISION") == TRIGGERING_COMMIT, (
        "EXPECTED_REVISION is no longer the run's own head SHA. ${{ github.sha }} is what "
        "metadata-action itself reads out of the runner environment, so anything else (an input, "
        "an image label, a tag) is a second source of truth that can agree with itself while wrong"
    )
    assert '"$EXPECTED_REVISION"' in script(), "nothing compares the published revision to the expected one"


def test_the_verification_also_checks_the_checkout() -> None:
    """The label is derived from the environment, so a wrong-ref checkout stays invisible to it.

    metadata-action answers with whatever GITHUB_SHA says, so on a checkout of the
    wrong tree the label would still name the triggering commit while describing a
    different tree, so nothing downstream could tell. Only `git` on the actual
    workspace can see that, and it costs no network.
    """
    assert "git rev-parse HEAD" in script(), "the checkout is no longer compared against the triggering commit"


def test_the_verification_inspects_by_digest_and_never_by_tag() -> None:
    """A digest names one immutable manifest list; a tag names whatever is current.

    Two queued Publish runs, or a leg that has been sitting in the queue, is enough
    for :edge to point somewhere else by the time the step asks the registry - and
    steps.build.outputs.digest is the manifest-list index digest for a
    multi-platform push, so it is exactly what this leg pushed.
    """
    verifier = step(VERIFY_STEP_NAME)

    assert (verifier.get("env") or {}).get("DIGEST") == "${{ steps.build.outputs.digest }}", (
        "the digest the check reads must be the one this leg pushed"
    )
    assert "${IMAGE}@" in script(), "the check no longer inspects image@digest"
    assert ":edge" not in script(), "the check inspects a moving tag, which a queued run can move"


def test_the_verification_reads_labels_off_the_platform_map() -> None:
    """``.Image`` is where the labels are; the index carries none at all.

    Measured on a correct image: ``.Manifest.Annotations`` is null and grepping the
    ``--raw`` manifest walk finds zero ``image.revision`` strings, so an assertion
    built on that walk is satisfied by any image, good or bad. ``.Image`` is the
    per-platform map, and buildx excludes the unknown/unknown attestation manifests
    from it.
    """
    step_script = script()

    assert ".Image" in step_script, "the check no longer reads the per-platform map, so it reads the annotation-less index"
    assert "Config.Labels" in step_script, "the check no longer reads the per-platform labels"
    assert REVISION_LABEL in step_script, f"the check no longer names {REVISION_LABEL}"


def test_the_verification_never_walks_the_raw_manifests() -> None:
    """The raw walk answers zero for a correct image, so zero would pass.

    ``docker buildx imagetools inspect --raw`` hands back the manifest list and then
    every manifest it points at, attestations included; the revision label lives in
    none of those. A check that greps that output for it reports "no revision" for a
    perfect image and "no revision" for a broken one, which is why the step reads the
    ``.Image`` map through ``--format`` instead - the commands are what is asserted,
    so the comments above can name the bad form out loud.
    """
    assert "--raw" not in script(), (
        "the tautological raw manifest walk is back: grepping it finds no revision even when all is well, "
        "so it passes on any image"
    )


def test_the_verification_fails_the_leg() -> None:
    """A check that cannot fail is a log line.

    No continue-on-error, no ``if: always()``: a leg whose published revision is
    wrong has to be red, because the tag it pushed is already deployable by then.
    """
    verifier = step(VERIFY_STEP_NAME)

    assert "continue-on-error" not in verifier, f"{VERIFY_STEP_NAME} is softened; a wrong revision would stay green"
    assert "if" not in verifier, f"{VERIFY_STEP_NAME} must run on the normal path and gate it"
    # The step leans on `<<<`, `read -a`, arrays and ${var:0:400}, all bash-isms: naming the
    # shell is free, and running it under `sh` would break the reads rather than the check.
    assert "shell" not in verifier, f"{VERIFY_STEP_NAME} no longer runs under the runner's default shell"

    step_script = script()
    assert "set -euo pipefail" in step_script, "the step no longer aborts on the first failing command"
    assert "::error::" in step_script, "a failed check has to annotate the run, not just print to the log"
    assert "exit 1" in step_script, "a failed check has to fail the leg"

    # Every annotation is a gate: an ``::error::`` whose branch lost its ``exit 1`` prints the
    # complaint, exits 0, and leaves the step summary claiming the identity was verified. Pairing
    # the two counts is what keeps "the check complains" from standing in for "the check fails".
    annotations = [line for line in step_script.splitlines() if "::error::" in line]
    failures = [line for line in step_script.splitlines() if line.strip() == "exit 1"]
    assert len(annotations) == len(failures), (
        f"{VERIFY_STEP_NAME} prints {len(annotations)} ::error:: annotations but has only {len(failures)} "
        "`exit 1` guards: an error line whose branch no longer exits is a log line, not a gate, and the "
        "leg goes green with the wrong revision published"
    )
    assert annotations, f"{VERIFY_STEP_NAME} annotates nothing, so there is no gate to pair with an exit"


def test_every_comparison_actually_compares_something_to_something() -> None:
    step_script = script()

    for comparison in COMPARISONS:
        assert comparison in step_script, (
            f"{VERIFY_STEP_NAME} no longer contains the comparison {comparison!r}; a step that still prints "
            "an error line but no longer compares the two values it exists to compare is a log line, not a gate"
        )


def test_the_verification_requires_both_platforms() -> None:
    """A multi-arch image that answers once came from one build, not two.

    buildx returns one ``.Image`` entry per real platform - amd64 and arm64 - so
    exactly two revision values is the signature of a complete push; one is a
    partially built manifest list, which is worse than no tag at all.
    """
    step_script = script()

    assert "-ne 2" in step_script or "!= 2" in step_script, (
        f"{VERIFY_STEP_NAME} no longer pins the number of published revisions to 2 (linux/amd64 + linux/arm64)"
    )
    assert "linux/arm64" in failure_text(step_script), (
        "the expected platform set is no longer stated in the failure text - in the message that "
        "comes with the red, not in the step summary, which names the platforms without gating anything"
    )


def test_the_revision_count_guard_tracks_the_build_platforms() -> None:
    """The expected count is the build matrix's platform count, not a constant.

    The guard and the matrix sit in different steps, so nothing in the YAML ties them
    together: add ``linux/arm/v7`` to ``platforms:`` and every publish goes red on a
    perfectly legitimate three-platform build, because the check still demands two
    revision values - and that red is indistinguishable from the failure the check
    exists to catch, a manifest list whose arm64 entry never landed. So the matrix is
    the source of truth here: the number the guard demands, and the platforms the
    failure text names, both have to follow it.

    The platform names are looked for among the ``::error::`` lines rather than the
    whole script (see ``failure_text``), because the step summary names both platforms
    too - a whole-script check stays green when the failure text degrades to "both
    platforms", which is exactly when a reader needs them spelled out.

    The script has to stay literally backslash-free, which is what makes that count
    mean what it says: the per-platform separator is a trailing comma, and a ``\n``
    written inside the ``--format`` string survives a YAML literal block scalar as two
    characters, so both platforms land on one line and the count guard passes on a
    one-platform image.
    """
    step_script = script()
    with_args = build_with()
    platforms = [platform.strip() for platform in (with_args.get("platforms") or "").split(",")]

    assert sorted(platforms) == sorted(EXPECTED_PLATFORMS), (
        f"the build step publishes {platforms}, not the {list(EXPECTED_PLATFORMS)} set the verification was "
        "written against: whoever changes the platform matrix has to change the expected revision count in "
        "the same commit, or every publish goes red for a legitimate build"
    )
    assert revision_count_guard(step_script) == len(platforms), (
        f"the build step publishes {len(platforms)} platforms ({', '.join(platforms)}) but "
        f"{VERIFY_STEP_NAME} demands a different number of {REVISION_LABEL} values: whoever adds a platform "
        "to the build must raise the expected count in the same change, or every publish goes red for a "
        "legitimate build - and that red looks exactly like the partially built manifest list this check "
        "exists to catch"
    )
    assert BACKSLASH not in step_script, (
        f"{VERIFY_STEP_NAME} contains a backslash: inside a YAML literal block scalar it reaches the shell "
        "as two characters, so a \\n used as the per-platform separator puts every platform on one line and "
        "the count guard above reports a complete push for a one-platform image"
    )

    error_lines = failure_text(step_script)
    for platform in platforms:
        assert platform in error_lines, (
            f"{platform} is built but no {VERIFY_STEP_NAME} failure message names it: the count guard's red "
            "has to state which platforms a complete push carries, or it reads as a bare number instead of "
            "the partial push it is meant to report"
        )


def test_metadata_action_still_uses_the_default_context() -> None:
    """The invariant the whole scheme rests on.

    metadata-action derives the revision label from the runner environment on the
    default ``context:`` (the workflow path). The day someone adds ``context: git``
    the label becomes a property of whatever was checked out, and nothing else in
    this file would notice.
    """
    with_args = metadata_with()

    assert "context" not in with_args, (
        "docker/metadata-action gained an explicit context: the revision label would then describe the "
        "checkout rather than the triggering commit, and a wrong-ref checkout becomes invisible again"
    )


def test_the_push_builds_what_the_workflow_claims_without_extra_annotations() -> None:
    """Both architectures in one invocation, so a platform that cannot build publishes nothing.

    ``annotations:`` on the push would be a second, later writer of the revision
    label - a second source of truth that agrees with nothing but itself.
    """
    with_args = build_with()

    assert "annotations" not in with_args, "the push now stamps its own annotations, bypassing the derived labels"
    assert "linux/amd64" in (with_args.get("platforms") or ""), "the images are no longer built for amd64"
    assert "linux/arm64" in (with_args.get("platforms") or ""), "the images are no longer built for arm64"
    assert with_args.get("labels") == "${{ steps.meta.outputs.labels }}", (
        "the push no longer takes the labels metadata-action derived, so the check verifies a different image "
        "than the one whose labels were asserted"
    )


def test_the_checkout_stays_a_bare_checkout() -> None:
    """No ``fetch-depth``, in either direction.

    The step's value is that it is the checkout metadata-action and the runner
    agree on. Tuning it (``fetch-depth: 0`` to get history, ``1`` to save time)
    changes what the workspace is, which is precisely the thing the first
    comparison exists to catch.
    """
    checkout = step_using("actions/checkout")

    assert "fetch-depth" not in (checkout.get("with") or {}), "the checkout is no longer the default one"


def test_publish_runs_queue_instead_of_cancelling() -> None:
    """A cancelled leg tears a multi-leg publish down the middle of one repository.

    ``cancel-in-progress`` replaces a running api leg while a web leg is still
    pushing, and the half-published state is what a deployment pulls.
    """
    concurrency = workflow()["concurrency"]

    assert concurrency.get("group"), "publish runs are no longer serialised per ref"
    assert "cancel-in-progress" not in concurrency, (
        "cancel-in-progress would let a new push abort a publish halfway through, leaving tags that "
        "describe a mixture of two trees"
    )


def test_metadata_action_still_overrides_the_labels_only_it_cannot_derive() -> None:
    """title and description carry information metadata-action has no way to know.

    Both legs of the api image publish into the same repository, so the title is
    what tells them apart, and the description says which one has the ZFS userspace.
    """
    labels = derived_labels()

    assert "org.opencontainers.image.title=curiarium-${{ matrix.name }}" in labels, (
        "the two api legs now share a title, so the published image no longer says which variant it is"
    )
    assert "org.opencontainers.image.description=${{ matrix.description }}" in labels, (
        "the published description no longer comes from the matrix"
    )


def test_no_label_override_restates_a_metadata_action_default() -> None:
    """``source`` was dropped because it re-typed metadata-action's own default.

    The published value measured byte-identical to the ``repo.html_url`` default, so
    the override only added a line to drift - the same argument rules out
    re-adding a revision label, which would be the second source of truth this
    workflow deliberately refuses.
    """
    labels = derived_labels()

    assert "org.opencontainers.image.source" not in labels, (
        "the redundant source override is back: it restates metadata-action's repo.html_url default and "
        "invites the two to disagree"
    )
    assert REVISION_LABEL not in labels, (
        f"an explicit {REVISION_LABEL} label is the second source of truth this ticket rejects: it would "
        "keep publishing the right value for the wrong reason"
    )


def test_the_workflow_asks_ghcr_for_exactly_what_it_needs() -> None:
    """The verification reads the manifest list back over the registry API.

    ``packages: write`` already covers reading a private package's manifests, so no
    extra ``packages: read`` is warranted - and asking for more than the job uses is
    how a workflow ends up unreviewable.
    """
    assert workflow()["permissions"] == {"contents": "read", "packages": "write"}, (
        "the publish job's permissions drifted from the minimum it needs to check out and push"
    )
