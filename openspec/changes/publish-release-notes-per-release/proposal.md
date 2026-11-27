## Why

Each Evermatter release already turns its user notes into a title and one plain sentence per change, but only the cell image carries them, and only for the 20 newest releases. A person who reads a GitHub release page sees the technical changelog and nothing a non-programmer can read. A later monthly digest cannot read a whole month of notes from the images either.

## What Changes

- Every published release attaches `release-notes.json`. It holds only that release's notes, in the existing notes file format (`schema` 2, `src/exomem/release_notes_format.py`), with at most one entry: this release's version, its date and its notes. A release with no notes attaches `{"schema": 2, "releases": []}`, so a missing file always means the notes are missing, never that there are none.
- The file is a required release step. A failure to build or upload it fails the release job. It uploads through `release_recovery.py upload`, so a rerun uploads it only when it is missing and never replaces a published file.
- The release plan counts the file when it decides whether a release is complete, so a later push or dispatch heals a release that lacks it. Releases cut before this change never carried it and stay complete.
- The release page body starts with a "What's new" section above the technical changelog: each note's title, then its sentence. A release with no notes gets no section. A marker comment makes the write happen once.
- A release's notes are exactly the entry that `scripts/user_notes.py generate` already gives that release, with the same date.

## Capabilities

### Modified Capabilities

- `software-release`: each release publishes its own notes file and starts its page with What's new.

## Impact

- Workflow: `.github/workflows/release-please.yml` (the `build-artifacts` job).
- Scripts: `scripts/release_notes_asset.py` (new), `infra/scripts/release_recovery.py` (`upload` and `assets`). `scripts/user_notes.py` and the format module stay as they are.
- Docs: `docs/release.md`.
- Users: a release page starts with a short list of what changed for them.
- Consumers: the monthly digest in Substrate reads this file; its change names the same shape.
- Rollback: revert the change. Published files and page sections stay as they were.
