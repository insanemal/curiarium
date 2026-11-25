#!/usr/bin/env python3
"""Measure a folder and propose what it should become. Stdlib only, writes nothing.

The engine behind `curiarium/scripts/propose_import.py`, and the half of that tool
the app is allowed to call. It walks a real storage tree and answers, per folder,
which interpretations the facts support ("one project", "one project per part
folder", "add these parts to an existing project"), each with the counts it would
produce, the reasons it is or is not recommended, and the arrangement it would
need.

STDLIB ONLY, and no app import of any kind: this module is imported by the API
process (later passes wire it into a route) *and* run from a bare host python in
an offline tight loop against a live CephFS mount. `fastapi` or `sqlalchemy` here
would make the tight loop impossible to start, so the constraint is audited by
`curiarium/api/tests/test_proposal_engine.py` rather than trusted.

Read-only in the strongest sense: no rename, no link, no unlink, no mkdir, no
write-file. It opens directories for reading and files only to parse a README.
The one thing that can run a subprocess is `propose()`, which asks git about a
checkout via `git_state` - and a GET handler must never do that, so the call is
behind `probe_git=False` (Mf-yitm). A proposal moves nothing either way.

Nothing here prints. `validate()` returns plain data and the CLI shim renders it,
so the same scoring can be asserted in a test instead of scraped from stdout.
"""

import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

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


# A trailing download-site code: `Vault_USSA123123123`, `thing_ABC123456`. Only when
# the token is long and digit-heavy, so a real word ("_remix", "_v2") survives.
SITE_CODE = re.compile(r"[ _\-]([A-Za-z]{0,8}\s?\d{6,}|[A-Za-z0-9]{10,}\d)\s*$")


# Short all-caps tokens that are part of a product name, not a site/series code.
KEEP_TOKENS = {"LED", "USB", "PLA", "ABS", "PETG", "TPU", "ASA", "XL", "XXL", "XXXL",
               "LCD", "OLED", "CNC", "PWM", "RGB", "SLA", "FDM", "HDMI", "RJ45"}


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


def git_state(path: str) -> Optional[dict]:
    """The repo's own declaration of what belongs to it, read without writing.

    `--no-optional-locks` + GIT_OPTIONAL_LOCKS=0 keep `status` from refreshing the
    index, so asking this question never writes to the working tree. Returns None
    for a non-repo, or when git is absent: a missing tool must not fail a proposal.
    """
    if not os.path.exists(os.path.join(path, ".git")):
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    def run(*args: str) -> Optional[str]:
        try:
            import subprocess
            out = subprocess.run(
                ["git", "--no-optional-locks", "-C", path, *args],
                capture_output=True, text=True, timeout=120, env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None
    branch = (run("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    head = (run("log", "-1", "--format=%h %ci") or "").strip()
    tracked = run("ls-files")
    status = run("status", "--porcelain", "--untracked-files=normal")
    if tracked is None or status is None:
        return None
    untracked = []
    for line in status.splitlines():
        if line.startswith("?? "):
            name = line[3:].strip().strip('"')
            untracked.append(name.rstrip("/"))
    return {
        "branch": branch or "?",
        "head": head,
        "tracked": [line for line in tracked.splitlines() if line],
        "untracked": sorted(set(untracked), key=str.casefold),
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
        # relative dir -> model count, only for dirs that carry models
        self.model_dirs: Dict[str, int] = {}
        # relative dir -> metadata filenames found directly in it
        self.meta: Dict[str, List[str]] = {}
        self.children: List[str] = []          # this folder's own subdirectory names
        self.child_models: Dict[str, int] = {}  # first path segment -> model count
        self.child_meta: Dict[str, List[str]] = {}  # first path segment -> meta files

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


def measure(path: str, rel_prefix: str = "") -> Facts:
    """Walk one candidate folder once and measure it."""
    facts = Facts(os.path.basename(path.rstrip(os.sep)) or path, path, rel_prefix)
    stack: List[Tuple[str, str]] = [(".", path)]
    while stack:
        rel_dir, absolute = stack.pop()
        files, dirs = read_dir(absolute)
        if rel_dir == ".":
            facts.children.extend(dirs)
        for entry in files:
            facts.bytes += entry.size
            ext = entry.ext
            if ext in ARCHIVE_EXTS:
                facts.archives += 1
            if ext in MODEL_EXTS:
                facts.models += 1
                facts.model_dirs[rel_dir] = facts.model_dirs.get(rel_dir, 0) + 1
                top = rel_dir.split(os.sep, 1)[0] if rel_dir != "." else "."
                facts.child_models[top] = facts.child_models.get(top, 0) + 1
            elif ext in IMAGE_EXTS:
                facts.images += 1
            elif ext in DOC_EXTS:
                facts.docs += 1
            else:
                facts.others += 1
            if ext in META_EXTS:
                facts.meta.setdefault(rel_dir, []).append(entry.name)
                top = rel_dir.split(os.sep, 1)[0] if rel_dir != "." else "."
                facts.child_meta.setdefault(top, []).append(entry.name)
        for name in dirs:
            stack.append((name if rel_dir == "." else os.path.join(rel_dir, name),
                          os.path.join(absolute, name)))
    return facts


# --- interpretations ------------------------------------------------------
#
# An interpretation is a NAMED outcome with a size and a consequence. The app
# should offer these as choices; this tool prints the ones the facts support,
# ranked, with the reason each is or is not recommended.

class Option:
    def __init__(self, shape: str, label: str, detail: str, consequence: str, score: int):
        self.shape = shape
        self.label = label
        self.detail = detail
        self.consequence = consequence
        self.score = score

    def as_dict(self) -> dict:
        return {
            "shape": self.shape,
            "label": self.label,
            "detail": self.detail,
            "consequence": self.consequence,
            "score": self.score,
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
            probe_git: bool = True) -> Tuple[List[Option], List[str], Optional[dict]]:
    """Every interpretation the facts support, ranked; plus the reasons.

    `probe_git=False` skips `git_state` entirely and reports no repo. That switch is
    for the API: `git_state` shells out to git, and a GET handler may never run a
    subprocess. The CLI, which is the operator's own tight loop, keeps the default.
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

    repo = git_state(facts.path) if probe_git else None
    if repo is not None:
        tracked_models = sum(
            1 for rel in repo["tracked"] if os.path.splitext(rel)[1].lower() in MODEL_EXTS
        )
        reasons.append(
            f"git checkout on {repo['branch']} ({repo['head'].split()[0] if repo['head'] else '?'}): "
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

    # Split. Only plausible when the children look like products, not parts.
    if len(tops) >= 2:
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
            "split-top-folders",
            f"split into {len(tops)} projects",
            ", ".join(f"{name} ({count})" for name, count in tops[:6])
            + (" ..." if len(tops) > 6 else ""),
            "one project per top folder; fragments a single product if it was one",
            split_score,
        ))

    # Split by the shallowest model-bearing folders (the fine-grained variant).
    shallow = [d for d in facts.model_dirs if d != "." and d.count(os.sep) + 1 == 1]
    if len(shallow) >= 3 and len(shallow) > len(tops):
        options.append(Option(
            "split-model-folders",
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
    return options, reasons, repo


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



def series_suggestion(facts: Facts) -> Optional[str]:
    """A numbered folder whose siblings share a title prefix inherits it.

    `17_ClockworkMonster` is a Fates End tower (owner, 2026-11-25); its siblings are
    `01_Fates End - Wizard Tower` ... `16_...`. The number is a series index, not
    clutter, so the proposal keeps it and supplies the missing series title.
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
    return f'rename to "{number}_{series} - {rest}" (belongs to the {series} series: {count} siblings)'


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
    root_twins = {os.path.splitext(name)[0].casefold() for name in facts.twin_archives()}
    lines: List[str] = []
    wrapper: Optional[str] = None
    wrapper_models = 0
    archives: List[str] = []
    title = facts.name
    for child, count in tops:
        if child == "(root files)":
            continue
        if child.casefold() in root_twins:
            # The zip sits BESIDE the folder, so a matching stem is only provenance
            # when the child is also the parent's content: endorphin's five
            # ingredients each keep their own archive and are five products.
            if count / facts.models < 0.9:
                continue
            archives = [
                twin for twin in facts.twin_archives()
                if os.path.splitext(twin)[0].casefold() == child.casefold()
            ]
        else:
            # The zip sits INSIDE the child. That zip is the provenance itself - a
            # child cannot hold an archive named after itself by coincidence - so no
            # share test is needed: a wrapper holding 10 of the folder's 13 models
            # beside a `Rhino models/` part is still the wrapper.
            inner = _child_twin(facts, child)
            if not inner:
                continue
            archives = [os.path.basename(inner)]
        wrapper, wrapper_models = child, count
        break
    if wrapper is None:
        if own_archive is None:
            return []
        wrapper, wrapper_models = facts.name, facts.models
        archives = [own_archive]
        title = wrapper_title(facts.name)
        # The candidate IS the wrapper, so there is no parent to take a share of.
        inside = f"{wrapper_models} models inside it"
    else:
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


def arrange_proposal(facts: Facts, chosen: str) -> List[str]:
    """What the disk would have to look like. Text only - this tool moves nothing."""
    lines: List[str] = []
    name = facts.name
    cleaned = re.sub(r"[-_ ]*model[_-]?files?$", "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(\d{2,})[_\-. ]+", "", cleaned)
    cleaned = re.sub(r"[_]+", " ", cleaned).strip()
    # Decided before the cosmetic rename below: when the candidate IS the extracted
    # wrapper, tidying its name renames the packaging and leaves the product unnamed,
    # and the drop supersedes it (Mf-yitm case (b)).
    own_archive = _own_wrapper_archive(facts) if chosen in ONE_PROJECT_SHAPES else None
    stripped_id = re.sub(r"[ _\-]+\d{5,}$", "", cleaned).strip()
    if stripped_id and stripped_id != cleaned:
        lines.append(f'(vendor id looks like "-{re.search(r"[ _\-]+(\d{5,})$", cleaned).group(1)}"; '
                     f'title could be "{stripped_id}")')
    if cleaned and cleaned != name and own_archive is None:
        lines.append(f'rename "{name}" -> "{cleaned}"')
    if chosen == "split-top-folders":
        lines.append("hoist each top folder into its own project folder (no file moves)" )
    if chosen == "add-to-project":
        lines.append("no move: the folder is already inside the project")
    if chosen == "git-project":
        lines.append("no move: the checkout stays put; each untracked pair becomes its own project in place")
    if chosen in ONE_PROJECT_SHAPES:
        hint = series_suggestion(facts)
        if hint:
            lines.append(hint)
        lines.extend(_wrapper_lines(facts, own_archive))
    prefix = re.match(r"^(\d{2,})[_.\- ]+", name)
    if prefix:
        lines.append(f'or keep the sort prefix as-is: "{name}"')
    if not lines:
        lines.append("none: this path is already a sane folder name")
    return lines



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


def arrange_archive(facts: ArchiveFacts, chosen: str) -> List[str]:
    if chosen != "archive-project":
        return ["none: leave the archive where it is"]
    # The PRODUCT names the folder, never the archive's inner wrapper: `02_Fighter`
    # is a vendor's packaging, while "02_Fates End - Fighter Tower" is the thing the
    # operator owns (owner ruling, Mf-0fd5: the archive title goes to the project).
    title = product_title(facts.stem)
    root = facts.common_root()
    lines = [f'unpack into "{title}/" (one project)']
    if root:
        lines.append(f'drop the wrapper folder "{root}/" while unpacking')
    lines.append(f'then file the archive inside it: "{title}/_source/{facts.name}" (owner rule 2026-11-25)')
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
