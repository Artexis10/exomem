## MODIFIED Requirements

### Requirement: A save its arguments condemn is refused, not queued

Before the intake accepts a save, the serving process SHALL apply the argument-only checks that exist for it: compact observation metadata, an episode recap's own validation, and the argument rules of `record_memory` and `plan_memory`. A save that one of them refuses SHALL get that refusal at once and SHALL NOT be queued, also when the terminal egress filter faults. A refusal that reads vault state SHALL stay at the replay, except the declared Records refusal that the requirements below define.

#### Scenario: An oversized recap behind the queue is refused at once

- **WHEN** an owner sends an `episode_memory` record whose decided item exceeds its size cap
- **AND** earlier saves are still queued
- **THEN** the call returns `EPISODE_TOO_LARGE`
- **AND** the intake holds only the earlier saves

#### Scenario: An edit of a page that a queued save creates still queues

- **WHEN** an owner sends an edit of a page that a queued save will create
- **AND** that queued save has not been applied yet
- **THEN** the edit is queued behind it, not refused for the missing page

#### Scenario: An argument refusal comes back when the egress filter faults

- **WHEN** an owner sends a `record_memory` call whose arguments its action refuses
- **AND** earlier saves are still queued
- **AND** the terminal egress filter faults
- **THEN** the call returns that refusal
- **AND** the intake holds only the earlier saves

## ADDED Requirements

### Requirement: A Records value its declared fields refuse is refused before queueing

Before the intake queues a `record_memory` append with an object `item`, or an update with an object `changes`, whose `collection` is a string, the serving process SHALL check the supplied values against the per-field rules of the declared fields they name: type, format, enum and nested values. A value that those rules refuse SHALL get that refusal at once and SHALL NOT be queued. Required fields, undeclared names and keys SHALL stay at the replay.

#### Scenario: An undeclared Records value behind the queue is refused at once

- **WHEN** an owner sends a `record_memory` append or update whose value lies outside a field's declared enum
- **AND** earlier saves are still queued
- **AND** there is no open queued save of a command that declares `sets_any_frontmatter_field`, and no open `record_memory` save carrying `manifest_text`
- **THEN** the call returns `SCHEMA_ENUM`
- **AND** the intake holds only the earlier saves

### Requirement: The declared Records check queues the save when it cannot be certain

The declared Records check SHALL leave the save queued when an open queued save of a command that declares `sets_any_frontmatter_field`, or an open `record_memory` save with a non-null `manifest_text`, exists; when the declaration read or the queue read fails or misses their shared 0.5 s budget; or when the egress filter faults. A declaration change made outside the queue and not committed at the check, such as a direct revise that holds the boundary, is not seen, so a value it adds is refused.

#### Scenario: A Records value behind a queued edit of its manifest still queues

- **WHEN** an owner sends a `record_memory` update whose value lies outside a field's declared enum
- **AND** an open queued save of a command that declares `sets_any_frontmatter_field`, or an open `record_memory` save carrying `manifest_text`, has not been applied yet
- **THEN** the update is queued behind it
- **AND** its replay checks the value against the declared fields that the queued save leaves

#### Scenario: A value that a queued revision declares is saved

- **WHEN** a `record_memory` revise that adds a value to a field's enum is queued
- **AND** an owner then sends an update of an existing row, named by its id, with that value
- **THEN** the update is queued
- **AND** once its replay is applied, the row holds the value

### Requirement: The declared Records refusal holds nothing and names the resend

The declared Records refusal SHALL carry the refusal code, the field, the allowed values in its message and the per-field issues. It SHALL hold nothing: no candidate is held and the intake keeps no copy of the save. Its remediation SHALL name resending as the route: with a value that the collection declares, or with the same value once a change that declares it has committed. It SHALL leave through the same terminal egress filter as a direct refusal.

#### Scenario: A busy refusal leaves through the egress filter as a direct one does

- **WHEN** a collection's declared values include a credential
- **AND** an owner sends an `add_record` with an undeclared value, once directly and once behind the queue
- **THEN** both calls return `SCHEMA_ENUM` with the credential blocked
- **AND** the busy refusal says that nothing is held
