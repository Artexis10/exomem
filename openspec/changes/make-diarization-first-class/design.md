# Design — make diarization first-class

## Rediarize lives on `backfill-media`, not a new subcommand

Backfill already owns the "deliberate one-shot pass over content that predates a feature" role,
is idempotent, and has the sidecar/engine parsing. A `--rediarize` flag composes with the
existing walk (ordering, skip-dirs, dry-run) instead of duplicating it. The done-marker is the
engine's `+diarized` suffix — no new frontmatter or state file.

## Guard: `--rediarize` without `KB_MCP_DIARIZE` is a logged no-op

Re-extracting without the flag would burn GPU-minutes per recording to produce byte-identical
plain transcripts. The guard logs and disables rediarize (dry-run included, so counts never
promise work a real run wouldn't do). It does not error: the rest of backfill (sidecars, OCR,
CLIP) still runs.

## Circuit-breaker: soft-failed diarization never rewrites the sidecar

`extract_text` soft-fails diarization by design (spec: Soft-Fail Degradation), so a rediarize
result may come back WITHOUT `+diarized` (sidecar venv gone, model gate, timeout). Writing it
would replace the sidecar with an equivalent plain transcript — pointless re-embedding and a
lying mtime. Instead: leave the sidecar bytes untouched, log once, and disable rediarize for the
remainder of the pass (the stack is broken; every further attempt would fail the same way).
Mirrors the existing `do_ocr = False` / `do_clip = False` degradation pattern.

## Readiness is a log line, not a health endpoint

The failure mode this fixes is "silently broken for days" — a boot-time WARNING in the service
log is the cheapest artifact that fixes it, and the log is already the deploy-triage surface
(CLAUDE.md connector triage). No new endpoint, no doctor coupling. The HF token is reported
`hf_token=True|False` only; the value never enters the format string. `enabled=False` boxes log
at INFO so lean installs aren't nagged.

## `_env_flag` scope: extract.py's two flags only

`KB_MCP_VIDEO_SCENE_FRAMES` (embeddings.py) and `KB_MCP_IMAGE_TAGS` (image_tags.py) share the
presence-check pattern but are out of scope here — flipping them belongs to their own capability
specs. The helper is module-local (`extract._env_flag`); if a third consumer appears it can move
to a shared util then.
