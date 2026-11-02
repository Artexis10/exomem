"""HEIC reaches both image consumers through the maintained pixel decoder."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from exomem import embeddings, extract
from exomem.ocr_models import OcrReading

SAMPLES = Path(__file__).parent / "fixtures" / "media-samples"


def test_real_heic_decodes_at_ocr_and_image_embedding_boundaries(monkeypatch):
    pytest.importorskip("pillow_heif")
    from PIL import Image

    from exomem import image_codecs

    source = SAMPLES / "ocr-eng.heic"
    with image_codecs.open_image(source) as image:
        assert image.format == "HEIF"
        image.load()
        size = image.size
    phrase = json.loads((SAMPLES / "expected.json").read_text())[source.name]
    ocr_calls = []

    def ocr(decoded):
        with Image.open(decoded) as image:
            image.load()
            assert image.format == "PNG"
            assert image.size == size
            assert image.getextrema()[0][0] < image.getextrema()[0][1]
        ocr_calls.append(True)
        return OcrReading(phrase, ())

    class Encoder:
        def encode(self, image, **_kwargs):
            assert image.mode == "RGB"
            assert image.size == size
            return np.ones(embeddings.CLIP_DIM, dtype=np.float32)

    monkeypatch.setattr(extract, "_ocr_png", ocr)
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: Encoder())
    assert phrase in extract.extract_text(source).text
    assert embeddings.embed_image(source).shape == (embeddings.CLIP_DIM,)
    assert ocr_calls == [True]


def test_missing_heic_decoder_is_actionable_for_both_consumers(monkeypatch):
    monkeypatch.setitem(sys.modules, "pillow_heif", None)
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: pytest.fail("decoder absence must precede model load"))
    source = SAMPLES / "ocr-eng.heic"
    with pytest.raises(extract.ExtractionUnavailable, match="image-codecs"):
        extract.extract_text(source)
    with pytest.raises(embeddings.ClipUnavailable, match="image-codecs"):
        embeddings.embed_image(source)
