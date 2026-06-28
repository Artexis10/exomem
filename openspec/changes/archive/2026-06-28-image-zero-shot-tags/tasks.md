# Tasks — Zero-shot image tags

## 1. CLIP text-encode seam (reuse, no new dep)
- [x] 1.1 Add `embeddings.embed_clip_texts(texts) -> np.ndarray (N, 512)`: batched CLIP text
      encoder beside `embed_clip_text`, L2-normalized, raises `ClipUnavailable` when CLIP missing.

## 2. Tag vocabulary + scoring (TDD-able, model stubbed)
- [x] 2.1 Add `src/kb_mcp/image_tags.py`: fixed generic `TAG_VOCAB` (~190 brand-free concepts),
      cached `_vocab_matrix()` (vocab embedded once via `embed_clip_texts`), `compute_tags(path)`
      (cosine image-vs-vocab → top-K above threshold, descending score, soft-fail → `[]`),
      `image_tags_enabled()`, `_top_k()`/`_threshold()` env knobs, `format_tags_line()`.
- [x] 2.2 Tests `tests/test_image_tags.py` (CLIP patched — no real model): threshold + top-K +
      ordering, env overrides + unparseable fallback, soft-fail (ClipUnavailable + unexpected
      error) → `[]`, vocab generic/unique/sized.

## 3. Wire tags into image extraction (default-off seam)
- [x] 3.1 In `extract._ocr_image`: after `_maybe_caption`, call new `_maybe_image_tags(text, path,
      engine)` — gated on `KB_MCP_IMAGE_TAGS`; appends `Tags: a, b, c` to the text and `+tags` to
      the engine. Flag off OR no tags → unchanged `(text, engine)`, byte-identical output.
- [x] 3.2 Tests in `tests/test_image_tags.py` for the seam: flag-off unchanged (compute never
      called), appended when enabled, empty-OCR → tags-only line, caption-engine preserved
      (`…+tags`), no-tags → unchanged.

## 4. Docs (no dep change — reuses the `embeddings` extra)
- [x] 4.1 Add `KB_MCP_IMAGE_TAGS` / `KB_MCP_IMAGE_TAGS_TOPK` / `KB_MCP_IMAGE_TAGS_THRESHOLD` rows
      to the README env table; brief note in `docs/deployment.md`. (Scaffold/SKILL untouched —
      leak-guarded; handled separately.)

## 5. Verify
- [x] 5.1 `PYTHONPATH=src KB_MCP_DISABLE_EMBEDDINGS=1 python -m pytest -q` green (full suite, no
      regression).
- [x] 5.2 `ruff check .` clean (no new findings).
- [x] 5.3 `openspec validate image-zero-shot-tags --strict` passes.
- [ ] 5.4 Desk-side smoke (GPU box, real CLIP): enable `KB_MCP_IMAGE_TAGS`, extract a few real
      images (an invoice scan, a whiteboard photo, a UI screenshot), confirm sensible `Tags:` lines
      and that `find` surfaces them by the tag words; tune `KB_MCP_IMAGE_TAGS_THRESHOLD` if needed.
      **(Hugo runs — needs GPU + the CLIP model on real images.)**

## 6. Follow-ups (post-merge, non-blocking)
- [ ] 6.1 Share the CLIP image vector between the do_ocr tag pass and the do_clip `ClipIndex` pass
      so an image is encoded once, not twice (pure perf; off the request path).
- [ ] 6.2 A `backfill-media` re-tag pass for already-indexed images (this change only tags newly
      extracted images).
- [ ] 6.3 Optional structured `tags:` frontmatter field + a `tag:` filter on `find`; per-vault
      custom vocabularies; prompt-templated vocab ("a photo of X") if recall needs it.
