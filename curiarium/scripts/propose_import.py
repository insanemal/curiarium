#!/usr/bin/env python3
"""Propose what each folder in a library SHOULD become. Writes nothing, ever.

An offline tight-loop tool. It walks a real storage tree and prints, per folder,
the competing interpretations the app could offer ("one project", "one project
per part folder", "add these parts to an existing project"), each with the counts
it would produce and the arrangement it would need. The point is to argue about
the *proposals* against real data without rebuilding the app for every idea.

This module contains no write path at all - no rename, no link, no unlink, no
mkdir, no write-file. It opens directories for reading and files only to parse a
README. That is why it is safe to point at a live CephFS mount.

    python3 curiarium/scripts/propose_import.py --root /mnt/ceph/3D_models --top 20
    python3 curiarium/scripts/propose_import.py --only Vault --only import/Stagetop
    python3 curiarium/scripts/propose_import.py --json > /tmp/proposals.json

Nothing here is wired into the app yet, and nothing here should be: the shapes it
proposes are a hypothesis to be measured, and the arranging half (rename, move,
hard-link) is a separate, approval-gated step that does not exist yet.
"""

import argparse
import json
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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


def propose(facts: Facts, project_keys: Sequence[str]) -> Tuple[List[Option], List[str], Optional[dict]]:
    """Every interpretation the facts support, ranked; plus the reasons."""
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

    repo = git_state(facts.path)
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


def arrange_proposal(facts: Facts, chosen: str) -> List[str]:
    """What the disk would have to look like. Text only - this tool moves nothing."""
    lines: List[str] = []
    name = facts.name
    cleaned = re.sub(r"[-_ ]*model[_-]?files?$", "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(\d{2,})[_\-. ]+", "", cleaned)
    cleaned = re.sub(r"[_]+", " ", cleaned).strip()
    stripped_id = re.sub(r"[ _\-]+\d{5,}$", "", cleaned).strip()
    if stripped_id and stripped_id != cleaned:
        lines.append(f'(vendor id looks like "-{re.search(r"[ _\-]+(\d{5,})$", cleaned).group(1)}"; '
                     f'title could be "{stripped_id}")')
    if cleaned and cleaned != name:
        lines.append(f'rename "{name}" -> "{cleaned}"')
    if chosen == "split-top-folders":
        lines.append("hoist each top folder into its own project folder (no file moves)" )
    if chosen == "add-to-project":
        lines.append("no move: the folder is already inside the project")
    if chosen == "git-project":
        lines.append("no move: the checkout stays put; each untracked pair becomes its own project in place")
    if chosen in {"one-project", "git-project"}:
        hint = series_suggestion(facts)
        if hint:
            lines.append(hint)
        # A dominant child that is the extracted twin of a sibling archive and carries
        # no product document of its own is pure packaging: drop it and file the zip.
        twins_here = {os.path.splitext(t)[0].casefold() for t in facts.twin_archives()}
        for child, count in facts.top_children_with_models():
            if child.casefold() not in twins_here or not facts.models:
                continue
            if count / facts.models < 0.9:
                continue
            child_share = count / facts.models
            if child_share < 0.9:
                continue
            lines.append(
                f'drop the wrapper "{child}/" while arranging: its name is the archive\'s, '
                f'not the product\'s ({count} of {facts.models} models inside it)'
            )
            for twin in facts.twin_archives():
                if os.path.splitext(twin)[0].casefold() == child.casefold():
                    lines.append(f'file "{twin}" in "{facts.name}/_source/{twin}"')
            break
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


def render_archive(rel: str, facts: ArchiveFacts, options: List[Option],
                   reasons: List[str], arrange: List[str]) -> str:
    out = [f"{rel:<58} {(options[0].label.upper() if options else 'NOTHING')}"]
    out.append(
        f"  members   {facts.models} models · {facts.images} images · {facts.docs} docs · "
        f"{facts.others} other · {len(facts.entries)} entries"
    )
    for reason in reasons[:4]:
        out.append(f"  why       {reason}")
    out.append("  options   (ranked)")
    for option in options[:3]:
        out.append(f"    {option.score:>3}  {option.label}")
        out.append(f"         {option.detail}")
    out.append("  arrange   " + arrange[0])
    for extra in arrange[1:]:
        out.append("            " + extra)
    return "\n".join(out)


def expand_candidates(root: str, only: Sequence[str], max_children: int = 12,
                      depth_left: int = 3) -> List[Tuple[str, str]]:
    """Candidate folders: the content top level, not a container of products."""
    out: List[Tuple[str, str]] = []
    if only:
        targets = []
        for item in only:
            path = item if os.path.isabs(item) else os.path.join(root, item)
            targets.append((path, item.strip("/")))
    else:
        targets = []
        for name in sorted(os.listdir(root), key=str.casefold):
            path = os.path.join(root, name)
            if name.startswith(".") or name in APP_OWNED or not os.path.isdir(path):
                continue
            targets.append((path, name))

    def consider(path: str, rel: str, depth_left: int) -> None:
        facts = measure(path, rel)
        with_models = [n for n, c in facts.top_children_with_models()]
        if facts.models == 0 and facts.archives == 0:
            out.append((path, rel))
            return
        if len(with_models) > max_children and depth_left > 0:
            for name in sorted(with_models, key=str.casefold):
                consider(os.path.join(path, name), f"{rel}/{name}", depth_left - 1)
            return
        out.append((path, rel))

    for path, rel in targets:
        consider(path, rel, depth_left)
    return out


def render(rel: str, options: List[Option], reasons: List[str], facts: Facts,
           chosen: str, arrange: List[str], repo: Optional[dict] = None,
           peel: Optional[List[dict]] = None) -> str:
    width = 58
    head = rel if len(rel) <= width else "..." + rel[-(width - 3):]
    out = [f"{head:<{width}} {options[0].label.upper() if options else 'NOTHING'}"]
    out.append(
        "  members   "
        f"{facts.models} models · {facts.images} images · {facts.docs} docs · "
        f"{facts.others} other · {facts.archives} archives"
    )
    tops = facts.top_children_with_models()
    if tops:
        out.append(
            f"  parts     {len(tops)} top folders · {len(facts.model_dirs)} model-bearing dirs · "
            f"depth {facts.depth}"
        )
        for name, count in tops[:8]:
            out.append(f"              {name[:44]:<44} {count:>4} models")
        if len(tops) > 8:
            out.append(f"              ... {len(tops) - 8} more")
    for reason in reasons[:5]:
        out.append(f"  why       {reason}")
    out.append("  options   (ranked)")
    for option in options[:4]:
        out.append(f"    {option.score:>3}  {option.label}")
        out.append(f"         {option.detail}")
        if option is not options[0]:
            out.append(f"         -> {option.consequence}")
    if repo is not None:
        out.append(
            f"  git       {repo['branch']} @ {(repo['head'].split() or ['?'])[0]} · "
            f"{len(repo['tracked'])} tracked · {len(repo['untracked'])} untracked"
        )
        for item in (peel or []):
            mark = "twin  " if item["twin"] else "      "
            vid = f" id={item['vendor_id']}" if item["vendor_id"] else ""
            out.append(
                f"            {mark}{item['name'][:40]:<40} {item['kind']:<11}"
                f"{vid} -> {item['labels'][0]}"
            )
    out.append("  arrange   " + arrange[0])
    for extra in arrange[1:]:
        out.append("            " + extra)
    return "\n".join(out)


ONE_PROJECT_SHAPES = ("one-project", "git-project")


def validate(root: str, labelled: Sequence[dict], project_keys: Sequence[str],
             labels: Sequence[dict] = ()) -> int:
    """Score the rules against the operator's own projects.

    151 labelled folders exist on the live library - each one a project the owner
    confirmed by creating it. That is the only ground truth available, and it is
    strong: if the rules would SPLIT one of them, the rules are wrong for the most
    common case in the house.
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

    total = len(agree) + len(disagree)
    print(f"labelled projects: {len(labelled)}   folder-keyed on disk: {total}   "
          f"archive-keyed: {len(archives)}   missing folders: {len(missing)}")
    if total:
        print(f"agree  (one project): {len(agree)} ({len(agree) / total:.1%})")
    print(f"disagree (would split): {len(disagree)}")
    if disagree:
        print()
        print("the rules would fragment these confirmed projects:")
        for item in disagree:
            print(f"  {item['key']:<52} -> {item['label']} ({item['score']})")
            if item["runner_up"]:
                print(f"       runner-up: {item['runner_up'][0]} ({item['runner_up'][1]})")
            for reason in item["reasons"][:3]:
                print(f"       why: {reason}")
    if archives:
        present = sum(1 for a in archives if a["exists"])
        print()
        print(f"archive-keyed projects (the tool has NO archive mode): {len(archives)}, "
              f"{present} of their zips still on disk")
        for item in archives[:4]:
            print(f"  {item['key'][:70]}  {'present' if item['exists'] else 'GONE'}")
    if missing:
        print()
        print("labelled but not on disk (renamed/moved since?):")
        for item in missing[:10]:
            print(f"  {item['key']}")
        if len(missing) > 10:
            print(f"  ... {len(missing) - 10} more")
    if archive_scored:
        agree_a = [a for a in archive_scored if a["chosen"] == "archive-project"]
        print()
        print(f"archive-keyed vs the rules: {len(agree_a)} of {len(archive_scored)} "
              f"recommend 'one project from this archive'")
        for item in archive_scored:
            if item["chosen"] != "archive-project":
                print(f"  {item['key'][:60]:<60} -> {item['chosen']} "
                      f"({item['models']} models, {item['entries']} entries)")

    # Confirmed SPLIT labels: the rule this library has no other evidence for.
    if labels:
        print()
        print("confirmed split labels:")
        failures = 0
        for label in labels:
            key, expect = label.get("key"), label.get("expect", "split")
            path = os.path.join(root, key)
            if not os.path.isdir(path):
                print(f"  {key:<44} MISSING")
                failures += 1
                continue
            facts = measure(path, key)
            options, _reasons, _repo = propose(facts, project_keys=project_keys)
            chosen = options[0] if options else None
            shape = chosen.shape if chosen else "leave"
            count = len(facts.top_children_with_models())
            must = label.get("arrange_contains") or []
            arrange_text = " | ".join(arrange_proposal(facts, shape))
            arrange_ok = all(needle in arrange_text for needle in must)
            if expect == "arrange":
                print(f"  {'OK ' if arrange_ok else 'BAD'} {key:<44} arrange: {arrange_text[:90]}")
                if not arrange_ok:
                    failures += 1
                continue
            if expect == "split":
                ok = shape == "split-top-folders" and (
                    not label.get("count") or count == label["count"]
                )
                detail = f"{shape} ({count} top folders, want {label.get('count', 'any')})"
            else:
                ok = shape in ONE_PROJECT_SHAPES
                detail = f"{shape}"
            ok = ok and arrange_ok
            print(f"  {'OK ' if ok else 'BAD'} {key:<44} {detail}"
                  + ("" if arrange_ok else f"  [arrange missing {[x for x in must if x not in arrange_text]}]"))
            if not ok:
                failures += 1
        if failures:
            return 1

    return 0 if not disagree else 1


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=os.environ.get("CURIARIUM_STORAGE",
                                                         "/mnt/ceph/3D_models"))
    parser.add_argument("--only", action="append", default=[],
                        help="relative (or absolute) folder; repeatable")
    parser.add_argument("--top", type=int, default=0, help="limit how many folders print")
    parser.add_argument("--json", action="store_true", help="machine-readable, for diffing")
    parser.add_argument(
        "--projects",
        help="JSON list of {key, project} - the operator's own labelled projects, used as "
             "ground truth and as 'this path is already a project'",
    )
    parser.add_argument(
        "--archives", action="store_true",
        help="also propose on every .zip under the root (read-only: nothing is extracted)",
    )
    parser.add_argument(
        "--labels",
        help="JSON list of confirmed split labels: {key, expect: split|one, count}",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="score the rules against --projects: every labelled key must be recommended as "
             "one project, never split",
    )
    parser.add_argument("--max-children", type=int, default=12,
                        help="a folder with more model-bearing children is a container")
    args = parser.parse_args(list(argv))

    if not os.path.isdir(args.root):
        print(f"root does not exist: {args.root}", file=sys.stderr)
        return 2

    labelled = []
    if args.projects:
        with open(args.projects, encoding="utf-8") as fh:
            labelled = json.load(fh)
    project_keys = [
        item["key"] for item in labelled
        if item.get("key") and not item["key"].startswith("archive:")
    ]

    labels = []
    if args.labels:
        with open(args.labels, encoding="utf-8") as fh:
            labels = json.load(fh)

    if args.validate:
        return validate(args.root, labelled, project_keys, labels)

    if args.only:
        # A path that does not exist must fail loudly: silently measuring nothing
        # produced a confident "one project" proposal for a folder that was not
        # where the caller said it was.
        missing_paths = [
            item for item in args.only
            if not os.path.exists(item if os.path.isabs(item) else os.path.join(args.root, item))
        ]
        if missing_paths:
            for item in missing_paths:
                print(f"no such folder or archive: {item}", file=sys.stderr)
            return 2

    candidates = expand_candidates(args.root, args.only, args.max_children)
    if args.top:
        candidates = candidates[: args.top]

    payload = []
    for path, rel in candidates:
        if os.path.isfile(path) and path.lower().endswith(".zip"):
            afacts = measure_archive(path, rel)
            aopts, areasons = propose_archive(afacts)
            achosen = aopts[0].shape if aopts else "leave"
            aarrange = arrange_archive(afacts, achosen)
            payload.append({
                "rel": rel, "kind": "archive", "recommended": achosen,
                "options": [o.as_dict() for o in aopts], "reasons": areasons,
                "arrange": aarrange,
                "facts": {"models": afacts.models, "images": afacts.images,
                          "docs": afacts.docs, "others": afacts.others,
                          "entries": len(afacts.entries),
                          "common_root": afacts.common_root()},
            })
            if not args.json:
                print(render_archive(rel, afacts, aopts, areasons, aarrange))
                print()
            continue
        facts = measure(path, rel)
        options, reasons, repo = propose(facts, project_keys=project_keys)
        chosen = options[0].shape if options else "leave"
        arrange = arrange_proposal(facts, chosen)
        peel = peel_proposals(facts, repo) if repo else []
        payload.append({
            "rel": rel,
            "recommended": chosen,
            "options": [o.as_dict() for o in options],
            "reasons": reasons,
            "git": repo,
            "peel": peel,
            "arrange": arrange,
            "facts": {
                "models": facts.models, "images": facts.images, "docs": facts.docs,
                "others": facts.others, "archives": facts.archives,
                "model_dirs": len(facts.model_dirs), "depth": facts.depth,
                "top_folders": facts.top_children_with_models(),
                "twins": facts.twin_archives(),
                "root_meta": facts.root_meta,
                "bytes": facts.bytes,
            },
        })

        if not args.json:
            print(render(rel, options, reasons, facts, chosen, arrange, repo, peel))
            print()

    if args.json:
        json.dump(payload, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0

    # A one-screen summary so a loop over 158 folders is still readable.
    by_shape: Dict[str, int] = {}
    for item in payload:
        by_shape[item["recommended"]] = by_shape.get(item["recommended"], 0) + 1
    print("summary")
    for shape, count in sorted(by_shape.items(), key=lambda kv: -kv[1]):
        print(f"  {shape:<22} {count}")
    contested = [i for i in payload if len(i["options"]) > 1
                 and i["options"][0]["score"] - i["options"][1]["score"] < 15]
    if contested:
        print()
        print("close calls (worth an argument):")
        for item in contested:
            top, second = item["options"][0], item["options"][1]
            print(f"  {item['rel']:<44} {top['label']} ({top['score']}) vs "
                  f"{second['label']} ({second['score']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
