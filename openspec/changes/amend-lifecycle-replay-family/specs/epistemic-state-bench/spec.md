## ADDED Requirements

### Requirement: Lifecycle-routing replay family f27

The pre-registration SHALL register family f27 `lifecycle_routing_replay` (operational journey; no public coverage) through a dated, reasoned §7 amendment entry as sequence 3 with a pending `preregistration-amendment-receipt.v1`, and the frozen registry SHALL mirror it (`f27 → 3`). The family SHALL be withheld from comparative runs, scores and claims until founder acknowledgment, under the same typed refusal as sequences 1 and 2. Its trajectory SHALL use only the existing operation vocabulary: `configure` for the arm, `agent_turn` per user utterance, `snapshot` per arm. The family SHALL add no budget constant, no catastrophic assertion and no operation kind, and SHALL leave `UNPROMPTED_FAMILIES`, `COMPOSES_ABSENCE_META` and `REQUIRES_ITEM_PAIR` unchanged.

#### Scenario: Registered and withheld at once

- **WHEN** the sequence-3 receipt is present but pending
- **THEN** f27 appears in the §1 registry and every surface that runs, scores or records it refuses with the typed pending-acknowledgment error naming sequence 3, while f01–f26 proceed as their own receipts prescribe

#### Scenario: The code mirror cannot drift from the document

- **WHEN** the registry's family table and amendment mapping are compared against §1 and the receipt chain
- **THEN** they are equal, and removing f27 from either side is a named drift failure

### Requirement: The replay corpus carries no store-bearing utterance

The f27 corpus SHALL be an ordered transcript of user turns in ordinary working language, each annotated with the consequences an expert lands after it in three tiers — `intent` (a plan item filed from stated intent), `outcome` (a record appended from an observed event), `transition` (an open item's status changed because of an outcome) — or `none`. The corpus SHALL include turns that land nothing: at least one tentative claim, one elapsed-time remark and one deferral. The expected end-state SHALL be the fold of the annotations, never the output of an agent. The corpus module SHALL pin a store-bearing vocabulary naming the store and the act of storing, and corpus construction and scenario loading SHALL refuse any user turn that matches it, naming the turn and the match. The corpus vocabulary SHALL be generic under the scaffold no-leak rule.

#### Scenario: A store-bearing turn refuses at load

- **WHEN** a fixture's user turn contains "save this one" or names the store
- **THEN** corpus construction and scenario loading refuse with the turn id and the matched token, and no scenario is produced

#### Scenario: Ordinary language is not refused

- **WHEN** a user turn says "the second one turned out really well, that's done" or "it's been a week since I touched the fifth"
- **THEN** the gate admits it, because the vocabulary names the store and the act of storing and nothing else

### Requirement: The replay journey drives a real agent in isolation and reports harness faults honestly

The f27 journey SHALL discover the installed agent CLI and record its version, refusing rather than substituting a library call when none is installed. Every turn SHALL run with the session's own Claude Code variables removed from the environment, project-only setting sources over a benchmark-owned project directory, a strict MCP configuration naming only an isolated exomem stdio server whose vault, config, leases, logs and hook state live under the benchmark workdir, a stream-json transcript with hook events, tools restricted to the exomem server plus what the arm declares, a bounded turn count, a pinned model, and one session id carried across turns. The journey SHALL run two arms on fresh copies of the seeded vault: a hookless thin client (no plugin, built-in tools disabled, the documented hookless custom-instructions block appended, prominence `maximal`) and a hooked plugin client (the shipped plugin directory, `Skill` enabled, prominence `balanced`); each arm's prominence SHALL be the product's own default for that surface. After the last turn the journey SHALL project the vault through the vault projector and persist the snapshot and transcripts through the evidence module. The runner SHALL be injectable so tests replay recorded transcripts, and a dry-run mode SHALL print the complete argv and environment delta per turn without executing. A non-zero exit, an error-subtype or `is_error` result, a login failure or a malformed transcript line SHALL mark the arm a harness fault: no snapshot is produced, the arm's assertions evaluate `blocked` with the reason, and nothing is scored.

#### Scenario: The session's own variables are stripped

- **WHEN** the journey is launched from inside a Claude Code session
- **THEN** the child invocation carries none of the `CLAUDECODE` / `CLAUDE_CODE_*` / `CLAUDE_PID` variables and the dry-run lists them as removed

#### Scenario: User configuration cannot leak into an arm

- **WHEN** the user's own settings carry hooks, MCP servers or a memory file that names the store
- **THEN** neither arm observes them: the hookless arm sees only the tool surface and the pasted block, the hooked arm only the plugin's skill and hooks

#### Scenario: A failed execution never becomes a product result

- **WHEN** the agent CLI exits non-zero or reports "Not logged in"
- **THEN** the arm is reported as a harness fault with the reason, no snapshot exists for it, and both assertions for that phase are `blocked`

### Requirement: Replay assertions are deterministic, paired and anti-vacuous

The registry SHALL add `lifecycle_consequence_landed_unprompted` and `no_structured_write_beyond_expectation`. Both SHALL evaluate only the snapshot's `collections` section and the scenario's expectation parameters, and both SHALL evaluate `blocked` when the section is empty or lacks a `planning` or a `records` collection. The first SHALL count landed over expected per tier, matching plan items by normalised title (NFKC, case fold, whitespace collapse — nothing looser), records by normalised `(title, event_type)` with `occurred_on` required present and compared only when the corpus states it, and transitions by expected status; it SHALL pass only when every tier is complete and SHALL carry the fractions and the missing keys in its detail. The second SHALL compute the extras — plan items outside the expected set or with a status the fold did not assign, records outside the expected set, any collection beyond the seeded two, any page outside the declared allowlist — and SHALL pass only when the extras set is empty, listing the extras otherwise. Each assertion SHALL have mechanism-removal tests for its gate, its matching and its counting. A run report SHALL present per arm the per-tier coverage beside the extras count from the same run, the capture-nudge firing count, the structured-write tool-use count and the manifest pins (CLI version, model, exomem version, prominence, corpus and fixture digests); no figure SHALL aggregate across tiers or arms, and coverage SHALL never be reported without its extras dual.

#### Scenario: Complete landing passes, partial landing fails with the fractions

- **WHEN** the replay snapshot holds every expected plan item, record and transition
- **THEN** `lifecycle_consequence_landed_unprompted` passes; and when one transition is missing it fails, naming the tier, the fraction and the missing key

#### Scenario: A spurious write fails the dual

- **WHEN** the replay appended a record for a tentative claim, or filed a plan item for a deferral, or created a collection
- **THEN** `no_structured_write_beyond_expectation` fails and lists exactly those extras, while the coverage assertion is unaffected

#### Scenario: An unprojected section cannot pass

- **WHEN** the snapshot's `collections` section is empty or lacks either seeded collection
- **THEN** both assertions evaluate `blocked`, never `pass`

#### Scenario: A development run is a finding, not a claim

- **WHEN** the journey is executed against the current runtime while sequence 3 is pending
- **THEN** the per-arm report is recorded in the change's tasks as the family's finding, and no comparative claim, score or run manifest cites f27
