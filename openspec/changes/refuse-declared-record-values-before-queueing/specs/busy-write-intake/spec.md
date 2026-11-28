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

For store-owned Planning saves, the intake SHALL check supplied values, including an add's explicit null, against Planning and field rules. It SHALL give a value refusal only if the normal route is open, this is the writer's first refusal, and any required store read below succeeds. Otherwise it SHALL queue.

#### Scenario: A Planning status outside the Planning values behind the queue is refused at once

- **WHEN** an owner sends a `plan_memory` add whose `status` is null or not one of the vault's Planning statuses
- **AND** the collection is store-owned and its save route is open
- **AND** earlier saves are still queued
- **AND** there is no open queued save of a command that declares `sets_any_frontmatter_field`, and no open `record_memory` or `plan_memory` save carrying `manifest_text`
- **THEN** the call returns `INVALID_PLAN` with the statuses that the vault defines
- **AND** the intake holds only the earlier saves

#### Scenario: The busy check honours a disabled summary route

- **WHEN** an owner sends a `plan_memory` add with an undeclared enum value to a store-owned summary collection
- **AND** the summary release gate is off and the store is busy
- **THEN** the save queues for the route's own refusal at replay without disclosing the collection's enum values

#### Scenario: A read-only Cloud cell refuses a save with a backlog

- **WHEN** a Cloud cell is in read-only mode, the store is busy and an earlier save is queued
- **AND** an owner sends a Planning save with an undeclared value
- **THEN** the ordinary dispatcher refuses the write with `CLOUD_CELL_READ_ONLY`
- **AND** the intake holds only the earlier save and discloses no allowed values

#### Scenario: A busy add to a file collection is queued

- **WHEN** an owner sends a `plan_memory` add with an undeclared enum value to a file-backed collection
- **AND** earlier saves are still queued
- **THEN** the save is queued
- **AND** its replay gives the same refusal as a direct add

### Requirement: The declared value check queues the save when it cannot be certain

The intake SHALL mark the queue before it reads the declaration and, after a refusal, look again from that mark. The check SHALL leave the save queued when a save open at the mark or accepted after it, in whatever state it is now, may change a declaration by its record or has no record; when a read fails or its answer is unknown; or when the egress filter faults. A declaration change outside the queue that has not committed at the check is not seen.

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

#### Scenario: A value that a revision commits during the declaration read is saved

- **WHEN** a `plan_memory` revise that adds a value to a field's enum is open when the intake marks the queue for an add with that value, or enters the queue while the intake reads the declaration
- **AND** the revise commits before the intake looks at the queue again
- **THEN** the add is queued
- **AND** once its replay is applied, the row holds the value

#### Scenario: An ordinary save that commits during the declaration read leaves the refusal

- **WHEN** an owner sends an append or add whose value lies outside a field's declared enum
- **AND** an ordinary queued save is open when the intake marks the queue and is applied while the intake reads the declaration
- **THEN** the call returns `SCHEMA_ENUM`
- **AND** the intake holds no copy of the refused save

### Requirement: The intake records which queued saves may change a declaration

The intake SHALL mark an accepted save as a possible declaration change when its command declares `sets_any_frontmatter_field` or may carry non-null `manifest_text`. An unmarked legacy save SHALL also count. At the next acceptance, an older store SHALL initialise the last possible change to its highest existing sequence in fixed work without scanning its saves.

#### Scenario: A store from before the record keeps a save behind its open revision queued

- **WHEN** an intake store without that record holds an open revise that adds a value to a field's enum
- **AND** an owner sends an append or add with that value, after another busy save or while another acceptance upgrades the store
- **THEN** the save is queued
- **AND** once its replay is applied, the row holds the value

#### Scenario: A revision that an earlier release queues keeps a save with its value queued

- **WHEN** an earlier release, after a rollback, queues a revise that adds a value to a field's enum, without a record
- **AND** an owner then sends an append or add with that value
- **THEN** the save is queued
- **AND** once its replay is applied, the row holds the value

#### Scenario: The upgrade reads no stored save

- **WHEN** an intake store without that record holds open saves of several MiB each
- **AND** an owner sends a busy save
- **THEN** the save is queued
- **AND** the upgrade holds none of the stored saves in memory

#### Scenario: The first acceptance does fixed work whatever the old history holds

- **WHEN** a save reaches the intake for the first time after upgrading an older store
- **THEN** the upgrade records the last legacy sequence without scanning or sorting past saves
- **AND** the work to add the marker does not grow with the number of past saves

### Requirement: The declared value check stays within its budget whatever the queue holds

Each queue read SHALL use fixed work. The check SHALL have 0.5 s to decide. Queue lock waits, store admission and coordinator timeouts SHALL use at most the time left. A locked store reader SHALL stop the check. Planning SHALL cap store SQL work. If either budget runs out, the save SHALL queue. The service SHALL release a borrowed checkout before returning the receipt, even after the decision deadline.

#### Scenario: A Planning save queues when its authorisation proof needs too much SQL

- **WHEN** an owner sends a busy Planning save with an undeclared value
- **AND** the store needs more SQL work than the busy check allows to authorise the save
- **THEN** the save is queued
- **AND** its replay follows the normal store checks

#### Scenario: A busy save reads no more of a longer queue

- **WHEN** an owner sends an append or add whose values the vault declares, or one whose value lies outside a field's declared enum
- **AND** earlier saves are still queued
- **THEN** the first save is queued and the second is refused with `SCHEMA_ENUM`
- **AND** the reads of the queue for each cost the same as behind a shorter queue

#### Scenario: A queue that answers after the budget queues the save

- **WHEN** an owner sends an append or add whose value lies outside a field's declared enum
- **AND** earlier saves are still queued
- **AND** the queue answers the look after the refusal only once the budget has run out
- **THEN** the save is queued
- **AND** its replay refuses it with `SCHEMA_ENUM`

#### Scenario: An open that spends most of the budget on a locked queue queues the save within it

- **WHEN** an owner sends an append or add whose value lies outside a field's declared enum
- **AND** another connection locks the queue, and opening the queue spends most of the budget
- **THEN** the save is queued within about the budget
- **AND** its replay refuses it with `SCHEMA_ENUM`

### Requirement: A Planning update is refused before queueing only for an item its store reads by key

A `plan_memory` update or triage with a value that the Planning check refuses SHALL be refused before queueing only when it names its item by `plan_id` and both guards, the store that owns the collection reads that item by its key, the guards equal the collection's container hash and the item's version, the caller reads the item whole, and, for a field the Planning values govern, the item does not store that value. Otherwise the save SHALL be queued.

#### Scenario: A busy Planning update that keeps a removed status is queued

- **WHEN** an item of a store-owned Planning collection stores a status that the owner has since removed from the Planning values
- **AND** an owner sends a `plan_memory` update of that item with the same status while the store is busy
- **THEN** the update is queued

#### Scenario: Another item's removed status on a busy store is refused at once

- **WHEN** an item of a store-owned Planning collection stores a status that the owner has since removed from the Planning values
- **AND** an owner sends a `plan_memory` update of another item, by its `plan_id` and both guards, with that status while the store is busy
- **THEN** the call returns `INVALID_PLAN` with the statuses that the vault defines
- **AND** the intake holds no copy of the update

#### Scenario: A busy update by a stale item id is queued

- **WHEN** an owner queries a Planning item and then changes it
- **AND** the owner sends a `plan_memory` update with the item's old `item_id` and a status outside the Planning values while the store is busy
- **THEN** the update is queued, not refused for its status

#### Scenario: A busy update of a file collection is queued

- **WHEN** an owner sends a `plan_memory` update or triage of a file collection item with a status outside the Planning values
- **AND** earlier saves are still queued
- **THEN** the save is queued
- **AND** its replay refuses it with `INVALID_PLAN`

### Requirement: The Planning check leaves the rest to the replay

The Planning check SHALL leave to the replay a governed value when the caller cannot read the vault's Planning values, an update's or a triage's null, and a save that its commit refuses first for another reason, such as an area's triage. Planning's own single-field rules MAY stay there: the title's length, the fixed lifecycle set, window dates, the bound on results and motivation references. Rules that join fields SHALL stay there, such as the lifecycle rules and what an area may carry.

#### Scenario: A busy area add with fields an area may not carry is refused at the replay for what an area carries

- **WHEN** an owner sends a `plan_memory` add of an area whose `status` is not one of the vault's Planning statuses and whose custom field's value lies outside its declared enum
- **AND** the collection is store-owned
- **AND** earlier saves are still queued
- **THEN** the add is queued
- **AND** its replay refuses it with `INVALID_PLAN` because an area cannot carry a status

#### Scenario: A busy triage of an area is refused at the replay as a direct triage is

- **WHEN** an item of a store-owned Planning collection is an area
- **AND** an owner sends a `plan_memory` triage of it, by its `plan_id` and both guards, that sets `parent` to null and `kind` to a value that Planning does not define, while the store is busy
- **THEN** the triage is queued
- **AND** its replay refuses it with `INVALID_PLAN` because only an active deliverable plan can be triaged

#### Scenario: A busy triage with a malformed transition is refused at the replay as a direct triage is

- **WHEN** an owner sends a `plan_memory` triage of an active work item of a store-owned Planning collection, by its `plan_id` and both guards, whose `transition` sets `kind` to a value that Planning does not define and also sets `status` to null, while the store is busy
- **THEN** the triage is queued
- **AND** its replay refuses it with `INVALID_PLAN_ARGUMENTS` because the transition is invalid

### Requirement: The Planning check judges an area save by the fields it gives

A `plan_memory` save of an item that is or was an area, which gives it a field that an area may not carry, SHALL be left whole to the replay, whose area rule judges it. An add's null gives its field. An update's or a triage's null deletes its field and gives none, so the check SHALL still judge that save's other values.

#### Scenario: A busy area update that deletes a field it lacks is refused for its other value

- **WHEN** an item of a store-owned Planning collection is an area with no parent
- **AND** an owner sends a `plan_memory` update of it, by its `plan_id` and both guards, that sets `parent` to null and a custom field to a value outside its declared enum, while the store is busy
- **THEN** the call returns `SCHEMA_ENUM`
- **AND** the intake holds no copy of the update

### Requirement: The declared value refusal holds nothing and names the resend

A declared Records refusal SHALL carry the refusal code, the field, the allowed values in its message and the per-field issues. A declared Planning refusal SHALL carry the code and message that the commit's rule gives for that value. Each SHALL hold nothing: no candidate is held and the intake keeps no copy of the save. Its remediation SHALL name resending as the route. It SHALL leave through the same terminal egress filter as a direct refusal.

#### Scenario: A busy refusal leaves through the egress filter as a direct one does

- **WHEN** a collection's declared values include a credential
- **AND** an owner sends an `add_record` with an undeclared value, once directly and once behind the queue
- **THEN** both calls return `SCHEMA_ENUM` with the credential blocked
- **AND** the busy refusal says that nothing is held
