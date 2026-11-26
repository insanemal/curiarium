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

Since Mf-1agz the app does NOT keep a second opinion: `GET /bundles/detect` asks
this same engine about every row and serves its ranking as `matches`, so what this
harness prints is what the operator is offered (the report bounds the git probe at
5 s per row; the CLI uses the engine's own default). The arranging half (rename,
move, hard-link) is still a separate, approval-gated step, and what it enacts is
`proposal_engine.arrange_moves`.

WHAT THIS FILE IS, since Mf-yitm: measurements and interpretations live in
`curiarium/api/proposal_engine.py`, an app-free module (no FastAPI, no database),
so the API can call the same rules a route needs without a subprocess or a second
copy. The engine is app-free, not stdlib-only: it uses `pypdf` for PDF text and
document info, so run this harness under the repo virtualenv
(`.venv/bin/python`, which installs `curiarium/api/requirements*.lock`). What is
left here is argv, the sys.path bootstrap that lets that interpreter find the
engine, and the rendering - which is why nothing below defines a rule, and why a
`def propose` in this file would be a fork of the engine rather than a fix to it.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ENGINE_DIR = Path(__file__).resolve().parents[1] / "api"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import proposal_engine  # noqa: E402  (after the sys.path bootstrap, by construction)


def render_archive(rel: str, facts: proposal_engine.ArchiveFacts,
                   options: List[proposal_engine.Option],
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
        out.append(f"    {option.score:>3}  {option.label}  [{option.confidence}]")
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
            if name.startswith(".") or name in proposal_engine.APP_OWNED or not os.path.isdir(path):
                continue
            targets.append((path, name))

    def consider(path: str, rel: str, depth_left: int) -> None:
        facts = proposal_engine.measure(path, rel)
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


def render(rel: str, options: List[proposal_engine.Option], reasons: List[str],
           facts: proposal_engine.Facts, chosen: str, arrange: List[str],
           repo: Optional[dict] = None, peel: Optional[List[dict]] = None) -> str:
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
        out.append(f"    {option.score:>3}  {option.label}  [{option.confidence}]")
        out.append(f"         {option.detail}")
        if option is not options[0]:
            out.append(f"         -> {option.consequence}")
    if repo is not None:
        out.append(
            f"  git       {(repo.get('branch') or ('detached at ' + (repo.get('sha') or '?')))} "
            f"@ {(repo['head'].split() or ['?'])[0]} · "
            f"{len(repo['tracked'])} tracked · {len(repo['untracked'])} untracked"
            + (" · truncated" if repo.get('truncated') else "")
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


def render_validation(report: dict) -> str:
    """The `--validate` text, the same characters the tool printed before Mf-yitm.

    The scoring is `proposal_engine.validate`'s: this only decides how it reads. A
    MISSING label keeps its old shape - the bare `MISSING` row, with no `OK `/`BAD `
    marker - because the marker means "a shape was measured", and nothing was.
    """
    out: List[str] = []
    agree, disagree = report["agree"], report["disagree"]
    archives, missing = report["archives"], report["missing"]
    total = len(agree) + len(disagree)
    out.append(
        f"labelled projects: {report['labelled']}   folder-keyed on disk: {total}   "
        f"archive-keyed: {len(archives)}   missing folders: {len(missing)}"
    )
    if total:
        out.append(f"agree  (one project): {len(agree)} ({len(agree) / total:.1%})")
    out.append(f"disagree (would split): {len(disagree)}")
    if disagree:
        out.append("")
        out.append("the rules would fragment these confirmed projects:")
        for item in disagree:
            out.append(f"  {item['key']:<52} -> {item['label']} ({item['score']})")
            if item["runner_up"]:
                out.append(f"       runner-up: {item['runner_up'][0]} ({item['runner_up'][1]})")
            for reason in item["reasons"][:3]:
                out.append(f"       why: {reason}")
    if archives:
        present = sum(1 for item in archives if item["exists"])
        out.append("")
        out.append(
            f"archive-keyed projects (the tool has NO archive mode): {len(archives)}, "
            f"{present} of their zips still on disk"
        )
        for item in archives[:4]:
            out.append(f"  {item['key'][:70]}  {'present' if item['exists'] else 'GONE'}")
    if missing:
        out.append("")
        out.append("labelled but not on disk (renamed/moved since?):")
        for item in missing[:10]:
            out.append(f"  {item['key']}")
        if len(missing) > 10:
            out.append(f"  ... {len(missing) - 10} more")
    if report["archive_scored"]:
        scored = report["archive_scored"]
        agree_archive = [item for item in scored if item["chosen"] == "archive-project"]
        out.append("")
        out.append(
            f"archive-keyed vs the rules: {len(agree_archive)} of {len(scored)} "
            f"recommend 'one project from this archive'"
        )
        for item in scored:
            if item["chosen"] != "archive-project":
                out.append(
                    f"  {item['key'][:60]:<60} -> {item['chosen']} "
                    f"({item['models']} models, {item['entries']} entries)"
                )
    # Confirmed SPLIT labels: the rule this library has no other evidence for.
    if report["labels"]:
        out.append("")
        out.append("confirmed split labels:")
        for row in report["labels"]:
            if row["shape"] is None:
                out.append(f"  {row['key']:<44} {row['detail']}")
            else:
                out.append(f"  {'OK ' if row['ok'] else 'BAD'} {row['key']:<44} {row['detail']}")
    return "\n".join(out) + "\n"


def folder_facts_json(facts: proposal_engine.Facts) -> dict:
    """The WHOLE fact sheet, with every section marked fact or suggestion.

    Rendering only: the values are the engine's. `kind: fact` means a measured count,
    structure or result; `kind: suggestion` means a value the operator may confirm,
    and every such entry carries its own `where` and `confidence`.
    """
    return {
        "kind": "folder",
        "rel": facts.rel,
        "counts": {
            "kind": "fact", "models": facts.models, "images": facts.images,
            "docs": facts.docs, "others": facts.others, "archives": facts.archives,
            "bytes": facts.bytes,
        },
        "structure": {
            "kind": "fact",
            "model_dirs": len(facts.model_dirs),
            "depth": facts.depth,
            "dominance": round(facts.dominance(), 3),
            "top_children": facts.top_children_with_models(),
            "root_meta": facts.root_meta,
            "children_with_product_document": facts.children_with_own_meta(),
            "truncated": facts.truncated,
        },
        "groups": {"kind": "fact", "tree": facts.groups},
        "twin": {"kind": "fact", **facts.twin, "archives": facts.twin_archives()},
        "cover": {"kind": "fact", **facts.cover},
        "documents": {"kind": "fact", "items": facts.documents},
        "metadata": {"kind": "suggestion", "fields": facts.metadata},
        "provenance": {
            "kind": "suggestion",
            "candidates": facts.provenance["candidates"],
            "unresolved": facts.provenance["unresolved"],
            "confidence": facts.provenance["confidence"],
        },
    }


def archive_facts_json(facts: proposal_engine.ArchiveFacts, rel: str) -> dict:
    """An archive's fact sheet: it has counts and a payload, not a metadata tree."""
    return {
        "kind": "archive",
        "rel": rel,
        "entries": {"kind": "fact", "count": len(facts.entries)},
        "counts": {
            "kind": "fact", "models": facts.models, "images": facts.images,
            "docs": facts.docs, "others": facts.others,
        },
        "common_root": {"kind": "fact", "value": facts.common_root()},
        "nested": {"kind": "fact", "items": facts.nested},
        "groups": {"kind": "fact", "tree": []},
        "twin": {"kind": "fact", "verified": False, "pairs": [], "unverified": []},
        "cover": {"kind": "fact", "reason": "none", "chosen": None, "candidates": []},
        "documents": {"kind": "fact", "items": []},
        "metadata": {"kind": "suggestion", "fields": {}},
        "provenance": {
            "kind": "suggestion", "candidates": [], "unresolved": [],
            "confidence": "suggested",
        },
    }


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=os.environ.get("CURIARIUM_STORAGE",
                                                         "/mnt/ceph/3D_models"))
    parser.add_argument("--only", action="append", default=[],
                        help="relative (or absolute) folder; repeatable")
    parser.add_argument("--top", type=int, default=0, help="limit how many folders print")
    parser.add_argument("--json", action="store_true", help="machine-readable, for diffing")
    parser.add_argument("--facts", metavar="KEY",
                        help="dump every fact the engine measured for one path, as JSON")
    parser.add_argument("--explain", metavar="KEY",
                        help="print the facts, every option with its score, and the reasons")
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
        report = proposal_engine.validate(args.root, labelled, project_keys, labels)
        sys.stdout.write(render_validation(report))
        return 0 if not report["disagree"] and not report["failures"] else 1

    if args.facts or args.explain:
        key = args.facts or args.explain
        path = key if os.path.isabs(key) else os.path.join(args.root, key)
        if not os.path.exists(path):
            print(f"no such folder or archive: {key}", file=sys.stderr)
            return 2
        if os.path.isfile(path) and path.lower().endswith(".zip"):
            afacts = proposal_engine.measure_archive(path, key)
            options, reasons = proposal_engine.propose_archive(afacts)
            facts_json = archive_facts_json(afacts, key)
        else:
            facts = proposal_engine.measure(path, key)
            options, reasons, _repo = proposal_engine.propose(facts, project_keys)
            facts_json = folder_facts_json(facts)
        if args.facts:
            print(json.dumps(facts_json, indent=1))
            return 0
        print(json.dumps(facts_json, indent=1))
        print("options:")
        for index, option in enumerate(options):
            print(f"  [{option.score:>3}] {option.label}"
                  f"  confidence={option.confidence}"
                  f"  ({'recommended' if index == 0 else 'alternative'})")
            print(f"        shape: {option.shape}")
            print(f"        {option.detail}")
            print(f"        -> {option.consequence}")
        print("reasons:")
        for reason in reasons:
            print("  -", reason)
        return 0

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
            afacts = proposal_engine.measure_archive(path, rel)
            aopts, areasons = proposal_engine.propose_archive(afacts)
            achosen = aopts[0].shape if aopts else "leave"
            aarrange = proposal_engine.arrange_archive(afacts, achosen)
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
        facts = proposal_engine.measure(path, rel)
        options, reasons, repo = proposal_engine.propose(facts, project_keys=project_keys)
        chosen = options[0].shape if options else "leave"
        arrange = proposal_engine.arrange_proposal(facts, chosen)
        peel = proposal_engine.peel_proposals(facts, repo) if repo else []
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
