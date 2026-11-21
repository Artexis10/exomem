"""What image OCR measured and kept out of search for one image.

OCR fills one `OcrFacts` beside the words it indexes. The job runtime carries
confidence and withheld text in its existing mapping. The engine label carries
text availability through both the job result and the sidecar.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_EXTRACTION_ENGINE_BYTES = 256
# The version and boolean tokens belong to the extraction provenance format.
# An old image label starts with tesseract, even with a configured caption basename.
_OCR_TEXT_PREFIX = "ocr-v1:"


@dataclass(frozen=True)
class OcrFacts:
    #: Mean word confidence of the chosen pass on Tesseract's 0-100 scale; None when no word was read.
    confidence: float | None = None
    #: The words under the confidence floor, in reading order. No search field reads them.
    low_confidence_text: str = ""
    #: Whether OCR retained any text before enrichment. None when unmeasured.
    text_available: bool | None = None

    def as_payload(self) -> dict[str, object]:
        return {"confidence": self.confidence, "low_confidence_text": self.low_confidence_text}

    @classmethod
    def from_payload(cls, value: object) -> OcrFacts:
        """Rebuild facts from a job result; a value of the wrong type is a corrupt result."""
        # The older runtime reads the same closed payload schema and verifies its hash.
        if not isinstance(value, dict) or value.keys() - {"confidence", "low_confidence_text"}:
            raise ValueError("OCR facts must be a mapping of the known fields")
        confidence = value.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool) or not isinstance(confidence, int | float)
        ):
            raise ValueError("OCR fact 'confidence' must be a number")
        text = value.get("low_confidence_text", "")
        if not isinstance(text, str):
            raise ValueError("OCR fact 'low_confidence_text' must be text")
        return cls(confidence=None if confidence is None else float(confidence), low_confidence_text=text)


def _is_image_ocr_engine(engine: str) -> bool:
    return engine.partition("+")[0] == "tesseract"


def engine_with_ocr_text(engine: str, ocr: OcrFacts | None) -> str:
    """Mark a measured OCR result while keeping the engine within its existing byte limit."""
    if ocr is None or ocr.text_available is None or not _is_image_ocr_engine(engine):
        return engine
    marked = f"{_OCR_TEXT_PREFIX}{int(ocr.text_available)}:{engine}"
    return marked if len(marked.encode("utf-8")) <= MAX_EXTRACTION_ENGINE_BYTES else engine


def ocr_text_available(engine: object) -> bool | None:
    """Read the measurement from a released extraction engine label."""
    if not isinstance(engine, str) or not engine.startswith(_OCR_TEXT_PREFIX):
        return None
    measured, _, original = engine.removeprefix(_OCR_TEXT_PREFIX).partition(":")
    if not _is_image_ocr_engine(original):
        return None
    return {"1": True, "0": False}.get(measured)
