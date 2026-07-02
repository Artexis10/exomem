# Tasks — make diarization first-class

- [x] 1. `_env_flag` truthy parse in `extract.py`; adopt in `_diarize_enabled` and
      `_vision_caption_enabled` (falsy: unset/`""`/`0`/`false`/`no`/`off`, case-insensitive).
- [x] 2. `log_diarization_readiness(vault_root)` in `extract.py` (never raises; WARNING when
      enabled with missing sidecar venv or HF token, else INFO; token as boolean only); call it
      from `MediaWorker.start()`; bump `_run_diarization`'s venv-missing log DEBUG → WARNING.
- [x] 3. Backfill rediarize: factor `_extracted_engine` out of `_ocr_done`; add
      `_needs_rediarize` (A/V only, completed ASR engine, no `+diarized`); `rediarize=` param
      with disabled-flag guard + soft-fail circuit-breaker; `BackfillStats.rediarized`; pass
      `speakers=res.speakers` to `update_sidecar_extraction` (bug fix); `--rediarize` on the
      `backfill-media` CLI.
- [x] 4. Tests: `test_backfill.py` (rediarize happy path incl. `speakers:` frontmatter,
      idempotent second run, non-A/V + pending/failed skipped, disabled-flag guard, soft-fail
      leaves sidecar untouched and stops, CLIP not re-run, dry-run counts);
      `test_extract.py` (`_env_flag` sweep, readiness content/levels/no-token-leak/never-raises);
      `test_diarizer_sidecar.py` (venv-missing WARNING); `test_media_worker.py` (start() logs
      readiness).
- [x] 5. Full suite green (989 passed, 8 optional-dep skips); `ruff check` clean on all touched
      files; `openspec validate make-diarization-first-class --strict` passes.

Hardening follow-ups from the live smoke (2026-07-02):

- [x] 6. Soft-fail boundary guard: wrap the `_diarize` call in `_transcribe` so ANY exception in
      the optional layer degrades to the plain transcript with a WARNING (a mid-run source
      change escaped via `_diarize`'s unguarded `speaker_assignment` import, violating the
      Soft-Fail Degradation requirement). Test: `_diarize` raising → plain transcript + warning.
- [x] 7. Thread `vault_root` through `extract_text → _transcribe → _diarize →
      _resolve_named_labels` and pass it from `MediaWorker._run_extraction` and
      `backfill_media` — a CLI back-fill run with only `--vault` no longer silently degrades to
      anonymous because `EXOMEM_VAULT_PATH` wasn't exported (env resolution stays as fallback).
      Tests: attribution uses the explicit vault_root without consulting env; worker and
      backfill pass their vault. (Suite: 1106 passed, 11 optional-dep skips.)
