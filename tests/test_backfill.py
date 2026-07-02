"""backfill-media — sidecar + OCR + CLIP for pre-existing Evidence files (engines stubbed)."""

from __future__ import annotations

import numpy as np
import pytest

from exomem import backfill, embeddings, extract, preserve

REL = "Knowledge Base/Evidence/Old/photos/legacy.jpg"


def _drop_image(vault, rel=REL, data=b"\xff\xd8\xffOLD"):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _quiet(*a, **k):
    pass


def test_ensure_media_sidecar_creates_stub(vault) -> None:
    img = _drop_image(vault)
    sidecar, created = preserve.ensure_media_sidecar(vault, img)
    assert created and sidecar.exists()
    body = sidecar.read_text("utf-8")
    assert "media_type: image" in body
    assert f"evidence_file: {REL}" in body
    assert "extracted_by: none" in body
    # idempotent
    sidecar2, created2 = preserve.ensure_media_sidecar(vault, img)
    assert sidecar2 == sidecar and created2 is False


def test_backfill_dry_run_writes_nothing(vault) -> None:
    img = _drop_image(vault)
    stats = backfill.backfill_media(vault, dry_run=True, log_fn=_quiet)
    assert stats.sidecars_created == 1
    assert not img.with_name(img.name + ".md").exists()  # nothing actually written


def test_backfill_creates_sidecar_ocr_clip(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    img = _drop_image(vault)
    monkeypatch.setattr(
        extract, "extract_text",
        lambda p, media_type=None: extract.ExtractResult(text="legacy invoice acme", media_type="image", engine="tesseract"),
    )
    monkeypatch.setattr(embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32))

    stats = backfill.backfill_media(vault, log_fn=_quiet)
    assert (stats.sidecars_created, stats.extracted, stats.clip_indexed) == (1, 1, 1)
    body = img.with_name(img.name + ".md").read_text("utf-8")
    assert "legacy invoice acme" in body
    assert "extracted_by: tesseract" in body
    assert embeddings.ClipIndex(vault).has(REL)

    # idempotent: a second pass does nothing
    stats2 = backfill.backfill_media(vault, log_fn=_quiet)
    assert (stats2.sidecars_created, stats2.extracted, stats2.clip_indexed) == (0, 0, 0)
    assert stats2.skipped >= 1


FINANCE_REL = "Knowledge Base/Finance/invoices/inv.jpg"


def test_backfill_covers_non_evidence_kb_subtree(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # A binary that lives OUTSIDE Evidence/ (e.g. an invoice in Finance/) must
    # still be picked up — coverage is the whole KB, not just Evidence/.
    p = vault / FINANCE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xd8\xffINV")
    monkeypatch.setattr(
        extract, "extract_text",
        lambda p, media_type=None: extract.ExtractResult(
            text="ugreen nexode 100w charger", media_type="image", engine="tesseract"
        ),
    )
    monkeypatch.setattr(embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32))

    stats = backfill.backfill_media(vault, log_fn=_quiet)
    assert stats.sidecars_created == 1
    assert stats.extracted == 1
    sidecar = p.with_name(p.name + ".md")
    assert sidecar.exists()
    body = sidecar.read_text("utf-8")
    assert "ugreen nexode 100w charger" in body
    assert f"evidence_file: {FINANCE_REL}" in body


def test_backfill_skips_schema_and_index_dirs(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cruft/config dirs must be pruned so the walk never indexes them.
    skipped = vault / "Knowledge Base" / "_Schema" / "_attachments" / "logo.png"
    skipped.parent.mkdir(parents=True, exist_ok=True)
    skipped.write_bytes(b"\xff\xd8\xffLOGO")
    stats = backfill.backfill_media(vault, dry_run=True, log_fn=_quiet)
    assert stats.sidecars_created == 0


def test_backfill_no_ocr_skips_extraction(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _drop_image(vault)
    monkeypatch.setattr(embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32))
    monkeypatch.setattr(
        extract, "extract_text",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OCR ran under --no-ocr")),
    )
    stats = backfill.backfill_media(vault, do_ocr=False, log_fn=_quiet)
    assert (stats.sidecars_created, stats.extracted, stats.clip_indexed) == (1, 0, 1)
