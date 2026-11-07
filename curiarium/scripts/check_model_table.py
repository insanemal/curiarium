#!/usr/bin/env python3
"""Fail when a copy of the model-suffix table drifts from the API's own (Mf-0ck5).

`curiarium/api/file_utils.py` holds `MODEL_MIME_BY_SUFFIX`, the library's answer for
the five extensions a 3D-print library is made of. Three copies live in the web app
- two `MODEL_EXTS` sets and the mime map `zipUtils` writes into an archive - and the
four silently rot the moment someone edits one: the gallery then calls a file a model
the scan stored as `application/octet-stream`, or an archive is written with a mime
the server never answers. The cover of an extract-and-create project is chosen by
extension alone, so a drifted copy is a blank card, not a cosmetic mismatch.

    python3 curiarium/scripts/check_model_table.py

Parses the sources with `re` rather than importing them, so it stays stdlib-only and
cannot drag the api package (or `fastapi`) into a build step. Prints every copy it
read, then exits 0 when all four name the same suffixes - and the same `model/*`
values, API against `zipUtils` - and 1 with the offending path and value on stderr
otherwise. A missing file or a renamed marker fails too, naming the path: a table
that cannot be found must not be read as agreement. To fix a mismatch, edit the
mirrors to match curiarium/api/file_utils.py; never bump a copy on its own.
"""

import re
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "curiarium" / "api" / "file_utils.py"
ASSET_KINDS = ROOT / "curiarium" / "web" / "src" / "lib" / "assetKinds.ts"
GRID_META = ROOT / "curiarium" / "web" / "src" / "ui" / "asset-grid" / "assetGridMeta.ts"
ZIP_UTILS = ROOT / "curiarium" / "web" / "src" / "lib" / "zipUtils.ts"

API_TABLE_MARKER = "MODEL_MIME_BY_SUFFIX"
EXT_SET_MARKER = "const MODEL_EXTS = new Set(["
GRID_EXT_SET_MARKER = f"export {EXT_SET_MARKER}"
ZIP_MAP_MARKER = "const MIME_BY_EXT: Record<string, string> = {"

DICT_BLOCK = re.compile(r"\{([^}]*)\}", re.DOTALL)
PAIR_LINE = re.compile(r'"([^"]+)"\s*:\s*"([^"]+)"')
SET_MEMBERS = re.compile(r"new Set\(\[([^\]]*)\]")
QUOTED = re.compile(r'"([^"]+)"')


def label(path: Path) -> str:
    """Repository-relative name, so the output reads the same from any cwd."""
    return str(path.relative_to(ROOT))


def read_source(path: Path, marker: str) -> str:
    """The file's text, or a loud failure naming it - an absent table is drift."""
    if not path.is_file():
        raise SystemExit(f"{label(path)}: not found - the model table copy it should hold is gone")
    text = path.read_text(encoding="utf-8")
    if marker not in text:
        raise SystemExit(f"{label(path)}: no `{marker}` found - a renamed table is not an agreement")
    return text


def parse_pairs(text: str, path: Path, marker: str) -> dict[str, str]:
    """The `"key": "value"` entries of the first brace block after `marker`."""
    block = DICT_BLOCK.search(text, text.index(marker))
    if block is None:
        raise SystemExit(f"{label(path)}: `{marker}` has no parseable {{...}} body")
    pairs = dict(PAIR_LINE.findall(block.group(1)))
    if not pairs:
        raise SystemExit(f"{label(path)}: `{marker}` parsed to no entries")
    return pairs


def read_api_table() -> dict[str, str]:
    """The canonical suffix -> mime mapping, both keys and values."""
    return parse_pairs(read_source(SOURCE, API_TABLE_MARKER), SOURCE, API_TABLE_MARKER)


def read_ext_set(path: Path, marker: str) -> dict[str, str]:
    """A web `MODEL_EXTS` set, dotted, so it compares against the API's keys."""
    text = read_source(path, marker)
    members = SET_MEMBERS.search(text, text.index(marker))
    if members is None:
        raise SystemExit(f"{label(path)}: `{marker}` has no parseable [...] body")
    exts = {f".{ext.lstrip('.').lower()}" for ext in QUOTED.findall(members.group(1)) if ext.strip()}
    if not exts:
        raise SystemExit(f"{label(path)}: `{marker}` parsed to no entries")
    return {ext: "" for ext in sorted(exts)}


def read_zip_mimes(canonical: dict[str, str]) -> dict[str, str]:
    """`zipUtils`' mime map, narrowed to the model suffixes the API claims.

    The map also carries the image types, which are nobody else's business here; what
    must match is one entry per model suffix, with the same value.
    """
    mimes = parse_pairs(read_source(ZIP_UTILS, ZIP_MAP_MARKER), ZIP_UTILS, ZIP_MAP_MARKER)
    return {suffix: mimes.get(suffix, "<missing>") for suffix in canonical}


def render(mapping: dict[str, str]) -> str:
    return ", ".join(f"{suffix}={value or '<set>'}" for suffix, value in sorted(mapping.items()))


def main(argv: Sequence[str]) -> int:
    if argv:
        raise SystemExit("check_model_table.py takes no arguments")
    canonical = read_api_table()
    # A `MODEL_EXTS` set carries suffixes only, so those two copies are compared on
    # their keys; `zipUtils` is a mime map, so it must agree on both halves.
    copies = [
        (f'{label(ASSET_KINDS)} MODEL_EXTS', read_ext_set(ASSET_KINDS, EXT_SET_MARKER), False),
        (f'{label(GRID_META)} MODEL_EXTS', read_ext_set(GRID_META, GRID_EXT_SET_MARKER), False),
        (f'{label(ZIP_UTILS)} MIME_BY_EXT (model rows)', read_zip_mimes(canonical), True),
    ]
    print(f"{label(SOURCE)} {API_TABLE_MARKER}: {render(canonical)}")
    for name, values, _carries_values in copies:
        print(f"{name}: {render(values)}")

    drifted = []
    for name, values, carries_values in copies:
        if set(values) != set(canonical):
            drifted.append((name, f"suffixes {sorted(values)} != {sorted(canonical)}"))
            continue
        if carries_values:
            wrong = {suffix: values[suffix] for suffix in canonical if values[suffix] != canonical[suffix]}
            if wrong:
                drifted.append((name, ", ".join(f"{suffix}={value}" for suffix, value in sorted(wrong.items()))))

    if drifted:
        sys.stdout.flush()  # keep the listing above the error when both are piped
        print(f"model table drift: {label(SOURCE)} says {render(canonical)}", file=sys.stderr)
        for name, detail in drifted:
            print(f"  {name} says {detail}", file=sys.stderr)
        print(f"Edit the mirrors to match {label(SOURCE)}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
