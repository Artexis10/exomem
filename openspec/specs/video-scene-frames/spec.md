# video-scene-frames Specification

## Purpose
Make a video's CLIP keyframes track what actually changes on screen instead of
sampling at uniform intervals: when enabled
(`EXOMEM_VIDEO_SCENE_FRAMES`), a cheap visual-change metrics pass detects
scene boundaries, persists one representative, OCR'd JPEG per scene as an
ordinary vault file, and `find` collapses a video's multiple scene-frame
matches into a single hit enriched with the matched scene's frame and
timestamp. The feature is default-off (byte-identical output when unset),
falls back to the existing uniform sampler on detection failure, and a frame
write failure never blocks the video's CLIP vectors from being indexed.
## Requirements
### Requirement: Visual-Change Scene Detection

The system SHALL, when scene frames are enabled (`EXOMEM_VIDEO_SCENE_FRAMES`), choose a video's
CLIP keyframes by visual-change scene detection instead of uniform-interval sampling: a cheap
I-frame-only metrics pass (perceptual average-hash + normalized grayscale histogram) SHALL feed
an anchor-based boundary detector with a minimum scene duration, and each detected scene SHALL
contribute one representative frame (nearest the scene's temporal midpoint). When detected
scenes exceed the existing per-video keyframe cap, the weakest boundaries SHALL be merged first.
Detection SHALL use only dependencies already in the stack (PyAV, Pillow, numpy).

#### Scenario: Slide change becomes a scene boundary

- **WHEN** a screen recording contains a hard visual change (e.g. a slide flip) and scene frames
  are enabled
- **THEN** a scene boundary is detected at that change and the resulting scenes' representative
  timestamps carry the video's CLIP vectors

#### Scenario: Talking-head video yields few scenes

- **WHEN** a video is visually static (jittering near-duplicate frames) for its whole duration
- **THEN** detection yields a single scene rather than fragmenting on jitter

#### Scenario: Flicker does not fragment scenes

- **WHEN** a visual change reverts within the minimum scene duration (A→B→A flicker or a fade)
- **THEN** the boundaries merge and at most one scene boundary results

#### Scenario: Cap merges weakest boundaries

- **WHEN** more scenes are detected than the per-video keyframe cap
- **THEN** adjacent scenes with the smallest boundary change scores merge until the cap is met,
  preserving the strongest boundaries

#### Scenario: Fallback to uniform sampling

- **WHEN** the metrics pass fails, the duration is unknown, or too few I-frame candidates decode
- **THEN** the existing uniform seek-sampling path supplies the candidates and processing
  completes as today

### Requirement: Persisted Scene Frames

The system SHALL persist one representative JPEG per detected scene in a `<video-filename>.frames/`
directory sibling to the video under Evidence, named `scene-<NNN>-t<ms>ms.jpg` (timestamp
parseable from the filename), downscaled to a bounded resolution. Each frame SHALL receive a
standard markdown sidecar carrying `media_type: image`, `evidence_file`, `extracted_by: pending`,
`parent_media` (the vault-relative parent video path), and `frame_ts` (seconds). Frames SHALL be
ordinary vault files reachable by `get`, `list_directory`, `audit`, and `/download` with no
special-case handling.

#### Scenario: Frames and sidecars are written per scene

- **WHEN** a video is processed with scene frames enabled and N scenes are detected
- **THEN** N JPEGs and N sidecars exist in the video's `.frames/` directory, each sidecar
  pointing at its JPEG via `evidence_file` and at the video via `parent_media` with its
  `frame_ts`

#### Scenario: Re-processing replaces owned frames

- **WHEN** a video is re-processed (re-upload or backfill)
- **THEN** previously written `scene-*.jpg` files and their sidecars are removed before the new
  set is written, and removed sidecars are purged from the text index

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

### Requirement: Find Groups Frames Under the Parent Video

`find` SHALL collapse scene-frame candidates into the parent video's hit before rank fusion so a
multi-scene video yields at most one hit, and SHALL enrich that hit with `scene_frame` (the
vault-relative path of the best-matching frame) and `scene_match_at` (the matched timestamp,
mm:ss). A CLIP-lane video hit SHALL resolve `scene_frame` to the nearest persisted frame by
filename timestamp when the frames directory exists. A scene frame whose parent video is gone
SHALL surface as an ordinary standalone image hit.

#### Scenario: One hit per video

- **WHEN** a query matches the OCR text of several frames of the same video
- **THEN** exactly one hit is returned — the parent video's sidecar — carrying the best-matching
  frame's `scene_frame` and `scene_match_at`, and no frame sidecar appears as its own hit

#### Scenario: Cross-lane fusion counts the video once

- **WHEN** the same video matches both via its own CLIP vectors and via a frame's OCR text
- **THEN** the collapsed candidate fuses as one video across lanes and the parent's best score
  per lane is kept

#### Scenario: Orphan frame degrades gracefully

- **WHEN** a scene frame's `parent_media` target no longer exists
- **THEN** the frame surfaces as a standalone image hit

### Requirement: Default-Off and Byte-Identical When Unset

Scene detection and frame persistence SHALL change no behavior unless `EXOMEM_VIDEO_SCENE_FRAMES`
is set. With the flag unset, video CLIP indexing SHALL be byte-identical to the current
uniform-sampling path, no files SHALL be written, and `find` output SHALL be unchanged.

#### Scenario: Flag unset leaves the video path unchanged

- **WHEN** a video is processed with `EXOMEM_VIDEO_SCENE_FRAMES` unset
- **THEN** keyframe selection, ClipIndex rows, and find results are exactly today's, and no
  `.frames/` directory is created

### Requirement: Soft-Fail Degradation

The scene-frame path SHALL soft-fail. A frame-write failure (unwritable directory, encode error)
SHALL be logged and MUST NOT prevent the video's CLIP vectors from being indexed; a total
detection failure SHALL fall back to the uniform sampler. Scene processing SHALL only run when
the CLIP path is available (`EXOMEM_DISABLE_CLIP` unset and CLIP importable). Configuration
overrides (`EXOMEM_VIDEO_SCENE_THRESHOLD`, `EXOMEM_VIDEO_SCENE_MIN_SECS`) SHALL fall back to
built-in defaults when unparseable.

#### Scenario: Frame write fails but vectors persist

- **WHEN** writing scene JPEGs fails while scene frames are enabled
- **THEN** the failure is logged, the video's scene-aware CLIP vectors are still upserted, and
  the worker does not raise

#### Scenario: Unparseable tuning value falls back

- **WHEN** a scene tuning env var is set to a non-numeric value
- **THEN** the built-in default is used and processing proceeds

### Requirement: Idempotent Backfill

`backfill-media` SHALL, when scene frames are enabled, regenerate scene frames and scene-aware
vectors for videos that lack persisted frames (including videos indexed by the earlier uniform
sampler), and SHALL skip videos whose frames already exist. Dry-run output SHALL name the
pending scene work.

#### Scenario: Legacy video is upgraded once

- **WHEN** `backfill-media` runs twice with scene frames enabled over a video indexed by the old
  uniform sampler with no persisted frames
- **THEN** the first run writes frames and replaces the video's ClipIndex rows with scene-aware
  ones, and the second run makes no changes

