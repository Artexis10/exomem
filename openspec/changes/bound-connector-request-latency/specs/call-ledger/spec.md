## MODIFIED Requirements

### Requirement: Every Row Carries The Call's Latency, Leaf And Total

Every row SHALL carry the latency of the call it records, for reads as well as writes and for
refusals as well as successes: `duration_ms` for the tool leaf, and `total_ms` for the wall
clock the caller actually waited, including work done before the leaf. `duration_ms` SHALL keep
its existing meaning, so that the prose trace and the per-tool duration metric are not silently
redefined. Rows SHALL also record the total serialized size of the arguments. Rows SHALL carry the
call's stage `spans` (name, count and milliseconds per stage, bounded in count and name length),
including `recall.<stage>` spans for recall calls, and a `budget` block (`seconds`,
`remaining_ms`, `skipped`) whenever a request budget applied, so a slow row can be attributed by
stage without a second call. Spans and budget blocks SHALL carry no query text, path, excerpt or
other content.

#### Scenario: A slow call reports how long it took

- **WHEN** a tool call takes appreciable time
- **THEN** the row's `duration_ms` reflects the leaf's own time
- **AND** `total_ms` is at least `duration_ms`

#### Scenario: Time spent before the leaf is visible

- **WHEN** a call spends time in a pre-leaf check — content guarding or argument normalization
- **THEN** that time is reflected in `total_ms`
- **AND** it is not hidden by `duration_ms`, which does not start until the leaf does

#### Scenario: A refusal reports how long the caller waited

- **WHEN** a call is refused after waiting on a contended resource
- **THEN** the row records both the refusal code and the elapsed time
- **AND** "it waited, then was refused" is reconstructable from the row alone

#### Scenario: Request size accompanies the latency

- **WHEN** two calls carry arguments of very different sizes
- **THEN** their rows record correspondingly different `request_bytes`
- **AND** no argument value is recorded to produce that figure

#### Scenario: A recall row attributes its time by stage

- **WHEN** an `ask_memory` call runs inside an MCP call
- **THEN** the row's `spans` include `recall.` entries for the stages that ran, with milliseconds
- **AND** no span carries the query text, a hit path or an excerpt

#### Scenario: A budgeted row records what the budget did

- **WHEN** a request budget applied to the call and a stage was skipped
- **THEN** the row's `budget` names the budget in seconds, the remaining milliseconds at return, and the skipped stages
