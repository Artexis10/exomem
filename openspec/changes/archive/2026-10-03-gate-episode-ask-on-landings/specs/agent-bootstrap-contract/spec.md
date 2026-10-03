## MODIFIED Requirements

### Requirement: The client capture nudge fires on landings

Where a client hook enforces the capture contract, at every prominence level
below the most aggressive it SHALL send the per-turn capture reminder only when
the latest turn contains a landing: a shell command that did not fail and runs
`git commit`, `git push`, `git merge`, `git tag`, `yadm commit`, `yadm push`,
`gh pr create`, `gh pr merge` or `gh release create`. The detector SHALL match
parsed command words, not substrings of prose, SHALL recognise the Claude and
Codex transcript shapes, and SHALL NOT count a command that reports an explicit non-zero exit code or an error result. A command with no reported exit code, such as a Codex call that returned while the command was still running, counts. A dry run, a merge `--abort` or `--quit`, a push that deletes a remote ref, and a `git tag` that only lists, verifies or deletes are not landings.
The most aggressive level SHALL keep the reply-length gate. At every level the
reminder SHALL stay silent for a turn that already wrote to the knowledge base,
SHALL respect `stop_hook_active` and the cooldown, and the episode ask SHALL
follow its own landing rule.

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

## ADDED Requirements

### Requirement: The client episode ask needs a landing since the last ask or record

Where a client hook asks for an episode record, it SHALL ask only when work has
landed (by the same detector as the capture nudge) since the last episode ask
or, if newer, the last successful `episode_memory` record, in addition to its
turn-count and cooldown cadence. A landing seen before the cadence is due SHALL
be remembered until it is. A session that never lands work SHALL NOT be asked,
and an ask answered without a record SHALL NOT be repeated until a new landing.
The `stop_hook_active` self-disarm, the revision re-base against the service,
candidate-coverage asks, `prominence=off` and the cadence overrides SHALL be
unaffected.

#### Scenario: A session that never lands work is not asked

- **WHEN** a session at `balanced` completes many substantive turns whose commands never commit, push, merge, tag or create a pull request or release
- **THEN** the episode ask does not fire

#### Scenario: A landing makes a due ask fire

- **WHEN** the turn count and cooldown are satisfied and a turn then runs `git push` successfully, in a Claude or a Codex transcript
- **THEN** the episode ask fires on that Stop

#### Scenario: An unanswered ask waits for a new landing

- **WHEN** an episode ask fired and the agent made no record, and later turns satisfy the cooldown without a landing
- **THEN** the ask does not repeat until a turn lands work

#### Scenario: A record resets the pending landing

- **WHEN** a successful `episode_memory` record is made after a landing
- **THEN** the ask needs both a fresh landing and a fresh turn count
