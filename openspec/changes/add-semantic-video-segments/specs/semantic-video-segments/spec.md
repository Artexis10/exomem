## ADDED Requirements

### Requirement: Timed Transcript Rendering

The system SHALL, when semantic segments are enabled (`EXOMEM_SEMANTIC_SEGMENTS`), render
audio/video extracted text as one line per ASR segment prefixed with a human-readable
timestamp — `[m:ss] …` (or `[h:mm:ss] …` at an hour and beyond), and
`[m:ss] [Speaker]: …` when diarization is active, repeating the speaker label per segment.
Rendering SHALL happen at the extraction source so every writer (worker, backfill, upload)
inherits it. The extraction engine marker SHALL gain `+timed`, ordered before `+diarized`.
The structured merged-turn `speakers` data and frontmatter SHALL be unchanged. With the flag
unset, extraction output SHALL be byte-identical to today for both plain and diarized paths.

#### Scenario: Plain transcript gains per-segment timestamps

- **WHEN** an audio/video file is transcribed with `EXOMEM_SEMANTIC_SEGMENTS` set and
  diarization off
- **THEN** the extracted text is one `[m:ss] <segment text>` line per ASR segment and the
  engine carries `+timed`

#### Scenario: Diarized transcript keeps per-segment granularity

- **WHEN** the same file is transcribed with diarization active under the gate
- **THEN** each line is `[m:ss] [<speaker>]: <segment text>` with the label repeated per
  segment, the engine ends `+timed+diarized`, and the structured merged-turn `speakers` list
  is identical to the ungated shape

#### Scenario: Gate unset is byte-identical

- **WHEN** extraction runs with `EXOMEM_SEMANTIC_SEGMENTS` unset
- **THEN** transcript text and engine strings are exactly today's output

### Requirement: Fused Semantic Segmentation as the Retrieval Unit

For gated, timed audio/video sidecars, the text-embedding chunker SHALL segment the
transcript at boundaries fused from deterministic signals: transcript-topic scores from
embedding-similarity valleys over sliding line windows, visual-change events from persisted
scene-frame timestamps, speaker-turn changes, and OCR-change events between consecutive
frame sidecars. Segments SHALL respect a minimum duration, a maximum word budget (splitting
rather than truncating), and a segment cap. Each chunk SHALL begin with its segment's
timestamp marker. Pages that are not gated timed media SHALL chunk exactly as today.

#### Scenario: Topic shift becomes a chunk boundary

- **WHEN** a timed transcript moves from one topic to a distinctly different one and the
  sidecar is re-embedded under the gate
- **THEN** the embedding index holds separate chunks for the two topics, each beginning with
  its segment's `[timestamp]` marker

#### Scenario: Corroborating events tip a moderate boundary

- **WHEN** a moderate topic valley coincides (within the proximity window) with a
  visual-change event or a speaker change
- **THEN** a segment boundary is placed there

#### Scenario: Non-timed pages are unaffected

- **WHEN** any page without timed media lines is embedded, gate set or not
- **THEN** its chunks are byte-identical to the existing paragraph chunker's output

### Requirement: Transcript Matches Surface the Moment

`find` SHALL surface `transcript_match_at` (human-readable timestamp) on hits whose match
localizes inside a timed transcript: from the matched chunk's leading timestamp marker in
the vector lane, or from the nearest preceding marker to the first query-token anchor for
BM25/keyword matches on timed audio/video pages. When such a hit has no CLIP-resolved frame,
the nearest persisted scene frame SHALL be attached by transcript timestamp. The surfacing
SHALL be data-driven — hits on flat (untimed) sidecars are byte-identical to today.

#### Scenario: A said phrase lands at its moment

- **WHEN** a query matches text spoken at 51:20 of a timed video transcript
- **THEN** the video's single hit carries `transcript_match_at: "51:20"` and, when frames
  are persisted, a `scene_frame` near that moment

#### Scenario: Flat sidecars unchanged

- **WHEN** a query matches a video sidecar whose transcript has no timestamp markers
- **THEN** the hit contains no `transcript_match_at` and is otherwise identical to today

### Requirement: Segment Signals Complete After Frame OCR

The media worker SHALL enqueue one re-embed of the parent sidecar after a video's frame-OCR
jobs — OCR-change events depend on frame sidecars extracted after the transcript is written —
so segmentation re-runs with all signals present, and the restart scan SHALL re-enqueue
deduped parent re-embeds after pending frame children. Segmentation SHALL tolerate missing
signals at any earlier run (absent events contribute nothing).

#### Scenario: Trailing re-embed folds in OCR events

- **WHEN** a gated video finishes ASR, scene frames are written, and their OCR jobs complete
- **THEN** the parent sidecar is re-embedded once afterwards and its segments reflect
  visual, speaker, and OCR events

### Requirement: Opt-In Re-Timing Backfill

`backfill-media` SHALL support an explicit opt-in re-ASR pass (`--retime`) that upgrades
completed flat-text audio/video sidecars to timed transcripts, keyed idempotent on the
`+timed` engine marker. One re-extraction SHALL serve `--retime` and `--rediarize` together
when both are requested. The pass SHALL warn and disable itself when the gate is unset.

#### Scenario: Legacy recording upgraded once

- **WHEN** `backfill-media --retime` runs twice over a video whose engine lacks `+timed`
- **THEN** the first run re-transcribes and writes timed lines (engine gains `+timed`) and
  the second run skips it

### Requirement: Soft-Fail Ladder and Pure-Substrate Bounds

Segmentation SHALL degrade without failing extraction or embedding: an embedding failure
falls back to event-only boundaries; fewer than the minimum timed lines, or any error,
falls back to the existing paragraph chunker. All signals SHALL be deterministic
measurements (embedding similarity, filename timestamps, label changes, token overlap);
no reasoning model SHALL be invoked.

#### Scenario: Embedding failure still segments

- **WHEN** window embedding raises while events are available
- **THEN** boundaries come from events (plus word-budget splits) and embedding of the
  resulting chunks proceeds

#### Scenario: Too little timed content falls back

- **WHEN** a timed sidecar has fewer than the minimum timed lines
- **THEN** the page chunks via the existing paragraph chunker unchanged
