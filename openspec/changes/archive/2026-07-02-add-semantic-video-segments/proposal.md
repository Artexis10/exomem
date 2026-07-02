## Why

A text match on a long recording today returns the whole two-hour sidecar: the
transcript is one flat blob, so `find` can say *that* a video discusses a topic
but not *when*. Only visual matches carry timestamps (`clip_match_at` from the
CLIP lane, `scene_match_at` from persisted scene frames). Meanwhile the two
ingredients for timed text retrieval already exist and are thrown away: Whisper
produces per-segment timings (materialized in `_transcribe` for diarization) and
the scene-frames feature persists visual-change events with timestamps.

This change makes the **semantic segment** the retrieval unit for timed media:
transcripts persist with human-readable timestamps, segmentation fuses
transcript-topic boundaries with visual/speaker/OCR events, and a transcript
match surfaces `transcript_match_at: "51:20"` with the nearest persisted scene
frame attached — "find where they said X" and "where they showed X" both answer
with a moment and a picture.

It stays **pure-substrate**: bge window embeddings + cosine similarity valleys +
fixed thresholds and event fusion are deterministic measurement (the same class
as CLIP, scene detection, and OCR) — no reasoning LLM. No new dependency.

## What Changes

- **Timed transcript rendering** (gate `EXOMEM_SEMANTIC_SEGMENTS`, default OFF):
  audio/video `## Extracted text` becomes one line per ASR segment —
  `[51:20] …` plain, `[51:20] [Alice]: …` diarized — rendered at the source in
  `extract` so every writer (worker, backfill, upload) inherits it. The engine
  marker gains `+timed` (before `+diarized` — suffix order is load-bearing for
  the rediarize idempotency check). The structured merged-turn `speakers` list
  and frontmatter are unchanged. Gate unset ⇒ byte-identical output.
- **Semantic segmentation as the chunking unit** (new `semantic_segments.py`):
  for gated, timed audio/video sidecars, the embedding chunker segments the
  transcript at fused boundaries — TextTiling-style embedding-valley topic
  scores combined with visual-change events (scene-frame filename timestamps),
  speaker-turn changes, and OCR-change events (token-Jaccard between consecutive
  frame sidecars) — with minimum segment duration, max-words force-splits, and a
  segment cap. Non-timed pages chunk exactly as today (equality-tested).
  Soft-fail ladder: embed failure ⇒ events-only boundaries; anything else ⇒
  paragraph chunking as today.
- **find surfaces the moment**: hits on timed media gain `transcript_match_at`
  (parsed from the matched chunk's leading timestamp in the vector lane, or the
  nearest preceding marker for BM25/keyword matches) and attach the nearest
  persisted scene frame when no CLIP frame already did. Data-driven, not gated —
  flat sidecars produce byte-identical responses.
- **Worker ordering**: after scene-frame OCR jobs, the worker enqueues one
  trailing re-embed of the parent sidecar so segmentation re-runs with all
  signals present (single-thread FIFO — no new synchronization). Restart scan
  re-enqueues deduped parent re-embeds after pending frame children.
- **Backfill `--retime`** (opt-in — re-ASR is expensive): upgrades existing
  flat-text audio/video to timed transcripts; idempotent via the `+timed`
  marker; one re-extract serves `--retime` and `--rediarize` together; warns
  and disables when the gate is unset.

Out of scope: VLM captions or any server-side reasoning; cross-video inference;
per-segment vectors in the CLIP index; Q/UnifiedSegment work (the pattern
transfers later).

## Capabilities

### New Capabilities
- `semantic-video-segments`: timed transcripts + fused semantic segmentation
  make moments the retrieval unit for audio/video — default-off, soft-fail,
  pure-substrate (deterministic embedding/event measurement, no LLM, no new
  dependency).

### Modified Capabilities
- `speaker-diarization`: gate carve-out — with `EXOMEM_SEMANTIC_SEGMENTS` set,
  diarized transcripts render as per-segment timed lines (labels repeated per
  line) instead of merged turns; the structured merged-turn list is unchanged.
- `video-scene-frames`: persisted frames additionally serve as segmentation
  boundary events; the worker enqueues a parent-sidecar re-embed after frame
  OCRs complete.

## Impact

- Code: `src/exomem/semantic_segments.py` (new — timestamps/parser/renderer,
  valley+event fusion segmenter); `extract.py` (gated timed rendering);
  `embeddings.py` (`_chunks_for_page` routing seam); `find.py`
  (`transcript_match_at` + nearest-frame attach, additive); `media_worker.py`
  (`do_reembed` trailing job); `backfill.py` + `__main__.py` (`--retime`).
- Deps: none added. Gate unset ⇒ zero behavior change anywhere.
- Cost: one extra windows-embedding batch per timed sidecar write and one
  bounded re-embed per video after frame OCR — off the request path, only when
  gated on. Sidecar grows ~10–15% from markers (existing 512 KB cap applies).
- Docs: `EXOMEM_SEMANTIC_SEGMENTS` env row + `--retime` in README/deployment.
