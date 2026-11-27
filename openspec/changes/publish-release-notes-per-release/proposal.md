## Why

Each Evermatter release already turns its user notes into a title and one plain sentence per change, but only the cell image carries them, and only for the 20 newest releases. A person who reads a GitHub release page sees the technical changelog and nothing a non-programmer can read. A monthly digest in another service cannot read a whole month of notes from the images either.

## What Changes

- Every published release attaches `release-notes.json`. It holds only that release's notes, in the existing notes file format (`schema` 2, `src/exomem/release_notes_format.py`), with at most one entry: this release's version, its date and its notes. A release with no notes attaches `{"schema": 2, "releases": []}`, so a missing file always means the notes are missing, never that there are none. `released_on` is the date of the release tag; a consumer places a release in a month by its GitHub publish time. The notes are read from the release tag's tree and the file is written by the code of the run that builds it (a run on main has main's code, a run on a release tag that tag's), so a healed file has that run's schema.
- The notes publish in their own job, `release-notes`, that no publish, image or deploy job waits for. Every release is attempted, and a later real run heals one whose attempt failed; that is all "required" means here. A failure turns that job and the workflow run red and stops no package, image or cell rollout. A red run counts toward the plan's failed hand-off runs for its tag only when it is a dispatch run on that tag, which a push starts only when the tag misses another artifact. The release plan does not count the file.
- Every real run of the release workflow gives its notes to each release published since the start of the previous UTC month that lacks the file, built from that release's tag. It reads the release list 100 a page until a page holds only releases published before that start (the list is in version order, not publish order). One run handles at most 40 releases, the one a dispatch names first and then the oldest first, so the first run after a merge stays inside the repository's shared GitHub token budget; the next run takes the rest. A release a failed run left without notes, and a release that predates this change, heal the same way. The `tag` input of a real `workflow_dispatch` run joins the set whatever its age, so an operator can heal one named older release. A release with no asset yet is left alone: the release controller reads its page to write the changelog, and publication starts only after that. Only a run that is not a dry run and runs on `main` or on the release tag writes to GitHub; a dispatch from any other ref builds the notes of its own tag and writes nothing, because the upload path never replaces a published file and unreviewed code must not leave one. Runs that write share one concurrency group that the others do not use.
- The file uploads through `release_recovery.py`, so it is added only when missing and never replaces a published file with other bytes. That refusal fails the notes job and stops no other job.
- The release page body starts with a "What's new" section above the technical changelog: each note's title, then its sentence. A release with no notes gets no section. A marker comment makes the section appear once, and a job writes it whenever the marker is absent from a page it processes. Note text is escaped so that the page shows no mention, issue, pull request or commit reference, autolink, math or emphasis from it.
- The file's format becomes a published contract with its readers: any change raises `schema`, and a reader accepts a new schema before a release emits it. A test pins the key sets, so a change to them fails until `schema` changes.

## Depends on

The active change `tell-users-whats-new` (capability `user-release-notes`) defines how a note is written, which release a note belongs to (the first that contains it) and the notes file format. This change depends on it, reuses its generator and format module, and restates none of it. It does not edit that change.

## Capabilities

### Modified Capabilities

- `software-release`: each release publishes its own notes file, off the release's critical path, and starts its page with What's new.

## Impact

- Workflow: `.github/workflows/release-please.yml` (a new `release-notes` job).
- Scripts: `scripts/release_notes_asset.py` (new), `infra/scripts/release_recovery.py` (an `upload-file` command), `scripts/user_notes.py` (`generate` accepts the revision to read).
- Docs: `docs/release.md`, and the format module's docstring names the outside reader.
- Users: a release page starts with a short list of what changed for them.
- Consumers: the monthly digest in Substrate reads this file; its change names the same shape.
- Rollback: revert the change. Published files and page sections stay as they were.
