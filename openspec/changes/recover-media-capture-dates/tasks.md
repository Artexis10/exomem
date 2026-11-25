## 1. Sources and file names

- [x] 1.1 The `capture-date-sources` pack and the `capture-date-name-formats` registry, with the module that reads them. Evidence: `capture_dates.py`, both packs under `src/exomem/vocabulary/packs/core/`, `tests/test_capture_dates.py` (precedence, class, formats, raw-protected prefix, overlay, no mtime).

## 2. Sidecar field and ingest

- [x] 2.1 `capture_date_source` and `capture_date_matched_from` through `set_capture_fields` and `render_sidecar_extraction`; extraction stamps `camera-exif`; the file name dates a media sidecar with no stated time. Evidence: `capture_metadata.py`, `preserve.py`, `media_worker.py`, `tests/test_capture_metadata.py`.
- [x] 2.2 The upload receipt warns about an image that carries no camera metadata. Evidence: `preserve.NO_CAMERA_METADATA_WARNING`, `tests/test_capture_metadata.py`.

## 3. Search

- [x] 3.1 `page.capture_date_source` as an indexed scalar axis, the `source-date` fallback derived in `page_view`, lexstore schema 19, the hit field. Evidence: `structured_filters.py`, `lexstore.py`, `find_types.py`, `tests/test_capture_metadata_index.py` (fallback labelled, source filter, schema-18 store upgrades).

## 4. Recovery

- [x] 4.1 Perceptual matching with the unique-match rule. Evidence: `capture_match.py`, `perceptual_hash.py`, `tests/test_capture_match.py`.
- [x] 4.2 `recover` and `originals_root` on the capture-metadata maintenance job, refused on Cloud. Evidence: `capture_backfill.py`, `commands.op_maintain_memory`, `operation_entries.py`.
- [x] 4.3 Integration test on a temp vault: camera original, stripped copy, screenshot name, ambiguous pair, unrelated image, and an `originals_root` match; source and date per item, nothing written under `originals_root`, the fallback labelled in a date search. Evidence: `tests/test_capture_recovery.py`.

## 5. Documentation

- [x] 5.1 The scaffold names the full-bytes routes, the source labels and the recovery job. Evidence: `operation-routing.md`, `vault-care.md`; plugin and Cloud copies regenerated; `tests/test_scaffold_no_leak.py` passes.

## 6. After release

- [ ] 6.1 Run the recovery preview on the owner's vault and read the unmatched list; calibrate the distance and margin if the preview shows wrong refusals or wrong matches.
