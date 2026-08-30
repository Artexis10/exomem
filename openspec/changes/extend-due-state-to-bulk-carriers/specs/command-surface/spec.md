## ADDED Requirements

### Requirement: Operation leaves are due-state carriers

An invocation of the operation leaves — `adopt_vault` mutating modes,
`adoption_studio` `apply` and `apply-proposal`, `maintain_memory` in `fix`
with `dry_run=false`, `reconcile`, `backfill-ids` with `dry_run=false`, or
`structured-files` with `apply=true`, `preserve_artifacts`, and
`process_media` mutating operations — that commits at least one governed
write SHALL carry the bounded advisory due-state block under the same
carrier contract as page writes: served from the committed terminal
projection, validated and bounded, never a key a client branches on for
operation outcome, family dispositions applied, emission recorded in the
ledger once per delivered block, and an unreadable review state yielding no
block while the operation still completes. Emission SHALL follow the
canonical emission-governance and emission-ledger requirements unchanged —
the invocation is one batch scope, change-only, delivered at the end.

An invocation that commits no governed write — a clean-vault repair pass,
already-valid media, a `retry` re-enqueue — SHALL carry no block even when
the projection has open items: it produces no committed terminal, and
extending carriage to non-committing responses is a response-contract change
outside this requirement. A partially failed invocation that committed at
least one governed write SHALL still carry under the change-only rule.
Read-only and dry-run invocations of these leaves SHALL NOT carry the block.
The leaves SHALL reuse the shared due-state projection helpers (the
`due_state.block_for_write` family behind the terminal's
`due_state_advisory` disclosure boundary) rather than re-deriving any of it,
and tool input schemas SHALL NOT change.

#### Scenario: A bulk apply reports accumulation exactly once, at the end

- **WHEN** an `adoption_studio` `apply` commits twelve governed writes whose
  deltas change the due-state counts
- **THEN** the invocation's response carries exactly one `due_state` block
  reflecting the projection after the batch
- **AND** the operation outcome keys are byte-identical to a projection-free
  response apart from the advisory block

#### Scenario: Unchanged totals stay quiet on a carrying leaf

- **WHEN** a `preserve_artifacts` invocation commits its artifacts while no
  category count differs from the last delivered block
- **THEN** the response carries no `due_state` block

#### Scenario: A committing-nothing invocation carries nothing

- **WHEN** `maintain_memory` in `fix` with `dry_run=false` runs over a clean
  vault and commits no write, while the projection holds open items
- **THEN** the response carries no `due_state` block and the operation's
  existing terminal is unchanged

#### Scenario: Previews and scans stay clean

- **WHEN** `adopt_vault` runs in scan-only mode, or `maintain_memory` `fix`
  runs with its default dry-run preview
- **THEN** the response carries no `due_state` block

#### Scenario: Unreadable review state never blocks the operation

- **WHEN** the review state cannot be read while a `process_media` processing
  invocation commits a transcript sidecar
- **THEN** the invocation completes with its existing terminal unchanged and
  no `due_state` block

## MODIFIED Requirements

### Requirement: The f23 family runs against the real runtime

A journey driver SHALL execute the f23 scenario's operations against an installed envelope — seed, maintenance passes, a triage dismissal, an engine restart, prominence reconfiguration across the full level range, and one bulk ingest — and SHALL project the resulting review state and emission ledger into the snapshot pair the family's assertions evaluate. The vault projector SHALL declare `due_state_counters` available through the projection file. The driver SHALL refuse to run rather than fall back when no envelope is installed.

#### Scenario: f23 reports what this runtime can decide, and no more

- **WHEN** the f23 journey runs against the current runtime
- **THEN** `dismissal_respected_across_passes` passes for the dismissed subject
- **AND** `counter_emission_not_repeated_per_write` is evaluated on the emission delta between the two snapshots, so it is decided only for a batch that delivered at least one block, and otherwise reports `unsupported` rather than passing vacuously or inheriting an earlier batch's delivery
- **AND** on this runtime it is decided: the bulk ingest commits through a carrying operation leaf, the batch delivers exactly one block, and the assertion passes on an emission delta of one against a write delta of twelve
- **AND** the batch-once requirement is proven where it is decidable: twelve write carriers inside one batch scope emit at most one block

#### Scenario: Removing the batch scope turns the counter assertion red

- **WHEN** the batch scope is disabled and twelve write carriers run over one vault
- **THEN** `counter_emission_not_repeated_per_write` fails with twelve emissions for twelve writes
