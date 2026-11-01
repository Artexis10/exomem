## Why

A plain question can return screenshots and video frames ahead of the notes that answer it. A read-only measurement on 24 real questions found three causes:

- Tesseract OCR text is indexed whole. Tesseract's per-word confidence is computed and then dropped, so misread words from photos and dense screenshots enter every text lane.
- The CLIP picture lane gives rank to its top images for every query, even at noise-level similarity. Only media can receive that rank, so text questions collect unrelated images.
- Any query that starts with "what", "which", "who" or "whose" is ranked as an exact lookup. That up-weights word overlap and favours long OCR and transcript bodies over the notes that answer the question.

## What Changes

- Image OCR keeps the words Tesseract reads with confidence at or above a floor in `## Extracted text`. The other words move to a `## Low-confidence OCR` section of the same sidecar, and every search field omits that section. The sidecar records the mean word confidence as `ocr_confidence`. The floor is a setting with a default of 70 on Tesseract's 0-100 scale.
- The CLIP lane admits an image only when its similarity to the query clears the chance level of the whole image population. The lane reuses the median-and-MAD band that the working set already uses for vectors, so no cosine value is fixed in code. A population too small to calibrate keeps today's behaviour.
- Intent routing no longer reads leading question words. Quotes and wikilinks still mark an exact lookup; temporal and relationship routing are unchanged.

Out of scope: re-extracting existing sidecars (an explicit `reprocess` per item, run as a separate operation), PDF page OCR, video transcripts, and the recall source lane (`surface-captured-sources`).

## Capabilities

### New Capabilities

- `search-lane-relevance`: the CLIP lane admits only matches above chance, and question words do not choose the ranking intent.

### Modified Capabilities

- `automatic-media-processing`: image OCR keeps confident words searchable and keeps the rest in the sidecar outside the search fields.

## Impact

- Code: `src/exomem/ocr_models.py`, `src/exomem/extract.py`, `src/exomem/preserve.py`, `src/exomem/find_types.py`, `src/exomem/clip_index.py`, `src/exomem/find_candidates.py`, `src/exomem/find_policy.py`, `src/exomem/ranking_config.py`.
- Behaviour: newly extracted image and video-frame sidecars carry `ocr_confidence` and may carry a `## Low-confidence OCR` section. Existing sidecars keep their text until they are reprocessed. Text questions see fewer unrelated images.
- Undo: set the OCR floor to 0, which keeps every word searchable; set the CLIP band alpha to 1, which admits every CLIP hit; revert the intent change on its own.
