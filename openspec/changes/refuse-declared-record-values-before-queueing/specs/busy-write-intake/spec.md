## MODIFIED Requirements

### Requirement: A save its arguments condemn is refused, not queued

Before the intake accepts a save, the serving process SHALL apply the argument-only checks that exist for it: compact observation metadata, an episode recap's own validation, and the argument rules of `record_memory` and `plan_memory`. A save that one of them refuses SHALL get that refusal at once and SHALL NOT be queued, also when the terminal egress filter faults. A refusal that reads vault state SHALL stay at the replay, except the declared value refusal that the requirements below define.

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
- **AND** there is no open queued save of a command that declares `sets_any_frontmatter_field`, and no open `record_memory` or `plan_memory` save carrying `manifest_text`
- **THEN** the call returns `SCHEMA_ENUM`
- **AND** the intake holds only the earlier saves

### Requirement: A Planning value that the vault does not define is refused before queueing

Before the intake queues a `plan_memory` add with an object `item`, an update with an object `changes` or a triage with an object `transition`, whose `collection` is a string, the serving process SHALL check each supplied value that is not null: a field that the Planning values govern with the Planning commit's own rule, then every field with the declared per-field rules. A refused value SHALL get that refusal at once and SHALL NOT be queued.

#### Scenario: A Planning status outside the Planning values behind the queue is refused at once

- **WHEN** an owner sends a `plan_memory` add, update or triage whose `status` is not one of the vault's Planning statuses, for an update or triage unless the target item stores that value
- **AND** earlier saves are still queued
- **AND** there is no open queued save of a command that declares `sets_any_frontmatter_field`, and no open `record_memory` or `plan_memory` save carrying `manifest_text`
- **THEN** the call returns `INVALID_PLAN` with the statuses that the vault defines
- **AND** the intake holds only the earlier saves

### Requirement: The declared value check queues the save when it cannot be certain

The declared value check SHALL leave the save queued when an open queued save of a command that declares `sets_any_frontmatter_field`, or an open save that carries a non-null `manifest_text`, exists; when the declaration read or the queue read fails or misses their shared 0.5 s budget; or when the egress filter faults. A declaration change outside the queue that has not committed at the check, such as a direct revise or a Planning values save, is not seen, so a value it adds is refused.

#### Scenario: A Records value behind a queued edit of its manifest still queues

- **WHEN** an owner sends a `record_memory` update whose value lies outside a field's declared enum
- **AND** an open queued save of a command that declares `sets_any_frontmatter_field`, or an open `record_memory` or `plan_memory` save carrying `manifest_text`, has not been applied yet
- **THEN** the update is queued behind it
- **AND** its replay checks the value against the declared fields that the queued save leaves

#### Scenario: A value that a queued revision declares is saved

- **WHEN** a `record_memory` or `plan_memory` revise that adds a value to a field's enum is queued
- **AND** an owner then sends an append or add with that value, or an update of an existing row, named by its id, with that value
- **THEN** the save is queued
- **AND** once its replay is applied, the row holds the value

### Requirement: A Planning value that its item may keep is not refused before queueing

For a `plan_memory` update or triage, a value that the Planning values deprecate or no longer define SHALL stay queued when the item that the save names by `plan_id` or `item_id` stores that value in that field, as the commit's rule accepts it. Another item's stored value SHALL NOT exempt it. The value SHALL also stay queued when no one item that the caller may read answers to that name.

#### Scenario: A busy Planning update that keeps a removed status is saved

- **WHEN** a Planning item stores a status that the owner has since removed from the Planning values
- **AND** an owner sends a `plan_memory` update of that item with the same status behind queued saves
- **THEN** the update is queued
- **AND** once its replay is applied, the item holds the status

#### Scenario: Another item's removed status behind the queue is refused at once

- **WHEN** a Planning item stores a status that the owner has since removed from the Planning values
- **AND** an owner sends a `plan_memory` update of another item with that status behind queued saves
- **THEN** the call returns `INVALID_PLAN` with the statuses that the vault defines
- **AND** the intake holds only the earlier saves

### Requirement: The Planning check leaves the rest to the replay

The Planning check SHALL leave a governed value to the replay when the caller cannot read the vault's Planning values. Planning's own single-field rules SHALL stay at the replay: the title's length, the fixed lifecycle set, window dates, the bound on results and motivation references. Rules that join fields, such as the lifecycle rules and what an area may carry, SHALL stay at the replay too.

#### Scenario: A Planning value that only a single-field rule refuses still queues

- **WHEN** an owner sends a `plan_memory` add whose every governed value the vault defines, but whose title exceeds its length bound
- **AND** earlier saves are still queued
- **THEN** the add is queued
- **AND** its replay refuses it

### Requirement: The declared value refusal holds nothing and names the resend

A declared Records refusal SHALL carry the refusal code, the field, the allowed values in its message and the per-field issues. A declared Planning refusal SHALL carry the code and message that the commit's rule gives for that value. Each SHALL hold nothing: no candidate is held and the intake keeps no copy of the save. Its remediation SHALL name resending as the route. It SHALL leave through the same terminal egress filter as a direct refusal.

#### Scenario: A busy refusal leaves through the egress filter as a direct one does

- **WHEN** a collection's declared values include a credential
- **AND** an owner sends an `add_record` with an undeclared value, once directly and once behind the queue
- **THEN** both calls return `SCHEMA_ENUM` with the credential blocked
- **AND** the busy refusal says that nothing is held
