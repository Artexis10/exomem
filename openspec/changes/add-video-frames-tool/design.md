# Design — on-demand inline video frames tool

## D1. Inline image content, not another URL

The whole point is removing the HTTP hop: connector-sandbox egress failing is the documented
production failure, so `mint_download_token`-style indirection is exactly the wrong shape.
The tool returns a FastMCP `ToolResult` whose `content` is one `TextContent` (the metadata
JSON, duplicated as `structured_content` for schema-aware clients) followed by one
`ImageContent` per frame (`fastmcp.utilities.types.Image(data=jpeg, format="jpeg")
.to_image_content()`). Verified in the installed fastmcp 3.3.1: `Tool.convert_result`
passes a `ToolResult` through untouched, and annotating the leaf `-> ToolResult` suppresses
output-schema generation, so the schema-fidelity fixture only gains the new tool's
description + inputSchema. Image block order matches `frames[].index` in the metadata.

## D2. Registry row with an mcp-only surface (not hand-registered)

`bind_vault` is a pure forwarder — leaf return values flow through untouched — so the leaf
can live in the registry like every other op: docstring = description, signature = schema,
`cli_writes=False` → `readOnlyHint`. What CANNOT work is the REST/CLI surfaces (their
generic handlers JSON-envelope the return value, which is meaningless for a `ToolResult`),
so the row uses a new `_M = frozenset({"mcp"})` surface; `commands_for(surface)` filtering
means no REST route, CLI subcommand, or OpenAPI path ever materializes. Precedent for a
subset surface: `note` (`_RC`). Hand-registration (the `note`/`mint_*` route) would add
boilerplate for none of the reasons those exceptions exist (per-vault description; no
`vault_root`). Tier 2: an escape-hatch read alongside `query_data`/`list_directory`,
dropping out under `KB_MCP_DISABLE_TIER2`.

## D3. Sampling: reuse the scene-frames decode primitives, touch nothing

`embeddings.py` just absorbed the scene-frames change; this tool deliberately adds zero
churn there. The backend composes existing primitives:

- **Duration probe** — a ~10-line `av.open` metadata read (the same
  `stream.duration × time_base` / `container.duration` logic the samplers use), no decode.
- **Known duration** — evenly-spaced midpoint timestamps over `[start_sec, end_sec]`
  (defaulting to the full duration), `2 × max_frames_effective` candidates (≤32 decodes at
  the cap), decoded via `embeddings._decode_frames_at` — the scene-frames pass-2 seek
  decoder, O(1) per timestamp, `None` for a failed seek.
- **Unknown duration, no window** — `embeddings._sample_video_keyframes` (its internal
  first-N sequential fallback covers exactly this case).
- **Unknown duration, windowed** — structured failure (`NO_DECODABLE_FRAMES` with a
  retry-without-window hint); seeking into an unindexed stream is not meaningfully
  windowable.
- **Then** `embeddings._dedup_keyframes` (pHash near-dup collapse, static runs fold away)
  and uniform subsample to `max_frames_effective` (the `np.linspace` keep-time-order
  pattern from `embed_video_frames`).

Evenly-spaced sampling (not scene detection) is the right default for an interactive tool:
predictable coverage of the requested span, no I-frame metrics pass on the request thread,
and the client iterates by windowing — overview call, then `start_sec`/`end_sec` around the
moment of interest. Scene-representative sampling via persisted `.frames/` is a named
follow-up, not v1.

## D4. Payload bounds are server policy

Default 8 frames ≈ the CLIP ingestion budget and ≈450 tokens per 768px JPEG for Claude —
a default call costs about one large `find`. Hard cap 16 (`KB_MCP_VIDEO_FRAMES_TOOL_CAP`
env override, mirroring `KB_MCP_MAX_VIDEO_KEYFRAMES` parsing: unparseable/non-positive →
default). `max_frames` outside `[1, cap]` clamps silently and the metadata reports
`max_frames_effective` — same contract as `find`'s limit cap. JPEG bounds
(`FRAME_JPEG_MAX_EDGE=768`, `FRAME_JPEG_QUALITY=80`) are module constants, not tool
params: the payload bound is server policy, and 768px keeps slide text legible while
costing ~⅓ of the scene-frames' 1280px persistence bound (which serves OCR, a different
consumer).

## D5. Errors and soft-fail

Backend raises `VideoFramesError(code, reason)`; the leaf re-raises
`ValueError(f"{e.code}: {e.reason}")` — the house pattern (`op_get`). Codes:

| Condition | Code |
|---|---|
| empty path / escapes vault (`VaultPathError` passthrough) | `INVALID_PATH` |
| no such file | `NOT_FOUND` |
| `extract.media_type_for(path) != "video"` | `NOT_A_VIDEO` |
| PyAV/Pillow absent (`ClipUnavailable` from the decode layer, or Pillow at encode) | `VIDEO_DEPS_MISSING` |
| no video stream / all decodes fail / corrupt container / unknown-duration window | `NO_DECODABLE_FRAMES` |
| `start_sec < 0`, `end_sec ≤ start_sec`, `start_sec` past end of video | `BAD_RANGE` |

No env gate. `KB_MCP_DISABLE_MEDIA_EXTRACTION` governs the background worker (the test
suite disables it autouse — gating here would also make the tool untestable by default);
`KB_MCP_VIDEO_SCENE_FRAMES` governs ingestion-time persistence. On-demand deterministic
decode is measurement and needs neither switch; absence of the media extra degrades to a
structured error naming the extra, never an unregistered tool or an unhandled traceback.

## D6. Relationship to `video-scene-frames`

Complementary, no interaction in v1. Scene frames make video moments *findable* (OCR text,
`scene_frame` hints in `find` hits) at ingestion; this tool makes any video moment
*viewable* on demand. A `find` hit's `clip_match_at`/`scene_match_at` timestamp is the
natural `start_sec`/`end_sec` input for a drill-down call. Follow-up (out of scope):
`get_frames` may prefer persisted `.frames/` JPEGs (re-encoded to the 768px bound) as a
zero-decode fast path when they exist for the requested span.
