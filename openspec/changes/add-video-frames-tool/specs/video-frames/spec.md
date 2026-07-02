## ADDED Requirements

### Requirement: Inline Video Keyframe Retrieval

The system SHALL provide a read-only MCP tool `get_video_frames` that, given a
vault-relative video path, returns sampled keyframes INLINE in the tool result as MCP
image content blocks (JPEG), preceded by one structured metadata block reporting the
resolved path, the video duration when known, and each returned frame's index and
timestamp in seconds. Frame retrieval SHALL NOT require any HTTP fetch by the client.
Frames SHALL be sampled evenly across the requested span, near-duplicate frames SHALL be
collapsed by perceptual hashing, and image block order SHALL match the metadata's frame
order.

#### Scenario: Client analyzes a video without downloading it

- **WHEN** a connected MCP client calls `get_video_frames` with a vault-relative path to
  an indexed or unindexed video file
- **THEN** the tool result contains a metadata block plus one image content block per
  sampled frame, each with a timestamp, and the client needs no `/download` round-trip

#### Scenario: Static video collapses to fewer frames

- **WHEN** the sampled candidates are visually near-identical
- **THEN** near-duplicates are collapsed, fewer frames than `max_frames` may return, and
  the metadata reports how many candidates were dropped by deduplication

### Requirement: Bounded Payload

The tool SHALL bound its payload: `max_frames` defaults to 8 and is clamped to
`[1, cap]` where the cap defaults to 16 and MAY be overridden by
`EXOMEM_VIDEO_FRAMES_TOOL_CAP` (unparseable or non-positive values fall back to the
default cap). Clamping SHALL be silent, with the effective value reported in the
metadata. Returned JPEGs SHALL be downscaled to a bounded longest edge (768px) at fixed
quality; encoding bounds SHALL NOT be client-negotiable parameters.

#### Scenario: Excessive request clamps to the cap

- **WHEN** a client calls the tool with `max_frames=999`
- **THEN** at most 16 frames return and the metadata reports the effective maximum

#### Scenario: Oversized source frames are downscaled

- **WHEN** the video's frames exceed 768px on their longest edge
- **THEN** every returned JPEG's longest edge is at most 768px

### Requirement: Windowed Sampling

The tool SHALL accept optional `start_sec` and `end_sec` to confine sampling to a time
window, enabling overview-then-drill-down analysis. Returned timestamps SHALL fall inside
the clamped window. Invalid windows (`start_sec < 0`, `end_sec ≤ start_sec`, or
`start_sec` at or past the video's end) SHALL fail with `BAD_RANGE`. When the video's
duration is unknown, an unwindowed call SHALL fall back to sequential first-frames
sampling, and a windowed call SHALL fail structurally with a hint to retry without a
window.

#### Scenario: Drill-down around a find hit

- **WHEN** a client re-calls the tool with `start_sec`/`end_sec` bracketing a timestamp
  surfaced by `find`
- **THEN** all returned frames' timestamps lie within that window

#### Scenario: Window past the end of the video

- **WHEN** `start_sec` is at or beyond the video's duration
- **THEN** the call fails with `BAD_RANGE` naming the video's duration

### Requirement: Vault Confinement and Typed Errors

The tool SHALL resolve paths with the canonical vault containment helper: paths escaping
the vault root fail with `INVALID_PATH`, missing files with `NOT_FOUND`, and paths whose
extension is not a known video type with `NOT_A_VIDEO`. Corrupt containers, streamless
files, and total decode failure SHALL fail with `NO_DECODABLE_FRAMES`. All failures SHALL
surface as structured `CODE: reason` tool errors, never unhandled tracebacks.

#### Scenario: Path traversal is refused

- **WHEN** a client calls the tool with a path containing `..` that escapes the vault
- **THEN** the call fails with `INVALID_PATH` and no filesystem access occurs outside the
  vault root

#### Scenario: Non-video file is refused

- **WHEN** a client calls the tool on a markdown page or image file
- **THEN** the call fails with `NOT_A_VIDEO`

### Requirement: Soft-Fail Without Media Dependencies

The tool SHALL always be registered — no environment gate — and SHALL soft-fail with
`VIDEO_DEPS_MISSING` naming the missing dependency when PyAV or Pillow is absent.
On-demand frame decoding SHALL NOT be gated by `EXOMEM_DISABLE_MEDIA_EXTRACTION` (which
governs background ingestion) nor by `EXOMEM_VIDEO_SCENE_FRAMES` (which governs
ingestion-time frame persistence).

#### Scenario: Lean install degrades structurally

- **WHEN** the tool is called on a server installed without the media extra
- **THEN** the call fails with `VIDEO_DEPS_MISSING` naming the missing package, and the
  tool remains listed in the MCP tool set

#### Scenario: Ingestion switches do not affect the tool

- **WHEN** `EXOMEM_DISABLE_MEDIA_EXTRACTION` is set and `EXOMEM_VIDEO_SCENE_FRAMES` is
  unset
- **THEN** `get_video_frames` still decodes and returns frames normally

### Requirement: MCP-Only Surface

The tool SHALL be exposed on the MCP surface only: no REST route, CLI subcommand, or
OpenAPI path SHALL be generated for it, and existing REST/CLI behavior SHALL be
byte-identical. The tool SHALL carry a read-only annotation and SHALL be
registry-generated (docstring-as-description, signature-as-schema) like other commands.

#### Scenario: REST and CLI surfaces are unchanged

- **WHEN** the REST facade's OpenAPI document and the CLI's subcommand list are generated
- **THEN** neither contains `get_video_frames`, and all previously existing entries are
  unchanged
