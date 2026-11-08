"""Pin the Content-Security-Policy the web image emits.

`curiarium/web/30-security-headers.sh` regenerates
`/etc/nginx/security-headers.conf` at container start, and the baked
`curiarium/web/security-headers.conf` is the fallback shipped when nginx runs
without that entrypoint. Both live above the API build context (`curiarium/api`
becomes the whole build context in-image), so every test here skips cleanly in
the container.

The generator is executed through `sh` instead of being parsed as source, so a
rewrite cannot pass these tests while leaving stale text behind. The host-side
run redirects SECURITY_HEADERS_CONF_PATH at a temporary file, so no test writes
to /etc.

The failure this guards against (`Mf-ce8l`): a split-host deployment used to
render no images because the derived API origin reached `connect-src` only. Both
directives now consume the same vetted origin, and every case below asserts they
agree.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
CURIARIUM_ROOT = API_ROOT.parent
SCRIPT_PATH = CURIARIUM_ROOT / "web" / "30-security-headers.sh"
BAKED_PATH = CURIARIUM_ROOT / "web" / "security-headers.conf"

requires_script = pytest.mark.skipif(
    not SCRIPT_PATH.exists() or not BAKED_PATH.exists(),
    reason="web CSP generator is outside the API image build context",
)
requires_sh = pytest.mark.skipif(
    shutil.which("sh") is None, reason="sh is required to run the CSP generator"
)

CSP_LINE_RE = re.compile(r'^add_header Content-Security-Policy "(?P<policy>[^"]*)" always;$', re.MULTILINE)

# Sources that are never a derived API origin.
NON_ORIGIN_SOURCES = {"'self'", "data:", "blob:"}

# The env cases exercised by the acceptance test, with the origin each must add.
ORIGIN_CASES = [
    pytest.param({}, [], id="no-origin"),
    pytest.param({"VITE_API_URL": "http://127.0.0.1:18000"}, ["http://127.0.0.1:18000"], id="vite-api-url"),
    pytest.param(
        {"VITE_API_URL": "", "PUBLIC_URL": "http://127.0.0.1:18000/"},
        ["http://127.0.0.1:18000"],
        id="public-url",
    ),
    pytest.param({"VITE_API_URL": "http://evil$host/api"}, [], id="unsafe-origin"),
]


def _run(extra_env: dict[str, str]) -> tuple[str, str]:
    """Run the generator with a from-scratch environment; return (conf, stderr)."""
    with tempfile.TemporaryDirectory() as directory:
        conf = Path(directory) / "security-headers.conf"
        # From scratch on purpose: an inherited SECURITY_HEADERS_CSP or
        # SECURITY_HEADERS_ENABLED from the surrounding shell would silently
        # change the policy under test.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SECURITY_HEADERS_CONF_PATH": str(conf),
        }
        env.update(extra_env)
        proc = subprocess.run(
            ["sh", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return conf.read_text(encoding="utf-8"), proc.stderr


def _policy(text: str) -> str:
    matches = list(CSP_LINE_RE.finditer(text))
    assert len(matches) == 1, f"expected exactly one CSP header line, found {len(matches)}"
    return matches[0].group("policy")


def _directives(policy: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for segment in policy.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        name, _, sources = segment.partition(" ")
        directives[name] = sources.split()
    return directives


def _baked_policy() -> str:
    return _policy(BAKED_PATH.read_text(encoding="utf-8"))


def _api_origins(sources: list[str]) -> list[str]:
    return [source for source in sources if source not in NON_ORIGIN_SOURCES]


def _assert_directives_agree(directives: dict[str, list[str]]) -> None:
    img_origins = _api_origins(directives["img-src"])
    connect_origins = _api_origins(directives["connect-src"])
    assert img_origins == connect_origins, (
        "img-src and connect-src disagree about the derived API origin: "
        f"img-src={directives['img-src']}, connect-src={directives['connect-src']}"
    )


@requires_script
@requires_sh
def test_no_api_origin_policy_matches_the_baked_header() -> None:
    emitted, _ = _run({})
    policy = _policy(emitted)

    assert policy == _baked_policy(), "the no-origin policy drifted from the baked security-headers.conf"
    directives = _directives(policy)
    assert directives["img-src"] == ["'self'", "data:", "blob:"]
    assert directives["connect-src"] == ["'self'"]
    _assert_directives_agree(directives)


@requires_script
@requires_sh
def test_vite_api_url_origin_reaches_connect_src_and_img_src() -> None:
    emitted, _ = _run({"VITE_API_URL": "http://127.0.0.1:18000"})
    policy = _policy(emitted)

    directives = _directives(policy)
    assert "http://127.0.0.1:18000" in directives["connect-src"]
    assert "http://127.0.0.1:18000" in directives["img-src"]
    assert "data:" in directives["img-src"]
    assert "blob:" in directives["img-src"]
    _assert_directives_agree(directives)


@requires_script
@requires_sh
def test_public_url_derived_origin_reaches_connect_src_and_img_src() -> None:
    emitted, _ = _run({"VITE_API_URL": "", "PUBLIC_URL": "http://127.0.0.1:18000/"})
    policy = _policy(emitted)

    directives = _directives(policy)
    assert "http://127.0.0.1:18000" in directives["connect-src"]
    assert "http://127.0.0.1:18000" in directives["img-src"]
    assert "data:" in directives["img-src"]
    assert "blob:" in directives["img-src"]
    _assert_directives_agree(directives)


@requires_script
@requires_sh
def test_unsafe_derived_origin_falls_back_to_self_for_both_directives() -> None:
    emitted, stderr = _run({"VITE_API_URL": "http://evil$host/api"})
    policy = _policy(emitted)

    assert "for connect-src and img-src" in stderr
    directives = _directives(policy)
    assert directives["img-src"] == ["'self'", "data:", "blob:"]
    assert directives["connect-src"] == ["'self'"]
    _assert_directives_agree(directives)


@requires_script
@requires_sh
@pytest.mark.parametrize(("extra_env", "expected_origins"), ORIGIN_CASES)
def test_img_src_and_connect_src_agree_about_the_api_origin(
    extra_env: dict[str, str],
    expected_origins: list[str],
) -> None:
    emitted, _ = _run(extra_env)
    policy = _policy(emitted)

    directives = _directives(policy)
    _assert_directives_agree(directives)
    assert _api_origins(directives["img-src"]) == expected_origins
    assert _api_origins(directives["connect-src"]) == expected_origins
