"""What image OCR measured and kept out of search for one image.

OCR fills one `OcrFacts` beside the words it indexes. The job runtime carries it as a
plain mapping, and the sidecar records it, so a reader of the page can see how sure
the engine was and which words it withheld.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class OcrFacts:
    #: Mean word confidence of the chosen pass on Tesseract's 0-100 scale; None when no word was read.
    confidence: float | None = None
    #: The words under the confidence floor, in reading order. No search field reads them.
    low_confidence_text: str = ""

    def as_payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> OcrFacts:
        """Rebuild facts from a job result; a value of the wrong type is a corrupt result."""
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
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
