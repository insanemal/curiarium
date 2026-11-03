"""The one authority for "is this zip entry OS metadata rather than a file".

The vocabulary is case-folded and matched per normalised path *segment*:

* ``JUNK_SEGMENTS`` - ``__MACOSX`` and ``$RECYCLE.BIN``: a subtree, so every
  entry whose path holds such a segment is junk, however deep it sits;
* ``JUNK_BASENAMES`` - ``.DS_Store``, ``Thumbs.db``, ``desktop.ini``;
* ``JUNK_PREFIXES`` - ``._`` (AppleDouble sidecars) and ``.Spotlight-``
  (Spotlight indexes).

A segment that starts with a junk prefix makes that segment and everything
beneath it junk, so a directory literally named ``._foo`` is junk too. That is
deliberate: no real model directory is named like an AppleDouble sidecar, and
the children of one are sidecars as well.

This predicate is the authority for *importability*: ``zip_service`` reports the
verdict on every entry it lists and refuses a junk entry before it can make a
folder, an asset row or an audit row, so junk can never become an asset, a
project member or a cover.

``curiarium/web/src/lib/zipJunk.ts`` is a hand-maintained mirror of this file.
The browser uses it to decide the import modal's default ticks AND to refuse OS
metadata from a locally picked zip, which is uploaded as ordinary files through
the plain upload route and never reaches ``zip_service``. Drift between the two
therefore matters in both directions: a path this file calls junk and the mirror
does not would be pre-ticked and, for a locally picked zip, uploaded for real.
Widen this file first, then its mirror.

Two narrower, pre-existing vocabularies stay where they are. This file does not
replace them and they are not wired to this predicate:

* ``bundle_detect.is_macosx_artifact`` matches only a *top-level* ``__MACOSX``
  segment, because bundle pairing and root detection ask the narrower question
  "is this entry outside the archive's payload root";
* ``library_scan.IGNORED_FILENAMES`` is a scan-level list of exactly three names
  for debris a filesystem walk meets, which is a different door.

This predicate is the broader one: it is the only vocabulary that decides what a
zip import may admit.
"""

# Mf-ls9v, the six rules in one table so a reader can count them:
#   1. a segment equal to __MACOSX        2. a segment equal to $RECYCLE.BIN
#   3. a segment equal to .DS_Store       4. a segment equal to Thumbs.db
#   5. a segment equal to desktop.ini     6. a segment starting with ._ or .Spotlight-
JUNK_SEGMENTS = frozenset({"__macosx", "$recycle.bin"})
JUNK_BASENAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
JUNK_PREFIXES = ("._", ".spotlight-")


def is_junk_entry(path: str) -> bool:
    """Whether this zip entry path is OS metadata rather than one of its files.

    Total on any input: an empty or missing path is not junk, and nothing but
    separators and case is normalised away. A zip member name is not guaranteed
    to use ``/`` (a hand-made archive can carry ``x\\__MACOSX\\y.stl``), so both
    separators split it, and empty segments between them are ignored.
    """
    if not path:
        return False
    for segment in path.replace("\\", "/").split("/"):
        if not segment:
            continue
        folded = segment.casefold()
        if folded in JUNK_SEGMENTS or folded in JUNK_BASENAMES:
            return True
        if folded.startswith(JUNK_PREFIXES):
            return True
    return False
