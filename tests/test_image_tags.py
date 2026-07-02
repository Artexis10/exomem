"""CLIP zero-shot image tagging — vocab scoring + the extraction-seam append (model stubbed).

Mirrors the caption tests' style: the CLIP model is never loaded. `embeddings.embed_image` and
`embeddings.embed_clip_texts` are patched to return controlled vectors, so scoring/threshold/
top-K/soft-fail are exercised deterministically. Default-OFF ⇒ extraction output unchanged.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from exomem import embeddings, extract, image_tags


def _unit(i: int) -> np.ndarray:
    v = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    v[i] = 1.0
    return v


# A tiny stand-in vocabulary; rows of the patched matrix are the matching basis vectors so
# `matrix @ image_vec` yields exactly the image_vec's leading components as cosine scores.
_FAKE_VOCAB = ("invoice", "table", "screenshot", "dog")
_FAKE_MATRIX = np.stack([_unit(i) for i in range(len(_FAKE_VOCAB))]).astype(np.float32)


def _patch_clip(monkeypatch: pytest.MonkeyPatch, image_vec: np.ndarray) -> None:
    """Stub the CLIP seams: fixed vocab + matrix, a controlled image vector, fresh cache."""
    monkeypatch.setattr(image_tags, "TAG_VOCAB", _FAKE_VOCAB)
    monkeypatch.setattr(image_tags, "_VOCAB_MATRIX", None)
    monkeypatch.setattr(embeddings, "embed_clip_texts", lambda texts: _FAKE_MATRIX)
    monkeypatch.setattr(embeddings, "embed_image", lambda p: image_vec)


def _img_vec(invoice: float, table: float, screenshot: float, dog: float) -> np.ndarray:
    v = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    v[0], v[1], v[2], v[3] = invoice, table, screenshot, dog
    return v


# ---- gate -------------------------------------------------------------------


def test_image_tags_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_IMAGE_TAGS", raising=False)
    assert image_tags.image_tags_enabled() is False
    monkeypatch.setenv("EXOMEM_IMAGE_TAGS", "1")
    assert image_tags.image_tags_enabled() is True


# ---- scoring: threshold + top-K + ordering ----------------------------------


def test_compute_tags_threshold_and_descending_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_IMAGE_TAGS_TOPK", raising=False)
    monkeypatch.delenv("EXOMEM_IMAGE_TAGS_THRESHOLD", raising=False)
    # Scores: invoice=0.9, table=0.5, screenshot=0.1, dog=0.0. Default threshold 0.22 drops
    # screenshot+dog; result is sorted by descending cosine.
    _patch_clip(monkeypatch, _img_vec(0.9, 0.5, 0.1, 0.0))
    assert image_tags.compute_tags(Path("x.png")) == ["invoice", "table"]


def test_compute_tags_top_k_caps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_IMAGE_TAGS_TOPK", "1")
    monkeypatch.delenv("EXOMEM_IMAGE_TAGS_THRESHOLD", raising=False)
    _patch_clip(monkeypatch, _img_vec(0.9, 0.5, 0.3, 0.25))
    assert image_tags.compute_tags(Path("x.png")) == ["invoice"]  # only the single best


def test_compute_tags_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_IMAGE_TAGS_TOPK", raising=False)
    monkeypatch.setenv("EXOMEM_IMAGE_TAGS_THRESHOLD", "0.05")  # lets screenshot (0.1) through
    _patch_clip(monkeypatch, _img_vec(0.9, 0.5, 0.1, 0.0))
    assert image_tags.compute_tags(Path("x.png")) == ["invoice", "table", "screenshot"]


def test_compute_tags_none_above_threshold_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_IMAGE_TAGS_THRESHOLD", raising=False)
    _patch_clip(monkeypatch, _img_vec(0.1, 0.05, 0.0, 0.0))  # all below 0.22
    assert image_tags.compute_tags(Path("x.png")) == []


# ---- soft-fail --------------------------------------------------------------


def test_compute_tags_soft_fails_when_clip_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_tags, "_VOCAB_MATRIX", None)

    def _no_clip(_p):
        raise embeddings.ClipUnavailable("sentence-transformers not installed")

    monkeypatch.setattr(embeddings, "embed_image", _no_clip)
    assert image_tags.compute_tags(Path("x.png")) == []


def test_compute_tags_soft_fails_on_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_tags, "TAG_VOCAB", _FAKE_VOCAB)
    monkeypatch.setattr(image_tags, "_VOCAB_MATRIX", None)
    monkeypatch.setattr(embeddings, "embed_image", lambda p: _img_vec(0.9, 0.0, 0.0, 0.0))

    def _boom(_texts):
        raise RuntimeError("vocab encode blew up")

    monkeypatch.setattr(embeddings, "embed_clip_texts", _boom)
    assert image_tags.compute_tags(Path("x.png")) == []  # error swallowed → no tags


# ---- format -----------------------------------------------------------------


def test_format_tags_line() -> None:
    assert image_tags.format_tags_line(["invoice", "table"]) == "Tags: invoice, table"
    assert image_tags.format_tags_line([]) == ""


# ---- extraction seam: _maybe_image_tags -------------------------------------


def test_maybe_image_tags_unchanged_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_IMAGE_TAGS", raising=False)
    called: list = []
    monkeypatch.setattr(image_tags, "compute_tags", lambda p: called.append(p) or ["invoice"])
    text, engine = extract._maybe_image_tags("ocr body", Path("x.png"), "tesseract")
    assert text == "ocr body"
    assert engine == "tesseract"
    assert called == []  # flag off → CLIP path is never invoked (byte-identical output)


def test_maybe_image_tags_appends_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_IMAGE_TAGS", "1")
    monkeypatch.setattr(image_tags, "compute_tags", lambda p: ["invoice", "table", "screenshot"])
    text, engine = extract._maybe_image_tags("INVOICE 7731", Path("x.png"), "tesseract")
    assert text == "INVOICE 7731\n\nTags: invoice, table, screenshot"
    assert engine == "tesseract+tags"


def test_maybe_image_tags_empty_ocr_returns_tags_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_IMAGE_TAGS", "1")
    monkeypatch.setattr(image_tags, "compute_tags", lambda p: ["beach", "ocean"])
    text, engine = extract._maybe_image_tags("", Path("x.png"), "tesseract")
    assert text == "Tags: beach, ocean"
    assert engine == "tesseract+tags"


def test_maybe_image_tags_preserves_caption_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tags stack on top of a caption: engine becomes `<caption-engine>+tags`."""
    monkeypatch.setenv("EXOMEM_IMAGE_TAGS", "1")
    monkeypatch.setattr(image_tags, "compute_tags", lambda p: ["whiteboard"])
    text, engine = extract._maybe_image_tags("a meeting room", Path("x.png"), "tesseract+blip-large")
    assert text == "a meeting room\n\nTags: whiteboard"
    assert engine == "tesseract+blip-large+tags"


def test_maybe_image_tags_unchanged_when_no_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_IMAGE_TAGS", "1")
    monkeypatch.setattr(image_tags, "compute_tags", lambda p: [])  # soft-fail / nothing clears
    text, engine = extract._maybe_image_tags("ocr body", Path("x.png"), "tesseract")
    assert text == "ocr body"
    assert engine == "tesseract"


# ---- vocabulary hygiene -----------------------------------------------------


def test_vocab_is_generic_unique_and_sized() -> None:
    vocab = image_tags.TAG_VOCAB
    assert len(vocab) >= 150  # a useful zero-shot surface
    assert len(set(vocab)) == len(vocab)  # no duplicate rows
    assert all(t == t.lower() and t.strip() == t and t for t in vocab)  # clean lowercase concepts
