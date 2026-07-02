# Design — video scene frames

## D1. Scene detection: two-pass, I-frame metrics + pure detection core

**Pass 1 — cheap metrics scan (no PIL images retained).** Sequential demux with
`stream.codec_context.skip_frame = "NONKEY"` so PyAV decodes I-frames only; encoder scenecut
logic already concentrates I-frames at hard cuts, so candidate density is naturally adaptive.
Each frame is reformatted libav-side to 64×64 `gray8` and read as an ndarray — full-res frames
are never materialized in this pass. Two metrics per candidate: the existing 64-bit average-hash
(refactored so a shared `_hash_bits(arr)` core serves both the PIL path and this ndarray path)
and a 32-bin normalized grayscale histogram. Thinning keeps at most one candidate per 2s with a
hard cap of ~900 candidates (gap widens to `duration/900` beyond that) to bound all-intra screen
captures.

**Detection — pure function.** `detect_scenes(series, *, hash_threshold, hist_threshold,
min_scene_secs, max_scenes)` over `[(ts, hash64, hist)]`, unit-testable with no PyAV/PIL:

- **Anchor-based comparison** (the `_dedup_keyframes` pattern generalized): each candidate is
  compared to the current scene's anchor, not the previous frame, so slow drift/zoom doesn't
  fragment and static runs collapse.
- **Boundary** when `hamming > SCENE_HASH_THRESHOLD` (default 10 of 64) OR histogram L1 >
  `SCENE_HIST_THRESHOLD` (default 0.35). Hash catches structural changes (slide flips, window
  switches); histogram catches global luminance shifts hash is blind to. The deliberate gap to
  `PHASH_DEDUP_DISTANCE=5` (dedup collapses ≤5; boundary needs >10) is a hysteresis band so
  near-dup jitter never becomes a scene.
- **Minimum scene duration** (default 4s): a boundary within min-secs of the previous boundary
  merges into it, with the anchor updating to the newest content — A→B→A flicker and fades
  yield one boundary, not three.
- **Representative frame** = candidate nearest the scene's temporal midpoint (avoids transition
  artifacts; any mid-slide frame is OCR-equivalent).
- **Cap:** when scenes exceed the existing `_max_video_keyframes()` (40), iteratively merge the
  adjacent pair whose boundary had the smallest change score — keeps the strongest boundaries,
  strictly better than uniform subsampling.

**Pass 2 — decode only winners.** Seek+decode one full-res frame per scene (existing pattern);
that single decode feeds both CLIP encoding and the JPEG write.

**Fallback:** unknown duration, fewer than `MIN_VIDEO_KEYFRAMES` I-frame candidates, or any
error in pass 1 → the existing `_sample_video_keyframes` uniform sampler is the candidate
source. Never hard-fails past what today's path tolerates.

**Gate-off = byte-identical:** with `EXOMEM_VIDEO_SCENE_FRAMES` unset, `embed_video_frames`
runs today's uniform sampling + `_dedup_keyframes` untouched and writes no files.

## D2. Storage layout

- Frames dir: sibling of the video — `Evidence/<scope>/<category>/<video-filename>.frames/`.
  Derivable in both directions, groups naturally in `list_directory`, Windows-safe.
- Filenames: `scene-<NNN>-t<ms>ms.jpg` — chronological sort; the timestamp is parseable from
  the name (`t(\d+)ms`), which is how `find` resolves "nearest frame" with no extra index.
- JPEG: longest side ≤1280px, quality 80 (~100–200 KB; preserves slide/stack-trace legibility
  for Tesseract).
- Per-frame sidecar `<jpg>.md` via extended `preserve._render_sidecar` (two new optional
  params): `media_type: image`, `evidence_file`, `extracted_by: pending`,
  `parent_media: <video rel path>`, `frame_ts: <float seconds>`, tag `scene-frame`. Standard
  sidecar convention ⇒ find excerpts, pending-scan recovery, `get`, and `/download` all work
  unchanged.

## D3. Indexing without double-counting

Frame JPEGs get **no ClipIndex rows** — the parent video's per-scene rows (same vectors, same
timestamps) own visual search. Enforced by skipping any image whose sidecar carries
`parent_media` at both CLIP-enqueue points: `media_worker._scan_unindexed_images` and
backfill's `need_clip`. OCR rides the existing pending-sidecar path (`do_ocr=True,
do_clip=False` per frame); `EXOMEM_IMAGE_TAGS` applies to frames for free.

## D4. `find` grouping — collapse at lane level, pre-fusion

New `_collapse_frame_children(ranking, vault_root)` applied to the vector/BM25/keyword lanes
(and CLIP defensively) right after each lane is built: a candidate sidecar whose page carries
`parent_media` is remapped to `<parent_media>.md` (dedup keep-first; per-path score/chunk maps
remapped keep-best), recording the best-ranked frame as attribution. Pre-fusion collapse means
RRF fuses one candidate per video — frames can never flood fusion, and a video hit via CLIP and
via frame OCR fuses as one candidate across lanes.

Hit enrichment: `Hit.scene_frame` (vault-relative JPEG path) + `Hit.scene_frame_ts`, emitted as
`scene_frame` + `scene_match_at` (mm:ss). Population: (a) text-lane frame attribution wins;
(b) else a CLIP-lane video hit with `clip_frame_ts` resolves the nearest saved frame by filename
timestamp (one directory listing, only when `<video>.frames/` exists). Keyword mode gets the
same remap at hit-build time. A collapsed parent with no lexical match of its own builds its
excerpt from the matched frame sidecar's OCR text so the "why" is visible. An orphan frame
(parent gone) surfaces as a standalone image hit — graceful.

## D5. Lifecycle

- **Re-processing:** the writer clears the `scene-*.jpg` + sidecars it owns before writing the
  new set (delete-then-insert, mirroring `upsert_frames`) and calls
  `embeddings.delete_after_remove` for removed sidecars so bge rows don't go stale.
- **Delete/move of parent video:** v1 tolerates orphan frames (reconcile/backfill heals drift;
  a frame whose `parent_media` target is gone degrades to a standalone image hit). Cascade is a
  documented follow-up.
- **Watcher safety:** frame JPEGs are invisible to the watcher (it only tracks `.md`); frame
  sidecars go through `batch_atomic_write`, which registers self-writes — no echo loop.

## D6. Backfill + migration

`backfill_media` grows `need_scenes` = gate on AND video AND not `scene_frames_done(video)`
(≥1 `scene-*.jpg` with sidecar in the frames dir). A legacy uniform-indexed video re-processes
in one decode pass — `upsert_frames` (already delete-then-insert) purges old rows; frames and
OCR jobs are produced. Second run skips (idempotent). `dry_run` prints a `scenes` tag.
Migration = run `exomem backfill-media` with the gate set; the env gate is the single switch
everywhere, no CLI flag.

## Risks

- All-intra / huge-GOP encodes distort I-frame density — mitigated by thinning/cap and the
  fallback; thresholds tunable desk-side via the two env knobs.
- `frame_ts` frontmatter parses defensively (`float(...)` try/except).
- Lane-collapse key remapping must keep the parent's best score when both parent and frame
  ranked — tested explicitly.
- Merge-collision discipline vs the in-flight `improve-find-latency-token-cost` change: all
  `find.py` edits are additive blocks; no reshaping of existing lane code.
