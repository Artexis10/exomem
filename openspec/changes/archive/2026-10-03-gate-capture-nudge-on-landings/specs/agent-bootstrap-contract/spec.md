## ADDED Requirements

### Requirement: The client capture nudge fires on landings

Where a client hook enforces the capture contract, at every prominence level
below the most aggressive it SHALL send the per-turn capture reminder only when
the latest turn contains a landing: a successful shell command that runs
`git commit`, `git push`, `git merge`, `git tag`, `yadm commit`, `yadm push`,
`gh pr create`, `gh pr merge` or `gh release create`. The detector SHALL match
parsed command words, not substrings of prose, SHALL recognise the Claude and
Codex transcript shapes, and SHALL NOT count a failed or unconfirmed command.
The most aggressive level SHALL keep the reply-length gate. At every level the
reminder SHALL stay silent for a turn that already wrote to the knowledge base,
SHALL respect `stop_hook_active` and the cooldown, and the episode ask SHALL be
unaffected.

#### Scenario: A long reply without a landing is not nudged

- **WHEN** a turn at `balanced` ends with a long reply and its only commands are reads such as `git log` or `gh pr view`
- **THEN** the capture nudge does not fire

#### Scenario: A landing is nudged

- **WHEN** a turn at `balanced` runs `git push` successfully and wrote nothing to the knowledge base
- **THEN** the capture nudge fires, whatever the reply length

#### Scenario: A failed landing is not a landing

- **WHEN** the turn's `git push` reports an error or a non-zero exit code
- **THEN** the capture nudge does not fire

#### Scenario: The most aggressive level keeps the length gate

- **WHEN** a turn at `maximal` has a long reply and no landing
- **THEN** the capture nudge fires
