"""Attached artifacts enter by intent, and every stored artifact is citable.

Two live reproductions motivated this module. A transcript kept as raw material
reached Evidence because the only lossless file-handle path goes there, and the
artifact received no page at all — no identifier, no `ingested_into`, and no
corpus presence, because the corpus indexes only `.md`. A screenshot reproduced
the routing half and then failed the same second way: `preserve` returns the
artifact path first, the page is named `<filename>.md`, and the citation
resolver replaced the extension instead — so `shot.png` resolved to a
`shot.md` that never existed.

The resolver defect is lane-independent and is covered first, because it is
what emits `source not found, ingested_into back-ref skipped` and it breaks the
provenance loop for every preserved artifact regardless of where it lives.
"""

from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path

from exomem import note as note_module
from exomem import preserve as preserve_module

TODAY = dt.date(2026, 8, 23)

# A one-pixel PNG: real enough to classify as media, small enough to inline.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _preserve_text(vault: Path, filename: str = "2026-08-23-session.txt"):
    return preserve_module.preserve(
        vault,
        scope="riverside",
        category="transcripts",
        filename=filename,
        content="Speaker A: the pier reopened in March.",
        today=TODAY,
    )


def _preserve_image(vault: Path, filename: str = "2026-08-23-shot.png"):
    return preserve_module.preserve(
        vault,
        scope="riverside",
        category="screenshots",
        filename=filename,
        content_base64=base64.b64encode(_PNG).decode(),
        today=TODAY,
    )


def _cite(vault: Path, source: str, title: str):
    return note_module.note(
        vault,
        content="## Finding\n\nThe pier reopening explains the traffic change.\n",
        note_type="insight",
        title=title,
        status="draft",
        sources=[source],
        today=TODAY,
    )


def _backref_skipped(warnings: list[str]) -> str | None:
    return next((w for w in warnings if "ingested_into back-ref skipped" in w), None)


def _ingested_into(vault: Path, page_rel: str) -> str:
    return (vault / page_rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 1. Citation resolution
# --------------------------------------------------------------------------


def test_citing_a_stored_media_artifact_lands_the_back_reference(vault: Path) -> None:
    """The reported failure, at its narrowest: the page exists and is missed.

    An image artifact does get a page today. `preserve` returns the artifact
    path first and only the page is citable, with nothing in the response
    saying so — so the natural citation is the one that cannot resolve.
    """
    stored = _preserve_image(vault)
    assert stored.sidecar_path is not None

    result = _cite(vault, stored.path, "Traffic changed after the pier reopened")

    assert _backref_skipped(result.warnings) is None, result.warnings
    assert "traffic-changed" in _ingested_into(vault, stored.sidecar_path)


def test_citing_a_stored_text_artifact_lands_the_back_reference(vault: Path) -> None:
    """The other half: a text artifact must have a page to receive the ref."""
    stored = _preserve_text(vault)
    assert stored.sidecar_path is not None, "a stored artifact must be addressable"

    result = _cite(vault, stored.path, "Pier reopening changed the traffic")

    assert _backref_skipped(result.warnings) is None, result.warnings
    assert "pier-reopening" in _ingested_into(vault, stored.sidecar_path)


def test_citing_the_page_directly_resolves_to_the_same_page(vault: Path) -> None:
    stored = _preserve_image(vault)

    result = _cite(vault, stored.sidecar_path, "Cited by page path")

    assert _backref_skipped(result.warnings) is None, result.warnings
    assert "cited-by-page-path" in _ingested_into(vault, stored.sidecar_path)


def test_an_ordinary_source_page_citation_is_unchanged(
    vault: Path, source_schema
) -> None:
    """Every citation that resolves today must keep resolving.

    The new lookups are additive and ordered ahead of extension replacement, so
    this pins that the fallback survives rather than being displaced.
    """
    from exomem import add as add_module

    added = add_module.add(
        vault,
        source_schema,
        content="Notes from the riverside council minutes.",
        title="Riverside council minutes",
        source_type="other",
        today=TODAY,
    )
    cited = added.path.removesuffix(".md")

    result = _cite(vault, cited, "Compiled from an ordinary source")

    assert _backref_skipped(result.warnings) is None, result.warnings
    assert "compiled-from-an-ordinary-source" in _ingested_into(vault, added.path)


def test_the_artifact_page_form_is_tried_before_extension_replacement(vault: Path) -> None:
    """Order is the property, not merely reachability.

    Both candidate names can exist at once: `shot.png.md` is the artifact's own
    page and `shot.md` is an unrelated page that happens to share the stem.
    Extension replacement finds the wrong one, so resolving to the artifact's
    page is what pins the ordering. A reordering that puts extension
    replacement first fails here and nowhere else.
    """
    stored = _preserve_image(vault)
    decoy = (vault / stored.path).with_suffix(".md")
    decoy.write_text(
        "---\ntype: source\ntitle: Unrelated stem twin\ningested_into: []\n---\n\n# Twin\n",
        encoding="utf-8",
    )

    resolved = note_module._resolve_source_path(vault, stored.path)

    assert resolved is not None
    assert resolved.name == Path(stored.sidecar_path).name


# --------------------------------------------------------------------------
# 2. Every ingested artifact is addressable
# --------------------------------------------------------------------------


def test_an_artifact_whose_type_is_not_extractable_still_gets_a_page(
    vault: Path,
) -> None:
    """Addressability must not be decided by an extraction-capability test.

    Whether a page was written used to depend on whether the extension happened
    to be extractable media and on which input mode the caller used. A `.txt`
    streamed in received a page because text is extractable; the same `.txt`
    supplied as content did not, because the stub condition also required the
    bytes to be absent; and a `.csv` received one on neither path. None of that
    has anything to do with whether the artifact can be cited.
    """
    stored = preserve_module.preserve(
        vault,
        scope="riverside",
        category="exports",
        filename="2026-08-23-counts.csv",
        content="segment,count\npier,412\n",
        today=TODAY,
    )

    assert stored.sidecar_path is not None
    page = (vault / stored.sidecar_path).read_text(encoding="utf-8")
    assert "type: source" in page
    assert "ingested_into: []" in page
    assert f"binary_sha256: {stored.hash}" in page
    assert "## Artifact" in page


def test_a_pending_media_stub_is_left_to_reconciliation(vault: Path) -> None:
    """The stub's fields are reconciliation's to write, not this path's.

    Media reconciliation re-renders a pending stub with provenance computed
    from the binary itself and decides, by comparing those fields, whether the
    stub is already current. Writing them here first would move that decision.
    """
    stored = _preserve_image(vault)

    page = (vault / stored.sidecar_path).read_text(encoding="utf-8")
    assert "extracted_by: pending" in page
    assert "binary_sha256:" not in page
    assert "## Extracted text" in page


def test_a_page_never_inlines_the_artifact_bytes(vault: Path) -> None:
    stored = _preserve_text(vault)

    page = (vault / stored.sidecar_path).read_text(encoding="utf-8")
    assert "the pier reopened in March" not in page
    assert Path(stored.path).name in page


# --------------------------------------------------------------------------
# 3. Page ownership is separated from the media pipeline
# --------------------------------------------------------------------------


_SOURCE_MEDIA_PAGE = """---
type: source
exomem_id: 11111111-1111-4111-8111-111111111111
title: Riverside council walkthrough
source_type: session
domain: urban-planning
projects: [riverside]
captured: 2026-08-23
media_type: image
evidence_file: Knowledge Base/Sources/Sessions/2026-08-23-walkthrough.png
extracted_by: pending
tags: [session, riverside]
ingested_into: []
---

# Riverside council walkthrough

> Captured during the pier reopening walkthrough.

## Capture

- Original filename: `walkthrough.png`
"""


def _source_media_page(vault: Path) -> tuple[Path, Path]:
    folder = vault / "Knowledge Base" / "Sources" / "Sessions"
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / "2026-08-23-walkthrough.png"
    binary.write_bytes(_PNG)
    page = folder / "2026-08-23-walkthrough.png.md"
    page.write_text(_SOURCE_MEDIA_PAGE, encoding="utf-8")
    return binary, page


def test_reconciliation_keeps_what_the_capture_owns(vault: Path) -> None:
    """The pipeline fills its own fields; it does not re-author the page.

    `reconcile_media` re-renders any media page that is not in its canonical
    pending shape, and that shape required `source_type: other` — which no real
    Source has. Measured before this change, a Source page for an image lost its
    title, kind, domain, projects and tags, and had its body demoted under
    `## Preserved notes`, while the locator line was rewritten to
    `Evidence/evidence/uncategorized/` — a path that does not exist.
    """
    from exomem import media_processing

    binary, page = _source_media_page(vault)

    media_processing.reconcile_media(vault, binary, explicit=True)

    after = page.read_text(encoding="utf-8")
    assert "title: Riverside council walkthrough" in after
    assert "source_type: session" in after
    assert "domain: urban-planning" in after
    assert "projects: [riverside]" in after
    assert "tags: [session, riverside]" in after
    assert "> Captured during the pier reopening walkthrough." in after
    assert "## Preserved notes" not in after
    assert "Evidence: 2026-08-23-walkthrough.png" not in after
    assert "Evidence/evidence/uncategorized" not in after


def test_reconciliation_still_fills_the_fields_it_owns(vault: Path) -> None:
    """Keeping the capture's fields must not cost the pipeline its own."""
    from exomem import media_processing

    binary, page = _source_media_page(vault)

    media_processing.reconcile_media(vault, binary, explicit=True)

    after = page.read_text(encoding="utf-8")
    assert "processing_state: pending" in after
    assert "original_filename: 2026-08-23-walkthrough.png" in after
    assert "binary_sha256:" in after
    assert "binary_size:" in after
    assert "binary_mtime_ns:" in after


def test_the_evidence_stub_path_is_unchanged(vault: Path) -> None:
    """A page already in the canonical shape converges exactly as before.

    The Evidence sidecar is the shape the checks were written around, so it is
    the control: broadening them must not change what happens to it.
    """
    from exomem import media_processing

    stored = _preserve_image(vault)
    page = vault / stored.sidecar_path

    media_processing.reconcile_media(vault, vault / stored.path, explicit=True)

    after = page.read_text(encoding="utf-8")
    assert "source_type: other" in after
    assert 'title: "Evidence: 2026-08-23-shot.png"' in after
    assert "tags: [evidence, riverside, screenshots]" in after
    assert "processing_state: pending" in after
    assert "## Preserved notes" not in after


def test_a_backfilled_sources_binary_names_its_own_tree(vault: Path) -> None:
    """Scope and category are folders, not a search for the word `evidence`.

    Both derivations located a literal `evidence` segment and defaulted when
    there was none, so a binary under `Sources/` was described as living in
    `Evidence/evidence/uncategorized/` — a locator line and a tag set naming a
    folder that does not exist.
    """
    from exomem import preserve as preserve_mod

    folder = vault / "Knowledge Base" / "Sources" / "Sessions"
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / "2026-08-23-orphan.png"
    binary.write_bytes(_PNG)

    sidecar, created = preserve_mod.ensure_media_sidecar(vault, binary)

    assert created is True
    page = sidecar.read_text(encoding="utf-8")
    assert "Preserved under `Sources/Sessions/uncategorized/`." in page
    assert "tags: [source, sessions, uncategorized]" in page
    assert "Evidence" not in page


def test_an_evidence_binary_still_names_evidence(vault: Path) -> None:
    from exomem import preserve as preserve_mod

    folder = vault / "Knowledge Base" / "Evidence" / "riverside" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / "2026-08-23-orphan.png"
    binary.write_bytes(_PNG)

    sidecar, created = preserve_mod.ensure_media_sidecar(vault, binary)

    assert created is True
    page = sidecar.read_text(encoding="utf-8")
    assert "Preserved under `Evidence/riverside/screenshots/`." in page
    assert "tags: [evidence, riverside, screenshots]" in page
