## ADDED Requirements

### Requirement: Shared-workload improvement is repeatable and work-bounded

Claims of shared Markdown workflow parity SHALL use at least three fresh paired
runs per declared corpus size with identical fixed bodies, public operations,
runtime pins, and isolated state. Product order SHALL alternate. The report SHALL
retain individual observations and compare median verified-closure times;
startup and optional graph convergence SHALL remain separate measurements.
Deterministic regression checks SHALL also measure unrelated body reads during
warm writer resolver preparation and topology dependency discovery. A timing
improvement SHALL NOT substitute for full-rebuild parity or durability evidence.

#### Scenario: One unusually fast run
- **WHEN** one observation meets the target but the paired median does not
- **THEN** the report records the observations and does not claim the parity target was met

#### Scenario: Background graph completion is delayed
- **WHEN** exact public reads and text search verify closure before graph convergence
- **THEN** the report names the two states separately and does not treat useful closure as proof that all derived work finished

#### Scenario: Work-count regression despite fast hardware
- **WHEN** a warm resolver or dependency-discovery path rereads unrelated Markdown
- **THEN** its deterministic regression check fails even if elapsed time remains under a wall-clock threshold

### Requirement: Comparative startup proves the indexed corpus size
Before the timed shared workflow, each product adapter SHALL prove that every
generated fixture path is represented exactly once in its Markdown metadata and
text-search projection in one coherent, read-only snapshot of the current run's
disposable derived store. It SHALL bind the proof to the expected fixture path
set and product/project identity, retain membership counts and digests, and
preserve independent public readiness gates. It SHALL NOT use directory file
counts, total row counts or one successful sentinel lookup as completeness proof.
The adapter SHALL report startup separately, invalidate unsupported schemas, and
produce no timed workflow result when its bounded completeness wait expires.
The proof SHALL NOT mutate the index or substitute internal operations for timed
public calls.

#### Scenario: Sentinel appears in the first indexing batch
- **WHEN** the sentinel is searchable but only 100 of 3,800 fixture notes are indexed
- **THEN** setup remains pending and the workflow clock does not start

#### Scenario: Counts match but identities or search rows do not
- **WHEN** a missing fixture path is replaced by an unexpected or duplicate row, a wrong-project row, or metadata without its text-search row
- **THEN** the completeness proof fails even if the aggregate row count matches

#### Scenario: Full indexed fixture becomes ready
- **WHEN** exact fixture membership and corresponding search rows are proven in one current-run snapshot and public readiness succeeds
- **THEN** the unchanged shared public workflow starts and records the completeness evidence outside its timing
