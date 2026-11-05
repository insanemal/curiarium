"""Serialise one bundle report to JSON, and diff two artifacts. Dev-only.

This module is a TOOL, not a surface. It is not imported by ``main.py`` or by any
router, it adds no route, no permission row and no audit action, and nothing in
the app imports it: the only way to run it is by hand from ``curiarium/api``::

    python bundle_serialize.py --root /path/to/storage --db /path/to/curiarium.db --out /tmp/report.json
    python bundle_serialize.py --diff OLD NEW

The artifact is a SUPERSET of the baseline measurement taken by hand for
``Mf-9ygi`` (``.plans/measurements/2026-11-04-bundles-d660de3.json``): every
baseline key is present with the same meaning, and the extra keys are what makes
two runs comparable - the rule-set version and stamp, the build sha and dirty
flag, the exact command, the database the numbers came from, and the rule that
refused every path that is not offered.

**Stamps decide comparability.** Two artifacts whose ``rule_set.stamp`` differs
were taken against different detector tables, so a row delta between them is a
delta of the RULES, not of the library. ``diff_artifacts`` says so explicitly and
``format_diff`` prints the warning; a pre-stamp baseline (anything older than
``Mf-9ygi``) is marked not comparable rather than compared to memory.

The report itself is read from disk, so this tool never writes to the library or
to the database it was pointed at. The database is opened read-only in practice -
the tool copies a sqlite file to a temp directory before the app imports, so even
a stray write would land in the copy.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

ARTIFACT_SCHEMA = "bundle-report/3"
# The keys the hand-taken baseline row carries. A row in this artifact carries
# these with the same meaning, plus the rule-set stamp that dates it.
BASELINE_ROW_KEYS = (
    "rel_path",
    "detector",
    "confidence",
    "status",
    "blocked_reason",
    "existing_project_match",
    "existing_project_name",
    "twin_verified",
)
# The row's role counts, added by Mf-94od and read from the report's own
# `model_count` / `image_count` / ... fields. Without them the Rocket's 96 -> 48
# could not be seen at all: the baseline row carries no count, so the artifact
# that recorded the rule change could not show the number the rule changed.
COUNT_FIELDS = ("model_count", "image_count", "text_count", "other_count")


def _repo_root(start=None) -> Optional[Path]:
    """The nearest ancestor of this module holding a `.git` entry, or ``None``.

    Walks up from ``Path(__file__).resolve().parent`` and never indexes
    ``parents[n]``: the API image flattens this module to
    ``/app/bundle_serialize.py``, where a fixed index is an ``IndexError``.
    ``.git`` is a directory in a checkout and a FILE in a linked worktree, so
    existence is the test, not ``is_dir()``. Returns ``None`` at the filesystem
    root, so a measurement without provenance is still a measurement.
    """
    current = Path(__file__) if start is None else Path(start)
    current = current.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _build_sha() -> dict:
    """The commit this artifact was produced from, and whether it was dirty.

    Never raises: a checkout with no git, no commits or a broken ``.git`` is a
    missing provenance field, not a reason a measurement cannot be taken.
    """
    repo_root = _repo_root()
    if repo_root is None:
        return {"build_sha": None, "build_dirty": False}
    sha = None
    dirty = False
    try:
        sha = (
            subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            or None
        )
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=False,
        )
        dirty = bool(status.stdout.strip())
    except OSError:
        return {"build_sha": None, "build_dirty": False}
    return {"build_sha": sha, "build_dirty": dirty}


def _immediate_children(area: Path) -> tuple:
    """Immediate child directories and immediate ``.zip`` files of ``area``.

    Read-only and symlink-free: ``follow_symlinks=False`` on both the entry and
    its stat, so a link out of the library is never counted as a bundle.
    """
    directories = []
    archives = []
    try:
        with os.scandir(area) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(entry.name)
                    elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".zip"):
                        archives.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return [], []
    return sorted(directories), sorted(archives)


def _database_counts() -> tuple:
    """``(assets, projects)`` rows in the database this process was pointed at."""
    from sqlalchemy import func
    from sqlmodel import Session, select

    from db import engine
    from models import Asset, Project

    with Session(engine) as session:
        assets = session.exec(select(func.count()).select_from(Asset)).one()
        projects = session.exec(select(func.count()).select_from(Project)).one()
    return int(assets or 0), int(projects or 0)


def _empty_database_note(path: str) -> str:
    return (
        f"the database at {path} holds 0 asset rows and 0 project rows: it is a dev/empty copy, "
        "not the library the report was taken against, so asset and project counts are NOT MEASURED "
        "(null), not zero"
    )


def serialise_report(root, *, limit=None, command=None, database_path=None) -> dict:
    """Build one artifact for ``root``: the report, its rule set, and its refusals.

    ``database_path`` is the database the caller actually pointed at. The CLI
    copies a sqlite file to a temp directory before the app imports, so
    ``db.DB_URL`` would otherwise record a path that is deleted when the tool
    exits; the caller passes the real source instead.
    """
    import bundle_detect
    import bundle_service
    import db

    root = Path(root)
    previous = bundle_service.STORAGE
    bundle_service.STORAGE = root
    try:
        report = bundle_service.build_report(limit=limit or bundle_service.MAX_SCAN_ENTRIES)
    finally:
        bundle_service.STORAGE = previous

    assets, projects = _database_counts()
    database_path = database_path or db.DB_URL
    empty_copy = assets == 0 and projects == 0
    asset_count = None if empty_copy else assets
    project_count = None if empty_copy else projects

    import_area = "import" if (root / "import").is_dir() else None
    area = root / import_area if import_area else root
    area_dirs, area_zips = _immediate_children(area)

    rows = []
    for row in report["bundles"]:
        archive = row["archive"]
        serialised = {key: row.get(key) for key in BASELINE_ROW_KEYS}
        serialised["twin_verified"] = archive["twin_verified"] if archive else None
        serialised["rule_set"] = row["rule_set"]
        # The counts and the shapes the row was DECIDED from. A count-only rule
        # change is invisible in a before/after pair of baseline rows, so these
        # are recorded here rather than left to a reader's memory (Mf-94od).
        serialised["counts"] = {
            field[: -len("_count")]: row.get(field) for field in COUNT_FIELDS
        }
        # A report row always carries both: `groups` is built unconditionally and
        # the row's shape field is `metadata_shape` (there is no `shape` key on a
        # row - only a marker has one).
        serialised["groups"] = row["groups"]
        serialised["shape"] = row["metadata_shape"]
        rows.append(serialised)
    rows.sort(key=lambda item: item["rel_path"])

    refused = sorted(
        (
            {"rel_path": item["rel_path"], "rule": item["rule"], "kind": item["kind"]}
            for item in report["refused"]
        ),
        key=lambda item: item["rel_path"],
    )
    ledger = {item["rel_path"]: item["rule"] for item in refused}
    offered = {row["rel_path"] for row in rows}
    dirs_not_offered = []
    for name in area_dirs:
        rel_path = f"{import_area}/{name}" if import_area else name
        if rel_path in offered:
            continue
        dirs_not_offered.append({"rel_path": rel_path, "rule": ledger.get(rel_path, "unclassified")})
    dirs_not_offered.sort(key=lambda item: item["rel_path"])

    offered_archives = [row for row in rows if row["rel_path"].lower().endswith(".zip")]
    refused_archives = [item for item in refused if item["kind"] == "archive"]
    build = _build_sha()

    return {
        "artifact_schema": ARTIFACT_SCHEMA,
        "taken_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "image_sha": None,
        "build_sha": build["build_sha"],
        "build_dirty": build["build_dirty"],
        "command": list(command) if command else ["bundle_serialize.serialise_report"],
        "rule_set": {
            "version": bundle_detect.RULE_SET_VERSION,
            "stamp": report["rule_set"]["stamp"],
            "tables": bundle_detect.rule_set_tables(),
        },
        "library": {
            "storage_root": str(root),
            "import_area": import_area,
            "assets": {"total": asset_count, "unfiled": None, "missing": None},
            "projects": project_count,
            "import_dirs": len(area_dirs),
            "import_zips": len(area_zips),
            "archives": len(refused_archives) + len(offered_archives),
        },
        "database": {
            "path": database_path,
            "assets": asset_count,
            "projects": project_count,
            "note": _empty_database_note(database_path) if empty_copy else None,
        },
        "truncated": report["truncated"],
        "counts": report["counts"],
        "duplicates": {"groups": len(report["duplicates"]), "bytes": report["duplicates_total_bytes"]},
        "duplicate_groups": sorted(report["duplicates"], key=lambda group: group["sha256"]),
        "rows": rows,
        "offered_keys": sorted(offered),
        "refused": refused,
        "dirs_not_offered": dirs_not_offered,
    }


def write_artifact(artifact, out) -> None:
    """Write one artifact as sorted, trailing-newline JSON; parents are created."""
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_artifact(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rows_by_path(artifact) -> dict:
    return {row["rel_path"]: row for row in artifact.get("rows") or ()}


def _refusals_by_path(artifact) -> dict:
    return {item["rel_path"]: item["rule"] for item in artifact.get("refused") or ()}


def diff_artifacts(old, new) -> dict:
    """What changed between two artifacts, and whether they are comparable at all.

    Comparability is decided by the rule-set stamp, never by the dates: two runs
    of the same stamp differ only by the library, while two different stamps
    differ by the rules, which is exactly the confusion this artifact exists to
    remove.
    """
    old_rule_set = old.get("rule_set") or {}
    new_rule_set = new.get("rule_set") or {}
    old_stamp = old_rule_set.get("stamp")
    new_stamp = new_rule_set.get("stamp")
    old_version = old_rule_set.get("version")
    new_version = new_rule_set.get("version")
    comparable = bool(old_stamp) and bool(new_stamp) and old_stamp == new_stamp
    if comparable:
        reason = None
    elif not old_stamp:
        reason = "the old artifact has no rule-set stamp (a pre-Mf-9ygi baseline): not comparable by rule set"
    elif not new_stamp:
        reason = "the new artifact has no rule-set stamp: not comparable by rule set"
    else:
        reason = f"the rule set changed: old {old_version} {old_stamp} -> new {new_version} {new_stamp}"

    old_rows = _rows_by_path(old)
    new_rows = _rows_by_path(new)
    added = sorted(set(new_rows) - set(old_rows))
    removed = sorted(set(old_rows) - set(new_rows))
    detector_changed = []
    confidence_changed = []
    status_changed = []
    match_changed = []
    twin_changed = []
    counts_changed = []
    common = sorted(set(old_rows) & set(new_rows))
    # Counts were added to a row in schema 3 (Mf-94od). An older artifact carries
    # none, so a count delta is DECLARED unavailable rather than invented - the
    # same shape as the pre-Mf-9ygi refusal ledger above. The comparison is PER
    # ROW: one schema-2 row must never suppress the deltas the other rows can
    # still prove, which is the invisible-count-movement failure this ticket
    # exists to prevent.
    counts_unavailable = [
        rel_path
        for rel_path in common
        if old_rows[rel_path].get("counts") is None or new_rows[rel_path].get("counts") is None
    ]
    counts_recorded = None if not common else len(counts_unavailable) < len(common)
    for rel_path in common:
        before = old_rows[rel_path]
        after = new_rows[rel_path]
        if before.get("counts") is None or after.get("counts") is None:
            continue
        if before["counts"] != after["counts"]:
            counts_changed.append(
                {"rel_path": rel_path, "old": before["counts"], "new": after["counts"]}
            )
        if before.get("detector") != after.get("detector"):
            detector_changed.append({"rel_path": rel_path, "old": before.get("detector"), "new": after.get("detector")})
        if before.get("confidence") != after.get("confidence"):
            confidence_changed.append(
                {"rel_path": rel_path, "old": before.get("confidence"), "new": after.get("confidence")}
            )
        if before.get("status") != after.get("status"):
            status_changed.append({"rel_path": rel_path, "old": before.get("status"), "new": after.get("status")})
        if before.get("existing_project_match") != after.get("existing_project_match"):
            match_changed.append(
                {
                    "rel_path": rel_path,
                    "old": before.get("existing_project_match"),
                    "new": after.get("existing_project_match"),
                }
            )
        if before.get("twin_verified") != after.get("twin_verified"):
            twin_changed.append(
                {"rel_path": rel_path, "old": before.get("twin_verified"), "new": after.get("twin_verified")}
            )

    # A pre-Mf-9ygi artifact has no `refused` key at all: its refusals were never
    # recorded. Reporting those as "added" would read as 500 things changing, so
    # the comparison is declared unavailable rather than invented.
    old_has_refusals = "refused" in old
    new_has_refusals = "refused" in new
    old_refusals = _refusals_by_path(old)
    new_refusals = _refusals_by_path(new)
    refusals_comparable = old_has_refusals and new_has_refusals
    refused_added = sorted(set(new_refusals) - set(old_refusals)) if refusals_comparable else []
    refused_removed = sorted(set(old_refusals) - set(new_refusals)) if refusals_comparable else []
    refusal_changed = (
        [
            {"rel_path": rel_path, "old": old_refusals[rel_path], "new": new_refusals[rel_path]}
            for rel_path in sorted(set(old_refusals) & set(new_refusals))
            if old_refusals[rel_path] != new_refusals[rel_path]
        ]
        if refusals_comparable
        else []
    )

    return {
        "old_rule_set": {"version": old_version, "stamp": old_stamp},
        "new_rule_set": {"version": new_version, "stamp": new_stamp},
        "comparable": comparable,
        "reason": reason,
        "old_truncated": old.get("truncated"),
        "new_truncated": new.get("truncated"),
        "old_rows": len(old_rows),
        "new_rows": len(new_rows),
        "added": added,
        "removed": removed,
        "counts_recorded": counts_recorded,
        "counts_unavailable": counts_unavailable,
        "counts_changed": counts_changed,
        "detector_changed": detector_changed,
        "confidence_changed": confidence_changed,
        "status_changed": status_changed,
        "existing_project_match_changed": match_changed,
        "twin_verified_changed": twin_changed,
        "refusals_comparable": refusals_comparable,
        "refused_added": refused_added,
        "refused_removed": refused_removed,
        "refusal_changed": refusal_changed,
    }


def _names(values) -> str:
    return ", ".join(values) if values else "-"


def _counts_text(counts) -> str:
    """One role-count mapping as ``model=48 image=0 ...``, keys sorted."""
    if not counts:
        return "-"
    return " ".join(f"{key}={counts[key]}" for key in sorted(counts))


def format_diff(result) -> str:
    """The printable form of ``diff_artifacts``: comparable, then every change."""
    lines = []
    new_rule_set = result["new_rule_set"]
    if result["comparable"]:
        lines.append(
            f"comparable: YES (rule set {new_rule_set['version']} {new_rule_set['stamp']})"
        )
    else:
        lines.append(f"comparable: NO - {result['reason']}")
        if result["old_rule_set"]["stamp"] and result["new_rule_set"]["stamp"]:
            lines.append(
                "the row delta below is caused by the rule change, not by the corpus; "
                "do not read the counts as a like-for-like comparison"
            )
        else:
            lines.append(
                "one artifact carries no rule-set stamp, so the row delta cannot be attributed "
                "to the corpus; treat it as diagnostic only, never as a like-for-like comparison"
            )
    if result["old_truncated"] or result["new_truncated"]:
        lines.append(
            f"truncated: old={result['old_truncated']} new={result['new_truncated']}"
        )
    lines.append(f"rows: {result['old_rows']} -> {result['new_rows']}")
    lines.append(f"added ({len(result['added'])}): {_names(result['added'])}")
    lines.append(f"removed ({len(result['removed'])}): {_names(result['removed'])}")
    if result["counts_recorded"] is False:
        lines.append(
            "counts: not recorded in either artifact (a pre-Mf-94od schema-2 row), "
            "so a count delta cannot be named"
        )
    # `counts_recorded is None` means no row is in common at all, so there is
    # nothing to compare and nothing to admit - an empty "counts changed (0)"
    # would read as a verified non-change.
    if result["counts_recorded"] is True:
        if result["counts_unavailable"]:
            lines.append(
                f"counts: not recorded for {len(result['counts_unavailable'])} of the common rows "
                f"(a pre-Mf-94od schema-2 row): {_names(result['counts_unavailable'])}"
            )
        lines.append(f"counts changed ({len(result['counts_changed'])}):")
        for item in result["counts_changed"]:
            lines.append(f"  {item['rel_path']}: {_counts_text(item['old'])} -> {_counts_text(item['new'])}")
        if not result["counts_changed"]:
            lines.append("  -")
    lines.append(f"detector changed ({len(result['detector_changed'])}):")
    for item in result["detector_changed"]:
        lines.append(f"  {item['rel_path']}: {item['old']} -> {item['new']}")
    if not result["detector_changed"]:
        lines.append("  -")
    lines.append(f"confidence changed ({len(result['confidence_changed'])}):")
    for item in result["confidence_changed"]:
        lines.append(f"  {item['rel_path']}: {item['old']} -> {item['new']}")
    if not result["confidence_changed"]:
        lines.append("  -")
    lines.append(f"status changed ({len(result['status_changed'])}):")
    for item in result["status_changed"]:
        lines.append(f"  {item['rel_path']}: {item['old']} -> {item['new']}")
    if not result["status_changed"]:
        lines.append("  -")
    lines.append(f"existing-project match changed ({len(result['existing_project_match_changed'])}):")
    for item in result["existing_project_match_changed"]:
        lines.append(f"  {item['rel_path']}: {item['old']} -> {item['new']}")
    if not result["existing_project_match_changed"]:
        lines.append("  -")
    lines.append(f"twin-verified changed ({len(result['twin_verified_changed'])}):")
    for item in result["twin_verified_changed"]:
        lines.append(f"  {item['rel_path']}: {item['old']} -> {item['new']}")
    if not result["twin_verified_changed"]:
        lines.append("  -")
    if not result["refusals_comparable"]:
        lines.append("refusals: the old artifact did not record them (pre-Mf-9ygi), so this list cannot be compared")
    lines.append(f"refused added ({len(result['refused_added'])}): {_names(result['refused_added'])}")
    lines.append(f"refused removed ({len(result['refused_removed'])}): {_names(result['refused_removed'])}")
    lines.append(f"refused changed ({len(result['refusal_changed'])}):")
    for item in result["refusal_changed"]:
        lines.append(f"  {item['rel_path']}: {item['old']} -> {item['new']}")
    if not result["refusal_changed"]:
        lines.append("  -")
    unchanged = not (
        result["added"]
        or result["removed"]
        or result["counts_changed"]
        or result["counts_unavailable"]
        or result["detector_changed"]
        or result["confidence_changed"]
        or result["status_changed"]
        or result["existing_project_match_changed"]
        or result["twin_verified_changed"]
        or result["refused_added"]
        or result["refused_removed"]
        or result["refusal_changed"]
    )
    if unchanged:
        lines.append("no row or refusal changed")
    return "\n".join(lines)


def main(argv=None) -> int:
    """The CLI: serialise a report to ``--out``, or diff two artifacts."""
    repo_root = _repo_root()
    parser = argparse.ArgumentParser(
        description="Serialise a bundle report to JSON, or diff two artifacts (dev-only).",
    )
    parser.add_argument("--root", help="storage root to scan")
    parser.add_argument(
        "--db",
        default=str(repo_root / "curiarium.db") if repo_root else None,
        help="database to count rows from",
    )
    parser.add_argument("--out", help="artifact path to write")
    parser.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"), help="compare two artifacts")
    parser.add_argument("--limit", type=int, default=None, help="row limit (default: the whole walk)")
    args = parser.parse_args(argv)

    if args.diff:
        print(format_diff(diff_artifacts(load_artifact(args.diff[0]), load_artifact(args.diff[1]))))
        return 0

    if not args.root or not args.out:
        parser.error("--root and --out are required unless --diff is given")

    if args.db is None:
        parser.error(
            "--db is required: no repository root, and so no default curiarium.db, was found above this module"
        )

    # The app binds its paths at IMPORT time, so the temp environment has to be in
    # place before bundle_service (or db) is imported - which is what this tool
    # does by importing them lazily inside serialise_report, after these lines.
    workdir = Path(tempfile.mkdtemp(prefix="bundle-serialize-"))
    os.environ["FILE_STORAGE"] = str(workdir / "storage")
    os.environ.setdefault("AUTH_MODE", "auto")
    os.environ["IMPORT_MOUNT_ON_STARTUP"] = "false"
    if "://" in args.db:
        os.environ["DB_URL"] = args.db
    else:
        database_copy = workdir / "db.sqlite"
        shutil.copy2(args.db, database_copy)
        os.environ["DB_URL"] = f"sqlite:///{database_copy}"

    artifact = serialise_report(
        Path(args.root),
        limit=args.limit,
        command=[sys.executable, *sys.argv],
        database_path=args.db,
    )
    write_artifact(artifact, args.out)
    print(args.out)
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
