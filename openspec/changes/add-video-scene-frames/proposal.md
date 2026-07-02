## Why

Video search today indexes per-keyframe CLIP vectors sampled at a uniform ~8s interval with
perceptual-hash near-dup suppression. Two gaps remain. First, the moments that get indexed are
chosen by a fixed clock, not by content change — a slide that flips 3s after a sample point can
be missed entirely, while a static talking-head run burns candidates on identical frames. Second,
nothing visual is ever persisted: a `find` hit says `clip_match_at: "14:32"`, but there is no
frame the user (or Claude) can retrieve, view, or `/download` — and no on-screen text from the
video ever reaches BM25/keyword search.

This change replaces the uniform sampler with visual-change scene detection (frame-difference +
the existing perceptual-hash logic, decoded via the already-present PyAV) and persists one
representative JPEG per detected scene as a real Evidence artifact with a standard sidecar. The
frames ride the existing image OCR path, so slide text, stack traces, and dashboard content
become keyword-findable at their timestamp; `find` groups frame hits under the parent video so
one video stays one hit.

It stays **pure-substrate**: scene-boundary detection is frame-difference/histogram arithmetic,
frame persistence is deterministic transcoding, and enrichment is the existing Tesseract OCR —
all measurement, no reasoning LLM. No new dependency: PyAV, Pillow, numpy, and CLIP are already
in the stack.

## What Changes

- **Scene-aware keyframe sampling** (`KB_MCP_VIDEO_SCENE_FRAMES` gate): a cheap I-frame-only
  metrics pass (PyAV `skip_frame NONKEY`, 64×64 grayscale reformat) feeds a pure
  `detect_scenes()` — anchor-based hash/histogram boundary detection with a minimum scene
  duration and a weakest-boundary merge when scenes exceed the existing keyframe cap. The
  video's CLIP rows in `.clip.sqlite` land at scene-representative timestamps instead of
  uniform-interval ones. On unknown duration, too-few I-frames, or any error, the existing
  uniform sampler is the fallback.
- **Persisted scene frames.** One representative frame per scene is decoded once (feeding both
  CLIP and disk), downscaled to ≤1280px JPEG, and written to a sibling
  `<video-filename>.frames/` directory under Evidence, named `scene-<NNN>-t<ms>ms.jpg`. Each
  frame gets a standard `<jpg>.md` sidecar (extended `_render_sidecar`) carrying
  `parent_media: <video rel path>` and `frame_ts: <seconds>`.
- **Frames are OCR'd through the existing image path** (`extracted_by: pending` sidecar →
  worker OCR → `## Extracted text`), so on-screen text is BM25/keyword-findable. Restart
  recovery and `KB_MCP_IMAGE_TAGS` apply for free.
- **No double-counting:** frame JPEGs get NO ClipIndex rows — the parent video's per-scene
  vectors own visual search. Both CLIP-enqueue points (worker startup scan, backfill) skip
  sidecars carrying `parent_media`.
- **`find` groups frames under the parent video:** frame-sidecar candidates are collapsed to the
  parent video's sidecar pre-fusion, so one video = one hit; the hit gains `scene_frame` (the
  best-matching frame's path, ready to `/download`) and `scene_match_at` (mm:ss). CLIP-lane
  video hits resolve the nearest saved frame by filename timestamp.
- **Backfill:** `exomem backfill-media` grows an idempotent `need_scenes` pass that regenerates
  scene frames (and scene-aware vectors) for already-indexed videos when the gate is on.
- **Env-gated, default-OFF, soft-fail.** `KB_MCP_VIDEO_SCENE_FRAMES` unset ⇒ byte-identical
  behavior (uniform sampling, no files written, `find` unchanged). Tuning:
  `KB_MCP_VIDEO_SCENE_THRESHOLD` (hash bits), `KB_MCP_VIDEO_SCENE_MIN_SECS`. Any frame-write
  failure still leaves the video vectors-indexed; total detection failure falls back to the
  uniform sampler. Requires the CLIP path (`KB_MCP_DISABLE_CLIP` ⇒ no scene work).
- **No new dependency.** PyAV (via faster-whisper), Pillow, numpy, CLIP — all already present.

Out of scope (future changes): semantic/transcript-topic segmentation fused with visual events;
cascade delete/move of a video's frames with the parent; zero-shot tags targeted at video
keyframes; per-scene VLM captions.

## Capabilities

### New Capabilities
- `video-scene-frames`: visual-change scene detection chooses which video moments get CLIP
  vectors, and one representative JPEG per scene is persisted, OCR'd, and grouped under the
  parent video in `find` — default-off, soft-fail, pure-substrate (frame-difference arithmetic +
  deterministic transcoding + existing OCR; no LLM, no new dependency).

## Impact

- Code: `src/kb_mcp/embeddings.py` (pure `detect_scenes` core, I-frame metrics pass, gate branch
  in `embed_video_frames`); `src/kb_mcp/scene_frames.py` (new — frame/sidecar writer, filename
  timestamp codec, nearest-frame lookup); `src/kb_mcp/media_worker.py` (video branch wiring +
  frame-child exclusion in the startup CLIP scan); `src/kb_mcp/find.py` (pre-fusion frame-child
  collapse, `scene_frame`/`scene_match_at` hit fields); `src/kb_mcp/preserve.py`
  (`_render_sidecar` optional `parent_media`/`frame_ts`); `src/kb_mcp/backfill.py`
  (`need_scenes` pass).
- Deps: none added.
- Default-off ⇒ zero behavior change when `KB_MCP_VIDEO_SCENE_FRAMES` is unset.
- Worker budget: pass 1 decodes I-frames only at 64×64 grayscale (seconds for an hour-long
  1080p video); pass 2 seeks/decodes ≤ the existing keyframe cap (40) at full res — comparable
  to today's path and far below ASR cost on the same worker thread.
- Storage: ≤ cap × ~100–200 KB JPEG per video plus one small sidecar per frame; frames are
  normal Evidence files (list/audit/get/download work unchanged).
- Docs: `KB_MCP_VIDEO_SCENE_FRAMES` / `..._THRESHOLD` / `..._MIN_SECS` in the README env table;
  note in `docs/deployment.md`. (Scaffold/SKILL untouched — leak-guarded; handled separately.)
