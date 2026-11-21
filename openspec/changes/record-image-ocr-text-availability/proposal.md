## Why

A completed image extraction can contain only tags or a caption. The page reader needs a direct way to tell whether OCR supplied any text.

## What Changes

- Return `ocr_text_available` on a page read as a boolean measured before captions and tags are appended.
- Omit the field when no measurement exists, including in existing sidecars.
- Keep the field with its extraction when reprocessing supersedes it.

## Capabilities

### Modified Capabilities

- `automatic-media-processing` reports whether image OCR supplied retained text through the existing page read.

## Impact

The image worker records the measurement in its existing durable engine label. The page reader derives the boolean from the released engine label.
