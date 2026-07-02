## MODIFIED Requirements

### Requirement: Scene Frames Are OCR'd Through the Image Path

Scene-frame sidecars SHALL enter the existing pending-extraction flow so each frame is OCR'd by
the existing image path and its on-screen text lands in the sidecar's extracted text, indexed by
BM25 and the text embedding index. Scene frames SHALL NOT receive their own ClipIndex rows — the
parent video's per-scene vectors own visual search — and every CLIP-enqueue point SHALL skip
images whose sidecar carries `parent_media`.

Persisted frames additionally serve as segmentation inputs: their filename timestamps and OCR
text are boundary-event sources for semantic segmentation. When `EXOMEM_SEMANTIC_SEGMENTS` is
set, the media worker SHALL enqueue one re-embed of the parent video's sidecar after the
frame-OCR jobs it created, so segmentation re-runs with visual and OCR events present.

#### Scenario: On-screen text becomes findable

- **WHEN** a scene frame containing legible text (a slide, a stack trace) is OCR'd
- **THEN** that text is stored in the frame's sidecar and is findable via keyword/BM25 search

#### Scenario: No duplicate visual vectors

- **WHEN** the media worker's startup scan or a backfill pass encounters a scene-frame image
- **THEN** it is not queued for CLIP indexing and no ClipIndex row is created for the frame file

#### Scenario: Parent re-embed follows frame OCR

- **WHEN** a gated video's scene frames finish their OCR jobs
- **THEN** exactly one re-embed job for the parent sidecar runs afterwards on the same queue
