"""Camera raw reaches both image consumers through LibRaw."""

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
    from PIL import Image

    source = SAMPLES / "ocr-eng.dng"
    seen = []

    def ocr(decoded):
        with Image.open(decoded) as image:
            seen.append(image.size)
        return OcrReading("", ())

    class Encoder:
        def encode(self, image, **_kwargs):
            seen.append(image.size)
            return np.ones(embeddings.CLIP_DIM, dtype=np.float32)

    monkeypatch.setattr(extract, "_ocr_png", ocr)
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: Encoder())
    extract.extract_text(source)
    assert embeddings.embed_image(source).shape == (embeddings.CLIP_DIM,)
    # The stored sensor rows are portrait; both consumers get the upright landscape image.
    assert seen == [(1068, 280), (1068, 280)]


def test_a_portrait_raw_decodes_upright_not_turned():
    """Defect: the camera orientation is ignored or applied the wrong way, so text reads sideways or upside down."""
    from PIL import Image, ImageChops, ImageStat

    from exomem import image_codecs

    with Image.open(SAMPLES / "ocr-eng.png") as drawn:
        upright = drawn.convert("L")
    with image_codecs.open_image(SAMPLES / "ocr-eng.dng") as decoded:
        grey = decoded.convert("L")

    def difference(candidate):
        return ImageStat.Stat(ImageChops.difference(grey, candidate)).mean[0]

    assert difference(upright) < difference(upright.rotate(180)) / 4


def test_missing_raw_decoder_is_actionable_for_both_consumers(monkeypatch):
    monkeypatch.setitem(sys.modules, "rawpy", None)
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: pytest.fail("decoder absence must precede model load"))
    source = SAMPLES / "ocr-eng.dng"
    with pytest.raises(extract.ExtractionUnavailable, match="image-codecs"):
        extract.extract_text(source)
    with pytest.raises(embeddings.ClipUnavailable, match="image-codecs"):
        embeddings.embed_image(source)
