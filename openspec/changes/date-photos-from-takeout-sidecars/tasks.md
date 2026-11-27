## 1. Recognition and pairing

- [ ] 1.1 Add the `google-photos-sidecar` entry (`parser: photo-sidecar`, `*.json`, `item_keys: [title, photoTakenTime]`) and the parser: one JSON object up to 1 MiB, paired by `title` and copy number within its folder, one record row per sidecar. Verify through the filer: the album, duplicate-name and missing-photo scenarios; an album `metadata.json` files as an ordinary file; a 46-character cut name pairs.
- [ ] 1.2 Count a paired sidecar in `left_out` with reason `IMPORT_SIDECAR_APPLIED`. Verify: the filing view's counts for the album scenario; a re-import of the same Takeout skips the photos and files no JSON Source.

## 2. Dates and captions

- [ ] 2.1 Add `export-sidecar` to `capture-date-sources.yaml` after `camera-exif`, and the page fields `stated_capture_time` (`source: export-sidecar`) and `caption` (`source: item-caption`) to `page-fields.yaml`. Filing writes them on the paired photo's Source page; the time is skipped when it equals `creationTime` to the second.
- [ ] 2.2 Make the media worker's capture step read `stated_capture_time` after the file's own facts and before the file name, and keep an `export-sidecar` date through re-extraction and recovery. Verify through the filer and the media worker: a no-EXIF JPEG gets the sidecar date; an EXIF JPEG keeps its own; an upload-time sidecar gives no date; a re-extraction keeps the date; a caption is found by search.

## 3. Delivery checks

- [ ] 3.1 Add a `user-notes/` entry. Run the scoped suites, the guard modules in `tests/pr_guard_modules.txt` and the read-surface test.
- [ ] 3.2 Before the release note goes out, file one real Google Photos Takeout from an owner's own account into a sandbox cell, with a duplicate-name pair and a long name. Count the paired, unpaired and time-skipped sidecars, and check that the dates match Google Photos.
- [ ] 3.3 Archive this change after `import-notes-and-mail-exports` and `recover-media-capture-dates`.
