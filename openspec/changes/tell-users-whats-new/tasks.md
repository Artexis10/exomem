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
  - Evidence: `release_notes.update_notice` and `command_surface.with_update_notice`; the per-app version lives in `.app-update-notices.json` in the vault's state directory (outside the vault, on the cell's data volume), registered in `reserved_paths` as rebuildable state, with its atomic-replace temp name. `tests/test_whats_new.py` drives a cloud cell over MCP: first call no field, upgrade with notes gives the field once, a new app and a call without an app id get none, the record survives a restart, an upgrade without notes moves the version silently, a refused write gives no field and a later call recovers, a rollback repeats nothing, an image without a notes file moves no record and the next image with notes tells what that image hid, and a personal service never carries the field. Removing the relay call fails the first and third cases.

## 4. Acceptance

- [x] 4.1 On a real Cloud cell after a rollout that carries a note: Home shows the note and one tool reply carries the line; the next reply does not.
  - Evidence: on 2026-11-26 a Cloud cell rolled from v0.201.0 to v0.207.0 in its upgrade window (pod restarted 22:28Z; the v0.207.0 image build logged "Wrote 4 release(s) with notes"). The owner reloaded Home: the footer read v0.207.0 and the What's new section listed the notes. In a chat app already connected to that cell, the first reply mentioned what's new once and the next reply did not.
- [ ] 4.2 A release with no note shows nothing new on Home and adds no line.

## 5. Note titles

- [x] 5.1 A note carries a short title in YAML front matter; the check requires it and the notes file carries it as schema 2.
  - Evidence: `scripts/user_notes.py` (`parse_note`, `title_problems`, `note_problems`) reads the front matter with PyYAML `safe_load`, and both workflows that run it install `pyyaml` at the exact version in `uv.lock`; an unquoted title that YAML reads differently from what was typed (`#`, `Yes`, a colon, a leading `@`) fails with the advice to use double quotes. `tests/test_user_notes.py` covers a missing, empty, non-text, 49-character, full-stopped, Markdown and multi-line title, an unknown key, invalid YAML, the `#`, `Yes`, colon and `@` cases (also under an indented or a quoted `title` key, found from the YAML parser's positions), a leading byte order mark, quoted titles that pass, and an unclosed fence (each fails with the change to make), and `generate` emitting titles and null for a file without front matter. `src/exomem/release_notes_format.py` is `SCHEMA = 2` with `Note`; `tests/test_whats_new.py` covers the round trip and refuses schema 1, a bare-string note, an empty title and an extra key.
- [x] 5.2 The cell serves `items` of `{title, text}` beside the unchanged `notes` sentences, and the assistant's line keeps the sentence.
  - Evidence: `release_notes.payload()`; `tests/test_whats_new.py` compares the section with `notes` as strings and `items` with titles (null for a note without one), and the update-notice cases still pass with the sentence in `say`.
- [x] 5.3 The authors' README and the check describe the title rule.
  - Evidence: `user-notes/README.md` shows the example file, the title rules and how to quote a title.
- [ ] 5.4 Every existing note has a title. A follow-up pull request adds them, because the note check runs the base revision's checker: titled notes pass only once this checker is on main.
- [ ] 5.5 On a real Cloud cell after a rollout that carries titles, Home's What's new lists each title and opens its sentence.
