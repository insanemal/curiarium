#!/usr/bin/env python3
"""Measure a folder and propose what it should become. Writes nothing.

The engine behind `curiarium/scripts/propose_import.py`, and the half of that tool
the app is allowed to call. It walks a real storage tree and answers, per folder,
which interpretations the facts support ("one project", "one project per part
folder", "add these parts to an existing project"), each with the counts it would
produce, the reasons it is or is not recommended, and the arrangement it would
need. It also measures the FACTS a report renders: vendor metadata, provenance
candidates, hierarchical groups, archive twin verification and cover candidates.

APP-FREE, not stdlib-only. The engine uses `pypdf` (pure Python, no system
libraries) for PDF text and document info, so run it under the repo virtualenv
(`.venv/bin/python`, which installs `curiarium/api/requirements*.lock`). What it
must never do is import an app module - `fastapi`, `sqlalchemy`, a database or
settings/router module - because that is what lets the API process import it AND
a host run it in an offline tight loop against a live CephFS mount. The rule is
audited by `curiarium/api/tests/test_proposal_engine.py` rather than trusted.

Read-only in the strongest sense: no rename, no link, no unlink, no mkdir, no
write-file. It opens directories for reading, files to parse a README/vendor
document, and archives read-only to compare a payload. The one thing that can run
a subprocess is `propose()`, which asks git about a checkout via `git_state`. Since
Mf-1agz the report ENABLES that probe with a bounded `git_timeout` (5.0 s in the
report), which deliberately reverses Mf-yitm's `probe_git=False` default for a GET:
the owner-approved label corpus is 7/7 with probing and 6/7 without, and the
measured cost over the 175-row live population is +0.507 s. The bound is what keeps
one hung checkout from costing a request `GIT_TIMEOUT_SECONDS`. A proposal moves
nothing either way.

Nothing here prints. `validate()` returns plain data and the CLI shim renders it,
so the same scoring can be asserted in a test instead of scraped from stdout.
"""

import hashlib
import json
import os
import re
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

# --- the corpus vocabulary ------------------------------------------------
# Model suffixes are generous on purpose: this tool is asked "what is this
# folder", and a Rhino file or a LightBurn file is a real payload even when no
# slicer here opens it. The app's own tables are narrower; that difference is
# exactly the kind of thing these proposals are meant to expose.
# OS metadata that ships inside downloaded archives. The import doors already
# refuse these (`zip_junk.py`); a proposal must not count them as content.
JUNK_NAMES = {"__macosx", ".ds_store", "thumbs.db", "desktop.ini", "$recycle.bin"}


def is_junk_path(name: str) -> bool:
    lowered = name.lower()
    if any(part in JUNK_NAMES or part.startswith("._") for part in lowered.split("/")):
        return True
    return os.path.basename(lowered) in JUNK_NAMES


MODEL_EXTS = {
    ".stl", ".3mf", ".step", ".stp", ".obj", ".scad", ".3dm", ".lys",
    ".lbrn", ".lbrn2", ".ctb", ".pwmx", ".pwma", ".sl1", ".sl1s",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".svg"}
# The model suffixes a browser can render as a preview when no image exists.
PREVIEW_EXTS = {".stl", ".3mf", ".step", ".stp"}
DOC_EXTS = {".pdf", ".txt", ".md", ".html", ".htm", ".rtf", ".doc", ".docx"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}

# App-owned directories are not content and are never proposed on.
APP_OWNED = {"thumbs", ".incoming", ".trash", "recovered", ".snapshots"}

# Names that read as a *part* of one product rather than a product of their own.
PART_NAME = re.compile(
    r"""^(?:
        \d+\s*[._)\-]            # 01. DRAWERS, 02_foo, 3) bar
      | wave\s*\d
      | \d+(?:st|nd|rd|th)\s+(?:wave|batch|pack|set)
      | (?:part|pt|set|edition|version|rev)\s*\d
      | (?:s|m|l|xl|xxl|2xl|3xl)(?:\u00b2)?\s*(?:famil|box|drawer|module|roof|base)
      | (?:core|legs?|rails?|accessor(?:y|ies)|fixes?|base|baseplate|module|roof|lid|box|drawer|bracket|cover|insert|tray|clip|mount|stand|plate)s?$
      | \d+
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# A file that carries vendor/product metadata, and therefore a claim to be a
# product boundary of its own.
META_EXTS = {".json", ".txt", ".md", ".html", ".htm", ".pdf"}

# The subset of documents that claims a product. A vendor export keeps a README,
# a licence, print settings or an info.json beside its models; a part folder keeps
# at most a note ("Wizard_Dice_v5.4_fixes.txt"), which claims nothing.
# Deliberately NOT "instructions": a PDF manual lives INSIDE a product (Vault's
# `Vault Archive_…/Instructions Manual.pdf`), so counting it as a product claim would
# hide the wrapper and block the flattening proposal. A claim is a vendor export's
# README/LICENSE/info.json/print-settings, which ships beside models as its own unit.
PRODUCT_DOC = re.compile(
    r"(readme|licen[cs]e|print.?settings|manifest|metadata|info)", re.IGNORECASE
)


# A vendor product id inside a folder name: `... - 5414731`, `1234567_foo`. This is
# a claim that the folder names somebody's product, not a part of one.
VENDOR_ID = re.compile(r"(?:^|[\s_\-])(\d{5,})\b")

# A vendor product id of 6-8 digits in a FOLDER name: somebody ELSE's product.
# Precision, not recall: `VENDOR_ID` above matches 5+ digits because it asks the
# recall question ("is this a whole product"), while `foreign` asks only the
# precision question ("is this somebody else's thing"), so it keeps the 6-8 digit
# window the deleted detector's `COMPOUND_FOREIGN_ID` used. Ported by value.
COMPOUND_FOREIGN_ID = re.compile(r"(?:^|[-_ ])\d{6,8}(?:$|[-_ ])")


# A trailing download-site code: `Vault_USSA123123123`, `thing_ABC123456`. Only when
# the token is long and digit-heavy, so a real word ("_remix", "_v2") survives.
SITE_CODE = re.compile(r"[ _\-]([A-Za-z]{0,8}\s?\d{6,}|[A-Za-z0-9]{10,}\d)\s*$")


# Short all-caps tokens that are part of a product name, not a site/series code.
KEEP_TOKENS = {"LED", "USB", "PLA", "ABS", "PETG", "TPU", "ASA", "XL", "XXL", "XXXL",
               "LCD", "OLED", "CNC", "PWM", "RGB", "SLA", "FDM", "HDMI", "RJ45"}


# --- bounded reading ------------------------------------------------------
#
# Every fact below is read by a GET-shaped caller, so every read has a ceiling.
# The numbers are chosen from the live library, not from optimism:
#
# * `PDF_MAX_BYTES` is 64 MiB. The largest manual measured in `/mnt/ceph/3D_models`
#   is `Vault/Vault Archive_18.11.2026/Instructions Manual_14.11.26.pdf` at
#   16.87 MB / 67 pages; a 64 MiB cap covers it with 3.8x headroom without letting
#   one hostile file pin a request.
# * `PDF_MAX_PAGES` is 4 because every attribution card puts its title, creator,
#   licence and links on page 1; stopping at 4 also bounds a 67-page manual.
# * `TEXT_MAX_BYTES` is 64 KiB - a vendor README is prose, not a payload.
# * `DOC_CHAR_CAP` bounds what one document contributes to a suggestion, so a
#   pathological line cannot grow the fact sheet without bound.
TEXT_MAX_BYTES = 64 * 1024
PDF_MAX_BYTES = 64 * 1024 * 1024
PDF_MAX_PAGES = 4
DOC_CHAR_CAP = 20_000
DOCS_MAX_PER_FOLDER = 32

# The one switch the before/after measurement flips. Read ONLY by `_collect_facts`
# (never by `_pdf_suggestions`), so both arms run the SAME code and the PDF gain is
# one flip rather than two codebases. A disabled PDF is recorded
# `skipped: "disabled"`, so the fact sheet never silently looks like it was absent.
PDF_DOCUMENTS_ENABLED = True

# Hierarchical groups are a report structure, not a full directory mirror. The
# depth and fan-out caps keep a genuinely enormous tree finite; a node that hits
# the child cap carries `truncated: True` so the report can say so rather than
# silently look complete.
GROUPS_MAX_DEPTH = 8
GROUPS_MAX_CHILDREN = 64

# Twin verification opens an archive read-only. A central directory is cheap,
# but a crafted archive could still be enormous, so both the archive size and
# the entry count are bounded and a bound that bites is reported, never ignored.
TWIN_MAX_BYTES = 16 * 1024 * 1024 * 1024
TWIN_MAX_ENTRIES = 50_000

# A cover is a pick plus a short ranked shortlist. The report never needs every
# image in a 794-model folder, only the best few and why they won.
COVER_MAX_CANDIDATES = 12

# Git output is bounded too: a checkout with a million tracked files must not
# turn a proposal into a memory test, and the four commands share one budget.
GIT_MAX_TRACKED = 5_000
GIT_MAX_UNTRACKED = 500
GIT_TIMEOUT_SECONDS = 120

# One archive cruft root, shared by the payload reader and the root finder, so
# the two can never disagree about what an archive's payload is.
MACOSX_ROOT = "__macosx"


# --- metadata vocabulary --------------------------------------------------
#
# The licence table is ported by VALUE from the detector this engine replaced. A
# known token maps to (display, URL); an unknown token keeps its own text and
# gets NO URL. A guessed licence link is a legal statement this engine is not
# entitled to make.
_CREATIVE_COMMONS = "https://creativecommons.org/licenses/"
_CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
LICENCE_TABLE: Dict[str, Tuple[str, str]] = {
    "cc_by": ("CC BY", f"{_CREATIVE_COMMONS}by/4.0/"),
    "cc_by_sa": ("CC BY-SA", f"{_CREATIVE_COMMONS}by-sa/4.0/"),
    "cc_by_nc": ("CC BY-NC", f"{_CREATIVE_COMMONS}by-nc/4.0/"),
    "cc_nc": ("CC BY-NC", f"{_CREATIVE_COMMONS}by-nc/4.0/"),
    "cc_by_nc_sa": ("CC BY-NC-SA", f"{_CREATIVE_COMMONS}by-nc-sa/4.0/"),
    "cc_nc_sa": ("CC BY-NC-SA", f"{_CREATIVE_COMMONS}by-nc-sa/4.0/"),
    "cc_by_nd": ("CC BY-ND", f"{_CREATIVE_COMMONS}by-nd/4.0/"),
    "cc_by_nc_nd": ("CC BY-NC-ND", f"{_CREATIVE_COMMONS}by-nc-nd/4.0/"),
    "cc_nc_nd": ("CC BY-NC-ND", f"{_CREATIVE_COMMONS}by-nc-nd/4.0/"),
    "cc0": ("CC0", _CC0_URL),
    "cc_zero": ("CC0", _CC0_URL),
    "public_domain": ("CC0", _CC0_URL),
}
_TOKEN_SEPARATORS = re.compile(r"[\s\-]+")

# A CC licence URL the document itself states also names the licence; this map
# only translates a URL we already hold into the display the table would use.
_CC_URL_TOKENS = {
    "by": "cc_by", "by-sa": "cc_by_sa", "by-nc": "cc_by_nc",
    "by-nc-sa": "cc_by_nc_sa", "by-nd": "cc_by_nd",
    "by-nc-nd": "cc_by_nc_nd",
}

# Document-info values that are tool boilerplate, never a title. Refusing this
# short list is what keeps `Printables PDF` from being offered as a product name.
BOILERPLATE_TITLES = {
    "", "untitled", "printables pdf", "attribution_cards", "attribution cards",
    "attribution card", "microsoft word", "document1", "pdf",
}

# The Thingiverse marker grammar, ported from the detector this engine replaced: one scan for the
# creator and the licence token it writes. Each half is independent, so a README
# that mentions a licence but not the uploader must not invent an author.
README_MARKERS = re.compile(
    r"created\s+by\s+thingiverse\s+user\s+(?P<creator>[^,\n]+)"
    r"|licensed\s+under\s+(?P<licence>[^,\n;.]+)",
    re.IGNORECASE,
)
# `<title> by <creator> on Thingiverse: <url>` - greedy title so the LAST ` by `
# before the marker wins, and the url stops at whitespace.
LICENSE_LINE = re.compile(
    r"(?P<title>.+) by (?P<creator>.+?) on Thingiverse:\s*(?P<url>\S+)",
    re.IGNORECASE,
)
_TRAILING_JUNK = ",;."
_SENTENCE_BOUNDARY = re.compile(r"[.!?]\s+")
# The attribution-card shape the live library actually ships:
#   <title> by <creator>
#   ...
#   Licensed under:
#   <token>
_ATTRIBUTION_FIRST_LINE = re.compile(r"^(?P<title>.+?)\s+by\s+(?P<creator>.+)$")
_LICENSED_UNDER_LINE = re.compile(r"licensed\s+under\b", re.IGNORECASE)
_SUMMARY_LINE = re.compile(r"^summary\s*:?\s*$", re.IGNORECASE)

# Provider table. The engine owns its own copy because importing
# `import_resolvers`/`url_utils` would drag fastapi's world in; a test pins the
# two copies for the three providers `url_utils.source_key` understands.
PROVIDER_HOSTS = {
    "thingiverse": frozenset({"thingiverse.com", "www.thingiverse.com"}),
    "printables": frozenset({"printables.com", "www.printables.com"}),
    "myminifactory": frozenset({"myminifactory.com", "www.myminifactory.com"}),
}
PROVIDER_MODEL_ID = {
    "thingiverse": re.compile(r"thing:(\d+)|/things/(\d+)", re.IGNORECASE),
    "printables": re.compile(r"/model/(\d+)"),
    "myminifactory": re.compile(r"/object/(\d+)"),
    "makerworld": re.compile(r"/models/(\d+)"),
}
# A provider URL that is deliberately NOT a model: a creator profile, a social
# card, a collection. The live Printables cards carry `/social/<id>/about` beside
# the model link, and MyMiniFactory carries `/users/<name>`.
_NON_MODEL_PATH = re.compile(
    r"/(?:social|users?|collections?|about|search|tags?|gallery|profile|u)/"
    r"|/@[^/]+",
    re.IGNORECASE,
)
# A bare model path carrying only a query (`/model?categoryId=21`) is a listing,
# not a truncated model page: reject it rather than call it unresolved.
_BARE_MODEL_PATHS = frozenset({"/model", "/models", "/thing", "/things", "/object"})
PROVENANCE_MAX = 32
PROVENANCE_UNRESOLVED_MAX = 16
PROVENANCE_ALSO_IN_MAX = 8
# `https://…`, `www.…` and a bare provider host all occur in real cards; the
# scheme-less `www.thingiverse.com/thing:2738211` is a measured witness.
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'\]\)\[\}\,]+(?:\.[^\s<>\"'\]\)\[\}\,]+)*"
    r"|(?:thingiverse|printables|myminifactory|makerworld)\.com/[^\s<>\"'\]\)\[\}\,]+",
    re.IGNORECASE,
)


def product_title(name: str) -> str:
    """The product's name from a downloaded folder/archive name.

    The owner renamed `Vault_USSA l 20261123.zip` to `Vault` by hand. Downloads bolt
    a site/series code onto the real name, sometimes with a stray space - so strip
    date-like and code-like tails repeatedly, keeping short all-caps tokens that are
    part of product names (`LAMP_USB` stays).
    """
    stem = os.path.splitext(name)[0].strip()
    for _ in range(6):
        before = stem
        stem = SITE_CODE.sub("", stem).strip(" _-.")
        tail = re.search(r"[ _\-]([A-Z]{3,8})$", stem)
        if tail and tail.group(1) not in KEEP_TOKENS:
            stem = stem[: tail.start()].strip(" _-.")
        if stem == before:
            break
    return stem or os.path.splitext(name)[0]


def vendor_id(name: str) -> Optional[str]:
    match = VENDOR_ID.search(name)
    return match.group(1) if match else None


def _resolve_to_candidate(rel_path: str, candidate: str, toplevel: str) -> Optional[str]:
    """One repo-root-relative path, rebased onto the candidate folder.

    `ls-files` and `status` answer from the repository's top level, which is not
    the candidate when the operator pointed at a subdirectory of a checkout. A
    path that sits outside the candidate is not the candidate's content and is
    dropped; without this a nested row reads another folder's files.
    """
    path = (rel_path or "").replace(os.sep, "/").lstrip("/")
    if not path:
        return None
    if not toplevel:
        return path
    try:
        prefix = os.path.relpath(candidate, toplevel).replace(os.sep, "/")
    except (OSError, ValueError):
        return path
    if prefix in ("", "."):
        return path
    prefix = prefix.rstrip("/") + "/"
    if path == prefix.rstrip("/"):
        return None
    if not path.startswith(prefix):
        return None
    return path[len(prefix):]


def _porcelain_untracked(status: str) -> List[str]:
    """The untracked names in `git status --porcelain -z` output, NUL-delimited.

    `-z` is what makes a path with a space or a rename survive: the output is
    never quoted and a rename contributes both its old and new name as separate
    NUL records, so we can take the new one without a `->` parser.
    """
    out: List[str] = []
    tokens = status.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        status_code = token[:2]
        name = token[3:] if len(token) > 3 else ""
        if status_code[:1] in {"R", "C"}:
            # A rename/copy is two records: `XY <new>` then `<old>`. Drop the old.
            index += 1
        if status_code == "??" and name:
            out.append(name.rstrip("/"))
    return out


# A `git` that cannot run is an UNKNOWN boundary, never a licence to split. The
# cause is a phrase a note can quote verbatim, so the report and the tests agree.
GIT_NOT_AVAILABLE = "git is not available"
GIT_TIMED_OUT = "git timed out"
GIT_UNREADABLE = "unreadable"
UNKNOWN_BOUNDARY_NOTE = (
    "git checkout detected, but git could not read it ({cause}): "
    "its boundary is unknown, so this is offered as one project, never as a split"
)


def unknown_boundary_note(cause: str) -> str:
    """The sentence an operator reads when a checkout's boundary could not be read.

    A boundary that cannot be read is a fact about the OBSERVER, not about the
    folder: the only honest reading is "this is one project until someone can read
    the boundary". The sentence names the cause so a missing binary is visible in
    the row, not hidden behind a structural guess (Mf-ky16).
    """
    return UNKNOWN_BOUNDARY_NOTE.format(cause=cause)


def git_state(
    path: str, *, max_tracked: Optional[int] = None,
    max_untracked: Optional[int] = None,
    timeout: Optional[float] = None,
) -> Optional[dict]:
    """The repo's own declaration of what belongs to it, read without writing.

    `--no-optional-locks` + GIT_OPTIONAL_LOCKS=0 keep `status` from refreshing the
    index, so asking this question never writes to the working tree.

    `None` means EXACTLY "not a repo": either no `.git` at all, or git itself ran
    and refused with "not a git repository" (a fake or stale `.git` with nothing
    behind it). When `.git` is present but the probe cannot read the checkout -
    git is missing, cannot be executed, times out, or its output is unreadable -
    this returns the error shape with a non-empty `git_error`, never `None`. That
    distinction is the contract: an unreadable boundary is an UNKNOWN, and silence
    can never be mistaken for "nothing tracked here" and licence a structural
    split of a real checkout (Mf-ky16).

    `.git` may be a DIRECTORY (normal repo) or a FILE (linked worktree, submodule)
    - `os.path.exists` covers both deliberately. A detached HEAD is reported as
    `branch: None, detached: True` plus the short sha, never the literal string
    `HEAD` as a branch. The four commands share one wall-clock budget and every
    listing has a cap; a cap that bites sets `truncated: True`.
    """
    budget = GIT_TIMEOUT_SECONDS if timeout is None else timeout
    tracked_cap = GIT_MAX_TRACKED if max_tracked is None else max_tracked
    untracked_cap = GIT_MAX_UNTRACKED if max_untracked is None else max_untracked
    if not os.path.exists(os.path.join(path, ".git")):
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    deadline = time.monotonic() + budget
    cause: Optional[str] = None
    refused = False

    def run(*args: str) -> Optional[str]:
        nonlocal cause, refused
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # The shared budget is spent: the boundary is unknown, not empty.
            if cause is None:
                cause = GIT_TIMED_OUT
            return None
        try:
            import subprocess
            out = subprocess.run(
                ["git", "--no-optional-locks", "-C", path, *args],
                capture_output=True, text=True, timeout=remaining, env=env,
            )
        except subprocess.TimeoutExpired:
            # A subclass of SubprocessError, so it must be caught first.
            if cause is None:
                cause = GIT_TIMED_OUT
            return None
        except (OSError, subprocess.SubprocessError):
            # The binary is missing or cannot be executed; the checkout is real
            # but its boundary is unreadable from here.
            if cause is None:
                cause = GIT_NOT_AVAILABLE
            return None
        if out.returncode != 0:
            if "not a git repository" in (out.stderr or "").lower():
                refused = True
            elif cause is None:
                cause = GIT_UNREADABLE
            return None
        if out.stdout is None:
            if cause is None:
                cause = GIT_UNREADABLE
            return None
        return out.stdout

    def error_state() -> dict:
        return {
            "branch": None,
            "detached": False,
            "head": "",
            "sha": "",
            "toplevel": None,
            "truncated": False,
            "tracked": [],
            "untracked": [],
            "git_error": cause or GIT_UNREADABLE,
        }

    branch = (run("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    head = (run("log", "-1", "--format=%h %ci") or "").strip()
    toplevel = (run("rev-parse", "--show-toplevel") or "").strip()
    tracked_raw = run("ls-files")
    status = run("status", "--porcelain", "-z", "--untracked-files=normal")
    if tracked_raw is None or status is None:
        if refused and cause is None:
            return None
        return error_state()
    detached = branch in ("HEAD", "")
    sha = head.split()[0] if head else ""
    truncated = False
    tracked: List[str] = []
    for line in tracked_raw.splitlines():
        if not line:
            continue
        resolved = _resolve_to_candidate(line, path, toplevel)
        if resolved is None:
            continue
        if len(tracked) >= tracked_cap:
            truncated = True
            break
        tracked.append(resolved)
    untracked: List[str] = []
    for name in _porcelain_untracked(status):
        resolved = _resolve_to_candidate(name, path, toplevel)
        if resolved is None:
            continue
        if len(untracked) >= untracked_cap:
            truncated = True
            break
        untracked.append(resolved)
    return {
        "branch": None if detached else branch,
        "detached": detached,
        "head": head,
        "sha": sha,
        "toplevel": toplevel or None,
        "truncated": truncated,
        "tracked": tracked,
        "untracked": sorted(set(untracked), key=str.casefold),
        "git_error": None,
    }


class Entry:
    __slots__ = ("name", "ext", "size")

    def __init__(self, name: str, size: int):
        self.name = name
        self.ext = os.path.splitext(name)[1].lower()
        self.size = size


def read_dir(path: str) -> Tuple[List[Entry], List[str]]:
    """One directory's files and subdirectories. Read-only, skips hidden + app-owned."""
    files: List[Entry] = []
    dirs: List[str] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.startswith(".") or entry.name in APP_OWNED:
                            continue
                        dirs.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name.startswith("."):
                            continue
                        files.append(Entry(entry.name, entry.stat(follow_symlinks=False).st_size))
                except OSError:
                    continue
    except OSError:
        return [], []
    return files, sorted(dirs, key=str.casefold)


class Facts:
    """Everything measured about one candidate folder, in one walk."""

    def __init__(self, name: str, path: str, rel: str):
        self.name = name
        self.path = path
        self.rel = rel
        self.models = 0
        self.images = 0
        self.docs = 0
        self.others = 0
        self.archives = 0
        self.bytes = 0
        # Directory entries the walk actually visited: files plus subdirectories.
        self.entries = 0
        # True when the walk stopped at a caller's entry budget, so this census is
        # partial and must not be read as a total.
        self.truncated = False
        # relative dir -> model count, only for dirs that carry models
        self.model_dirs: Dict[str, int] = {}
        # relative dir -> metadata filenames found directly in it
        self.meta: Dict[str, List[str]] = {}
        # relative dir -> filenames whose STEM claims a product (README/LICENSE/
        # print-settings/info ...), including extension-less ones. Only the flag
        # predicates read it, so a full file listing is never retained.
        self.dir_product_docs: Dict[str, List[str]] = {}
        self.children: List[str] = []          # this folder's own subdirectory names
        self.child_models: Dict[str, int] = {}  # first path segment -> model count
        self.child_meta: Dict[str, List[str]] = {}  # first path segment -> meta files
        # --- the fact sheet the report renders (Mf-9h0v) ------------------
        # Every one of these is ADDITIVE: no fact below changes a score, a shape
        # or an `arrange` line. They describe the same walk the ranking already
        # measured, so a report can show provenance without a second walk.
        # field -> {"value", "where", "confidence"}; an absent field is ABSENT,
        # never a None-with-a-guess.
        self.metadata: Dict[str, dict] = {}
        self.provenance: dict = {
            "candidates": [], "unresolved": [], "confidence": "suggested",
        }
        # Hierarchical list of directory nodes that hold models somewhere below.
        self.groups: List[dict] = []
        self.twin: dict = {"verified": False, "pairs": [], "unverified": []}
        self.cover: dict = {"reason": "none", "chosen": None, "candidates": []}
        # What was read and what was skipped, so a cap hit is visible.
        self.documents: List[dict] = []
        # Walk-local file records used to build `cover`; not part of the API.
        self._images: List[Tuple[str, int]] = []
        self._previews: List[Tuple[str, int]] = []

    # -- derived -----------------------------------------------------------
    @property
    def payload(self) -> int:
        return self.models + self.images + self.docs + self.archives

    @property
    def depth(self) -> int:
        if not self.model_dirs:
            return 0
        return max(0 if d == "." else d.count(os.sep) + 1 for d in self.model_dirs)

    @property
    def root_meta(self) -> List[str]:
        return self.meta.get(".", [])

    def meta_dirs(self) -> int:
        return sum(1 for files in self.meta.values() if files)

    def twin_archives(self) -> List[str]:
        """Archives whose stem matches a subdirectory: the extracted twin, not a product."""
        stems = {name.casefold() for name in self.children}
        out = []
        try:
            files, _ = read_dir(self.path)
        except OSError:
            return out
        for entry in files:
            if entry.ext in ARCHIVE_EXTS:
                stem = os.path.splitext(entry.name)[0].casefold()
                if stem in stems:
                    out.append(entry.name)
        return sorted(out)

    def top_children_with_models(self) -> List[Tuple[str, int]]:
        """Real sub-folders that carry models; `(root files)` stands in for depth 0."""
        return sorted(
            (((("(root files)" if k == "." else k)), v) for k, v in self.child_models.items() if v),
            key=lambda kv: (-kv[1], kv[0].casefold()),
        )

    def part_like_children(self) -> List[str]:
        return [name for name, _ in self.top_children_with_models() if PART_NAME.match(name)]

    def children_with_own_meta(self) -> List[str]:
        """Top folders carrying their own metadata: each claims to be a product.

        A vendor export keeps its README/LICENSE/JSON beside its models. A part
        folder of one product does not - Stagetop's nine parts carry no document
        at all, while endorphin's five ingredients each carry README + LICENSE.
        """
        return [
            name
            for name, _ in self.top_children_with_models()
            if any(
                os.path.splitext(f)[1].lower() in {".json", ".pdf", ".html", ".htm"}
                or PRODUCT_DOC.search(os.path.splitext(f)[0])
                for f in self.child_meta.get(name, [])
            )
        ]

    def children_with_vendor_id(self) -> List[str]:
        return [
            name
            for name, _ in self.top_children_with_models()
            if vendor_id(name)
        ]

    def dominance(self) -> float:
        tops = self.top_children_with_models()
        if not tops or not self.models:
            return 1.0
        return tops[0][1] / self.models


def measure(
    path: str, rel_prefix: str = "", *, max_entries: Optional[int] = None
) -> Facts:
    """Walk one candidate folder once and measure it.

    ``max_entries`` bounds the walk at the directory-entry level (files plus
    subdirectories): the app reads this walk inside a GET whose own walk is already
    bounded (``bundle_service.MAX_SCAN_ENTRIES``), so a second unbounded walk per row
    could cost far more than the report it feeds. A partial census must be
    detectable rather than silently reported as a total, so a walk that hits the
    budget sets ``Facts.truncated``. The check runs once per directory, so a walk
    may overshoot by one directory's listing - the bound is on how much of the tree
    gets classified, and a single listing is the smallest step there is. ``None`` -
    every CLI caller - leaves the walk unbounded, byte-identical to before the
    budget existed.
    """
    facts = Facts(os.path.basename(path.rstrip(os.sep)) or path, path, rel_prefix)
    stack: List[Tuple[str, str]] = [(".", path)]
    while stack:
        rel_dir, absolute = stack.pop()
        files, dirs = read_dir(absolute)
        facts.entries += len(files) + len(dirs)
        if max_entries is not None and facts.entries > max_entries:
            facts.truncated = True
            break
        if rel_dir == ".":
            facts.children.extend(dirs)
        for entry in files:
            facts.bytes += entry.size
            ext = entry.ext
            rel_file = entry.name if rel_dir == "." else f"{rel_dir}/{entry.name}"
            if ext in ARCHIVE_EXTS:
                facts.archives += 1
            if ext in MODEL_EXTS:
                facts.models += 1
                facts.model_dirs[rel_dir] = facts.model_dirs.get(rel_dir, 0) + 1
                top = rel_dir.split(os.sep, 1)[0] if rel_dir != "." else "."
                facts.child_models[top] = facts.child_models.get(top, 0) + 1
                if ext in PREVIEW_EXTS:
                    facts._previews.append((rel_file, entry.size))
            elif ext in IMAGE_EXTS:
                facts.images += 1
                facts._images.append((rel_file, entry.size))
            elif ext in DOC_EXTS:
                facts.docs += 1
            else:
                facts.others += 1
            if ext in META_EXTS:
                facts.meta.setdefault(rel_dir, []).append(entry.name)
                top = rel_dir.split(os.sep, 1)[0] if rel_dir != "." else "."
                facts.child_meta.setdefault(top, []).append(entry.name)
            stem = os.path.splitext(entry.name)[0]
            if stem.casefold() in {"readme", "license", "licence"} or PRODUCT_DOC.search(stem):
                facts.dir_product_docs.setdefault(rel_dir, []).append(entry.name)
        for name in dirs:
            stack.append((name if rel_dir == "." else os.path.join(rel_dir, name),
                          os.path.join(absolute, name)))
    _collect_facts(facts)
    return facts


# --- the fact sheet: metadata, provenance, groups, twin, cover -------------
#
# Everything below is ADDITIVE evidence for a report. Nothing here feeds a score,
# a shape or a move: those are measured above and must stay byte-identical when a
# fact is added (the Mf-9h0v invariant).

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _trim(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip().strip(_TRAILING_JUNK).strip()
    return cleaned or None


def _trim_to_last_sentence(value: Optional[str]) -> Optional[str]:
    """`value` from its last sentence boundary on; unchanged when there is none."""
    if not value:
        return value
    return _SENTENCE_BOUNDARY.split(value)[-1].strip() or None


def licence_from_token(token: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """`(display, url)` for a known licence token; `(token, None)` otherwise.

    `token is None` stays `(None, None)`. An unknown token is echoed back
    unmodified and gets no URL: this function never invents a licence link.
    """
    if not token:
        return None, None
    normalised = _TOKEN_SEPARATORS.sub("_", token.strip().lower()).strip("_")
    known = LICENCE_TABLE.get(normalised)
    if known is not None:
        return known
    return token, None


def _licence_display_from_url(url: Optional[str]) -> Optional[str]:
    """The display a CC URL the document itself states implies, or None."""
    value = (url or "").strip()
    match = re.search(r"/licenses/([a-z\-]+)/([0-9.]+)", value, re.IGNORECASE)
    if match:
        token = _CC_URL_TOKENS.get(match.group(1).lower())
        if token:
            return f"{LICENCE_TABLE[token][0]} {match.group(2)}"
    if "publicdomain/zero" in value.lower():
        return "CC0"
    return None


def _set_licence(meta: dict, display: Optional[str], url: Optional[str],
                 where: str, confidence: str) -> None:
    """Record a licence the DOCUMENT states, with its URL only when the table knows
    one. A stronger suggestion replaces a weaker one; an equal one keeps the first."""
    if not display:
        return
    existing = meta.get("licence")
    if existing is not None and (
        _CONFIDENCE_RANK[existing["confidence"]] >= _CONFIDENCE_RANK[confidence]
    ):
        return
    meta["licence"] = {"value": display, "where": where, "confidence": confidence}
    if url:
        meta["licence_url"] = {"value": url, "where": where, "confidence": confidence}
    else:
        meta.pop("licence_url", None)


def _offer_licence(meta: dict, token: Optional[str], where: str, confidence: str) -> None:
    display, url = licence_from_token(token)
    _set_licence(meta, display, url, where, confidence)


def _offer(meta: dict, field: str, value: Optional[str], where: str,
           confidence: str) -> None:
    """One field suggestion. A stronger one wins; an equal one keeps the first."""
    if value is None:
        return
    text = " ".join(str(value).split()).strip()
    if not text:
        return
    if len(text) > DOC_CHAR_CAP:
        text = text[:DOC_CHAR_CAP]
    existing = meta.get(field)
    if existing is not None and (
        _CONFIDENCE_RANK[existing["confidence"]] >= _CONFIDENCE_RANK[confidence]
    ):
        return
    meta[field] = {"value": text, "where": where, "confidence": confidence}


def _provider_for_host(host: str) -> Optional[str]:
    host = (host or "").lower()
    for name, hosts in PROVIDER_HOSTS.items():
        if host in hosts:
            return name
    # Exact/subdomain only: `evil-makerworld.com` is not MakerWorld.
    if host == "makerworld.com" or host.endswith(".makerworld.com"):
        return "makerworld"
    return None


def _classify_url(raw: Optional[str]) -> Tuple[Optional[str], Optional[dict]]:
    """A provider URL as `("candidate", {...})`, `("unresolved", {...})` or None.

    A provider host with a model id is a candidate. A provider host with the model
    marker but no id is UNRESOLVED (a truncated extraction), never an invented id.
    A creator/social/collection page is not a model URL at all and is dropped.
    """
    text = (raw or "").strip().strip("<>\"'")
    text = text.rstrip(_TRAILING_JUNK).strip()
    if not text:
        return None, None
    normalised = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(normalised)
    except ValueError:
        return None, None
    provider = _provider_for_host(parsed.hostname or "")
    if provider is None:
        return None, None
    path = parsed.path or ""
    if _NON_MODEL_PATH.search(path):
        return None, None
    if parsed.query and path.rstrip("/") in _BARE_MODEL_PATHS:
        return None, None
    match = PROVIDER_MODEL_ID[provider].search(path)
    subject_id = None
    if match:
        subject_id = next((group for group in match.groups() if group), None)
    if subject_id:
        return "candidate", {
            "url": normalised, "provider": provider, "subject_id": subject_id,
        }
    return "unresolved", {
        "text": text, "provider": provider,
        "reason": "provider URL without a model id (truncated?)",
    }


def _iter_urls(text: str) -> Iterable[str]:
    seen = set()
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?'\")]}")
        if url and url not in seen:
            seen.add(url)
            yield url


def _note_alternative(existing: dict, where: str, confidence: str) -> None:
    """Record a second sighting of an already-known subject, bounded, without noise."""
    item = {"where": where, "confidence": confidence}
    if item == {"where": existing["where"], "confidence": existing["confidence"]}:
        return
    also = existing.setdefault("also_in", [])
    if item in also or len(also) >= PROVENANCE_ALSO_IN_MAX:
        return
    also.append(item)


def _offer_provenance(prov: dict, raw: Optional[str], where: str,
                      confidence: str) -> None:
    status, data = _classify_url(raw)
    if status == "candidate":
        for existing in prov["candidates"]:
            if (existing["provider"], existing["subject_id"]) == (
                data["provider"], data["subject_id"]
            ):
                # Keep the strongest sighting as the face of the candidate; ties
                # keep the first. A medium text link must never suppress the
                # model's own HIGH annotation from another document, so the
                # weaker location moves to `also_in` and stays visible.
                if _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[existing["confidence"]]:
                    previous_where = existing["where"]
                    previous_confidence = existing["confidence"]
                    existing["url"] = data["url"]
                    existing["where"] = where
                    existing["confidence"] = confidence
                    _note_alternative(existing, previous_where, previous_confidence)
                else:
                    _note_alternative(existing, where, confidence)
                return
        if len(prov["candidates"]) >= PROVENANCE_MAX:
            return
        prov["candidates"].append({
            "url": data["url"], "provider": data["provider"],
            "subject_id": data["subject_id"], "where": where,
            "confidence": confidence,
        })
    elif status == "unresolved":
        if any(item["text"] == data["text"] for item in prov["unresolved"]):
            return
        if len(prov["unresolved"]) >= PROVENANCE_UNRESOLVED_MAX:
            return
        prov["unresolved"].append({
            "text": data["text"], "where": where, "reason": data["reason"],
        })


def _first_provider_url(text: str) -> Optional[str]:
    for url in _iter_urls(text):
        status, _data = _classify_url(url)
        if status == "candidate":
            return url
    return None


def _parse_vendor_text(text: str, where: str, meta: dict, prov: dict) -> None:
    """The vendor markers a text document or a PDF page can state.

    Strength is deliberate: the Thingiverse pair (`created by … user`, `licensed
    under …`) and the `<title> by <creator> on Thingiverse: <url>` line are explicit
    machine markers and HIGH; an attribution card's first line, a `Licensed under:`
    next line and a Summary block are heuristics and MEDIUM. A licence is only ever
    what the text says - never invented, and never given a URL the table cannot back.
    """
    body = (text or "")[:DOC_CHAR_CAP]
    if not body:
        return
    for match in README_MARKERS.finditer(body):
        if match.group("creator"):
            _offer(meta, "creator", _trim(match.group("creator")), where, "high")
        token = _trim(match.group("licence"))
        if token and token.casefold() not in {"a", "an", "the", ":", "-"}:
            _offer_licence(meta, token, where, "high")

    license_line = LICENSE_LINE.search(body)
    if license_line is not None:
        title = _trim_to_last_sentence(_trim(license_line.group("title")))
        creator = _trim(license_line.group("creator"))
        url = license_line.group("url").rstrip(").,")
        _offer(meta, "title", title, where, "high")
        _offer(meta, "creator", creator, where, "high")
        if url:
            _offer(meta, "source_url", url, where, "high")
            _offer_provenance(prov, url, where, "high")

    lines = [line.strip() for line in body.splitlines()]
    summary: List[str] = []
    for index, line in enumerate(lines):
        licensed = _LICENSED_UNDER_LINE.search(line)
        if licensed is not None:
            inline = line[licensed.end():].strip().lstrip(":").strip()
            if inline and inline.casefold() not in {"a", "an", "the"}:
                _offer_licence(meta, _trim(inline), where, "medium")
            else:
                for nxt in lines[index + 1:]:
                    if nxt:
                        _offer_licence(meta, _trim(nxt), where, "medium")
                        break
        if _SUMMARY_LINE.match(line) and not summary:
            for nxt in lines[index + 1:]:
                if not nxt:
                    break
                summary.append(nxt)

    first = next((line for line in lines if line), "")
    if license_line is None and first and " on Thingiverse:" not in first:
        attribution = _ATTRIBUTION_FIRST_LINE.match(first)
        if attribution:
            _offer(meta, "title", _trim(attribution.group("title")), where, "medium")
            _offer(meta, "creator", _trim(attribution.group("creator")), where, "medium")

    if summary:
        _offer(meta, "description", " ".join(summary), where, "medium")
    elif "description" not in meta and "readme" in os.path.basename(where).casefold():
        # Leading prose is a description only in a README. A `LICENSE.txt` whose
        # first line is "This thing was created by …" is not a summary, and a
        # licence token is not one either.
        marker_at = len(body)
        for marker in README_MARKERS.finditer(body):
            marker_at = min(marker_at, marker.start())
        leading = body[:marker_at].strip()
        # An attribution card's first line is `<title> by <creator>`, not prose;
        # treating it as a description would file the title as the summary.
        if 3 <= len(leading) <= 400 and not _ATTRIBUTION_FIRST_LINE.match(leading):
            _offer(meta, "description", leading, where, "medium")

    for url in _iter_urls(body):
        _offer_provenance(prov, url, where, "medium")
    if "source_url" not in meta:
        first_url = _first_provider_url(body)
        if first_url:
            _offer(meta, "source_url", first_url, where, "medium")


def _json_suggestions(text: str, where: str, meta: dict, prov: dict) -> int:
    """`info.json`: the one vendor document whose keys are machine markers (HIGH)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ValueError("not JSON") from error
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")

    def pick(*keys: str) -> Optional[str]:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    _offer(meta, "title", pick("title", "name"), where, "high")
    _offer(meta, "creator", pick("creator", "author", "designer"), where, "high")
    _offer_licence(meta, pick("license", "licence"), where, "high")
    url = pick("url", "source_url", "thingiverse_url")
    if url:
        _offer(meta, "source_url", url, where, "high")
        _offer_provenance(prov, url, where, "high")
    _offer(meta, "description", pick("description"), where, "high")
    return len(text)


def _html_suggestions(text: str, where: str, meta: dict, prov: dict) -> int:
    """A vendor `.html` card: og:title/author/licence-link are markers (HIGH)."""
    body = (text or "")[:DOC_CHAR_CAP]
    og_title = re.search(
        r"<meta[^>]+property=[\"']og:title[\"'][^>]*content=[\"']([^\"']*)",
        body, re.IGNORECASE,
    ) or re.search(
        r"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*property=[\"']og:title[\"']",
        body, re.IGNORECASE,
    )
    if og_title:
        _offer(meta, "title", og_title.group(1), where, "high")
    document_title = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    if document_title:
        _offer(meta, "title", document_title.group(1), where, "medium")
    author = re.search(
        r"<meta[^>]+name=[\"']author[\"'][^>]*content=[\"']([^\"']*)",
        body, re.IGNORECASE,
    ) or re.search(
        r"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*name=[\"']author[\"']",
        body, re.IGNORECASE,
    )
    if author:
        _offer(meta, "creator", author.group(1), where, "high")
    licence_link = re.search(
        r"<link[^>]+rel=[\"']license[\"'][^>]*href=[\"']([^\"']+)",
        body, re.IGNORECASE,
    ) or re.search(
        r"<link[^>]+href=[\"']([^\"']+)[\"'][^>]*rel=[\"']license[\"']",
        body, re.IGNORECASE,
    )
    if licence_link:
        href = licence_link.group(1)
        _set_licence(meta, _licence_display_from_url(href), href, where, "high")
    # Markup is not prose: take the URLs, but never let a tag soup become a
    # description the way `_parse_vendor_text`'s leading-prose rule would.
    for url in _iter_urls(body):
        _offer_provenance(prov, url, where, "medium")
    if "source_url" not in meta:
        first_url = _first_provider_url(body)
        if first_url:
            _offer(meta, "source_url", first_url, where, "medium")
    return len(body)


def _pdf_suggestions(path: str, rel: str, meta: dict, prov: dict) -> int:
    """A PDF's document info, text and link annotations. Untrusted input, bounded.

    `PDF_MAX_PAGES` is 4 because every attribution card keeps its facts on page 1;
    a 67-page manual is still read only at its head. A parse failure is raised to
    the caller, which records the document as `unreadable` - never a crash.
    """
    from pypdf import PdfReader

    reader = PdfReader(path)
    chars = 0
    info = getattr(reader, "metadata", None)
    if info is not None:
        title = getattr(info, "title", None)
        author = getattr(info, "author", None)
        if title and str(title).strip().casefold() not in BOILERPLATE_TITLES:
            _offer(meta, "title", str(title), f"{rel} info", "low")
        if author and str(author).strip():
            _offer(meta, "creator", str(author), f"{rel} info", "high")
    pages = list(reader.pages)[:PDF_MAX_PAGES]
    for number, page in enumerate(pages, start=1):
        where = f"{rel} p{number}"
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text:
            chars += len(text)
            _parse_vendor_text(text, where, meta, prov)
        try:
            annotations = page.get("/Annots")
        except Exception:
            annotations = None
        for reference in annotations or []:
            try:
                obj = reference.get_object()
                action = obj.get("/A")
                uri = action.get("/URI") if action is not None else None
            except Exception:
                continue
            if not uri:
                continue
            uri_text = str(uri)
            where_annot = f"{rel} annotations p{number}"
            _offer_provenance(prov, uri_text, where_annot, "high")
            status, data = _classify_url(uri_text)
            if status == "candidate":
                # HIGH, and it may replace a text-derived medium: the annotation
                # is the model's own link, the text is often a related-model link.
                _offer(meta, "source_url", data["url"], where_annot, "high")
    return chars


def _document_kind(ext: str) -> Optional[str]:
    if ext == ".pdf":
        return "pdf"
    if ext == ".json":
        return "json"
    if ext in {".html", ".htm"}:
        return "html"
    if ext in {".txt", ".md"}:
        return "text"
    return None


def _read_text_suggestions(abs_path: str, rel: str, kind: str,
                           meta: dict, prov: dict) -> int:
    with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read(TEXT_MAX_BYTES)
    text = text[:DOC_CHAR_CAP]
    if kind == "json":
        return _json_suggestions(text, rel, meta, prov)
    if kind == "html":
        return _html_suggestions(text, rel, meta, prov)
    _parse_vendor_text(text, rel, meta, prov)
    return len(text)


def _collect_facts(facts: Facts) -> None:
    """Read the folder's documents in a deterministic order and build every fact.

    Reading order is `(depth, path casefolded)`: root documents first, then the
    shallowest, so two runs over the same tree give one answer. A document over its
    cap is recorded as skipped rather than dropped, and a parse failure is a
    DOCUMENT (`skipped: "unreadable"`), never an exception out of `measure`.
    """
    candidates: List[Tuple[int, str, str]] = []
    for rel_dir, names in facts.meta.items():
        for name in names:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            candidates.append((rel.count("/"), rel.casefold(), rel))
    candidates.sort()
    read_so_far = 0
    for _depth, _folded, rel in candidates:
        abs_path = os.path.join(facts.path, rel)
        kind = _document_kind(os.path.splitext(rel)[1].lower())
        if kind is None:
            continue
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = 0
        record = {"path": rel, "kind": kind, "bytes": size,
                  "read": False, "skipped": None, "chars": 0}
        if kind == "pdf" and not PDF_DOCUMENTS_ENABLED:
            record["skipped"] = "disabled"
            facts.documents.append(record)
            continue
        limit = PDF_MAX_BYTES if kind == "pdf" else TEXT_MAX_BYTES
        if size > limit:
            record["skipped"] = "too-large"
            facts.documents.append(record)
            continue
        if read_so_far >= DOCS_MAX_PER_FOLDER:
            record["skipped"] = "cap"
            facts.documents.append(record)
            continue
        read_so_far += 1
        try:
            if kind == "pdf":
                chars = _pdf_suggestions(abs_path, rel, facts.metadata, facts.provenance)
            else:
                chars = _read_text_suggestions(
                    abs_path, rel, kind, facts.metadata, facts.provenance
                )
        except Exception:
            record["skipped"] = "unreadable"
            facts.documents.append(record)
            continue
        record["read"] = True
        record["chars"] = chars
        facts.documents.append(record)

    facts.groups = _build_groups(facts)
    facts.twin = _build_twin(facts)
    facts.cover = _build_cover(facts)


def _owns_product_doc(facts: Facts, rel_dir: str) -> bool:
    """Whether a directory's OWN root holds a product document.

    The same predicate `children_with_own_meta` uses, applied at any depth: a JSON/
    PDF/HTML document or a README/LICENCE/print-settings/info stem sitting DIRECTLY
    in the directory. A document one level deeper is payload, not a claim.
    """
    for name in facts.meta.get(rel_dir, []):
        stem, ext = os.path.splitext(name)
        if ext.lower() in {".json", ".pdf", ".html", ".htm"} or PRODUCT_DOC.search(stem):
            return True
    # Extension-less vendor documents (`README`, `LICENSE`) never reach `meta`.
    return bool(facts.dir_product_docs.get(rel_dir))


def _owns_readme_stem(facts: Facts, rel_dir: str) -> bool:
    for name in facts.dir_product_docs.get(rel_dir, []):
        if os.path.splitext(name)[0].casefold() in {"readme", "license", "licence"}:
            return True
    return False


def _build_groups(facts: Facts) -> List[dict]:
    """The hierarchical groups a report renders: directories that hold models below.

    `foreign` is the legacy R7 definition, preserved exactly: the name carries a
    compound vendor id of 6-8 digits (`COMPOUND_FOREIGN_ID`), or the group's OWN
    root (exactly one level down) holds a README/LICENSE/LICENCE stem.
    `separable` is not "is a product" but "could stand as its own project": its
    own root carries a product document (or its name carries a vendor id), and its
    name does not read as a part.

    The measured Vault tree (`/mnt/ceph/3D_models/Vault/
    Vault Archive_18.11.2026/`) witnesses both flags:

      Instructions Manual_14.11.26.pdf      <- at the wrapper's own root
      01. DRAWERS/ -> 01. S Families/ -> {Facades, S3, S4, S5, S6, S7} -> parts
                   -> 02. M Families/ ... 07. Universal/
      02. SUSTAINERS/   03. MODIFICATIONS/

    `Vault Archive_18.11.2026` IS `separable: true` because the manual sits at its
    own root, so the wrapper could stand as its own project. `01. DRAWERS` and
    `01. S Families` are NOT separable: their names read as parts (the ordinal
    branch of `PART_NAME`) and neither owns a product document at its own root.
    A document in a child documents the child, not the parent.
    """
    direct: Dict[str, int] = {
        path: count for path, count in facts.model_dirs.items() if path != "."
    }
    if not direct:
        return []
    nodes = set()
    for path in direct:
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            nodes.add("/".join(parts[:index]))
    children_of: Dict[str, List[str]] = {}
    for path in nodes:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        children_of.setdefault(parent, []).append(path)

    def subtree_models(path: str) -> int:
        return sum(
            count for key, count in direct.items()
            if key == path or key.startswith(path + "/")
        )

    def node(path: str, depth: int) -> dict:
        name = path.rsplit("/", 1)[-1]
        kids = sorted(
            children_of.get(path, []),
            key=lambda item: item.rsplit("/", 1)[-1].casefold(),
        )
        truncated = False
        if len(kids) > GROUPS_MAX_CHILDREN:
            kids = kids[:GROUPS_MAX_CHILDREN]
            truncated = True
        children: List[dict] = []
        if depth < GROUPS_MAX_DEPTH:
            children = [node(kid, depth + 1) for kid in kids]
        elif kids:
            # The depth cap bit: the node keeps its subtree count but not its
            # children, and says so.
            truncated = True
        record = {
            "name": name,
            "path": path,
            "models": subtree_models(path),
            "foreign": bool(
                COMPOUND_FOREIGN_ID.search(name)
                or _owns_readme_stem(facts, path)
            ),
            "separable": _group_separable(facts, path, name),
            "children": children,
        }
        if truncated:
            record["truncated"] = True
        return record

    roots = sorted(
        children_of.get("", []),
        key=lambda item: item.rsplit("/", 1)[-1].casefold(),
    )
    return [node(root, 1) for root in roots]


def _group_separable(facts: Facts, path: str, name: str) -> bool:
    # A part-shaped name is never a project boundary, whatever document it holds:
    # `01. DRAWERS` is a sub-assembly of the vault, not a product.
    if PART_NAME.match(name):
        return False
    return bool(vendor_id(name)) or _owns_product_doc(facts, path)


def _build_cover(facts: Facts) -> dict:
    """The report's cover preference, from disk: largest image, then model preview.

    Registered-ness is app state and stays out: this reports candidates and why,
    and the app decides what it can serve. Ties break by path so the pick is a
    function of the tree.
    """
    images = sorted(facts._images, key=lambda item: (-item[1], item[0].casefold()))
    previews = sorted(facts._previews, key=lambda item: (-item[1], item[0].casefold()))
    ranked = [
        {"path": path, "bytes": size, "reason": "largest-image"}
        for path, size in images
    ] + [
        {"path": path, "bytes": size, "reason": "model-preview"}
        for path, size in previews
    ]
    if images:
        reason = "largest-image"
    elif previews:
        reason = "model-preview"
    else:
        reason = "none"
    return {
        "reason": reason,
        "chosen": ranked[0] if ranked else None,
        "candidates": ranked[:COVER_MAX_CANDIDATES],
    }


def is_macosx_artifact(rel_path: str) -> bool:
    """Whether this entry is AppleDouble cruft rather than the archive's payload."""
    head = (rel_path or "").split("/", 1)[0]
    return head.casefold() == MACOSX_ROOT


def twin_key(name: str) -> str:
    """The identity a folder and a `.zip` share: casefolded, one `.zip` off.

    THE one definition (Mf-1agz): the archive pairing in `bundle_service` and the
    twin verification below both read it, so a folder and its zip cannot be keyed
    two ways.
    """
    key = (name or "").strip().casefold()
    if key.endswith(".zip"):
        key = key[: -len(".zip")]
    return key


def common_entry_root(rel_paths: Iterable[str]) -> Optional[str]:
    """The one top-level directory every entry sits under, or None.

    AppleDouble cruft is ignored, entries are compared casefolded, and a loose entry
    at the top level (or two different first segments) means there is no single
    wrapper.

    THE one definition (Mf-1agz): the archive pairing in `bundle_service` reads it
    rather than keeping a second copy free to drift.
    """
    root: Optional[str] = None
    for rel_path in rel_paths:
        if is_macosx_artifact(rel_path):
            continue
        head, separator, _rest = (rel_path or "").partition("/")
        if not separator:
            return None
        if root is None:
            root = head
        elif head.casefold() != root.casefold():
            return None
    return root


def archive_payload_contained(archive_entries: Iterable[Tuple[str, int]],
                              folder_entries: Iterable[Tuple[str, int]],
                              entry_root: Optional[str] = None) -> bool:
    """Whether this archive's payload is contained in this directory's listing.

    THE one containment predicate, asked by both archive/directory pairing routes
    (the stem route and the named-entry-root route), so they cannot disagree about
    what an archive's payload is or about when it fits a folder. It judges two
    listings and a string; it never touches disk.

    Containment, not equality: a lived-in directory legitimately holds more than
    the archive that shipped it. Declared size is an archive HEADER field, so this
    decides pairing and is never an integrity claim. The skip is
    `is_macosx_artifact`, so an archive of cruft alone has verified nothing and is
    refused.

    ``entry_root`` given is the strict strip-and-compare: every payload entry's
    first segment must casefold-equal the root and the remainder plus its declared
    size must sit in the folder. An entry outside the root is ``False``, and there
    is deliberately no identity shortcut on this route - trying one would silently
    widen the rule that produced the root.

    ``entry_root is None`` is the stem route, where nobody named the wrapper: the
    wrapper decision is made ONCE for the whole archive, never per entry and never
    twice - identity first, else a single `common_entry_root` strip. Two strips are
    refused on purpose, because that would pair a wrapper's wrapper with anything
    nested one level deeper.
    """
    entries = [
        (str(path), int(size)) for path, size in archive_entries
        if not is_macosx_artifact(str(path))
    ]
    if not entries:
        return False
    folder = {(str(path).casefold(), int(size)) for path, size in folder_entries}
    if entry_root is not None:
        stripped = _payload_without_root(entries, entry_root)
        return stripped is not None and stripped <= folder
    if {(path.casefold(), size) for path, size in entries} <= folder:
        return True
    root = common_entry_root([path for path, _size in entries])
    if root is None:
        return False
    stripped = _payload_without_root(entries, root)
    return stripped is not None and stripped <= folder


def _payload_without_root(payload: Iterable[Tuple[str, int]],
                          entry_root: str) -> Optional[set]:
    """Every payload entry with ``entry_root``'s first segment split off, casefolded.

    ``None`` as soon as one entry sits outside the root. The first segment is split
    off rather than sliced by the length of a casefolded prefix: casefolding may
    change a string's length, and a slice would then cut the tail in the wrong place.
    """
    wanted = (entry_root or "").casefold()
    stripped = set()
    for path, size in payload:
        head, separator, rest = path.partition("/")
        if not separator or head.casefold() != wanted:
            return None
        stripped.add((rest.casefold(), size))
    return stripped


def _payload_contained(payload: Iterable[Tuple[str, int]],
                       folder: Iterable[Tuple[str, int]]) -> bool:
    """`archive_payload_contained` on the stem route: the twin's own call."""
    return archive_payload_contained(payload, folder)


def _folder_listing(directory: str, max_entries: int) -> Tuple[set, int, bool]:
    """A directory's files as `(casefolded rel, size)`, its model count, truncation."""
    listing: set = set()
    models = 0
    truncated = False
    stack: List[Tuple[str, str]] = [("", directory)]
    while stack:
        rel_dir, absolute = stack.pop()
        files, dirs = read_dir(absolute)
        for entry in files:
            if len(listing) >= max_entries:
                truncated = True
                continue
            rel = entry.name if not rel_dir else f"{rel_dir}/{entry.name}"
            listing.add((rel.casefold(), entry.size))
            if entry.ext in MODEL_EXTS:
                models += 1
        for name in dirs:
            stack.append((name if not rel_dir else f"{rel_dir}/{name}",
                          os.path.join(absolute, name)))
    return listing, models, truncated


def _zip_payload(path: str) -> Tuple[Optional[List[Tuple[str, int]]], Optional[str]]:
    """An archive's read-only payload, or `(None, reason)` when it cannot be read."""
    import zipfile
    try:
        if os.path.getsize(path) > TWIN_MAX_BYTES:
            return None, f"archive larger than TWIN_MAX_BYTES ({TWIN_MAX_BYTES})"
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > TWIN_MAX_ENTRIES:
                return None, f"more than TWIN_MAX_ENTRIES ({TWIN_MAX_ENTRIES}) entries"
            payload = []
            for info in infos:
                name = info.filename.replace("\\", "/").lstrip("/")
                if not name or is_macosx_artifact(name):
                    continue
                payload.append((name, info.file_size))
        return payload, None
    except (OSError, Exception):
        return None, "archive could not be read"


def _verify_pair(archive_rel: str, folder_rel: str, archive_path: str,
                 folder_path: str) -> Tuple[dict, Optional[dict]]:
    """Compare one archive against one folder; return (pair, unverified-or-None)."""
    payload, problem = _zip_payload(archive_path)
    if payload is None:
        pair = {
            "archive": archive_rel, "folder": folder_rel,
            "models_in_archive": 0, "models_in_folder": 0,
            "confirmed": False, "reason": problem or "unreadable",
        }
        return pair, {"archive": archive_rel, "folder": folder_rel,
                      "reason": problem or "unreadable"}
    listing, folder_models, truncated = _folder_listing(folder_path, TWIN_MAX_ENTRIES)
    archive_models = sum(1 for name, _size in payload if os.path.splitext(name)[1].lower() in MODEL_EXTS)
    confirmed = _payload_contained(payload, listing)
    if truncated:
        return (
            {"archive": archive_rel, "folder": folder_rel,
             "models_in_archive": archive_models, "models_in_folder": folder_models,
             "confirmed": False, "reason": "folder listing hit TWIN_MAX_ENTRIES"},
            {"archive": archive_rel, "folder": folder_rel,
             "reason": "folder listing hit TWIN_MAX_ENTRIES"},
        )
    reason = (
        "payload is contained in the folder (casefolded path + declared size)"
        if confirmed else
        "payload differs from the folder listing"
    )
    pair = {
        "archive": archive_rel, "folder": folder_rel,
        "models_in_archive": archive_models, "models_in_folder": folder_models,
        "confirmed": confirmed, "reason": reason,
    }
    unverified = None if confirmed else {
        "archive": archive_rel, "folder": folder_rel, "reason": reason,
    }
    return pair, unverified


def _build_twin(facts: Facts) -> dict:
    """Archive/directory pairs the tree claims, verified by containment.

    Two candidate layouts, one predicate: a root archive whose stem matches a child
    directory (`Facts.twin_archives()`), and the case where the candidate IS the
    wrapper whose zip sits one level up (`_own_wrapper_archive`). Verified by
    opening the zip read-only and comparing its payload against the paired folder;
    the declared sizes are archive header fields, so this decides pairing and is
    never an integrity claim.
    """
    pairs: List[dict] = []
    unverified: List[dict] = []
    for archive in facts.twin_archives():
        stem = twin_key(archive)
        child = next(
            (name for name in facts.children if twin_key(name) == stem), None
        )
        if child is None:
            continue
        pair, failed = _verify_pair(
            archive, child,
            os.path.join(facts.path, archive),
            os.path.join(facts.path, child),
        )
        pairs.append(pair)
        if failed is not None:
            unverified.append(failed)
    own = _own_wrapper_archive(facts)
    if own is not None:
        pair, failed = _verify_pair(
            f"../{own}", ".",
            os.path.join(os.path.dirname(facts.path), own),
            facts.path,
        )
        pairs.append(pair)
        if failed is not None:
            unverified.append(failed)
    return {
        "verified": any(pair["confirmed"] for pair in pairs),
        "pairs": pairs,
        "unverified": unverified,
    }


# --- interpretations ------------------------------------------------------
#
# An interpretation is a NAMED outcome with a size and a consequence. The app
# should offer these as choices; this tool prints the ones the facts support,
# ranked, with the reason each is or is not recommended.

class Option:
    def __init__(self, shape: str, label: str, detail: str, consequence: str,
                 score: int, confidence: str = "low", note: Optional[str] = None):
        self.shape = shape
        self.label = label
        self.detail = detail
        self.consequence = consequence
        self.score = score
        # `high`/`medium`/`low` from the evidence that supports this reading. In
        # the guided flow medium and low only choose a DEFAULT; they never remove
        # an option.
        self.confidence = confidence
        # What the row must tell the operator when the option needs explaining.
        # Normally absent; today only the unreadable-boundary `one-project` carries
        # one, because its confidence is deliberately medium and the note is why
        # (Mf-ky16).
        self.note = note

    def as_dict(self) -> dict:
        return {
            "shape": self.shape,
            "label": self.label,
            "detail": self.detail,
            "consequence": self.consequence,
            "score": self.score,
            "confidence": self.confidence,
            "note": self.note,
        }


def parse_document_kind(facts: Facts) -> Optional[str]:
    """What the folder's own metadata claims, if anything readable and small."""
    for name in facts.root_meta:
        ext = os.path.splitext(name)[1].lower()
        if ext not in {".json", ".txt", ".md"}:
            continue
        try:
            with open(os.path.join(facts.path, name), "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        lowered = head.lower()
        if "thingiverse" in lowered or "printables" in lowered or "myminifactory" in lowered:
            return f"vendor document ({name})"
        if "license" in lowered or "licence" in lowered:
            return f"licence document ({name})"
    return None


def propose(facts: Facts, project_keys: Sequence[str], *,
            probe_git: bool = True,
            git_timeout: Optional[float] = None) -> Tuple[List[Option], List[str], Optional[dict]]:
    """Every interpretation the facts support, ranked; plus the reasons.

    `probe_git=False` skips `git_state` entirely and reports no repo; the default is
    the CLI's own tight loop, where a checkout is worth asking about. `git_timeout`
    bounds that probe (Mf-1agz): `git_state` gives its whole four-command budget
    this many seconds, so the report - which reads inside a GET and probes git
    because the label corpus is 7/7 with probing and 6/7 without - pays at most
    that much for one hung checkout instead of `GIT_TIMEOUT_SECONDS`. `None` keeps
    `git_state`'s own default, so every CLI caller is unchanged.
    """
    reasons: List[str] = []
    options: List[Option] = []
    tops = facts.top_children_with_models()
    twins = facts.twin_archives()
    part_like = facts.part_like_children()
    # A twin child is normally the parent's own payload (Vault: one extracted
    # archive holds 793 of 794 models). But a child that ships its OWN archive is a
    # product in its own right and keeps its metadata claim (endorphin: five
    # ingredients, each with README + LICENSE + a matching .zip). The share of the
    # parent's models is what tells them apart.
    twins_plain = {os.path.splitext(name)[0].casefold() for name in twins}
    # A child that IS the extracted twin of a sibling archive AND holds nearly all of
    # the parent's models is the parent's packaging, whatever documents sit inside it:
    # Vault's `Vault Archive_…/` keeps the manual, and the product is "Vault". Its own
    # documents say nothing about being a separate product.
    dominant_twins = {
        name for name, count in facts.top_children_with_models()
        if name.casefold() in twins_plain and facts.models and count / facts.models >= 0.9
    }
    own_meta = [
        name for name in facts.children_with_own_meta()
        if name not in dominant_twins
    ]
    doc = parse_document_kind(facts)

    repo = git_state(facts.path, timeout=git_timeout) if probe_git else None
    git_error = repo.get("git_error") if repo is not None else None
    if git_error:
        # Name the cause in the same sentence the row carries: the operator must see
        # that the boundary could not be read, not merely that something was skipped.
        reasons.append(unknown_boundary_note(git_error))
    if repo is not None and git_error is None:
        tracked_models = sum(
            1 for rel in repo["tracked"] if os.path.splitext(rel)[1].lower() in MODEL_EXTS
        )
        where = repo["branch"] or (f"detached at {repo.get('sha') or '?'}")
        if tracked_models:
            reasons.append(
                f"git checkout on {where} ({repo['head'].split()[0] if repo['head'] else '?'}): "
                f"{len(repo['tracked'])} tracked files ({tracked_models} models) belong to this project, "
                f"{len(repo['untracked'])} untracked entries are materials"
            )
            options.append(Option(
                "git-project",
                "one project (the repo's own content)",
                f"{len(repo['tracked'])} tracked files, {tracked_models} of them models; "
                f"{len(repo['untracked'])} untracked entries peel off",
                "the working tree declares the boundary: no guessing about what is a part",
                95,
            ))
        else:
            # A checkout with no model in its tracked set would tell the operator to
            # import nothing. Record why it is not offered instead.
            reasons.append(
                f"git checkout on {where} tracked {len(repo['tracked'])} files but no model; "
                f"git-project is not offered because there is nothing to import"
            )

    inside = next((key for key in project_keys if facts.rel.startswith(key + "/")), None)
    if inside:
        reasons.append(f"sits inside the existing project key {inside!r}")
        options.append(Option(
            "add-to-project",
            f"add as parts to {inside!r}",
            f"{facts.models} models, {facts.docs} docs, {facts.archives} archives "
            f"under {len(tops)} folders",
            "extends the project the operator already owns; nothing is duplicated",
            90,
        ))

    # One product. The corpus says this is the overwhelmingly common truth: 153 of
    # 158 folders measured are one product.
    one_score = 40
    if twins:
        one_score += 20
        reasons.append(
            f"{len(twins)} archive(s) match an extracted folder by stem - provenance, not products"
        )
    if part_like and len(part_like) >= max(2, len(tops) // 2):
        one_score += 15
        reasons.append(
            f"{len(part_like)} of {len(tops)} top folders read as parts ({', '.join(part_like[:4])})"
        )
    if facts.depth >= 2:
        one_score += 10
        reasons.append(f"models reach depth {facts.depth} - a hierarchy inside one product")
    if doc:
        one_score += 10
        reasons.append(f"a {doc} at the root claims the folder as one product")
    if facts.dominance() >= 0.75:
        one_score += 5
        reasons.append(f"one folder holds {facts.dominance():.0%} of the models")
    # A child carrying its own product document CONTRADICTS "one product": the
    # folder is a collection. Endorphin proves it - five untracked ingredients,
    # each with README + LICENSE and two with a Thingiverse id in the name - against
    # Stagetop's nine parts, which carry no document anywhere.
    if own_meta:
        one_score -= 30
    if len(own_meta) >= 2 and len(own_meta) * 2 >= len(tops):
        one_score -= 20
        reasons.append(
            f"{len(own_meta)} of {len(tops)} top folders carry their own document - a collection, not one product"
        )

    options.append(Option(
        "one-project",
        "one project",
        f"{facts.models} models, {facts.images} images, {facts.docs} docs, "
        f"{facts.archives} archives in {len(facts.model_dirs)} model-bearing folders",
        f"parts become groups inside one project ({len(tops)} top folders)",
        one_score,
    ))

    # Split. Only plausible when the children look like products, not parts. An
    # unreadable checkout is never evidence FOR a split: the split arms stay shut.
    if len(tops) >= 2 and git_error is None:
        ids = facts.children_with_vendor_id()
        split_score = 20 + (30 if own_meta else 0) + (15 if ids else 0) + (10 if not part_like else 0)
        if ids:
            reasons.append(
                f"{len(ids)} top folder(s) carry a vendor product id ({', '.join(ids[:3])})"
            )
        if own_meta:
            reasons.append(
                f"{len(own_meta)} top folders carry their own metadata "
                f"({', '.join(own_meta[:3])}) - each claims to be a product"
            )
        else:
            reasons.append("no top folder carries its own metadata")
        options.append(Option(
            SPLIT_SHAPES[0],
            f"split into {len(tops)} projects",
            ", ".join(f"{name} ({count})" for name, count in tops[:6])
            + (" ..." if len(tops) > 6 else ""),
            "one project per top folder; fragments a single product if it was one",
            split_score,
        ))

    # Split by the shallowest model-bearing folders (the fine-grained variant).
    shallow = [d for d in facts.model_dirs if d != "." and d.count(os.sep) + 1 == 1]
    if git_error is None and len(shallow) >= 3 and len(shallow) > len(tops):
        options.append(Option(
            SPLIT_SHAPES[1],
            f"split into {len(shallow)} projects",
            ", ".join(sorted(shallow, key=str.casefold)[:6]) + (" ..." if len(shallow) > 6 else ""),
            "a project per model-bearing folder at depth 1",
            10,
        ))

    if facts.models == 0 and facts.archives:
        options.append(Option(
            "archive-only", f"{facts.archives} archive(s) only", "no models on disk",
            "extract first; the shape is unknown until something is unpacked", 60,
        ))
    if facts.models == 0 and not facts.archives:
        options.append(Option("leave", "leave alone", "no models, no archives",
                              "nothing to import", 70))

    options.sort(key=lambda o: (-o.score, o.shape))
    _assign_confidence(options, facts)
    if git_error:
        _force_unknown_boundary(options, git_error)
    return options, reasons, repo


def _documented_at_root(facts: Facts) -> bool:
    """Whether a HIGH-confidence field comes from a document at the candidate's OWN root.

    `where` is a rel path (plus an optional page suffix). A document one level down is
    a CHILD's document: `OutOfMarbles`' three PDFs sit in its children and its own
    root holds none, so they document the children, never the collection.
    """
    for field in ("title", "creator", "licence", "source_url"):
        entry = facts.metadata.get(field)
        if entry and entry.get("confidence") == "high":
            where = entry.get("where") or ""
            if "/" not in where:
                return True
    return False


def _assign_confidence(options: List[Option], facts: Facts) -> None:
    """Set each option's confidence from the evidence that supports THAT reading.

    * `high` - documentary evidence backs it: a `high`-confidence title, creator,
      licence or source_url whose `where` is a document at the candidate's OWN
      root (no `/` in the path) - the folder itself is the documented product -
      or git's own declaration of the boundary (and its operator-declared cousin,
      an existing project key).
    * `medium` - structural evidence with a named discriminator (twin archives,
      dominance >= 0.75, depth >= 2, part-shaped names, a separable split) AND a
      clear score margin over the runner-up (>= 15 points).
    * `low` - the reading rests on score/name alone, or the top two options are
      within 15 points.

    A document in a CHILD documents the child, not the parent: `OutOfMarbles`'
    three PDFs sit in its children and its own root holds none, so they do not
    make `one-project` a documented reading of the collection.

    INVARIANT: an alternative never outranks the recommendation. If its own
    evidence would put it in a higher band, it is capped at the recommendation's
    band - a more-certain alternative would mean the SCORE is wrong - so the
    disagreement stays visible in the reasons rather than being dressed up as a
    more certain pick than the one actually recommended.

    Medium and low only guide a DEFAULT in the guided flow; they NEVER remove an
    option. Every option stays on the list whatever its confidence.
    """
    if not options:
        return
    top = options[0].score
    runner_up = options[1].score if len(options) > 1 else None
    contested = runner_up is not None and (top - runner_up) < 15
    documentary = _documented_at_root(facts)
    structural = {
        "one-project": bool(facts.twin["verified"] or facts.twin_archives())
        or facts.depth >= 2
        or facts.dominance() >= 0.75
        or len(facts.part_like_children()) >= 2,
        "split-top-folders": bool(facts.children_with_own_meta())
        or bool(facts.children_with_vendor_id()),
        "add-to-project": True,
        "git-project": True,
    }
    for index, option in enumerate(options):
        if option.shape in {"git-project", "add-to-project"}:
            option.confidence = "high"
        elif documentary and option.shape in ONE_PROJECT_SHAPES:
            option.confidence = "high"
        elif contested:
            option.confidence = "low"
        elif index == 0 and structural.get(option.shape, False):
            option.confidence = "medium"
        else:
            option.confidence = "low"

    # INVARIANT: an alternative never outranks the recommendation. If its own
    # evidence would put it in a higher band, cap it at the recommendation's band:
    # a more certain alternative would mean the SCORE is wrong, and the
    # disagreement belongs in the reasons (where it is visible), not dressed up as
    # a more certain pick than the one actually recommended.
    top_band = _CONFIDENCE_RANK[options[0].confidence]
    for option in options[1:]:
        if _CONFIDENCE_RANK[option.confidence] > top_band:
            option.confidence = options[0].confidence


def _force_unknown_boundary(options: List[Option], git_error: str) -> None:
    """Make the unreadable-boundary `one-project` the recommendation, and say why.

    A boundary that cannot be read is a fact about the OBSERVER, not about the
    folder, so the only honest reading is "this is one project until someone can
    read the boundary". The recommendation is forced AFTER sorting and confidence
    assignment because the structural evidence may well argue otherwise - and that
    argument is exactly the one that must not win.

    Its band is `medium`, deliberately NOT `high`: `one-project` is in
    `AUTO_SHAPES` and AUTO requires HIGH, so a high band would auto-import a project
    whose boundary nobody read. Medium sends the row to GUIDED, where the operator
    reads the note. Every remaining option is capped at medium for the same reason
    `_assign_confidence` caps alternatives: an `add-to-project` at high beside a
    medium recommendation would break the documented INVARIANT that an alternative
    never outranks the recommendation.
    """
    if not options:
        return
    recommendation = next((option for option in options if option.shape == "one-project"), None)
    if recommendation is None:
        return
    others = [option.score for option in options if option is not recommendation]
    options.remove(recommendation)
    options.insert(0, recommendation)
    # Above EVERY alternative, whatever the structure says: the boundary is unknown
    # and the only defensible answer is "one project".
    if others:
        recommendation.score = max(others) + 5
    recommendation.confidence = "medium"
    recommendation.note = unknown_boundary_note(git_error)
    for option in options[1:]:
        if _CONFIDENCE_RANK[option.confidence] > _CONFIDENCE_RANK["medium"]:
            option.confidence = "medium"


def peel_proposals(facts: Facts, repo: dict) -> List[dict]:
    """Each untracked entry in a git checkout, judged on its own.

    The materials an operator drops into a repo are separate products; the checkout
    itself is one. Asking the filesystem about each entry keeps this honest: a
    `zip`+folder pair is a complete product with its extracted twin, a loose model
    is a part, and only the second kind is attributed to the repo's own project.
    """
    out: List[dict] = []
    names = set(os.listdir(facts.path))
    for name in repo["untracked"]:
        if name not in names:
            continue
        path = os.path.join(facts.path, name)
        if os.path.isdir(path):
            child = measure(path, f"{facts.rel}/{name}")
            options, _, _ = propose(child, project_keys=())
            out.append({
                "name": name,
                "kind": "folder",
                "twin": f"{name}.zip" in names,
                "vendor_id": vendor_id(name),
                "models": child.models,
                "docs": child.docs,
                "labels": [o.label for o in options[:2]],
                "recommended": options[0].shape if options else "leave",
            })
        elif os.path.splitext(name)[1].lower() in MODEL_EXTS:
            out.append({
                "name": name, "kind": "loose model", "twin": False,
                "vendor_id": vendor_id(name), "models": 1, "docs": 0,
                "labels": ["part of the repo's own project"],
                "recommended": "part-of-project",
            })
    return out



def _series_match(facts: Facts) -> Optional[Tuple[str, str, int]]:
    """The series title a numbered folder inherits, with its evidence.

    Returns `(the renamed folder, the series name, how many siblings share it)`, or
    None when the folder is not numbered or has no series beside it. One measurement,
    two renderings: `series_title` wants the name, `series_suggestion` the argument.
    """
    match = re.match(r"^(\d{2,})[_.\- ]+(.*)$", facts.name)
    if not match:
        return None
    number, rest = match.group(1), match.group(2)
    try:
        siblings = sorted(os.listdir(os.path.dirname(facts.path)))
    except OSError:
        return None
    titles: Dict[str, int] = {}
    for name in siblings:
        sib = re.match(r"^\d{2,}[_.\- ]+(.+)$", name)
        if not sib:
            continue
        head = sib.group(1).split(" - ", 1)
        if len(head) == 2:
            stripped = re.sub(r"^\d{2,}[_.\- ]+", "", head[0]).strip()
            titles[stripped] = titles.get(stripped, 0) + 1
    if not titles:
        return None
    series, count = max(titles.items(), key=lambda kv: kv[1])
    if count < 3 or rest.startswith(series):
        return None
    return f"{number}_{series} - {rest}", series, count


def series_title(facts: Facts) -> Optional[str]:
    """The renamed folder name, e.g. "17_Fates End - ClockworkMonster", or None."""
    match = _series_match(facts)
    return match[0] if match is not None else None


def series_suggestion(facts: Facts) -> Optional[str]:
    """A numbered folder whose siblings share a title prefix inherits it.

    `17_ClockworkMonster` is a Fates End tower (owner, 2026-11-25); its siblings are
    `01_Fates End - Wizard Tower` ... `16_...`. The number is a series index, not
    clutter, so the proposal keeps it and supplies the missing series title. The name
    is rendered FROM `series_title`, so the rename the operator reads is the move the
    plan makes.
    """
    match = _series_match(facts)
    if match is None:
        return None
    title, series, count = match
    return f'rename to "{title}" (belongs to the {series} series: {count} siblings)'


def folder_title(name: str) -> str:
    """The product title of a folder name, keeping a numeric series prefix.

    A numbered folder's number is a series index, not clutter (`17_ClockworkMonster`
    is the 17th Fates End tower, owner 2026-11-25), so it survives: stripping it is
    the bug this function exists to not have. Only underscores and a `model files`
    tail are tidied away. A name left with no body falls back to itself - an unnamed
    product is worse than a badly named one.
    """
    prefix = re.match(r"^(\d{2,})[_.\- ]+", name)
    body = name[prefix.end():] if prefix else name
    body = re.sub(r"[-_ ]*model[_-]?files?$", "", body, flags=re.IGNORECASE)
    body = body.replace("_", " ").strip(" _-.")
    if not body:
        return name
    return f"{prefix.group(1)}_{body}" if prefix else body


# --- wrappers -------------------------------------------------------------
#
# A download lands as `<product> Archive_<date>.zip` plus the folder it extracted
# to, and that folder's name is the ARCHIVE's, not the product's. Three layouts are
# measured in the library and all three need the same answer - drop the wrapper,
# keep the product - but each hides the archive somewhere else, so each needs its
# own way of finding it: a zip beside the wrapper (Vault), a zip INSIDE the wrapper
# child (the same download unzipped one level down), and the candidate itself being
# the wrapper (the operator pointed the tool at the extracted folder).

# The shapes that already mean "one project". A wrapper is only proposed for
# dropping inside them: under a split it is one of the pieces being hoisted out.
ONE_PROJECT_SHAPES = ("one-project", "git-project")

# The two shapes that mean "this folder is SEVERAL projects". Named here because
# they are this module's own vocabulary: ``bundle_service`` reads them to offer the
# dissent and to refuse a create of that shape, and a literal restated there could
# drift from the table that produces it. Used at the two Option(...) sites in
# ``propose``.
SPLIT_SHAPES = ("split-top-folders", "split-model-folders")

# `Vault Archive_18.11.2026` -> `Vault`: the packaging tail a downloader bolts onto
# the product name. The tail must end the name or begin with a digit, so a product
# genuinely called `Archive Box` survives.
PACKAGING_TAIL = re.compile(r"[ _\-]+archives?(?:[ _\-.]+[0-9].*)?$", re.IGNORECASE)


def wrapper_title(name: str) -> str:
    """The product's name from a wrapper folder's name, never an empty one.

    A wrapper is named after its archive, so it carries the download's packaging
    tail: `Vault Archive_18.11.2026` is the product `Vault`, and the `_source/`
    directory below must be filed under the product, not under the packaging. A
    name that is nothing BUT packaging (`Archive.zip` extracted to `Archive`) falls
    back to itself: an unnamed project is worse than a badly named one.
    """
    title = PACKAGING_TAIL.sub("", name)
    title = re.sub(r"[ _\-]*model[ _\-]?files?[ _\-]*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"[ _\-]+", " ", title).strip(" _-.")
    return title or name


def _child_twin(facts: Facts, child: str) -> Optional[str]:
    """The archive a wrapper child was extracted from, found INSIDE that child.

    `Facts.twin_archives()` only scans the candidate's own root, so a download whose
    zip landed inside the extracted folder (`Vault/Vault Archive_18.11.2026/Vault
    Archive_18.11.2026.zip`) reads as a child with no provenance at all and the
    wrapper is never proposed for dropping. Returns the path relative to the
    candidate, or None.
    """
    files, _dirs = read_dir(os.path.join(facts.path, child))
    for entry in files:
        if entry.ext in ARCHIVE_EXTS and os.path.splitext(entry.name)[0].casefold() == child.casefold():
            return f"{child}/{entry.name}"
    return None


def _own_wrapper_archive(facts: Facts) -> Optional[str]:
    """The sibling archive this candidate was extracted from, if it IS a wrapper.

    The operator pointed the tool at `Vault Archive_18.11.2026/` while its zip sits
    one level up: the folder is packaging and its own name is the archive's, so the
    proposal must drop it rather than tidy its name.
    """
    try:
        names = os.listdir(os.path.dirname(facts.path))
    except OSError:
        return None
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext.lower() in ARCHIVE_EXTS and stem.casefold() == facts.name.casefold():
            return name
    return None


def _wrapper_child(facts: Facts) -> Optional[Tuple[str, int, List[str]]]:
    """The child that is packaging: its name, its model count, its archive(s).

    Two layouts, one answer - a zip beside the child (matched by stem, and only when
    the child carries nearly the whole folder's payload) or a zip INSIDE it (the zip
    is the provenance itself, so no share test). Returns None when no child is a
    wrapper: `endorphin`'s five ingredients each keep their own archive and are five
    products.
    """
    if not facts.models:
        return None
    root_twins = {os.path.splitext(name)[0].casefold() for name in facts.twin_archives()}
    for child, count in facts.top_children_with_models():
        if child == "(root files)":
            continue
        if child.casefold() in root_twins:
            if count / facts.models < 0.9:
                continue
            archives = [
                twin for twin in facts.twin_archives()
                if os.path.splitext(twin)[0].casefold() == child.casefold()
            ]
        else:
            inner = _child_twin(facts, child)
            if not inner:
                continue
            archives = [os.path.basename(inner)]
        return child, count, archives
    return None


def _wrapper_lines(facts: Facts, own_archive: Optional[str]) -> List[str]:
    """Drop-the-wrapper proposal for whichever of the three layouts the facts fit.

    A wrapper is never a product: its name is the archive's, its content is a part
    of the product inside it, and the project folder belongs at the level the
    wrapper occupied. The zip is always filed under the PRODUCT's `_source/`, which
    is why the title comes from `wrapper_title` and not from the wrapper.
    """
    if not facts.models:
        return []
    tops = facts.top_children_with_models()
    lines: List[str] = []
    title = wrapper_title(facts.name)
    match = _wrapper_child(facts)
    if match is None:
        if own_archive is None:
            return []
        wrapper, wrapper_models, archives = facts.name, facts.models, [own_archive]
        # The candidate IS the wrapper, so there is no parent to take a share of.
        inside = f"{wrapper_models} models inside it"
    else:
        wrapper, wrapper_models, archives = match
        inside = f"{wrapper_models} of {facts.models} models inside it"
    lines.append(
        f'drop the wrapper "{wrapper}/" while arranging: its name is the archive\'s, '
        f'not the product\'s ({inside})'
    )
    for archive in archives:
        lines.append(f'file "{archive}" in "{title}/_source/{archive}"')
    # Whatever is left carrying models is a part of the one product: it stays where
    # it is and becomes a group, so the operator can see the wrapper was the only
    # thing in the way.
    parts = [child for child, _count in tops if child not in (wrapper, "(root files)")]
    for child in parts[:4]:
        lines.append(f'keep "{child}/" as a part')
    if len(parts) > 4:
        lines.append(f"and {len(parts) - 4} more part(s)")
    return lines


def _rel_join(*parts: str) -> str:
    """Join storage-relative path parts. Never absolute, never a leading slash."""
    return "/".join(part.strip("/") for part in parts if part and part != ".")


def _entry_names(path: str) -> List[str]:
    """One directory's files and subdirectories, sorted: a move list has one order."""
    files, dirs = read_dir(path)
    return sorted([entry.name for entry in files] + dirs, key=str.casefold)


def _move(facts: Facts, source: str, target: str, action: str) -> dict:
    """One move record: two fields, two questions.

    `kind` (`dir` | `file`) is what the path IS on disk and what the mover must do;
    it is read here, from the source. `action` says WHY the move exists and is
    supplied by the call site: `hoist` | `move` | `file-archive` | `rename`.
    `retire-container` is deliberately NOT a token: a retired container is a
    property of the emptied container, not of any move record.
    """
    local = os.path.join(facts.path, os.path.relpath(source or ".", facts.rel or "."))
    return {
        "from": source,
        "to": target,
        "kind": "dir" if os.path.isdir(local) else "file",
        "action": action,
    }


def _identity_free(moves: List[dict]) -> List[dict]:
    """Drop a move that would rename nothing: a plan holds only real effects."""
    return [move for move in moves if move["from"] != move["to"]]


def _own_wrapper_moves(facts: Facts, archive: str) -> List[dict]:
    """Case (b): the candidate IS the wrapper, so its content rises one level.

    The product folder takes the wrapper's place and the archive is filed under it.
    Moves that would not move anything are dropped: a candidate already named after
    its product needs the archive filed, not its children renamed in place.
    """
    base = facts.rel
    parent = os.path.dirname(base)
    product_base = _rel_join(parent, wrapper_title(facts.name))
    sources = [
        (_rel_join(base, name), _rel_join(product_base, name), "move")
        for name in _entry_names(facts.path) if name != archive
    ]
    # The children rise into a DIFFERENT, newly named product folder: a move, not a
    # hoist (the candidate's name does not survive). The zip is filed as provenance.
    sources.append(
        (_rel_join(parent, archive), _rel_join(product_base, "_source", archive), "file-archive")
    )
    return _identity_free([
        _move(facts, source, target, action) for source, target, action in sources
    ])


def _wrapper_child_moves(facts: Facts, wrapper: str, archives: List[str]) -> List[dict]:
    """Case (a)/(c): the candidate holds the wrapper, so its content rises into it.

    The candidate already IS the product folder (`Vault/`), so the wrapper's children
    move up one level and the archive is filed under `_source/` - whether that zip
    sat beside the wrapper or inside it.
    """
    base = facts.rel
    inner = _child_twin(facts, wrapper)
    inner_archive = os.path.basename(inner) if inner else None
    # The wrapper's children rise one level into the candidate, which keeps its name:
    # a hoist. Every archive filed under `_source/` is provenance being filed.
    sources = [
        (_rel_join(base, wrapper, name), _rel_join(base, name), "hoist")
        for name in _entry_names(os.path.join(facts.path, wrapper))
        if name != inner_archive
    ]
    for archive in archives:
        # The zip is the provenance wherever it sits: beside the wrapper (a sibling of
        # the child) or inside it (the extracted folder's own download).
        source = (
            _rel_join(base, wrapper, archive) if archive == inner_archive
            else _rel_join(base, archive)
        )
        sources.append((source, _rel_join(base, "_source", archive), "file-archive"))
    return [
        _move(facts, source, target, action) for source, target, action in sources
    ]


def arrange_moves(facts: Facts, chosen: str) -> List[dict]:
    """The moves the chosen shape implies, as storage-relative records.

    A move list is the plan: `arrange_proposal` prints one line per record and
    nothing else it cannot back, and `plan_digest` hashes exactly this list, so a
    shape's whole effect is one reviewable structure. Read-only: it measures what is
    a directory and returns records; it moves nothing.
    """
    base = facts.rel
    parent = os.path.dirname(base)
    name = facts.name

    if chosen == "split-top-folders":
        # The candidate is a container, not a product: every child that carries
        # models is hoisted out to sit beside it, where the corpus says products live
        # (`import/Stagetop`, `import/17_ClockworkMonster`, `import/endorphin`).
        children = [
            child for child, _count in facts.top_children_with_models()
            if child != "(root files)"
        ]
        return _identity_free([
            _move(facts, _rel_join(base, child), _rel_join(parent, child), "hoist")
            for child in children
        ])

    if chosen == "split-model-folders":
        shallow = [
            folder for folder in facts.model_dirs
            if folder != "." and folder.count(os.sep) + 1 == 1
        ]
        return _identity_free([
            _move(facts, _rel_join(base, folder), _rel_join(parent, folder), "hoist")
            for folder in sorted(shallow, key=str.casefold)
        ])

    if chosen not in ONE_PROJECT_SHAPES:
        # `add-to-project` / `git-project` keep the folder where it is, and every
        # other shape belongs to another door.
        return []

    own_archive = _own_wrapper_archive(facts)
    if own_archive is not None:
        return _own_wrapper_moves(facts, own_archive)

    match = _wrapper_child(facts)
    if match is not None:
        wrapper, _count, archives = match
        return _wrapper_child_moves(facts, wrapper, archives)

    # Only now, with no wrapper in the way, does the folder's own name get a say:
    # the series rename comes last because the moves above only touch `base`'s
    # children and `base` keeps its name until this move.
    title = series_title(facts)
    if title is not None and title != name:
        return [_move(facts, base, _rel_join(parent, title), "rename")]

    if folder_title(name) != name:
        return [_move(facts, base, _rel_join(parent, folder_title(name)), "rename")]

    return []


def _move_line(move: dict) -> str:
    return f'move "{move["from"]}" -> "{move["to"]}"'


def arrange_proposal(facts: Facts, chosen: str) -> List[str]:
    """What the disk would have to look like, rendered FROM the move list.

    The prose argues; the `move` lines are the plan. Nothing prints that a record
    does not back: the old `(no file moves)` and the prefix-stripping rename were the
    engine saying two things the disk would not do. Text only - this tool moves
    nothing.
    """
    lines: List[str] = []
    name = facts.name
    cleaned = re.sub(r"[-_ ]*model[_-]?files?$", "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(\d{2,})[_\-. ]+", "", cleaned)
    cleaned = re.sub(r"[_]+", " ", cleaned).strip()
    # The vendor id is a suggestion about a TITLE, never a move: nothing on disk is
    # keyed on it, so it stays prose.
    stripped_id = re.sub(r"[ _\-]+\d{5,}$", "", cleaned).strip()
    if stripped_id and stripped_id != cleaned:
        lines.append(f'(vendor id looks like "-{re.search(r"[ _\-]+(\d{5,})$", cleaned).group(1)}"; '
                     f'title could be "{stripped_id}")')
    if chosen == "split-top-folders":
        lines.append("hoist each top folder into its own project folder")
    if chosen == "add-to-project":
        lines.append("no move: the folder is already inside the project")
    if chosen == "git-project":
        lines.append("no move: the checkout stays put; each untracked pair becomes its own project in place")
    if chosen in ONE_PROJECT_SHAPES:
        hint = series_suggestion(facts)
        if hint:
            lines.append(hint)
        lines.extend(_wrapper_lines(facts, _own_wrapper_archive(facts)))
    moves = arrange_moves(facts, chosen)
    lines.extend(_move_line(move) for move in moves)
    if not lines:
        lines.append("none: this path is already a sane folder name")
    return lines


def plan_digest(key: str, shape: str, moves, creates, adopted) -> str:
    """A stable id for one plan: the same list twice, the same id twice.

    Canonical JSON (sorted keys, no whitespace) over the key, the shape, the moves as
    `[from, to, kind]` triples, the creates and the ADOPTED asset ids, so a stale plan
    is refused by identity rather than by re-reading the disk and hoping. The digest
    deliberately excludes `action`: it is a label for the move, not part of its effect.

    `adopted` is the membership delta, and it belongs in the hash because an adopt can
    add hundreds of files while leaving `moves` and `creates` empty: without the ids, a
    rescan between preview and apply would attach a larger set while the plan id still
    matched, which is exactly the stale-plan case the guard exists for on the one path
    this digest could otherwise not see. The ids are sorted, so the digest is a function
    of the SET a plan attaches and not of the order the rows came back in.

    Mutation: hash only key/shape/moves/creates and a rescan between preview and apply
    attaches every newly registered file under a matching plan id.
    """
    import hashlib
    import json
    canonical = json.dumps(
        {
            "key": key,
            "shape": shape,
            "moves": [[move["from"], move["to"], move["kind"]] for move in moves],
            "creates": list(creates),
            # Sorted by id alone: the digest is a function of the SET a plan
            # attaches, while the wire's samples are ordered path-then-id. The two
            # orderings differ on purpose - this one only has to be stable.
            "adopted": sorted(str(asset_id) for asset_id in adopted),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- the rule set a report is stamped against ------------------------------
#
# Mf-1agz: ONE engine ranks, and every report row carries the stamp of the tables
# that decided it, so two reports are comparable by construction or explicitly not.
# `rule_set_tables` captures every row-deciding constant BY VALUE - sets become
# sorted lists, a regex becomes `{pattern, flags}`, a mapping is sorted - so the
# digest is a function of the tables and not of process memory, dict insertion
# order or set iteration order. A table edit therefore moves the stamp, which
# `tests/test_proposal_engine.py` pins.

# The revision of this rule set. It moves when a rule CHANGES MEANING and the
# tables alone cannot say so; a table edit needs no bump because the stamp already
# moves.
RULE_SET_VERSION = "2"


def _regex_table(pattern) -> dict:
    """One compiled regex as the canonical ``{pattern, flags}`` table entry."""
    return {"pattern": pattern.pattern, "flags": pattern.flags}


def rule_set_tables() -> dict:
    """Every constant a row-deciding path consults, captured BY VALUE.

    Read from the module at call time, so editing a table changes the stamp that a
    test pins - a table edit cannot go unnoticed.

    Deliberately EXCLUDED, because they bound WORK rather than decide a row: the
    walk caps (``TWIN_MAX_ENTRIES``, ``PROVENANCE_MAX``) and the report's own entry
    budget in ``bundle_service``. Including them would make a cosmetic bound edit
    look like a classification change, which is the noise this stamp exists to
    remove. The document caps ARE included: a smaller ``DOCS_MAX_PER_FOLDER`` or
    ``PDF_MAX_BYTES`` changes which facts get read, and so the counts a row states.
    """
    return {
        # The shapes a row can be recommended as, and the two that are enactable.
        "ONE_PROJECT_SHAPES": list(ONE_PROJECT_SHAPES),
        "SPLIT_SHAPES": list(SPLIT_SHAPES),
        "PACKAGING_TAIL": _regex_table(PACKAGING_TAIL),
        # What counts as what.
        "MODEL_EXTS": sorted(MODEL_EXTS),
        "IMAGE_EXTS": sorted(IMAGE_EXTS),
        "PREVIEW_EXTS": sorted(PREVIEW_EXTS),
        "DOC_EXTS": sorted(DOC_EXTS),
        "ARCHIVE_EXTS": sorted(ARCHIVE_EXTS),
        "META_EXTS": sorted(META_EXTS),
        "JUNK_NAMES": sorted(JUNK_NAMES),
        "APP_OWNED": sorted(APP_OWNED),
        # The naming and grouping vocabulary the score and the groups read.
        "PART_NAME": _regex_table(PART_NAME),
        "PRODUCT_DOC": _regex_table(PRODUCT_DOC),
        "VENDOR_ID": _regex_table(VENDOR_ID),
        "COMPOUND_FOREIGN_ID": _regex_table(COMPOUND_FOREIGN_ID),
        "SITE_CODE": _regex_table(SITE_CODE),
        "KEEP_TOKENS": sorted(KEEP_TOKENS),
        "MACOSX_ROOT": MACOSX_ROOT,
        # The group/hierarchy bounds a rendered fact depends on.
        "GROUPS_MAX_DEPTH": GROUPS_MAX_DEPTH,
        "GROUPS_MAX_CHILDREN": GROUPS_MAX_CHILDREN,
        # The document reads a fact sheet depends on.
        "TEXT_MAX_BYTES": TEXT_MAX_BYTES,
        "PDF_MAX_BYTES": PDF_MAX_BYTES,
        "PDF_MAX_PAGES": PDF_MAX_PAGES,
        "DOC_CHAR_CAP": DOC_CHAR_CAP,
        "DOCS_MAX_PER_FOLDER": DOCS_MAX_PER_FOLDER,
        "PDF_DOCUMENTS_ENABLED": PDF_DOCUMENTS_ENABLED,
        # The git probe that adds the `git-project` reading.
        "GIT_MAX_TRACKED": GIT_MAX_TRACKED,
        "GIT_MAX_UNTRACKED": GIT_MAX_UNTRACKED,
        "GIT_TIMEOUT_SECONDS": GIT_TIMEOUT_SECONDS,
        # Provenance and licences.
        "LICENCE_TABLE": {key: list(value) for key, value in sorted(LICENCE_TABLE.items())},
        "_CC_URL_TOKENS": {key: value for key, value in sorted(_CC_URL_TOKENS.items())},
        "_CREATIVE_COMMONS": _CREATIVE_COMMONS,
        "BOILERPLATE_TITLES": sorted(BOILERPLATE_TITLES),
        "README_MARKERS": _regex_table(README_MARKERS),
        "LICENSE_LINE": _regex_table(LICENSE_LINE),
        "_ATTRIBUTION_FIRST_LINE": _regex_table(_ATTRIBUTION_FIRST_LINE),
        "_LICENSED_UNDER_LINE": _regex_table(_LICENSED_UNDER_LINE),
        "_SUMMARY_LINE": _regex_table(_SUMMARY_LINE),
        "_SENTENCE_BOUNDARY": _regex_table(_SENTENCE_BOUNDARY),
        "_TRAILING_JUNK": _TRAILING_JUNK,
        "_TOKEN_SEPARATORS": _regex_table(_TOKEN_SEPARATORS),
        "PROVIDER_HOSTS": {key: sorted(value) for key, value in sorted(PROVIDER_HOSTS.items())},
        "PROVIDER_MODEL_ID": {key: _regex_table(value) for key, value in sorted(PROVIDER_MODEL_ID.items())},
        "_NON_MODEL_PATH": _regex_table(_NON_MODEL_PATH),
        "_BARE_MODEL_PATHS": sorted(_BARE_MODEL_PATHS),
        "_URL_RE": _regex_table(_URL_RE),
    }


def rule_set_stamp() -> str:
    """The sha256 stamp of the rule tables as they are NOW, not as they were imported.

    Read at call time on purpose: a test can change one table and re-derive the
    stamp, which is how the pin below is proven to cover the tables it claims.
    """
    canonical = json.dumps(rule_set_tables(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# The stamp of the tables as this process imported them: what every report row
# records, so two reports are comparable by construction or explicitly not.
RULE_SET_STAMP = rule_set_stamp()


def rule_set() -> dict:
    """The rule set a report is taken against: its version and its stamp."""
    return {"version": RULE_SET_VERSION, "stamp": rule_set_stamp()}


# --- archives -------------------------------------------------------------
#
# A `.zip` is the door 25 of the owner's 151 confirmed projects came through, and
# it is the one the Fates End saga was entirely about. Read-only: the zip is
# opened for reading and nothing is extracted.

class ArchiveFacts:
    def __init__(self, name: str, path: str, rel: str):
        self.name = name
        self.path = path
        self.rel = rel
        self.entries: List[Tuple[str, int]] = []
        self.models = self.images = self.docs = self.others = 0
        self.top: Dict[str, int] = {}
        self.top_meta: Dict[str, List[str]] = {}
        # One level down: a zip of zips (Vault arrived as an outer zip holding two)
        # must not read as "no models inside".
        self.nested: List[dict] = []

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    def common_root(self) -> Optional[str]:
        """The one top-level folder every entry sits under, if there is exactly one."""
        tops = {rel.split("/", 1)[0] for rel, _ in self.entries}
        if len(tops) == 1:
            only = next(iter(tops))
            if all("/" in rel for rel, _ in self.entries):
                return only
        return None

    def top_with_models(self) -> List[Tuple[str, int]]:
        return sorted(((k, v) for k, v in self.top.items() if v),
                      key=lambda kv: (-kv[1], kv[0].casefold()))


NESTED_INSPECT_MAX = 64 * 1024 * 1024


def inspect_nested(zf, name: str, size: int) -> Optional[dict]:
    """Count what an inner archive holds, bounded. Read-only, never extracts to disk."""
    if size > NESTED_INSPECT_MAX:
        return {"name": name, "too_large": True, "models": 0, "images": 0, "docs": 0}
    try:
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(zf.read(name))) as inner:
            counts = {"models": 0, "images": 0, "docs": 0}
            for info in inner.infolist():
                if info.is_dir():
                    continue
                ext = os.path.splitext(info.filename)[1].lower()
                if ext in MODEL_EXTS:
                    counts["models"] += 1
                elif ext in IMAGE_EXTS:
                    counts["images"] += 1
                elif ext in DOC_EXTS:
                    counts["docs"] += 1
        return {"name": name, **counts}
    except Exception:
        return {"name": name, "unreadable": True, "models": 0, "images": 0, "docs": 0}


def measure_archive(path: str, rel: str) -> ArchiveFacts:
    facts = ArchiveFacts(os.path.basename(path), path, rel)
    try:
        import zipfile
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/").lstrip("/")
                if not name or name.endswith("/") or is_junk_path(name):
                    continue
                base = name.rsplit("/", 1)[-1]
                if base.startswith("."):
                    continue
                ext = os.path.splitext(base)[1].lower()
                facts.entries.append((name, info.file_size))
                if ext in ARCHIVE_EXTS:
                    facts.others += 1
                    nested = inspect_nested(zf, name, info.file_size)
                    if nested:
                        nested["bytes"] = info.file_size
                        facts.nested.append(nested)
                        facts.models += nested["models"]
                        facts.images += nested["images"]
                        facts.docs += nested["docs"]
                    top = name.split("/", 1)[0] if "/" in name else "."
                    facts.top_meta.setdefault(top, [])
                    continue
                top = name.split("/", 1)[0] if "/" in name else "."
                if ext in ARCHIVE_EXTS:
                    facts.others += 1
                elif ext in MODEL_EXTS:
                    facts.models += 1
                    facts.top[top] = facts.top.get(top, 0) + 1
                elif ext in IMAGE_EXTS:
                    facts.images += 1
                elif ext in DOC_EXTS:
                    facts.docs += 1
                else:
                    facts.others += 1
                if os.path.splitext(base)[1].lower() in META_EXTS or PRODUCT_DOC.search(
                    os.path.splitext(base)[0]
                ):
                    facts.top_meta.setdefault(top, []).append(base)
    except (OSError, Exception):
        pass
    return facts


def propose_archive(facts: ArchiveFacts) -> Tuple[List[Option], List[str]]:
    reasons: List[str] = []
    options: List[Option] = []
    root = facts.common_root()
    if root:
        reasons.append(f"every entry sits under one folder ({root!r}) - the archive wraps one product")
    for inner in facts.nested:
        if inner.get("models"):
            reasons.append(
                f"inner archive {inner['name']!r} holds {inner['models']} models, "
                f"{inner['images']} images - a zip of zips is still one product"
            )
        elif inner.get("too_large"):
            reasons.append(f"inner archive {inner['name']!r} is too large to inspect; counted as opaque")
        elif inner.get("unreadable"):
            reasons.append(f"inner archive {inner['name']!r} could not be read; counted as opaque")
    tops = facts.top_with_models()
    opaque = [n for n in facts.nested if n.get("too_large") or n.get("unreadable")]
    if facts.models == 0 and not opaque:
        options.append(Option("archive-leave", "leave alone", "no models inside",
                              "nothing to make a project from", 60))
        return options, reasons
    if facts.models == 0 and opaque:
        # The archive holds archives we chose not to read (5.35 GB in Vault's case).
        # An unread payload is still payload: offering "leave alone" here would tell
        # the operator there is nothing to import in a 6.5 GB download.
        reasons.append(
            f"{len(opaque)} inner archive(s) hold {sum(n.get('bytes', 0) for n in opaque) / 1e9:.2f} GB "
            f"that this read-only tool does not open - counted as payload, counts unknown"
        )
        options.append(Option(
            "archive-project",
            "one project from this archive (contents not inspected)",
            f"{len(facts.entries)} entries, {len(opaque)} inner archive(s), "
            f"{sum(n.get('bytes', 0) for n in opaque) / 1e9:.2f} GB of unread payload",
            "unpack, then let the scan and the detector look at the files that appear",
            70,
        ))
        return options, reasons
    one = 45 + (15 if root else 0) + (10 if facts.docs else 0)
    options.append(Option(
        "archive-project",
        "one project from this archive",
        f"{facts.models} models, {facts.images} images, {facts.docs} docs"
        + (f", wrapped in {root!r}" if root else ""),
        "unpack into a folder named after the product, then file the zip inside it",
        one,
    ))
    if len(tops) >= 2:
        ids = [name for name, _ in tops if vendor_id(name)]
        split = 15 + (20 if ids else 0) + (15 if any(facts.top_meta.get(n) for n, _ in tops) else 0)
        if ids:
            reasons.append(f"{len(ids)} top-level folder(s) inside the zip carry a vendor id")
        options.append(Option(
            "archive-split",
            f"{len(tops)} projects from this archive",
            ", ".join(f"{name} ({count})" for name, count in tops[:6]),
            "one project per top-level folder inside the zip",
            split,
        ))
    options.sort(key=lambda o: (-o.score, o.shape))
    return options, reasons


def arrange_archive_moves(facts: "ArchiveFacts", chosen: str) -> List[dict]:
    """The one move an archive needs: unpack into the product folder, file the zip.

    The title runs BOTH strips: `product_title` removes a download site's code and
    `wrapper_title` the packaging tail, so `Vault Archive_18.11.2026.zip` and a
    `Vault Archive_18.11.2026/` extracted twin name ONE product folder - `Vault/` -
    and not two (correction 8).
    """
    if chosen != "archive-project":
        return []
    title = wrapper_title(product_title(facts.stem))
    return [{
        "from": facts.rel,
        "to": _rel_join(title, "_source", facts.name),
        "kind": "file",
    }]


def arrange_archive(facts: ArchiveFacts, chosen: str) -> List[str]:
    if chosen != "archive-project":
        return ["none: leave the archive where it is"]
    # The PRODUCT names the folder, never the archive's inner wrapper: `02_Fighter`
    # is a vendor's packaging, while "02_Fates End - Fighter Tower" is the thing the
    # operator owns (owner ruling, Mf-0fd5: the archive title goes to the project).
    title = wrapper_title(product_title(facts.stem))
    root = facts.common_root()
    lines = [f'unpack into "{title}/" (one project)']
    if root:
        lines.append(f'drop the wrapper folder "{root}/" while unpacking')
    lines.append(f'then file the archive inside it: "{title}/_source/{facts.name}" (owner rule 2026-11-25)')
    lines.extend(_move_line(move) for move in arrange_archive_moves(facts, chosen))
    return lines




# --- scoring against the operator's own projects ---------------------------
#
# The rules above are heuristics argued from the corpus; this section is how they
# are held to it. Nothing here prints (Mf-yitm): the report is plain data so a test
# can assert the same scoring the CLI shows the operator.


def _label_row(root: str, label: dict, project_keys: Sequence[str]) -> dict:
    """One confirmed label scored against the rules, as plain data.

    `shape` is None for a key that is not on disk at all, so the renderer can keep
    printing a MISSING row without pretending a shape was measured.
    """
    key = label.get("key")
    expect = label.get("expect", "split")
    must = label.get("arrange_contains") or []
    path = os.path.join(root, key) if key else ""
    if not key or not os.path.isdir(path):
        return {
            "key": key, "expect": expect, "shape": None, "count": None,
            "arrange": [], "ok": False, "missing_arrange": list(must), "detail": "MISSING",
        }
    facts = measure(path, key)
    options, _reasons, _repo = propose(facts, project_keys=project_keys)
    chosen = options[0] if options else None
    shape = chosen.shape if chosen else "leave"
    count = len(facts.top_children_with_models())
    arrange = arrange_proposal(facts, shape)
    arrange_text = " | ".join(arrange)
    missing_arrange = [needle for needle in must if needle not in arrange_text]
    arrange_ok = not missing_arrange
    if expect == "arrange":
        # An arrange-only label asserts the arrangement and nothing about the shape:
        # the shape it rides on is still an open argument.
        ok = arrange_ok
        detail = f"arrange: {arrange_text[:90]}"
    elif expect == "split":
        ok = shape == "split-top-folders" and (
            not label.get("count") or count == label["count"]
        )
        detail = f"{shape} ({count} top folders, want {label.get('count', 'any')})"
    else:
        ok = shape in ONE_PROJECT_SHAPES
        detail = f"{shape}"
    if expect != "arrange":
        ok = ok and arrange_ok
        if not arrange_ok:
            detail += f"  [arrange missing {missing_arrange}]"
    return {
        "key": key, "expect": expect, "shape": shape, "count": count,
        "arrange": arrange, "ok": ok, "missing_arrange": missing_arrange, "detail": detail,
    }


def validate(root: str, labelled: Sequence[dict], project_keys: Sequence[str],
             labels: Sequence[dict] = ()) -> dict:
    """Score the rules against the operator's own projects. Returns plain data.

    151 labelled folders exist on the live library - each one a project the owner
    confirmed by creating it. That is the only ground truth available, and it is
    strong: if the rules would SPLIT one of them, the rules are wrong for the most
    common case in the house.

    Nothing here prints: the caller renders. `labels` is scored into one row per
    label, always (`shape` None when the folder is not on disk), so a label can
    never be silently skipped; `failures` counts the rows that are not ok.
    `labelled` is the input count, which the CLI header prints and which no other
    key can reproduce (an entry with no key is counted but produces no record).
    """
    agree: List[dict] = []
    disagree: List[dict] = []
    missing: List[dict] = []
    archive_scored: List[dict] = []
    archives: List[dict] = []
    for item in labelled:
        key = item.get("key")
        if not key:
            continue
        if key.startswith("archive:"):
            rel = key.split(":", 1)[1]
            path = os.path.join(root, rel)
            item = dict(item, archive=rel, exists=os.path.isfile(path))
            archives.append(item)
            if item["exists"]:
                afacts = measure_archive(path, rel)
                aopts, _ = propose_archive(afacts)
                archive_scored.append({
                    "key": key,
                    "chosen": aopts[0].shape if aopts else "leave",
                    "models": afacts.models,
                    "entries": len(afacts.entries),
                })
            continue
        path = os.path.join(root, key)
        if not os.path.isdir(path):
            missing.append(item)
            continue
        facts = measure(path, key)
        options, reasons, repo = propose(facts, project_keys=project_keys)
        chosen = options[0].shape if options else "leave"
        record = {
            "key": key,
            "project": item.get("project"),
            "chosen": chosen,
            "label": options[0].label if options else "nothing",
            "score": options[0].score if options else 0,
            "runner_up": (options[1].label, options[1].score) if len(options) > 1 else None,
            "reasons": reasons,
            "models": facts.models,
        }
        (agree if chosen in ONE_PROJECT_SHAPES else disagree).append(record)

    rows = [_label_row(root, label, project_keys) for label in labels]
    return {
        "labelled": len(labelled),
        "agree": agree,
        "disagree": disagree,
        "missing": missing,
        "archives": archives,
        "archive_scored": archive_scored,
        "labels": rows,
        "failures": sum(1 for row in rows if not row["ok"]),
    }
