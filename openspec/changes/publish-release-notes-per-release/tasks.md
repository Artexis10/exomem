## 1. Build the notes file and the page section

- [x] 1.1 Build the one-entry file for a release from `generate`, read at the release's own tag, so the notes and date match the image's notes.
- [x] 1.2 Build the "What's new" section from the notes file, escaped for GitHub, with a marker so a second run changes nothing.

## 2. Publish through the release workflow

- [x] 2.1 Add the `release-notes` job: after `build-artifacts`, needed by no other job, one run at a time.
- [x] 2.2 Check every release published since the start of the previous UTC month (the list paged 100 at a time, to the first page that is all older) and the tag a dispatch names; publish the notes of at most 40 that have assets and lack the file, built from their tags, the named one first and then the oldest; a run that writes nothing builds its own tag only.
- [x] 2.3 Let only a real run on main or the release tag write to GitHub: the plan's `notes_publish` output decides, and a test pins it.
- [x] 2.4 Write the section before the upload, upload through `release_recovery.py upload-file`, and let one release's failure leave the others to finish before the job fails.
- [x] 2.5 Describe the job, the asset and the page section in `docs/release.md`; name the outside reader in the format module.
- [x] 2.6 Pin the format's key sets by schema in a test, so a key change edits the current schema's entry.

## 3. Acceptance

- [ ] 3.1 The next release carries `release-notes.json` and its page starts with "What's new".
- [ ] 3.2 A run again on that release uploads nothing and leaves one section on the page.
- [ ] 3.3 The section on the first real release page shows no link from note text (check after merge).

## 4. Follow-up from the review of the first delivery

- [x] 4.1 A real run on a release tag serves its own tag alone, when it lacks the file, and never sweeps; the sweep runs only on main, and a dispatch from main still names one older tag with `--also`. Evidence: `missing --only TAG` in `scripts/release_notes_asset.py`, the scan step's `GITHUB_REF` branch in `release-please.yml`, `test_a_real_run_on_a_release_tag_publishes_the_notes_of_its_own_tag_and_never_sweeps` (red on the tag case before the workflow change: `tags=missing --also v1.2.3`; green after) and `test_only_serves_the_named_release_and_reads_no_release_list` (red before: the script rejected `--only`).
- [x] 4.2 The first sentence of "Publishing the notes never gates the release" says a later real run on `main` heals a failed release in its window or one that a dispatch names.
- [x] 4.3 The workflow comment and the dispatch requirement say a named dispatch tag can be dropped when a newer real run replaces the waiting one, and that the operator dispatches again when the job shows cancelled. Evidence: the concurrency comment in `release-please.yml`, the spec's dispatch requirement and its new scenario, `docs/release.md`.
- [x] 4.4 The proposal says the key-set pin makes a key change visible in review as an edit to that schema's key set, and no longer says a change fails until `schema` changes.
