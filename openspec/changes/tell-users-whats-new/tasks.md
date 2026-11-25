## 1. Notes and the check

- [x] 1.1 `user-notes/` with its README for authors and the first note.
  - Evidence: `user-notes/README.md` and `user-notes/see-what-changed-after-updates.md` (91 characters); `scripts/user_notes.py check` passes the first note through the shape rule in `tests/test_user_notes.py`.
- [x] 1.2 The PR check: a `feat`, `fix` or `perf` PR adds a `user-notes/*.md` file or carries `User note: none`; a note is one paragraph of at most 200 characters with no Markdown syntax.
  - Evidence: `scripts/user_notes.py check` and `.github/workflows/user-note.yml`; `tests/test_user_notes.py` covers feat without note or marker (fails), fix with the marker (passes), chore (passes), a changed-not-added note (fails), five shape failures, one per check (201 characters, two lines, empty, a block that is not a paragraph, inline Markdown), the deciding types read from a release-please config, and a plain sentence (passes). The deciding types are the changelog types that `release-please-config.json` does not hide. The workflow runs the base revision's checker on the merge result read as data; the pull request that introduces the check has no base copy and is skipped with a `::notice::`.
- [x] 1.3 `AGENTS.md` delivery rule and the PR template line.
  - Evidence: `AGENTS.md` section "A user-visible change adds a user note" and `.github/pull_request_template.md` (new: the repository had none).

## 2. Release build

- [x] 2.1 The generator script: notes per release from the tags, newest first, 20 releases at most.
  - Evidence: `scripts/user_notes.py generate`; `tests/test_user_notes.py` builds a throwaway git repository with tags v1.2.0, v1.2.1 (no note) and v1.3.0, a reworded older note and an unreleased note, and gets releases 1.4.0, 1.3.0 and 1.2.0 with plain `X.Y.Z` versions and `YYYY-MM-DD` dates; the 20-release cap and the shallow-clone refusal have their own cases. A note that breaks the shape rule is left out with a `::warning::` that escapes the file name, and the build goes on. The writer and the reader share `src/exomem/release_notes_format.py`; the generator checks its document with the cell's own parser.
- [x] 2.2 The release workflow writes the notes file into each cell image build.
  - Evidence: `build-cloud-images` in `.github/workflows/release-please.yml` fetches the full history and tags without file contents and writes `src/exomem/user_release_notes.json` before the image build (`continue-on-error`, so a failed run builds an image whose notes are unavailable, which the cell treats as unknown and not as empty); `pyproject.toml` names the gitignored file as a hatch artifact, and a wheel built with and without that line holds the file only with it.

## 3. Cell

- [x] 3.1 `bootstrap(profile="compact", section="whats_new")`.
  - Evidence: `src/exomem/release_notes.py` `payload()`, which answers `{"whats_new": {"version", "releases", "notes_available"}}`, behind `bootstrap_core.WHATS_NEW_SECTION`; `tests/test_whats_new.py` covers the section from one file read, through the cell over MCP, and with no file, bad JSON, an unknown schema and an unreadable release (empty `releases`, `notes_available` false), and with a valid empty file (`notes_available` true).
- [x] 3.2 Per-app last-told version in cell state and the `update_notice` tool-result field on cloud cells.
  - Evidence: `release_notes.update_notice` and `command_surface.with_update_notice`; the per-app version lives in `.app-update-notices.json` in the vault's state directory (outside the vault, on the cell's data volume), registered in `reserved_paths` as non-rebuildable state. `tests/test_whats_new.py` drives a cloud cell over MCP: first call no field, upgrade with notes gives the field once, a new app and a call without an app id get none, the record survives a restart, an upgrade without notes moves the version silently, a refused write gives no field and a later call recovers, a rollback repeats nothing, an image without a notes file moves no record and the next image with notes tells what that image hid, and a personal service never carries the field. Removing the relay call fails the first and third cases.

## 4. Acceptance

- [ ] 4.1 On a real Cloud cell after a rollout that carries a note: Home shows the note and one tool reply carries the line; the next reply does not.
- [ ] 4.2 A release with no note shows nothing new on Home and adds no line.
