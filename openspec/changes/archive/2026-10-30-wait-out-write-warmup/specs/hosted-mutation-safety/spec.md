## ADDED Requirements

### Requirement: Keyed Writes Wait Out Warm-Up Within Their Request

A keyed governed write that arrives while a write-gating warm-up component is still warming SHALL wait for that component inside its request, within the bounded wait and the parked-worker cap that a busy boundary uses, and SHALL then run once. It SHALL return the retryable warm-up refusal only when that bound runs out first or no wait slot is free, and the refusal SHALL report how long the write waited. An unkeyed write SHALL keep the immediate retryable refusal, because a resend after a client gave up during the wait would run it twice. A write attempted on a collection store's thread SHALL keep it too, because that thread never parks.

#### Scenario: A keyed write during a short warm-up commits

- **WHEN** a keyed governed write arrives while the semantic corpus is warming
- **AND** the corpus becomes ready within the bounded wait
- **THEN** the write runs once and commits
- **AND** the client receives no warm-up refusal

#### Scenario: A warm-up longer than the bound refuses

- **WHEN** a keyed governed write arrives while a write-gating component stays warming past the bounded wait
- **THEN** the write returns the retryable warm-up refusal before its request deadline
- **AND** the refusal reports the time waited
- **AND** the command leaf does not execute

#### Scenario: An unkeyed write is refused at once

- **WHEN** an unkeyed governed write arrives while a write-gating component is warming
- **THEN** it returns the retryable warm-up refusal without waiting

#### Scenario: A store-routed write does not park the store thread

- **WHEN** a keyed governed write routed to a collection store arrives while a write-gating component is warming
- **THEN** it returns the retryable warm-up refusal without waiting on the store thread
