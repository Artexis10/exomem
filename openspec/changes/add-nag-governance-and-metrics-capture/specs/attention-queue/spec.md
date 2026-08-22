## ADDED Requirements

### Requirement: Signal families carry a durable disposition

The portable review state SHALL record, per registered signal family, a disposition of `normal`, `quiet`, or `off`, with the reason code, the free-text why, the timestamp, and the origin of the decision. A family SHALL be any registered attention category or write-advisory kind; any other name SHALL be refused. A disposition SHALL persist across engine restarts and across prominence-level changes, and SHALL change only through an explicit decision or a reset to `normal`.

A `quiet` family SHALL be excluded from the default attention union, SHALL contribute nothing to the due-state projection or to any carrier, and SHALL emit no write-path advisory of its kind, while remaining included when its category is requested explicitly, annotated with its disposition. An `off` family SHALL additionally be excluded from explicit category review except under the all-states view, where it is annotated. Exclusion SHALL be applied before cross-queue fusion, so an item flagged only by excluded families is absent and a multi-flagged item keeps its remaining reasons. Audit measurement SHALL be unaffected by any disposition. Per-item decisions and pair stances SHALL compose unchanged with dispositions. A disposition SHALL NOT rewrite a decision: an item whose composed reasons change when a family returns to `normal` SHALL resurface under the existing new-reason rule, with the earlier record still standing under the fingerprint it was recorded against.

#### Scenario: A quiet family leaves the default union and the carriers

- **WHEN** a family is set to `quiet` with a reason, and a page in the vault trips that family and no other
- **THEN** the default attention view does not list the page
- **AND** the due-state block served on the next write, recall, and bootstrap carries no count and no reference for that family
- **AND** an explicit review of that category still lists the page, annotated `quiet`

#### Scenario: An off family is reachable only through the all-states view

- **WHEN** a family is set to `off`
- **THEN** an explicit category review of that family under the open view lists nothing for it
- **AND** the all-states view lists its items annotated `off`
- **AND** the audit still reports the family's findings

#### Scenario: A disposition survives restart and prominence changes

- **WHEN** a family is set to `quiet`, the engine is restarted, and the prominence level is changed to the minimum and then the maximum
- **THEN** the family is still `quiet` with the original reason and timestamp

#### Scenario: Dispositions and item decisions compose

- **WHEN** an item in a `quiet` family is dismissed and the family is later reset to `normal`
- **THEN** the item stays dismissed under its fingerprint
- **AND** a different item in the same family is open

#### Scenario: A multi-flagged item resurfaces under the new-reason rule when a family returns

- **WHEN** an item flagged by two families is dismissed while one of them is `quiet`, and that family is later reset to `normal`
- **THEN** the item is open, because its composed reasons now include one the decision was never taken against
- **AND** the earlier record still stands under the fingerprint it was recorded against

#### Scenario: An unregistered family is refused

- **WHEN** a disposition is requested for a name that is neither a registered attention category nor a write-advisory kind
- **THEN** the decision is refused with a family-specific error and no state changes

### Requirement: Triage decisions record a closed reason code

Dismissals and family dispositions SHALL record a reason from the closed vocabulary `intentional`, `false_positive`, `handled`, `deferred`, `too_frequent`, `unspecified`. The code SHALL be carried as the leading colon-terminated token of the free-text why; the why SHALL be stored verbatim and the reason separately. A missing or unrecognised token SHALL record `unspecified` and SHALL NOT refuse an item decision. A `quiet` or `off` disposition SHALL require a code other than `unspecified`.

#### Scenario: A coded dismissal stores both the code and the text

- **WHEN** an item is dismissed with why `intentional: this page is a deliberate hub`
- **THEN** the record carries reason `intentional` and the why verbatim

#### Scenario: Free text without a code is still accepted

- **WHEN** an item is dismissed with why `looks fine to me`
- **THEN** the record carries reason `unspecified` and the decision is recorded

#### Scenario: A disposition without a code is refused

- **WHEN** a family is set to `quiet` with no reason token
- **THEN** the decision is refused and the family's disposition is unchanged

### Requirement: Signals carry a first-surfaced record

The review state SHALL record, per review identity and fingerprint, the time a signal was first composed onto a served surface and which surface that was: a returned attention or activation report, a served due-state reference, or an emitted write-path advisory. The ledger SHALL start empty, SHALL NOT be backfilled, SHALL NOT record anything withheld by egress or excluded by a disposition, and SHALL NOT be written by audit measurement. Recording SHALL be failure-isolated: a surface's content, outcome, and latency budget SHALL NOT change because the record could not be written. Attention items SHALL expose `first_surfaced_at` when a record exists.

#### Scenario: The first listing stamps the ledger once

- **WHEN** a signal is listed by the attention view for the first time and listed again on a later pass
- **THEN** the item carries the same `first_surfaced_at` on both passes
- **AND** the ledger holds one record for that identity and fingerprint

#### Scenario: A withheld signal is never recorded

- **WHEN** a signal's page is withheld from the requesting audience by governance
- **THEN** the ledger holds no record for it after the request

#### Scenario: An unwritable ledger does not change the surface

- **WHEN** the review state cannot be written during an attention request
- **THEN** the report is returned with the same items, order, and counts as when it can be

### Requirement: Review-state records carry their origin

Every record and disposition SHALL carry `origin` as `manual` when written through the explicit triage surface or `automatic` when written by the runtime itself. Records migrated from the previous schema SHALL carry `manual`. The count of manual records within a time window SHALL be computable from the store alone.

#### Scenario: A triage decision is manual and a compaction rewrite is automatic

- **WHEN** an item is dismissed through triage and the store is later compacted
- **THEN** the dismissal record carries origin `manual`
- **AND** any record the compaction rewrote carries origin `automatic`

### Requirement: The review-state store has a schema, retention, compaction, and a stress gate

The store SHALL use a sectioned schema holding records, dispositions, the surfaced ledger, and stats. A previous-schema file SHALL be migrated on load and rewritten on the next write; a newer schema SHALL be refused by an older runtime. Lapsed snooze records older than the retention window and ledger entries older than their retention window with no standing decision SHALL be eligible for compaction. Compaction SHALL run on write past a declared size or record threshold and on reconcile, SHALL report what it dropped, and SHALL never drop a standing dismissal, competing stance, or disposition. A stress gate SHALL build the store at multi-year cardinality and assert declared budgets for load, lookup, apply, and compaction and that the compacted file stays under the read limit; the budgets SHALL be pinned with measured evidence.

#### Scenario: A previous-schema store keeps its decisions

- **WHEN** a schema-1 review state with decisions is loaded and one new decision is applied
- **THEN** every earlier decision still resolves to the same state
- **AND** the file is rewritten in the current schema

#### Scenario: Compaction drops only what retention allows

- **WHEN** the store holds a lapsed snooze older than the retention window, a standing dismissal, a disposition, and a stale ledger entry, and compaction runs
- **THEN** the lapsed snooze and the stale ledger entry are gone and reported
- **AND** the dismissal and the disposition are unchanged

#### Scenario: The stress gate holds at multi-year cardinality

- **WHEN** the store is built with fifty thousand decision records and one hundred fifty thousand ledger entries
- **THEN** load, one lookup, one apply, and one compaction each complete within their pinned budgets
- **AND** the compacted file is under the read limit
