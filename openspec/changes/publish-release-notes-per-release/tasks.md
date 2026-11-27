## 1. Build the notes file and the page section

- [x] 1.1 Build the one-entry file for a release from `generate`, read at the release's own tag, so the notes and date match the image's notes.
- [x] 1.2 Build the "What's new" section from the notes file, escaped for GitHub, with a marker so a second run changes nothing.

## 2. Publish through the release workflow

- [x] 2.1 Add the `release-notes` job: after `build-artifacts`, needed by no other job, one run at a time.
- [x] 2.2 Check the 20 newest published releases that have assets and lack the file, build each from its own tag and publish it; a dry run builds its own tag and writes nothing.
- [x] 2.3 Write the section before the upload, upload through `release_recovery.py upload-file`, and let one release's failure leave the others to finish before the job fails.
- [x] 2.4 Describe the job, the asset and the page section in `docs/release.md`; name the outside reader in the format module.

## 3. Acceptance

- [ ] 3.1 The next release carries `release-notes.json` and its page starts with "What's new".
- [ ] 3.2 A run again on that release uploads nothing and leaves one section on the page.
