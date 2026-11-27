## MODIFIED Requirements

### Requirement: A failed queued save stays visible

A queued save whose replay ends in a refusal other than contention, or returns `committed: false` with rejected rows, SHALL be recorded `failed` with the refusal or the first rejected row's code, and SHALL keep its arguments. The service SHALL NOT change a queued save to make it apply, nor delete a failed one; the owner lists and dismisses them with `exomem intake list` and `exomem intake dismiss <id>`. An applied save SHALL keep its compact outcome and drop its arguments.

#### Scenario: A stale edit fails visibly

- **WHEN** a queued `edit_memory` carries a hash guard
- **AND** the page changes before the edit is applied
- **THEN** the save is recorded `failed` with the guard's refusal
- **AND** reading it returns the refusal and the original arguments

#### Scenario: A bulk write that rejects every row fails visibly

- **WHEN** a queued `record_memory` bulk write names a source page that does not exist
- **AND** its replay rejects every row and commits nothing
- **THEN** the save is recorded `failed` with `SOURCE_NOT_FOUND` and its arguments
- **AND** reading it does not say it was saved

### Requirement: Callers can read a queued save's outcome

`review_memory(mode="queued-write")` SHALL list open and failed saves, oldest first, bounded by `limit`. With `ref` it SHALL return the save's state, attempts, compact outcome when applied (read as partly saved when it counts rejected rows), refusal and arguments when failed. `coordination_status` SHALL report the queued, applying and failed counts and the oldest queued age, or `unknown` when the store cannot be read.

#### Scenario: A later session checks a queued save

- **WHEN** a save was queued in one session
- **AND** a later session reads its `ref` after the save landed
- **THEN** the read reports `applied` with the compact outcome of the commit

#### Scenario: An unreadable store is not reported as empty

- **WHEN** the intake store cannot be read
- **THEN** `coordination_status` reports the intake as `unknown`, not as zero saves

### Requirement: A save its arguments condemn is refused, not queued

Before the intake accepts a save, the serving process SHALL apply the argument-only checks that exist for it: compact observation metadata, an episode recap's own validation, and the argument rules of `record_memory` (including malformed or sourceless bulk rows under `on_reject: abort`) and `plan_memory`. A save that one of them refuses SHALL get that refusal at once and SHALL NOT be queued. A refusal that reads vault state SHALL stay at the replay.

#### Scenario: An oversized recap behind the queue is refused at once

- **WHEN** an owner sends an `episode_memory` record whose decided item exceeds its size cap
- **AND** earlier saves are still queued
- **THEN** the call returns `EPISODE_TOO_LARGE`
- **AND** the intake holds only the earlier saves

#### Scenario: An edit of a page that a queued save creates still queues

- **WHEN** an owner sends an edit of a page that a queued save will create
- **AND** that queued save has not been applied yet
- **THEN** the edit is queued behind it, not refused for the missing page

#### Scenario: Bare bulk rows behind the queue are refused at once

- **WHEN** an owner sends a `record_memory` bulk write whose rows lack `item` or a source, under `on_reject: abort`
- **AND** earlier saves are still queued
- **THEN** the call returns `INVALID_RECORD_ARGUMENTS` naming the rejected rows
- **AND** the intake holds only the earlier saves
