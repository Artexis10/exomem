## Decisions

Measure text availability at the output of the existing confidence filter. The field describes what OCR retained before the extractor adds enrichment.

Encode a measured value in the existing engine label at job packaging or direct sidecar rendering. Use `ocr-v1:1:` for true and `ocr-v1:0:` for false before the Tesseract label. Legacy Tesseract labels start with `tesseract`, so a configured caption basename cannot imitate the prefix.

The page reader derives `ocr_text_available` from the released `extracted_by` label. It returns the boolean alongside the page fields and keeps raw content and its hash tied to the stored bytes.

## Compatibility

Leave the engine label unmarked when the measurement is unknown or the marker would exceed the existing engine byte limit. An unmarked label has no availability field on a read.

The nested OCR object keeps its existing fields and canonical serialization. The prior runtime accepts the marked engine as an ordinary label and can publish a queued result. The current runtime accepts unmarked results staged by the prior runtime.

## Rollout and rollback

Use the existing release and rollback procedures. Both runtime versions can consume in-flight results, and committed sidecar bytes remain readable across the change.

Reprocessing atomically replaces the current engine label and archives the earlier label with its extraction. A reprocess on the prior runtime therefore clears the current measurement and preserves the archived measurement for a later release that can read it.
