# Tasks — semantic video segments

## 1. Timestamp core (pure, no models)
- [x] 1.1 `semantic_segments.py`: `format_ts` (m:ss / h:mm:ss, identical to
      `find._format_timestamp` output), `TIMED_LINE_RE`, `parse_timed_lines`
      (unmatched lines fold into previous), `render_timed_lines` (plain +
      per-segment diarized lines).
- [x] 1.2 `find._format_timestamp` kept as-is (zero collision surface with the
      concurrent find.py work); output-identity with the shared formatter is
      asserted by test instead of by delegation.
- [x] 1.3 Tests `tests/test_semantic_segments.py`: format/parse round-trip incl.
      hour rollover, diarized/plain groups, folding, renderer shapes.

## 2. Gated timed rendering (extract)
- [x] 2.1 `_transcribe`: gate on ⇒ `render_timed_lines` + engine `+timed`;
      gate off byte-identical; renderer failure ⇒ flat join (soft).
- [x] 2.2 `_diarize`: gate on ⇒ per-segment `[ts] [Speaker]:` lines, engine
      `…+timed+diarized` (order load-bearing); merged-turn `speakers` list
      unchanged; gate off byte-identical.
- [x] 2.3 Tests (extend `tests/test_extract.py`, `_FakeWhisper` pattern):
      byte-identical off (plain + diarized), timed shapes on, engine markers,
      diarization soft-fail under gate ⇒ timed plain.

## 3. Segmenter
- [x] 3.1 Windows/valleys: text-only windows (4, stride 1), injected `embed_fn`,
      adjacent-window cosine, TextTiling depth → `topic` signal.
- [x] 3.2 `gather_events`: scene-frame midpoints (filename parse), speaker-change
      gaps, OCR Jaccard events (real-engine frame sidecars only).
- [x] 3.3 Fusion + constraints: weights 1.0/0.5/0.35/0.4, threshold 0.55,
      `MIN_SEG_SECS` 25, `MAX_SEG_WORDS` force-split, `MAX_SEGMENTS` 256.
- [x] 3.4 Ladder: embed failure ⇒ events-only; <8 timed lines or error ⇒ None.
- [x] 3.5 Tests: fake `embed_fn` two-topic boundary, uniform ⇒ none, event
      fixtures on a tmp vault, constraint cases, ladder cases, determinism.

## 4. Chunker seam (embeddings)
- [x] 4.1 `_chunks_for_page`: route gated timed A/V sidecars to the segmenter
      (transcript section) + `chunk_text` for surrounding sections in document
      order; everything else and segmenter-None ⇒ `chunk_text`.
- [x] 4.2 Tests: equality with `chunk_text` for non-media pages and gate-off;
      segment chunks + section chunks composition; None fallback.

## 5. find surface
- [x] 5.1 `Hit.transcript_ts` + `transcript_match_at` in `as_dict`/compact.
- [x] 5.2 `_transcript_ts_for_hit`: vector-chunk leading marker; BM25/keyword
      nearest-preceding-marker for timed A/V pages; title-only ⇒ None.
- [x] 5.3 Nearest-frame attach extension (`clip_frame_ts` first, else
      `transcript_ts`); `_find_keyword` parity.
- [x] 5.4 Tests `tests/test_find_transcript_match.py` (embeddings disabled):
      keyword/BM25 hit on a timed sidecar ⇒ `transcript_match_at` + attached
      `scene_frame`; chunk-branch unit test; coexistence with `scene_match_at`;
      flat sidecar ⇒ field absent; compact emission.

## 6. Worker ordering
- [x] 6.1 `_Job.do_reembed` + `_process` branch (`upsert_after_write`).
- [x] 6.2 `_persist_scene_frames`: enqueue parent re-embed after frame OCRs
      (gate on). `_scan_pending_ocr`: deduped parent re-embeds after pending
      frame children.
- [x] 6.3 Tests: enqueue-order assertions via stubs; restart-scan dedup.

## 7. Backfill --retime
- [x] 7.1 `_needs_retime` (a/v ∧ real engine ∧ no `+timed`); one re-extract
      serves retime+rediarize; generalized marker degradation; `retimed` stat;
      gate-off warn-and-disable; dry-run tag; post-frames parent re-embed.
- [x] 7.2 `__main__.py` `--retime` arg (help names the gate).
- [x] 7.3 Tests: detection matrix, combined retime+rediarize single extract,
      idempotent second run, gate-off disable, dry-run.

## 8. Docs + verify
- [x] 8.1 README env row (`EXOMEM_SEMANTIC_SEGMENTS`) + `--retime`;
      deployment.md note (cost, worker ordering, backfill upgrade path).
- [x] 8.2 Full suite green; ruff no net-new; leak guard;
      `openspec validate add-semantic-video-segments --strict`.
- [x] 8.3 Desk-side e2e (Hugo/desk, GPU + gate on): upload a talking-head+slides
      recording → timed sidecar bytes → segment chunks in `.embeddings.sqlite` →
      `find("<phrase said>")` returns the video with `transcript_match_at` + a
      `scene_frame` → `backfill-media --retime --dry-run` then real run on one
      legacy video; tune thresholds if boundaries feel off.

## 9. Follow-ups (non-blocking)
- [ ] 9.1 Content-type-aware weights (screen-recording vs podcast profiles).
- [ ] 9.2 Segment-aware context packs / get_video_frames integration.
- [ ] 9.3 Port the pattern to Q's UnifiedSegment (separate repo).
