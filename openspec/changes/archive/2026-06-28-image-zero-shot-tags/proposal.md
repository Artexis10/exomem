## Why

Image search today rests on three thin signals: Tesseract OCR (text that is literally on the
image), an optional one-line BLIP caption (`KB_MCP_VISION_CAPTION`, default-off), and the CLIP
visual embedding (`find` matches a text query against the image vector). A photo with no
on-image text and captioning off contributes nothing to BM25 or the bge text index — it is only
findable via the CLIP vector, which the user must trigger with a visual-style query. The
high-value, cheap enrichment we are NOT yet extracting: *what the image depicts* as plain words
("invoice", "whiteboard", "screenshot", "receipt", "chart") that land in the normal text index.

The CLIP model is already loaded and already encodes BOTH images and text in one shared space.
Scoring an image's CLIP embedding against a fixed tag vocabulary by cosine is a free, frozen
MEASUREMENT — no new model, no new dependency. This change turns that into per-image tags
appended to the indexed text, so a depicted concept becomes BM25-findable and bge-embedded.

It stays **pure-substrate**: CLIP zero-shot cosine is deterministic transduction (the same class
as OCR/CLIP/bge), not a reasoning LLM. The user has signed off this class of enrichment —
"richer per-image measurement is in-bounds; cross-image inference stays Claude's."

## What Changes

- **Zero-shot image tags from the already-loaded CLIP model.** A fixed, generic visual-concept
  vocabulary (~190 terms: document/screen kinds, objects, scenes) is embedded ONCE with CLIP's
  TEXT encoder and cached. For each image, cosine its CLIP image embedding against the vocab and
  take the **top-K tags above a threshold**.
- **Tags append to the image's extracted text at the extraction seam** (`extract._ocr_image`):
  a single `Tags: invoice, table, screenshot` line is appended after OCR (and after any caption),
  so it flows through the existing sidecar → BM25 + bge indexing path. No `find` changes, no new
  index, no schema migration.
- **Reuse, not duplicate:** the existing CLIP model singleton and its device logic (the
  whisper-cuDNN-shadow CPU fallback in `embeddings._clip_device()`) are reused as-is. The only new
  embeddings surface is a batched `embed_clip_texts()` text encoder beside `embed_clip_text()`.
- **Env-gated, default-OFF, soft-fail.** `KB_MCP_IMAGE_TAGS` is unset by default ⇒ extraction
  output is byte-identical. `KB_MCP_IMAGE_TAGS_TOPK` (default 5) and `KB_MCP_IMAGE_TAGS_THRESHOLD`
  (default 0.22 raw cosine) tune it. Any failure (CLIP/Pillow/model absent, unreadable image,
  encode error) yields no tags and unchanged output — extraction never raises.
- **No new dependency.** Tagging needs only the existing `embeddings` extra (sentence-transformers
  + Pillow) that already powers CLIP; a box without it soft-fails to no tags.

Out of scope (future changes): tagging video keyframes; a structured `tags:` frontmatter field or
a `tag:` filter on `find`; per-vault custom vocabularies; prompt-templated vocab ("a photo of X");
re-tagging already-indexed images (a `backfill-media` pass would carry it — not wired here).

## Capabilities

### New Capabilities
- `image-tags`: zero-shot CLIP tags enrich an image's indexed text with the concepts it depicts —
  default-off, soft-fail, pure-substrate (frozen cosine measurement, no LLM, no new dependency).

## Impact

- Code: `src/kb_mcp/image_tags.py` (new — vocabulary + cached vocab matrix + `compute_tags`);
  `src/kb_mcp/extract.py` (`_maybe_image_tags` seam in `_ocr_image`); `src/kb_mcp/embeddings.py`
  (new batched `embed_clip_texts`). `find` / sidecar / index code is untouched.
- Deps: none added — reuses the `embeddings` extra (CLIP via sentence-transformers + Pillow).
- Default-off ⇒ zero behavior change when `KB_MCP_IMAGE_TAGS` is unset — image extraction output
  is byte-identical to today.
- GPU: tagging runs on the existing CLIP model and inherits `embeddings._clip_device()` (CPU when
  ASR is active, dodging the whisper cuDNN shadow); a tiny ViT-B/32 text+image encode off the
  request path in the async media worker.
- Docs: `KB_MCP_IMAGE_TAGS*` env vars in the README env table; brief note in `docs/deployment.md`.
  (SKILL/scaffold media-doc copy handled separately — scaffold left untouched, leak-guarded.)
