## 1. Build the notes file and the page section

- [x] 1.1 Build the one-entry file for a release from `generate`, so the notes and date match the image's notes.
- [x] 1.2 Build the "What's new" section from the notes file, with a marker so a second run changes nothing.

## 2. Publish through the release job

- [x] 2.1 Build `release-notes.json` in `build-artifacts` after the Cloud archives, on a clone with the full history and tags, also in a dry run.
- [x] 2.2 Write the section to the release page before the upload, in a real run only, so the file's presence shows that the page was written.
- [x] 2.3 Upload the file through `release_recovery.py upload`, which fails when the file is missing, and count it in the release plan's asset check for a tag that carries its builder.
- [x] 2.4 Describe the asset and the page section in `docs/release.md`.

## 3. Acceptance

- [ ] 3.1 The next release carries `release-notes.json` and its page starts with "What's new".
- [ ] 3.2 A run again on that release uploads nothing and leaves one section on the page.
