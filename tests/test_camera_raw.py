"""Camera raw reaches both image consumers through LibRaw."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from exomem import embeddings, extract
from exomem.ocr_models import OcrReading

pytestmark = pytest.mark.frame_codecs
SAMPLES = Path(__file__).parent / "fixtures" / "media-samples"


def test_a_camera_raw_is_read_and_visually_indexed_upright(monkeypatch):
    """Defect: a DNG gets no OCR text and no visual vector, so search never finds it."""
    pytest.importorskip("rawpy")
    from PIL import Image

    source = SAMPLES / "ocr-eng.dng"
    phrase = json.loads((SAMPLES / "expected.json").read_text())[source.name]
    seen = []

    def ocr(decoded):
        with Image.open(decoded) as image:
            seen.append(image.size)
        return OcrReading(phrase, ())

    class Encoder:
        def encode(self, image, **_kwargs):
            seen.append(image.size)
            return np.ones(embeddings.CLIP_DIM, dtype=np.float32)

    monkeypatch.setattr(extract, "_ocr_png", ocr)
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: Encoder())
    assert phrase in extract.extract_text(source).text
    assert embeddings.embed_image(source).shape == (embeddings.CLIP_DIM,)
    # The stored sensor rows are portrait; both consumers get the upright landscape image.
    assert seen == [(1068, 280), (1068, 280)]


def test_missing_raw_decoder_is_actionable_for_both_consumers(monkeypatch):
    monkeypatch.setitem(sys.modules, "rawpy", None)
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: pytest.fail("decoder absence must precede model load"))
    source = SAMPLES / "ocr-eng.dng"
    with pytest.raises(extract.ExtractionUnavailable, match="image-codecs"):
        extract.extract_text(source)
    with pytest.raises(embeddings.ClipUnavailable, match="image-codecs"):
        embeddings.embed_image(source)
