# Tasks — on-demand inline video frames tool

## 1. Backend tests first (no PyAV/PIL needed — monkeypatch the decode seam)

- [x] 1.1 `tests/test_video_frames.py` error paths against `video_frames.get_frames`:
      `../escape.mp4` → `INVALID_PATH` (template: `tests/test_tier2.py`
      path-escape guard); missing file → `NOT_FOUND`; a `.md` page → `NOT_A_VIDEO`;
      decode seam raising `ClipUnavailable` → `VIDEO_DEPS_MISSING`; seam returning
      all-`None`/empty → `NO_DECODABLE_FRAMES`; `start_sec=-1` / `end_sec <= start_sec` /
      `start_sec` past probed duration → `BAD_RANGE`; unknown duration + window →
      `NO_DECODABLE_FRAMES` with the retry hint.
- [x] 1.2 Bounding tests: `max_frames=999` clamps to 16 with
      `max_frames_effective == 16`; `EXOMEM_VIDEO_FRAMES_TOOL_CAP` override honored and
      unparseable value falls back; timestamps evenly spaced inside `[start_sec, end_sec]`
      and strictly increasing; near-dup candidates collapse (fake frames with controlled
      `_avg_hash` behavior) and `dedup_dropped` reported; uniform subsample preserves
      order.
- [x] 1.3 JPEG encode tests behind `pytest.importorskip("PIL")`: 2000×1000 synthetic
      frame → JPEG magic bytes, longest side ≤ 768; RGBA/odd-mode input survives
      `convert("RGB")`.

## 2. Backend implementation

- [x] 2.1 `src/exomem/video_frames.py`: `VideoFramesError`, `Frame`, `FramesResult`,
      `_tool_frames_cap()` (env override), `_probe_duration()`, `_encode_jpeg()`,
      `get_frames()` composing `resolve_under_vault` → `media_type_for` →
      probe → `embeddings._decode_frames_at` (windowed midpoints, 2×cap candidates) /
      `embeddings._sample_video_keyframes` (unknown-duration fallback) →
      `embeddings._dedup_keyframes` → uniform subsample → encode. 1.x green.

## 3. MCP wiring tests, then wiring

- [x] 3.1 MCP-level tests (harness: `tests/test_consolidated_tools.py` `_build`/`_call`):
      monkeypatch `commands.video_frames_module.get_frames` to a canned `FramesResult`
      with fake JPEG bytes → `call_tool("get_video_frames", ...)` returns
      `content[0]` TextContent whose JSON equals `structured_content`, then N
      `ImageContent` blocks with `mimeType == "image/jpeg"` and base64 round-tripping to
      the fake bytes, ordered by `frames[].index`; error path `../escape.mp4` raises
      `ToolError` carrying `INVALID_PATH`; tool absent from `commands_for("rest")` /
      `commands_for("cli")`; `readOnlyHint` true.
- [x] 3.2 `src/exomem/commands.py`: imports (`json`, `TextContent`, `ToolResult`,
      `fastmcp.utilities.types.Image`, `video_frames_module`), `op_get_video_frames`
      leaf with full Google-style docstring (summary, Args, Returns, Errors),
      `_M = frozenset({"mcp"})`, `_SPEC` row
      `("get_video_frames", op_get_video_frames, 2, False, False, None, _M)`. 3.1 green.
- [x] 3.3 Regenerate `tests/fixtures/mcp_tool_schemas.json` via
      `scripts/dump-tool-schemas.py`; diff shows only the added tool;
      `tests/test_mcp_schema_fidelity.py` green.

## 4. Integration (skippable — dev venv lacks the media extra)

- [x] 4.1 One `pytest.importorskip("av")` test: synthesize a tiny two-color mp4
      (`av.open(..., "w")`, mpeg4, 64×64, ~4s), assert default call returns ≥2 frames
      with in-range timestamps and window `start_sec`/`end_sec` confines them.

## 5. Verify

- [x] 5.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 python -m pytest -q` green
      (via `uv run`).
- [x] 5.2 `ruff check` clean; leak guard green (all labels generic).
- [x] 5.3 `openspec validate add-video-frames-tool --strict` passes.
- [ ] 5.4 Desk-side smoke (media extra, live service): call `get_video_frames` from a
      connected claude.ai session on a real vault video → frames render inline;
      windowed drill-down around a `find` `clip_match_at` timestamp works.
      **(Hugo runs — needs the live connector.)**
