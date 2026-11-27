## 1. Recognition and pairing

- [ ] 1.1 Add the `google-photos-sidecar` entry (`parser: photo-sidecar`, `*.json`, `item_keys`, the suffix word, the media-name limit, the replaced characters) and the JSON-object container (at most 1 MiB). Pair in one budgeted, resumable pass per stage with one state entry and one row per sidecar, one to one by name and title. Verify through the filer, with the name forms that TakeoutExtractor and GPTH Neo document: cut sidecar suffixes (`.supplemental-me.json`, `.suppl(1).json`), a cut media name, `&` replaced by `_`, copy numbers in both places, two sidecars fitting one photo, an album `metadata.json`, a sidecar over 1 MiB.
- [ ] 1.2 Count each sidecar under one `left_out` reason, list the unpaired ones, and keep sidecar rows out of the stage total. Verify: the album, missing-photo and re-import scenarios; the filing view reads done.

## 2. Dates and captions

- [ ] 2.1 Add `export-sidecar` to `capture-date-sources.yaml`, last among the evidence entries, and a closed attribute `reread: false` on `matched-original` and `export-sidecar`. Make `capture_dates.survives_extraction` read that attribute instead of naming `perceptual-match`. Verify: a re-extraction, a plain capture-metadata run and a recovery run each keep an `export-sidecar` date; a file-name time replaces it.
- [ ] 2.2 At filing, write a paired photo's time into its capture record (`capture_date_source: export-sidecar`, `capture_date_basis: utc`, `capture_date_matched_from` set to the sidecar's export path), unless the time equals `creationTime` to the second. Put a non-empty description into the description section through the upload's redaction. If the photo's sidecar page does not exist at filing, stop and report. Verify through the filer and the media worker: a no-EXIF JPEG gets the sidecar date; an EXIF JPEG keeps its own; an upload-time sidecar gives no date; a caption is found by search; a card number in a caption is redacted.
- [ ] 2.3 Update the skill scaffold's `references/vault-care.md` capture-date section with the new source and its rank.

## 3. Delivery checks

- [ ] 3.1 Add a `user-notes/` entry. Run the scoped suites, the guard modules in `tests/pr_guard_modules.txt` and the read-surface test.
- [ ] 3.2 Before the release note goes out, file one real Google Photos Takeout from an owner's own account, in its default 2 GB parts, into a sandbox cell. Count each sidecar reason and the unpaired share caused by split parts. On photos that state no time, compare the sidecar time with known truth (file-name times, matched originals from the recovery preview) and record the agreement rate in the design.
- [ ] 3.3 Archive this change after `import-notes-and-mail-exports` and `recover-media-capture-dates`.
