## ADDED Requirements

### Requirement: Family dispositions are set and read on the existing review surfaces

The triage command SHALL accept a family review reference of the form `exomem://review/family/<family>` with the actions `quiet`, `off`, and `normal`, recording or clearing the family's disposition with the reason token and why, and SHALL refuse those actions on any non-family reference and the item actions on a family reference. The review command SHALL offer a dispositions view listing every family with a non-default disposition, its reason, why, timestamp, and origin, together with per-family counts of manual dismissals, so the state is inspectable on every client. No tool input parameter SHALL be added or removed; the tool descriptions SHALL describe the family actions and the view, and the packaged tool-surface digest SHALL be regenerated and recorded as pending through the documented two-phase rollout.

#### Scenario: Quieting a family through triage

- **WHEN** `triage_memory` is called with a family reference, action `quiet`, and why `too_frequent: not useful in this vault`
- **THEN** the response reports the family, disposition `quiet`, reason `too_frequent`, and origin `manual`
- **AND** the dispositions view lists the family with the same values

#### Scenario: Item actions on a family reference are refused

- **WHEN** `triage_memory` is called with a family reference and action `dismiss`
- **THEN** the call is refused with an action-specific error and no disposition changes

#### Scenario: The regenerated surface is recorded as pending

- **WHEN** the packaged tool schemas are regenerated after this change
- **THEN** the packaged contract digest matches the live discovery surface
- **AND** the connector plugin contract records that digest as pending with a refresh required

### Requirement: Due-state emission is captured and batched

The due-state projection SHALL persist an emission ledger holding the number of governed writes applied to the projection and the number of due-state blocks emitted, readable from the projection file. A product command that commits more than one governed write in one invocation SHALL emit its due-state block at most once, at the end of the invocation, under the unchanged change-only rule; the per-write projection deltas SHALL still apply inside the batch. Separate invocations SHALL remain separate batches.

#### Scenario: A bulk command emits once

- **WHEN** one command commits twelve governed writes that each change the due-state counts
- **THEN** the command's response carries at most one due-state block
- **AND** the emission ledger's write count rose by twelve and its emission count by at most one

#### Scenario: The ledger is readable by a projector

- **WHEN** the projection file is read after a batch
- **THEN** it carries an emission section with the write count and the emission count

### Requirement: The f23 family runs against the real runtime

A journey driver SHALL execute the f23 scenario's operations against an installed envelope — seed, maintenance passes, a triage dismissal, an engine restart, prominence reconfiguration across the full level range, and one bulk ingest — and SHALL project the resulting review state and emission ledger into the snapshot pair the family's assertions evaluate. The vault projector SHALL declare `due_state_counters` available through the projection file. The driver SHALL refuse to run rather than fall back when no envelope is installed.

#### Scenario: f23 is green on this runtime

- **WHEN** the f23 journey runs against the current runtime
- **THEN** `dismissal_respected_across_passes` passes for the dismissed subject
- **AND** `counter_emission_not_repeated_per_write` passes for the bulk batch

#### Scenario: Removing the batch scope turns the counter assertion red

- **WHEN** the batch scope is disabled and the f23 journey runs
- **THEN** `counter_emission_not_repeated_per_write` fails
