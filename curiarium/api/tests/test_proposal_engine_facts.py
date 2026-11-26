"""The fact sheet Mf-9h0v adds to the proposal engine, pinned one behaviour at a time.

Every test names, in its docstring, the MUTATION that would red it. The engine's
validated RANKING is unchanged by any of this: metadata, provenance, groups, twin,
cover and documents are additive evidence for a report, so a test here may only
assert the new fact - the existing `test_proposal_engine.py` still pins the score,
the shape and every `arrange` line.

Synthetic PDFs are written by hand (`make_pdf`) with a real FlateDecode text
stream, real document info and real `/Annots` link actions, so the PDF path is
exercised without a network or a fixture file outside `tmp_path`.
"""

import json
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

import proposal_engine

HIGH, MEDIUM, LOW = "high", "medium", "low"


# --- helpers --------------------------------------------------------------

def write(path: Path, text: str = "") -> None:
    """One text file, parents and all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_models(folder: Path, count: int, suffix: str = ".stl") -> None:
    for index in range(count):
        write_bytes(folder / f"model{index}{suffix}", b"solid\n")


def make_pdf(text: str = "", annots=(), info=None, *, compress: bool = True) -> bytes:
    """A minimal but real PDF: compressed text stream, info dict, link annotations.

    Hand-written rather than `PdfWriter` because the test needs a FlateDecode text
    stream and `/Annots` link actions together, which is exactly the shape the live
    Printables and Thingiverse cards ship.
    """
    lines = text.splitlines() or [""]
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
    ops = "BT /F1 12 Tf 72 720 Td " + " ".join(
        f"({line}) Tj 0 -16 Td" for line in escaped
    ) + " ET"
    stream = ops.encode("latin-1")
    body = zlib.compress(stream) if compress else stream
    filtered = b" /Filter /FlateDecode" if compress else b""
    first_annot = 5
    annots_refs = b""
    if annots:
        annots_refs = (" /Annots [" + " ".join(
            f"{first_annot + index} 0 R" for index in range(len(annots))
        ) + "]").encode()
    content_obj = first_annot + len(annots)
    info_obj = content_obj + 1
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents "
            + str(content_obj).encode() + b" 0 R" + annots_refs + b" >>"),
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, uri in enumerate(annots):
        objects[first_annot + index] = (
            "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] "
            f"/A << /S /URI /URI ({uri}) >> >>"
        ).encode()
    objects[content_obj] = (
        b"<< /Length " + str(len(body)).encode() + filtered
        + b" >>\nstream\n" + body + b"\nendstream"
    )
    trailer_extra = b""
    if info:
        parts = " ".join(f"/{key} ({value})" for key, value in info.items())
        objects[info_obj] = ("<< " + parts + " >>").encode()
        trailer_extra = f" /Info {info_obj} 0 R".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for number in range(1, size):
        if number in offsets:
            out += f"{offsets[number]:010d} 00000 n \n".encode()
        else:
            out += b"0000000000 65535 f \n"
    out += (f"trailer\n<< /Size {size} /Root 1 0 R".encode() + trailer_extra
            + b" >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(out)


def facts_for(folder: Path) -> proposal_engine.Facts:
    return proposal_engine.measure(str(folder), folder.name)


# --- metadata from vendor documents ---------------------------------------

def test_metadata_from_a_thingiverse_readme_pair(tmp_path: Path) -> None:
    """The Thingiverse marker pair and licence line become suggestions with a source.

    Mutation: default a title or a licence when the document states none, or drop
    `where`, and the report can no longer say which document made the claim.
    """
    product = tmp_path / "Wall Mount"
    write_models(product, 2)
    write(product / "README.txt",
          "A sturdy wall bracket.\n\n"
          "Created by Thingiverse user DrLex\n"
          "Licensed under cc_by_sa\n"
          "Wall Mount by DrLex on Thingiverse: https://www.thingiverse.com/thing:1234567\n")
    write(product / "LICENSE.txt", "CC BY-SA\n")
    facts = facts_for(product)
    meta = facts.metadata
    assert meta["title"]["value"] == "Wall Mount"
    assert meta["title"]["confidence"] == HIGH
    assert meta["creator"]["value"] == "DrLex"
    assert meta["creator"]["confidence"] == HIGH
    assert meta["licence"]["value"] == "CC BY-SA"
    assert meta["licence_url"]["value"] == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert meta["source_url"]["value"] == "https://www.thingiverse.com/thing:1234567"
    assert meta["description"]["value"] == "A sturdy wall bracket."
    for field in ("title", "creator", "licence", "licence_url", "source_url", "description"):
        assert meta[field]["where"], f"{field} must name the document it came from"


def test_metadata_from_info_json(tmp_path: Path) -> None:
    """`info.json` keys are machine markers, so they are HIGH with their own source.

    Mutation: read `title`/`author`/`license` as a loose text heuristic (or ignore
    the keys) and a vendor export loses its strongest, cheapest evidence.
    """
    product = tmp_path / "Widget"
    write_models(product, 2)
    write(product / "info.json", json.dumps({
        "title": "Widget Deluxe",
        "author": "Ada",
        "license": "cc_by",
        "source_url": "https://www.printables.com/model/54172-widget",
        "description": "A widget with a lid.",
    }))
    facts = facts_for(product)
    meta = facts.metadata
    assert meta["title"] == {"value": "Widget Deluxe", "where": "info.json", "confidence": HIGH}
    assert meta["creator"]["value"] == "Ada"
    assert meta["licence"]["value"] == "CC BY"
    assert meta["source_url"]["value"] == "https://www.printables.com/model/54172-widget"
    assert meta["description"]["value"] == "A widget with a lid."


def test_metadata_from_a_vendor_html_card(tmp_path: Path) -> None:
    """A vendor `.html` card: og:title/meta-author/link-rel-license are markers.

    Mutation: fall back to a regex over `class=` names and the title lands on a
    navigation label; reading the documented `<meta>`/`<link>` attributes is what
    keeps the card honest.
    """
    product = tmp_path / "Card"
    write_models(product, 1)
    write(product / "product.html", """<!doctype html>
<html><head>
<title>Widget - Printables</title>
<meta property="og:title" content="Widget Deluxe">
<meta name="author" content="Ada">
<link rel="license" href="https://creativecommons.org/licenses/by-sa/4.0/">
</head><body>Widget</body></html>
""")
    facts = facts_for(product)
    meta = facts.metadata
    assert meta["title"]["value"] == "Widget Deluxe"
    assert meta["title"]["confidence"] == HIGH
    assert meta["creator"]["value"] == "Ada"
    assert meta["licence"]["value"] == "CC BY-SA 4.0"
    assert meta["licence_url"]["value"] == "https://creativecommons.org/licenses/by-sa/4.0/"


def test_a_licence_is_never_invented(tmp_path: Path) -> None:
    """A folder with models, images and a prose README states no licence.

    Mutation: default a licence (or copy one from a neighbouring field) and the
    engine makes a legal claim no document made. `licence` and `licence_url` must
    both be ABSENT, not empty strings and not guesses.
    """
    product = tmp_path / "Plain"
    write_models(product, 2)
    write_bytes(product / "cover.png", b"\x89PNG")
    write(product / "README.md", "A holder for the wall.\nNo licence is stated here.\n")
    facts = facts_for(product)
    assert "licence" not in facts.metadata, facts.metadata
    assert "licence_url" not in facts.metadata, facts.metadata


def test_a_truncated_licence_marker_is_not_a_licence(tmp_path: Path) -> None:
    """A Printables card's `This work is licensed under a` line must not yield `a`.

    Mutation: accept the marker's truncated trailing determiner as the licence and
    every Printables card claims a licence called "a" - a legal statement nobody
    made. The next line is the real statement and is taken instead.
    """
    product = tmp_path / "Cards"
    write_models(product, 1)
    write_bytes(product / "card.pdf", make_pdf(
        "Triple Twister by Some Maker\n"
        "This work is licensed under a\n"
        "Creative Commons (4.0 International License)\n"
    ))
    facts = facts_for(product)
    assert facts.metadata.get("licence", {}).get("value") != "a", facts.metadata
    assert facts.metadata["licence"]["value"].startswith("Creative Commons")


def test_an_unknown_licence_token_keeps_its_text_and_gets_no_url(tmp_path: Path) -> None:
    """Only a token the table knows maps to a URL; an unknown one is echoed.

    Mutation: synthesise a Creative Commons URL for whatever token appears and the
    engine tells the operator a licence the document did not grant.
    """
    product = tmp_path / "Odd"
    write_models(product, 1)
    write(product / "README.md",
          "Odd licence test\n\nCreated by Thingiverse user Kim\nLicensed under:\nHouse Rules 1.0\n")
    facts = facts_for(product)
    assert facts.metadata["licence"]["value"] == "House Rules 1.0"
    assert "licence_url" not in facts.metadata, facts.metadata


# --- provenance -----------------------------------------------------------

def test_provenance_from_a_text_document_all_four_providers(tmp_path: Path) -> None:
    """Text URLs become candidates; a scheme-less `www.` one is accepted.

    Mutation: require a scheme, and every attribution card's
    `www.thingiverse.com/thing:...` vanishes; accept any provider URL, and the
    `/users/Zeta3d` creator page is offered as a model.
    """
    product = tmp_path / "Sourced"
    write_models(product, 1)
    write(product / "README.md", "\n".join([
        "Downloaded from:",
        "https://www.thingiverse.com/thing:111",
        "https://www.printables.com/model/222-widget",
        "https://www.myminifactory.com/object/124572",
        "https://makerworld.com/models/333",
        "www.thingiverse.com/thing:444",
        "https://www.myminifactory.com/users/Zeta3d",
    ]))
    facts = facts_for(product)
    found = {(item["provider"], item["subject_id"]) for item in facts.provenance["candidates"]}
    assert ("thingiverse", "111") in found
    assert ("thingiverse", "444") in found, "a scheme-less www. URL must be accepted"
    assert ("printables", "222") in found
    assert ("myminifactory", "124572") in found
    assert ("makerworld", "333") in found
    assert all(item["subject_id"] != "Zeta3d" for item in facts.provenance["candidates"])
    assert facts.provenance["confidence"] == "suggested"
    for item in facts.provenance["candidates"]:
        assert item["where"] == "README.md"
        assert item["confidence"] in {HIGH, MEDIUM, LOW}


def test_provenance_from_a_pdf_link_annotation(tmp_path: Path) -> None:
    """The model URL lives in `/Annots`; a `/social/` creator card does not.

    Mutation: scan the PDF only as text and the Printables model link is missed (it
    is an annotation, not text); accept every URI and the creator page becomes a
    model candidate.
    """
    product = tmp_path / "Cards"
    write_models(product, 1)
    write_bytes(product / "268795-triple-twister.pdf", make_pdf(
        text="Triple Twister by Some Maker\nSummary\nA twisting module.",
        annots=[
            "https://www.printables.com/model/268795-triple-twister-module",
            "https://www.printables.com/social/112610/about",
            "https://outofmarbles.com/shop",
        ],
        info={"Title": "Printables PDF"},
    ))
    facts = facts_for(product)
    candidates = facts.provenance["candidates"]
    assert [(item["provider"], item["subject_id"]) for item in candidates] == [
        ("printables", "268795"),
    ], candidates
    assert candidates[0]["confidence"] == HIGH
    assert "annotations" in candidates[0]["where"]
    assert facts.metadata["source_url"]["value"] == (
        "https://www.printables.com/model/268795-triple-twister-module"
    )


def test_a_stronger_annotation_promotes_over_an_earlier_weaker_sighting(tmp_path: Path) -> None:
    """Duplicate subjects keep the HIGH sighting; the weak one stays in `also_in`.

    Mutation: return on the first `(provider, subject_id)` hit and a medium text
    link from one product suppresses the model's own HIGH annotation from another,
    exactly the OutOfMarbles/triple-twister row.
    """
    collection = tmp_path / "Collection"
    write_models(collection / "A", 1)
    write(collection / "A" / "notes.txt",
          "see https://www.printables.com/model/268795-triple-twister\n")
    write_models(collection / "B", 1)
    write_bytes(collection / "B" / "card.pdf", make_pdf(
        annots=["https://www.printables.com/model/268795-triple-twister"]))
    write_models(collection / "C", 1)
    write_bytes(collection / "C" / "card.pdf", make_pdf(
        annots=["https://www.printables.com/model/268795-triple-twister"]))
    facts = facts_for(collection)
    candidate = next(
        c for c in facts.provenance["candidates"] if c["subject_id"] == "268795"
    )
    assert candidate["confidence"] == HIGH
    assert "B/card.pdf" in candidate["where"], candidate
    locations = [entry["where"] for entry in candidate["also_in"]]
    assert any("A/notes.txt" in where for where in locations), (
        "the weaker text sighting must stay visible"
    )
    assert any("C/card.pdf" in where for where in locations), (
        "a tied sighting keeps the first as its face and records the other"
    )


def test_provenance_from_a_flate_decoded_pdf_text_stream(tmp_path: Path) -> None:
    """A compressed text stream is decompressed for reading; a truncated URL is not invented.

    Mutation: regex the raw PDF bytes without decompressing and the URL is invisible;
    invent an id for `thing:` with no digits and the engine names a model that the
    truncated extraction never contained.
    """
    product = tmp_path / "Compressed"
    write_models(product, 1)
    write_bytes(product / "manual.pdf", make_pdf(
        "Downloaded from https://www.thingiverse.com/thing:555111\n"
        "See also https://www.thingiverse.com/thing:"
    ))
    facts = facts_for(product)
    found = {(item["provider"], item["subject_id"]) for item in facts.provenance["candidates"]}
    assert ("thingiverse", "555111") in found
    unresolved = [item["text"] for item in facts.provenance["unresolved"]]
    assert any("thingiverse.com/thing" in text for text in unresolved), (
        facts.provenance["unresolved"]
    )
    assert all(item["subject_id"] for item in facts.provenance["candidates"]), (
        "a provider URL with no id must stay unresolved, never become a candidate"
    )


def test_a_pdf_licence_is_a_suggestion_naming_its_page(tmp_path: Path) -> None:
    """Licence text in a PDF is reported with the page it came from, never as a value.

    Mutation: write a licence into the project record directly, or drop `where`, and
    the guided flow cannot show the operator the page to confirm.
    """
    product = tmp_path / "Licenced"
    write_models(product, 1)
    write_bytes(product / "card.pdf", make_pdf(
        "Flexi Rex with stronger links by DrLex\n"
        "Licensed under:\n"
        "CC BY-SA\n"
    ))
    facts = facts_for(product)
    assert facts.metadata["licence"]["value"] == "CC BY-SA"
    assert facts.metadata["licence"]["where"] == "card.pdf p1"
    assert facts.metadata["title"]["value"] == "Flexi Rex with stronger links"
    assert facts.metadata["creator"]["value"] == "DrLex"


def test_pdf_document_info_is_the_weakest_source_and_boilerplate_is_refused(tmp_path: Path) -> None:
    """A real `/Title` is only LOW; `Printables PDF` is not a title at all.

    Mutation: trust document info as a value (instead of a low suggestion) and every
    Printables card is renamed "Printables PDF".
    """
    good = tmp_path / "Good"
    write_models(good, 1)
    write_bytes(good / "card.pdf", make_pdf(text="", info={"Title": "Wall Mount"}))
    facts = facts_for(good)
    assert facts.metadata["title"] == {
        "value": "Wall Mount", "where": "card.pdf info", "confidence": LOW,
    }

    boiler = tmp_path / "Boiler"
    write_models(boiler, 1)
    write_bytes(boiler / "card.pdf", make_pdf(text="", info={"Title": "Printables PDF"}))
    facts = facts_for(boiler)
    assert "title" not in facts.metadata, facts.metadata


def test_a_corrupt_pdf_is_a_document_not_a_crash(tmp_path: Path) -> None:
    """Garbage behind a `%PDF-` header is recorded unreadable and the walk continues.

    Mutation: let the parse raise out of `measure` and one hostile file blinds the
    whole row - the models, the cover and the groups all vanish with it.
    """
    product = tmp_path / "Broken"
    write_models(product, 2)
    write_bytes(product / "broken.pdf", b"%PDF-1.4\nthis is not a pdf\n")
    facts = facts_for(product)
    assert facts.models == 2
    assert facts.cover["reason"] == "model-preview"
    broken = next(doc for doc in facts.documents if doc["path"] == "broken.pdf")
    assert broken["read"] is False
    assert broken["skipped"] == "unreadable"


def test_a_pdf_over_the_size_cap_is_skipped_and_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cap that bites is a recorded skip, not a silent absence.

    Mutation: drop the document entirely and the operator cannot tell a no-gain PDF
    from one the engine refused to read.
    """
    product = tmp_path / "Large"
    write_models(product, 1)
    write_bytes(product / "big.pdf", b"%PDF-1.4\n" + b"x" * 200)
    monkeypatch.setattr(proposal_engine, "PDF_MAX_BYTES", 10)
    facts = facts_for(product)
    large = next(doc for doc in facts.documents if doc["path"] == "big.pdf")
    assert large["read"] is False
    assert large["skipped"] == "too-large"
    assert facts.models == 1, "one skipped document must not stop the rest of the facts"


def test_measure_degrades_when_pypdf_is_absent(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """A host without `pypdf` records the PDF unreadable and still measures the rest.

    Mutation: a top-level `import pypdf` (or an unwrapped lazy import) makes
    `measure()` fail on a host without the dependency; only the import audit would
    notice, and it could be satisfied by a lazy import that still is not wrapped.
    """
    product = tmp_path / "Product"
    write_models(product, 2)
    write(product / "info.json", json.dumps({"title": "Product", "author": "Ada"}))
    write_bytes(product / "manual.pdf", make_pdf("Title by Ada\nLicensed under cc_by\n"))
    monkeypatch.setitem(sys.modules, "pypdf", None)
    facts = facts_for(product)  # must not raise
    documents = {record["path"]: record for record in facts.documents}
    assert documents["manual.pdf"]["skipped"] == "unreadable"
    assert documents["manual.pdf"]["read"] is False
    assert documents["info.json"]["read"] is True
    assert facts.metadata["title"]["value"] == "Product"
    assert facts.models == 2
    assert facts.metadata.get("licence") is None, "the unreadable PDF said nothing"


def test_pdfs_can_be_switched_off_and_are_recorded_as_disabled(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The measurement switch records a disabled PDF and still reads text documents.

    Mutation: read the flag inside `_pdf_suggestions` and the two measurement arms
    are two codebases; skip a disabled PDF without a record and the fact sheet
    silently looks like the file was never there.
    """
    product = tmp_path / "Product"
    write_models(product, 1)
    write_bytes(product / "manual.pdf", make_pdf("Title by Ada\nLicensed under cc_by\n"))
    write(product / "README.txt", "a product for testing\n")
    monkeypatch.setattr(proposal_engine, "PDF_DOCUMENTS_ENABLED", False)
    facts = facts_for(product)
    pdf = next(record for record in facts.documents if record["path"] == "manual.pdf")
    assert pdf["skipped"] == "disabled"
    assert pdf["read"] is False
    assert "licence" not in facts.metadata, "the disabled PDF's claim must not be read"
    assert facts.metadata.get("description"), "the README beside it is still read"


# --- hierarchical groups --------------------------------------------------

def test_groups_are_hierarchical_for_the_vault_shape(tmp_path: Path) -> None:
    """A minimal nesting fixture: two family folders of parts under one top folder.

    The subject is the TREE, not the names: a manual PDF at the candidate root, one
    top folder whose `children` are two families, and part folders below each family,
    so every level and every subtree total has to be carried. The faithful measured
    tree lives in `test_proposal_engine_matrix.py`'s `build_deep_catalogue`:
    `01. DRAWERS` -> `0N. <size> Families` -> `<size>3` -> part folders, five levels
    (`01. DRAWERS` / `01. S Families` / `S3` / parts).

    The labels here are synthetic: no `S_DRAWERS`/`M_DRAWERS` exists in the Vault
    tree, and `PART_NAME.match("S_DRAWERS")` is False. The fixture's families are
    non-separable for the DOCUMENT reason (they own no document), while the corpus's
    `01. S Families` is non-separable for the ORDINAL reason; `DRAWERS` at the top
    matches `PART_NAME` (the `drawers?` arm) either way.

    Mutation: flatten the tree to per-top-folder counts and the `children` nesting
    assertion reds; count only a node's OWN level and the subtree total stops
    including the levels below it.
    """
    vault = tmp_path / "Vault"
    for family, parts in (("S_DRAWERS", 2), ("M_DRAWERS", 3)):
        for part in range(parts):
            write_models(vault / "DRAWERS" / family / f"part{part}", 1)
    # The manual sits at the candidate root, NOT at a family's own root: a document
    # directly inside S_DRAWERS would legitimately make that family separable.
    write(vault / "Instructions Manual.pdf", "%PDF-1.4\n")
    facts = facts_for(vault)
    assert [node["name"] for node in facts.groups] == ["DRAWERS"]
    drawers = facts.groups[0]
    assert drawers["models"] == 5
    assert drawers["separable"] is False, "DRAWERS reads as a part, not a product"
    kids = {node["name"]: node for node in drawers["children"]}
    assert set(kids) == {"M_DRAWERS", "S_DRAWERS"}
    assert kids["S_DRAWERS"]["models"] == 2, "a subtree count includes every level below"
    assert kids["S_DRAWERS"]["separable"] is False, "S_DRAWERS owns no document, so it cannot stand alone"
    assert {node["name"] for node in kids["S_DRAWERS"]["children"]} == {"part0", "part1"}
    assert drawers["children"], "a flat tree would lose the nesting this row needs"


def test_groups_carry_foreign_and_separable_from_the_legacy_rule(tmp_path: Path) -> None:
    """`foreign` is R7's rule; `separable` is a product document or a vendor id.

    Mutation: drop the vendor-id leg and the foreign product is swallowed as a part;
    drop the document leg and OutOfMarbles' three PDF-bearing children stop being
    separable.
    """
    root = tmp_path / "Mixed"
    write_models(root / "Gadget - 5414731" / "part", 2)
    write_models(root / "Documented" / "part", 1)
    write(root / "Documented" / "README.md", "a product\n")
    write_models(root / "Plain part" / "part", 1)
    facts = facts_for(root)
    by_name = {node["name"]: node for node in facts.groups}
    assert by_name["Gadget - 5414731"]["foreign"] is True
    assert by_name["Gadget - 5414731"]["separable"] is True
    assert by_name["Documented"]["foreign"] is True
    assert by_name["Documented"]["separable"] is True
    assert by_name["Plain part"]["separable"] is False


def test_a_part_shaped_folder_is_not_separable_even_with_its_own_document(tmp_path: Path) -> None:
    """`01. DRAWERS` reads as a part, so it is not a boundary even with a README.

    Mutation: drop the `PART_NAME` guard in `_group_separable` and a documented
    numbered sub-assembly is offered as its own project.
    """
    vault = tmp_path / "Vault"
    write_models(vault / "01. DRAWERS" / "01. S Families" / "S3" / "Boxes", 2)
    write(vault / "01. DRAWERS" / "README.txt", "Drawers by someone\nLicensed under cc_by\n")
    facts = facts_for(vault)
    node = facts.groups[0]
    assert node["name"] == "01. DRAWERS"
    assert node["separable"] is False, "a part-shaped name is not a project boundary"


def test_groups_for_the_stagetop_shape_keep_waves_and_parts(tmp_path: Path) -> None:
    """Stagetop is waves -> parts; no wave carries a document so none is separable.

    Mutation: treat `wave 1` as a product boundary and the nine parts become nine
    projects; drop the child level and the part names disappear.
    """
    stage = tmp_path / "Stagetop"
    for wave in ("Wave 1", "Wave 2"):
        for part in ("bracket", "rail"):
            write_models(stage / wave / part, 1)
    facts = facts_for(stage)
    assert [node["name"] for node in facts.groups] == ["Wave 1", "Wave 2"]
    for wave in facts.groups:
        assert wave["separable"] is False
        assert wave["foreign"] is False
        assert {child["name"] for child in wave["children"]} == {"bracket", "rail"}


# --- twin verification ----------------------------------------------------

def test_a_twin_payload_contained_in_the_folder_verifies(tmp_path: Path) -> None:
    """Casefolded path + declared size, one common-root strip: the pair is confirmed.

    Mutation: compare raw case (or require equality) and a real extracted twin -
    which holds extra hand-added files - stops verifying.
    """
    container = tmp_path / "Container"
    product = container / "Product"
    write_bytes(product / "MODEL.STL", b"solid product\n")
    write(product / "readme.txt", "hello")
    with zipfile.ZipFile(container / "Product.zip", "w") as archive:
        archive.writestr("Product/model.stl", b"solid product\n")
        archive.writestr("Product/readme.txt", "hello")
        archive.writestr("__MACOSX/Product/._model.stl", b"junk")
    facts = facts_for(container)
    assert facts.twin["verified"] is True
    pair = facts.twin["pairs"][0]
    assert pair["archive"] == "Product.zip" and pair["folder"] == "Product"
    assert pair["confirmed"] is True
    assert pair["models_in_archive"] == 1


def test_a_twin_payload_that_differs_is_unverified_with_a_reason(tmp_path: Path) -> None:
    """A zip whose payload is not contained is named in `unverified`, not dropped.

    Mutation: treat every stem match as verified and a coincidental same-name zip
    pairs two unrelated products.
    """
    container = tmp_path / "Container"
    write_bytes(container / "Product" / "model.stl", b"solid\n")
    with zipfile.ZipFile(container / "Product.zip", "w") as archive:
        archive.writestr("Product/model.stl", b"a different length entirely\n")
    facts = facts_for(container)
    assert facts.twin["verified"] is False
    assert facts.twin["unverified"], "a failed pair must be visible"
    assert "differs" in facts.twin["unverified"][0]["reason"]


def test_the_wrapper_case_needs_the_common_root_strip(tmp_path: Path) -> None:
    """The candidate IS the wrapper, its zip one level up, and the zip wraps a root.

    Mutation: remove the single `common_entry_root` strip and this pair - the exact
    `Vault Archive_…/` layout - no longer verifies even though the payload matches.
    """
    base = tmp_path / "base"
    wrapper = base / "Thing Archive_1"
    write_bytes(wrapper / "part" / "model.stl", b"solid thing\n")
    with zipfile.ZipFile(base / "Thing Archive_1.zip", "w") as archive:
        archive.writestr("Thing Archive_1/part/model.stl", b"solid thing\n")
    facts = facts_for(wrapper)
    assert facts.twin["verified"] is True
    pair = next(item for item in facts.twin["pairs"] if item["folder"] == ".")
    assert pair["archive"] == "../Thing Archive_1.zip"
    assert pair["confirmed"] is True


# --- cover ----------------------------------------------------------------

def test_cover_prefers_the_largest_image(tmp_path: Path) -> None:
    """Images rank by (-size, path) with reason `largest-image`.

    Mutation: sort by path only and the tiny `a.png` wins over the real cover.
    """
    product = tmp_path / "Covers"
    write_models(product, 1)
    write_bytes(product / "a.png", b"x")
    write_bytes(product / "cover.png", b"x" * 50)
    write(product / "README.md", "no licence\n")
    facts = facts_for(product)
    assert facts.cover["reason"] == "largest-image"
    assert facts.cover["chosen"]["path"] == "cover.png"
    assert facts.cover["chosen"]["reason"] == "largest-image"
    assert [item["path"] for item in facts.cover["candidates"]][:2] == ["cover.png", "a.png"]


def test_cover_falls_back_to_a_model_preview(tmp_path: Path) -> None:
    """No image: the largest renderable model is the preview, with its own reason.

    Mutation: return `none` when only models exist and the report has no cover for
    the most common folder in the library.
    """
    product = tmp_path / "NoImage"
    write_bytes(product / "small.stl", b"x")
    write_bytes(product / "big.stl", b"x" * 40)
    write_bytes(product / "notes.txt", b"not a cover")
    facts = facts_for(product)
    assert facts.cover["reason"] == "model-preview"
    assert facts.cover["chosen"]["path"] == "big.stl"
    assert facts.cover["chosen"]["reason"] == "model-preview"


# --- confidence bands -----------------------------------------------------

def test_a_documented_row_is_high_confidence(tmp_path: Path) -> None:
    """Documentary evidence (an info.json marker) makes the reading HIGH.

    Mutation: consult only the score and a vendor-documented product is indistinguishable
    from a guess.
    """
    product = tmp_path / "Documented"
    write_models(product / "part", 2)
    write(product / "info.json", json.dumps({"title": "Documented", "author": "Ada"}))
    facts = facts_for(product)
    options, _reasons, _repo = proposal_engine.propose(facts, (), probe_git=False)
    assert options[0].confidence == HIGH, options[0].as_dict()


def test_a_contested_row_is_low_but_still_offers_every_option(tmp_path: Path) -> None:
    """Top two within 15 points is LOW, and low NEVER removes an option.

    Mutation: drop the losing option at low confidence and the operator loses the
    very choice the guided flow exists to ask about.
    """
    product = tmp_path / "Contested"
    write_models(product / "Alpha", 1)
    write_models(product / "Beta", 1)
    facts = facts_for(product)
    options, _reasons, _repo = proposal_engine.propose(facts, (), probe_git=False)
    top, second = options[0], options[1]
    assert top.score - second.score < 15
    assert top.confidence == LOW and second.confidence == LOW
    assert {option.shape for option in options} == {"one-project", "split-top-folders"}
    assert all(option.confidence in {HIGH, MEDIUM, LOW} for option in options)


def test_a_child_document_does_not_make_the_parent_one_project_high(tmp_path: Path) -> None:
    """A child's document backs the CHILD, not the collection; an alternative is capped.

    Mutation: treat any high-confidence metadata anywhere in the subtree as
    documentary backing for `one-project`; the three live collection rows then
    report the split as medium and the merge as high, i.e. the recommendation
    argues against itself. Also reds if an alternative is allowed a higher band
    than the recommendation.
    """
    collection = tmp_path / "Collection"
    for name in ("Alpha", "Beta", "Gamma"):
        write_models(collection / name, 2)
        write(collection / name / "README.txt",
              f"{name} by someone\nLicensed under cc_by\n")
    facts = facts_for(collection)
    options, _reasons, _repo = proposal_engine.propose(facts, (), probe_git=False)
    assert options[0].shape == "split-top-folders"
    assert options[0].confidence == MEDIUM
    rank = {"high": 3, "medium": 2, "low": 1}
    for option in options[1:]:
        assert rank[option.confidence] <= rank[options[0].confidence], option.as_dict()


# --- the provider table cannot drift from the app's ------------------------

def test_the_engine_provider_table_agrees_with_url_utils_source_key() -> None:
    """The engine owns a provider table; it must agree with `url_utils.source_key`.

    Mutation: change one copy's host set or id regex and the app keys a model page
    `thingiverse:1` while the engine offers `thingiverse:01` (or misses it entirely).
    """
    import url_utils

    table = {
        "https://www.thingiverse.com/thing:2738211": ("thingiverse", "2738211"),
        "https://thingiverse.com/things/123456": ("thingiverse", "123456"),
        "https://www.printables.com/model/268795-triple-twister": ("printables", "268795"),
        "https://makerworld.com/models/12345": ("makerworld", "12345"),
        "https://www.makerworld.com/en/models/999": ("makerworld", "999"),
    }
    for url, (provider, subject) in table.items():
        status, data = proposal_engine._classify_url(url)
        assert status == "candidate", (url, status, data)
        assert (data["provider"], data["subject_id"]) == (provider, subject), url
        assert url_utils.source_key(url) == f"{provider}:{subject}", (
            f"the app keys {url} differently from the engine's provider table"
        )
    # MyMiniFactory is in the engine's table but not in `source_key`, which is why
    # the agreement is pinned for the three shared providers only.
    status, data = proposal_engine._classify_url(
        "https://www.myminifactory.com/object/124572"
    )
    assert status == "candidate" and data["provider"] == "myminifactory"
    assert proposal_engine._classify_url(
        "https://www.myminifactory.com/users/Zeta3d"
    ) == (None, None)
