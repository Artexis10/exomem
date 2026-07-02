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
