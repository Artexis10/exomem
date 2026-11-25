## 1. Notes and the check

- [ ] 1.1 `user-notes/` with its README for authors and the first note.
- [ ] 1.2 The PR check: a `feat`, `fix` or `perf` PR adds a `user-notes/*.md` file or carries `User note: none`; a note is one paragraph of at most 200 characters with no Markdown syntax.
- [ ] 1.3 `AGENTS.md` delivery rule and the PR template line.

## 2. Release build

- [ ] 2.1 The generator script: notes per release from the tags, newest first, 20 releases at most.
- [ ] 2.2 The release workflow writes the notes file into each cell image build.

## 3. Cell

- [ ] 3.1 `bootstrap(profile="compact", section="whats_new")`.
- [ ] 3.2 Per-app last-told version in cell state and the `update_notice` tool-result field on cloud cells.

## 4. Acceptance

- [ ] 4.1 On a real Cloud cell after a rollout that carries a note: Home shows the note and one tool reply carries the line; the next reply does not.
- [ ] 4.2 A release with no note shows nothing new on Home and adds no line.
