## Why

The live deploy smoke for named-speaker diarization surfaced three gaps that make the capability
second-class in practice:

1. **No reprocess path.** Recordings transcribed before diarization was enabled keep their plain
   transcript forever: `backfill._ocr_done()` counts any real engine as done, and the worker's
   startup scan only re-enqueues `extracted_by: pending`. The live vault holds recordings with
   `extracted_by: faster-whisper:large-v3` and no `speakers:` — unreachable by any command.
2. **Breakage is invisible.** Every diarization failure soft-fails to the plain transcript by
   design, but nothing reports readiness: a missing sidecar venv logs at DEBUG, a missing HF token
   or empty profile store logs nothing. The live stack was silently broken for days.
3. **`KB_MCP_DIARIZE=0` enables diarization.** The gate is a presence check
   (`bool(os.environ.get(...))`), so `0`/`false` opt IN. Same bug class in `KB_MCP_VISION_CAPTION`.

Plus a latent bug found during triage: `backfill.py` calls `update_sidecar_extraction` without
`speakers=`, so even a successful diarized extraction through backfill would drop the `speakers:`
frontmatter that `find(speakers=[…])` filters on.

## What Changes

- `backfill-media --rediarize`: re-extract audio/video sidecars whose engine is a completed ASR
  run without the `+diarized` marker, so pre-diarization recordings gain labeled turns and
  `speakers:` frontmatter. Idempotent (`+diarized` is the done-marker); guarded (no-op with a
  message when `KB_MCP_DIARIZE` is off); circuit-breaks when diarization soft-fails mid-pass
  (never rewrites a sidecar with an identical plain transcript); never touches non-A/V media or
  re-runs CLIP.
- Startup readiness diagnostics: one log line at media-worker start —
  `diarization readiness: enabled=… sidecar_venv=… hf_token=… profiles=N (names)` — WARNING when
  the flag is on but a prerequisite is missing, else INFO. Token reported as a boolean only. The
  sidecar-venv-missing soft-fail in `_run_diarization` moves DEBUG → WARNING.
- Truthy env parse for opt-in flags: `""`/`0`/`false`/`no`/`off` (case-insensitive) now read as
  OFF for `KB_MCP_DIARIZE` and `KB_MCP_VISION_CAPTION` (strictly tightens default-off).
- Bug fix: backfill's `update_sidecar_extraction` call now passes `speakers=`.

Pure-substrate intact: no new model or judgment — the same measured extraction, now reachable,
observable, and correctly gated.

## Capabilities

### Modified Capabilities
- `speaker-diarization`: pre-diarization media becomes re-processable (`--rediarize`), the stack's
  readiness is observable at startup, and the opt-in gate parses falsy values as OFF.

## Impact

- Code: `src/kb_mcp/backfill.py` (`_extracted_engine`, `_needs_rediarize`, `rediarize=` param,
  `BackfillStats.rediarized`, `speakers=` fix), `src/kb_mcp/extract.py` (`_env_flag`,
  `log_diarization_readiness`, WARNING on unprovisioned sidecar), `src/kb_mcp/media_worker.py`
  (readiness call in `start()`), `src/kb_mcp/__main__.py` (`--rediarize` flag).
- Behavior: default runs unchanged — `--rediarize` is opt-in, the readiness line is log-only, and
  the env parse only turns OFF configurations that were misconfigured ON.
