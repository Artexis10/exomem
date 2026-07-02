# Tasks — video scene frames

## 1. Pure scene-detection core (TDD-able, no PyAV/PIL)
- [x] 1.1 Refactor `embeddings._avg_hash` to share a `_hash_bits(arr: np.ndarray) -> int` core
      between the PIL path and a new ndarray path; add `_gray_hist(arr)` (32-bin normalized).
- [x] 1.2 Add `Scene` dataclass (`start_ts`, `end_ts`, `rep_ts`, `boundary_score`) and pure
      `detect_scenes(series, *, hash_threshold, hist_threshold, min_scene_secs, max_scenes)`
      over `[(ts, hash64, hist)]`: anchor-based boundaries, min-duration merge, midpoint
      representative, weakest-boundary merge at cap. Env knobs
      `KB_MCP_VIDEO_SCENE_THRESHOLD` / `KB_MCP_VIDEO_SCENE_MIN_SECS` with unparseable→default.
- [x] 1.3 Tests `tests/test_scene_detect.py` (no PyAV): static jitter → 1 scene; N distinct
      patterns → N scenes at correct boundaries; sub-min-secs flicker merges; cap merges the
      weakest boundaries; histogram-only change detected; empty/single-candidate edges; env
      knob overrides + unparseable fallback.

## 2. PyAV sampling (gate branch, fallback preserved)
- [x] 2.1 Add `_iter_iframe_metrics(path)` (skip_frame NONKEY, 64×64 gray8 reformat, ≥2s
      thinning, ~900 candidate cap) and `_decode_frames_at(path, ts_list)` (pass 2, existing
      seek pattern).
- [x] 2.2 Add `sample_video_scenes(path)` and the `KB_MCP_VIDEO_SCENE_FRAMES` gate branch in
      `embed_video_frames` — a variant returning `(vectors, scenes_with_images)` so the worker
      gets both from one decode pass; fallback to `_sample_video_keyframes` candidates on
      unknown duration / too-few I-frames / any pass-1 error.
- [x] 2.3 Tests: gate-off path byte-identical (spy on samplers); fallback triggers; one
      `pytest.importorskip("av")` integration test encoding a tiny two-scene synthetic video
      (solid color blocks) asserting one boundary near the switch (skippable — CI lacks the
      media extra).

## 3. Frame writer (new module) + sidecar extension
- [x] 3.1 Extend `preserve._render_sidecar` with optional `parent_media` / `frame_ts`
      frontmatter (emitted only when provided; existing callers unchanged).
- [x] 3.2 Add `src/kb_mcp/scene_frames.py`: `scene_frames_enabled()`, `frames_dir_for(video)`,
      `frame_filename(idx, ts)` + `parse_frame_ts(name)`, `clear_scene_frames(vault_root,
      video)` (removes owned jpg+sidecars, calls `embeddings.delete_after_remove`),
      `write_scene_frames(vault_root, video, scenes_with_images)` (≤1280px q80 JPEG + sidecar
      via 3.1), `nearest_frame(vault_root, video_rel, ts)`. All labels generic (leak guard).
- [x] 3.3 Tests `tests/test_scene_frames.py`: file+sidecar creation with
      `parent_media`/`frame_ts`; filename ts round-trip; rewrite clears stale frames; soft-fail
      on unwritable dir returns partial/empty without raising.

## 4. Worker wiring
- [x] 4.1 `media_worker._run_clip` video branch: gate on → scene pipeline → `upsert_frames`
      with scene vectors → `write_scene_frames` → enqueue one OCR job per new frame
      (`do_ocr=True, do_clip=False`). Frame-write failure logs and still upserts vectors.
- [x] 4.2 `_scan_unindexed_images`: skip images whose sidecar carries `parent_media`.
- [x] 4.3 Tests: frames written + OCR jobs enqueued (extract stubbed); gate-off unchanged;
      startup scan never CLIP-queues frame children; frame-write failure still upserts.

## 5. `find` grouping
- [x] 5.1 `ParsedPage`: `parent_media` / `frame_ts` properties (defensive float parse).
- [x] 5.2 `_collapse_frame_children(ranking, vault_root)` applied pre-fusion to vector/BM25/
      keyword (+CLIP defensively) lanes; remap per-path score/chunk maps keep-best; record
      best-frame attribution.
- [x] 5.3 `Hit.scene_frame` / `Hit.scene_frame_ts`; emit `scene_frame` + `scene_match_at` in
      `as_dict`/compact; nearest-saved-frame resolution for CLIP-lane video hits; keyword-mode
      remap; frame-OCR excerpt fallback for collapsed parents.
- [x] 5.4 Tests: frame-OCR query → exactly ONE hit (parent video sidecar) carrying
      `scene_frame`+`scene_match_at`, no frame sidecars in results; parent+frame both ranked →
      parent keeps best score; CLIP video hit resolves nearest frame; orphan frame surfaces
      standalone; `file_types=["video"]` matches via frame text; stills and frame-less videos
      byte-identical to today.

## 6. Backfill
- [x] 6.1 `need_scenes` + `scene_frames_done` idempotency; `need_clip` skips `parent_media`
      children; dry-run `scenes` tag; stats field `scene_frames_written`.
- [x] 6.2 Tests: legacy uniform-indexed video regenerates when gated on; second run idempotent;
      gate-off run untouched.

## 7. Docs
- [x] 7.1 README env table rows (`KB_MCP_VIDEO_SCENE_FRAMES`, `KB_MCP_VIDEO_SCENE_THRESHOLD`,
      `KB_MCP_VIDEO_SCENE_MIN_SECS`); note in `docs/deployment.md` (worker budget, backfill
      migration). Scaffold/SKILL untouched.

## 8. Verify
- [x] 8.1 `PYTHONPATH=src KB_MCP_DISABLE_EMBEDDINGS=1 python -m pytest -q` green.
- [x] 8.2 `ruff check` clean.
- [x] 8.3 `openspec validate add-video-scene-frames --strict` passes; leak guard green.
- [ ] 8.4 Desk-side smoke (GPU box, media extra, gate on): upload a real screen recording →
      `.frames/` JPEGs + sidecars appear → after OCR, `find("<on-slide text>")` returns ONE
      video hit with `scene_frame`/`scene_match_at` → `/download` renders the frame; tune
      thresholds on real recordings. **(Hugo runs — needs GPU + real videos.)**

## 9. Follow-ups (post-merge, non-blocking)
- [ ] 9.1 Semantic/transcript-topic segmentation fused with visual events (phase 2).
- [ ] 9.2 Cascade delete/move of `.frames/` with the parent video.
- [ ] 9.3 Zero-shot tags targeted at video keyframes / per-scene captions.
