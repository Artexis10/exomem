# Proposal — on-demand inline video frames tool

## Why

Nothing in the tool surface can show Claude what a video looks like. `find` can say
`clip_match_at: "14:32"` (and, with scene frames enabled, name a persisted `scene_frame`
path), but every route to the pixels goes through `/download` — and connector-sandbox HTTP
egress is unreliable in practice (documented failure: the claude.ai sandbox intermittently
cannot fetch minted download URLs, leaving Claude unable to analyze a vault video at all).
The `video-scene-frames` capability does not close this gap either: it is ingestion-time,
default-off, requires the CLIP path, and its persisted frames are still fetched via
`/download`.

The durable fix is in-band: a read-only MCP tool that resolves a vault-relative video path
server-side, samples keyframes with the decode primitives the server already has, and
returns the frames INLINE as MCP image content blocks — no minted URL, no sandbox egress,
works on any vault video regardless of ingestion gates.

This stays **pure-substrate**: seek-decode, perceptual-hash near-dup suppression, and JPEG
transcoding are deterministic measurement — no model runs, no reasoning. No new dependency:
PyAV, Pillow, and numpy are already in the stack (media extra).

## What Changes

- **New MCP tool `get_video_frames(path, max_frames=8, start_sec=None, end_sec=None)`** —
  registry-driven (`_SPEC` row), read-only, tier 2, **mcp-only surface** (a new
  `frozenset({"mcp"})`, precedent: `note`'s `_RC` subset). Returns a FastMCP `ToolResult`:
  one JSON text/structured block of metadata (`path`, `duration_sec`, per-frame
  `timestamp_sec`, candidate/dedup/cap counters) followed by one `ImageContent` block per
  frame. First tool in the server to return image content — the mechanism
  (`fastmcp.utilities.types.Image(...).to_image_content()`, `ToolResult` passthrough) is
  native FastMCP; `bind_vault` forwards return values untouched.
- **New backend `src/exomem/video_frames.py`** — typed `VideoFramesError(code, reason)`,
  `get_frames()`: `resolve_under_vault` (must_exist, must_be_file) → extension check via
  `extract.media_type_for` → duration probe → evenly-spaced seek-decode through the existing
  `embeddings._decode_frames_at` (window-clamped when `start_sec`/`end_sec` are given;
  `embeddings._sample_video_keyframes` supplies the unknown-duration fallback) →
  `embeddings._dedup_keyframes` → uniform subsample to the cap → bounded JPEG encode.
  `embeddings.py` itself is untouched.
- **Bounded payload.** Default 8 frames, hard cap 16 (`EXOMEM_VIDEO_FRAMES_TOOL_CAP`
  overrides), out-of-range `max_frames` clamps silently with the effective value reported.
  JPEG longest side ≤768px, quality 80 — fixed server policy, not tool params. Worst case
  ≈16 × ~450 tokens — comparable to a large `find` payload; beyond that the client should
  drill down with a time window instead.
- **Always-on, soft-fail.** No env gate: the tool is on-demand pure decode, so it does not
  ride `EXOMEM_DISABLE_MEDIA_EXTRACTION` (which governs the background ingestion worker) nor
  `EXOMEM_VIDEO_SCENE_FRAMES` (ingestion-time persistence). When PyAV/Pillow are absent the
  tool stays registered and fails structurally with `VIDEO_DEPS_MISSING` naming the missing
  extra. Corrupt/streamless files yield `NO_DECODABLE_FRAMES`; bad windows yield `BAD_RANGE`.
- **No new dependency.**

Out of scope (future changes): preferring persisted `.frames/` scene JPEGs as a zero-decode
fast path when `video-scene-frames` has run; an explicit `timestamps: list[float]` param
(windowed calls cover the drill-down loop); non-video media (stills are already covered by
`get`/CLIP/OCR).

## Capabilities

### New Capabilities

- `video-frames`: on-demand, read-only retrieval of sampled video keyframes as inline MCP
  image content with timestamps — bounded payload, vault-confined, soft-fail without the
  media extra, pure-substrate (seek-decode + hash dedup + JPEG transcode; no model, no new
  dependency).

## Impact

- Code: `src/exomem/video_frames.py` (new backend); `src/exomem/commands.py`
  (`op_get_video_frames` leaf, mcp-only surface set, `_SPEC` row, fastmcp imports —
  measured incremental import cost 0.02s since `mcp.types` already pays for pydantic);
  `tests/fixtures/mcp_tool_schemas.json` (regenerated — one added tool);
  `tests/test_video_frames.py` (new).
- Deps: none added.
- Existing tools, REST facade, and CLI are byte-identical — the mcp-only surface never
  materializes a REST route, CLI subcommand, or OpenAPI path.
- Worker budget: none — decode happens on the request thread, bounded at ≤32 O(1) seeks
  (2× the frame cap) plus one duration probe.
