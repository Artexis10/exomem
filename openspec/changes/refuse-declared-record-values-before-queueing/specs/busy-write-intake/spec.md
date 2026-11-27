## MODIFIED Requirements

### Requirement: A save its arguments condemn is refused, not queued

Before the intake accepts a save, the serving process SHALL apply the argument-only checks that exist for it: compact observation metadata, an episode recap's own validation, and the argument rules of `record_memory` and `plan_memory`. A save that one of them refuses SHALL get that refusal at once and SHALL NOT be queued. A refusal that reads vault state SHALL stay at the replay, except a Records value that its collection's declared fields certainly refuse, which SHALL be refused at once.

#### Scenario: An oversized recap behind the queue is refused at once

- **WHEN** an owner sends an `episode_memory` record whose decided item exceeds its size cap
- **AND** earlier saves are still queued
- **THEN** the call returns `EPISODE_TOO_LARGE`
- **AND** the intake holds only the earlier saves

#### Scenario: An edit of a page that a queued save creates still queues

- **WHEN** an owner sends an edit of a page that a queued save will create
- **AND** that queued save has not been applied yet
- **THEN** the edit is queued behind it, not refused for the missing page

#### Scenario: An undeclared Records value behind the queue is refused at once

- **WHEN** an owner sends a `record_memory` append or update whose value lies outside a field's declared enum
- **AND** earlier saves are still queued
- **AND** none of them can change the collection's declared fields
- **THEN** the call returns `SCHEMA_ENUM` through the terminal egress filter, as the save's own refusal would
- **AND** the intake holds only the earlier saves

#### Scenario: A Records value behind a queued edit of its manifest still queues

- **WHEN** an owner sends a `record_memory` update whose value lies outside a field's declared enum
- **AND** a queued save that can rewrite a page's frontmatter, or a queued Records save that carries a manifest's text, has not been applied yet
- **THEN** the update is queued behind it
- **AND** its replay checks the value against the declared fields that the queued save leaves
