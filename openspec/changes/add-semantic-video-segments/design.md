# Design — semantic video segments

## D1. Timed rendering + gate

One gate, `EXOMEM_SEMANTIC_SEGMENTS` (truthy opt-in, legacy `KB_MCP_` promoted),
covers rendering AND segmentation; the find surface is data-driven, not gated
(scene-frames precedent). Rendering happens **in `extract`** — `_transcribe` for
plain, `_diarize` for labeled — because per-segment timings exist only there,
and every writer inherits the behavior. Format: one line per ASR segment,
`[m:ss]`/`[h:mm:ss]` prefix (identical semantics to `find._format_timestamp`),
diarized lines repeat the speaker label per segment (merged turns would span
minutes and kill both windowing and match localization; the structured
merged-turn `speakers` list is untouched). Engine marker `+timed` precedes
`+diarized` (`_needs_rediarize` matches `endswith("+diarized")`). Canonical
parse regex lives in `semantic_segments.TIMED_LINE_RE`; `find._format_timestamp`
delegates to the shared formatter. Renderer failure soft-fails to the flat join;
gate unset is byte-identical (regression-tested for plain AND diarized).

## D2. Segmenter (`semantic_segments.py`)

- `parse_timed_lines` → `[(ts, speaker|None, text)]`; unmatched lines fold into
  the previous line.
- Windows of `WINDOW_LINES=4` (stride 1), text-only (markers stripped), embedded
  in one batch via an **injected `embed_fn`** (defaults to
  `embeddings.embed_texts` via lazy import — the test seam that lets the whole
  segmenter run with embeddings disabled).
- Valley scores: adjacent-window cosine + TextTiling depth
  `((peak_left - sim) + (peak_right - sim)) / 2`, normalized to `topic ∈ [0,1]`
  by `depth / 0.4`.
- Events (`gather_events`): visual = midpoints between consecutive scene-frame
  `rep_ts` values parsed from `<video>.frames/scene-*-t<ms>ms.jpg` filenames
  (rep_ts is a scene midpoint, so the boundary lies between reps — ±5 s
  proximity absorbs the approximation); speaker = label change between adjacent
  lines; OCR = token-set Jaccard < 0.5 between consecutive frame sidecars with
  real engines (`(no text detected)` = empty).
- Fusion per gap: `1.0*topic + 0.5*scene(±5s) + 0.35*speaker + 0.4*ocr(±5s)`,
  boundary at `>= 0.55` — a deep valley alone passes; a moderate valley plus any
  corroborating event passes; scene+OCR+speaker together pass without a valley.
- Constraints: `MIN_SEG_SECS=25` (keep the higher-scoring of two close
  boundaries), `MAX_SEG_WORDS = MAX_WORDS_PER_CHUNK - 30` (force-split at the
  best interior gap — nothing silently truncates), `MAX_SEGMENTS=256` (drop
  weakest first).
- Chunks: segment lines verbatim (markers included) + standard title prefix;
  the first line's `[ts]` is what find parses.
- Fallback ladder (unit-tested): full fusion → events-only on embed failure →
  `None` (< 8 timed lines or any error) ⇒ caller falls back to `chunk_text`.

## D3. Chunker seam (`embeddings.py`)

`upsert_after_write` calls `_chunks_for_page(vault_root, page)` instead of
`chunk_text` directly. Semantic path only when gate on ∧ `media_type` audio or
video ∧ ≥ 8 timed lines in `## Extracted text`; sections before/after the
transcript still paragraph-chunk in document order. All other pages —
provably identical output to `chunk_text` (equality tests). The seam is shared
automatically by every writer, the file watcher, and reconcile.

## D4. find surface

`Hit.transcript_ts` emitted as `transcript_match_at` (same formatter).
Resolution: vector lane parses the matched chunk's first timed marker;
BM25/keyword-only hits on timed A/V pages locate the first query-token anchor in
the body and take the nearest **preceding** marker; title-only matches emit
nothing. The existing nearest-frame block extends: `clip_frame_ts` first (as
today), else `transcript_ts` — `scene_match_at` and `transcript_match_at`
coexist, never suppress each other. `_find_keyword` gets the same helper. One
video = one hit is already guaranteed (best-chunk-per-file + frame-child
collapse).

## D5. Worker ordering

Segmentation is a pure function of (sidecar bytes, frames-dir state) — safe to
re-run. `_Job.do_reembed` + `_process` branch calls `upsert_after_write` on the
sidecar. `_persist_scene_frames` enqueues the frame-OCR jobs THEN one parent
re-embed (single-thread FIFO ⇒ runs after all OCRs; no synchronization). First
upsert at ASR time segments from transcript+speaker signals only (missing
events contribute zero — soft); the trailing job re-segments with all four.
Restart: `_scan_pending_ocr` enqueues deduped parent re-embeds after pending
frame children. The narrow crash window (frames OCR'd, re-embed lost) heals on
any later sidecar write or backfill.

## D6. Backfill `--retime`

CLI arg mirroring `--rediarize` (help text names the gate; warn-and-disable
when unset). `_needs_retime`: audio/video ∧ completed real engine ∧
`"+timed" not in engine`. One `extract_text` call serves retime+rediarize;
the existing marker-degradation path generalizes to whichever markers were
requested. Stats gain `retimed`. After `need_scenes` writes+OCRs fresh frames,
a final parent `upsert_after_write` folds the new events into segments.

## Risks

- Concurrent session in find.py-adjacent code — all edits additive; drop the
  `_format_timestamp` delegation if it collides.
- `_chunks_for_page` touches every file's embedding path — the non-timed branch
  equality tests are the regression guard.
- Thresholds are educated guesses — deterministic, centralized in one constants
  block, tuned desk-side on real ASR output.
- Sidecar size +10–15% vs the 512 KB cap — very long videos truncate slightly
  earlier; documented, cap unchanged.
