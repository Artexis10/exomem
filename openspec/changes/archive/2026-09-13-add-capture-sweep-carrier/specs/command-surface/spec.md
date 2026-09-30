## ADDED Requirements

### Requirement: Compact write responses may carry one bounded capture-sweep advisory

Default compact responses to a committed durable write — the compiled page writers `remember`, `edit_memory`, `observe_memory` and `replace_memory`, and the structured writers `record_memory` and `plan_memory` — MAY carry one `capture_sweep` block under exactly these keys:

- `boundary`: the boundary that produced the block, drawn from a closed vocabulary.
- `rule`: the bounded pass the agent is asked to make. Its own text SHALL state that the class list is examples rather than a closed set, because it is the only prose the block carries.
- `consider`: the open list of example classes.
- `written_recently`: at most eight `exomem://` references to that caller's recent writes, newest first. Optional, and absent rather than empty.
- `unpaged_mentions`: at most five bounded names the committed page reaches for without a page of their own. Optional, and absent rather than empty.

No other key SHALL appear in the block on the wire; the terminal rebuilds the block from the named keys and never passes a leaf through.

The block SHALL follow the established advisory posture: produced at the post-commit seam, validated and bounded again at the mutation terminal rather than trusted, never a key a client branches on for the mutation outcome, and absent — never null, never empty — when there is nothing to say. It SHALL NOT alter `status`, `mutated`, `path`, `warnings_count`, mutation identity, or replay behaviour. The `legacy` response detail SHALL omit it. Tool descriptions, tool input schemas and the packaged tool-surface digest SHALL NOT change.

A fault anywhere in producing, validating or attaching the block SHALL cost the caller the advisory and never the write.

#### Scenario: The first write after a quiet interval carries the block

- **WHEN** a caller commits a durable write and that caller has not written durably within the quiet interval
- **THEN** the default compact response carries a `capture_sweep` block whose `boundary` is `quiet-interval`

#### Scenario: A following write inside the interval is silent

- **WHEN** the same caller commits a second durable write inside the quiet interval
- **THEN** that response carries no `capture_sweep` key and the write is otherwise unchanged

#### Scenario: The legacy detail omits the advisory

- **WHEN** a write that produced the block is projected at `legacy` detail
- **THEN** the returned leaf contains no `capture_sweep` key

#### Scenario: A malformed or oversized block is dropped rather than widened

- **WHEN** a leaf attaches a block whose boundary is outside the closed vocabulary, or whose references or mentions exceed their bounds
- **THEN** the terminal discards the whole block, never repairing it to fit, and the response stays valid

#### Scenario: A multi-write command emits at most once

- **WHEN** one product-command invocation commits many governed writes
- **THEN** the invocation's response carries at most one `capture_sweep` block

### Requirement: The capture-sweep advisory is governed by a caller-scoped quiet interval

The advisory SHALL be governed by its own emission rule and SHALL NOT change the due-state governor, its batch scope, or its first-surfaced ledger. The rule SHALL be a quiet interval over an in-memory ledger keyed on the calling principal scope, the calling client, and the vault; a first-ever write from a key SHALL qualify as after-quiet. Every successful durable write SHALL update the ledger whether or not a block was emitted. The ledger SHALL be bounded and SHALL NOT be persisted, so a restart re-arms each caller once rather than letting a durable file decide what an agent is told. The interval SHALL be a module constant with no environment override.

Where the transport supplies no usable stable caller scope — an HTTP call whose scope is session-derived or absent — the system SHALL emit nothing rather than collapse distinct callers into one bucket. Outside any call, where identity is absent by design, a process-lifetime key SHALL apply.

The advisory SHALL be emitted only while the resolved delegation envelope permits proactive capture, read through the existing prominence and envelope resolution rather than a parallel table.

#### Scenario: Distinct callers are not collapsed

- **WHEN** an HTTP call carries no stable principal or credential scope
- **THEN** no `capture_sweep` block is emitted for that call

#### Scenario: A quiet profile never emits

- **WHEN** the resolved envelope disposition for proactive capture is `off`
- **THEN** no response carries a `capture_sweep` block

#### Scenario: The ledger records a write that emitted nothing

- **WHEN** a durable write is committed while the caller is inside the quiet interval
- **THEN** the ledger still records that write, so the interval is measured from the latest write rather than the latest advisory
