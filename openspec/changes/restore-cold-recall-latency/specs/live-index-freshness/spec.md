## MODIFIED Requirements

### Requirement: Event-Maintained Markdown Freshness Keys

The system SHALL maintain, in memory, a per-scope registry of policy-admitted `{path: signature}` rows for markdown under each recall scope (`kb`, `vault`), updated from a safely enumerated seed, the live file watcher, and in-process writer events. A live registry SHALL answer a scope's freshness triple and exact allowed-path projection without a request-time filesystem walk. Seed and reconcile SHALL publish a complete replacement map and its checkpoint atomically; readers SHALL continue observing the last proven map until the replacement is authoritative. Observation events received before initial seed publication SHALL be retained and replayed against the published generation. Event-derived paths SHALL retain Windows long-name canonicalisation, reparse/no-follow validation, access-policy checks, and Records/Planning admission before publication.

Activated server consumers MUST NOT fall back to a walk when a registry is not live; they SHALL report retrieval warming/unavailable and allow background recovery to establish authority. Explicit offline callers and deployments with event indexes disabled MAY use the prior walk fallback. A rename MUST change the scope digest even when mtime is preserved, and a suppressed self-write MUST still update every affected live projection.

#### Scenario: Live registry answers freshness without a walk

- **WHEN** the recall registry for a scope is live and a caller requests its checkpoint and allowed paths
- **THEN** both are copied from one authoritative in-memory generation with no filesystem walk
- **AND** they equal a fresh policy-projected walk over the same state

#### Scenario: Server with a not-live registry declines without walking

- **WHEN** an activated server request needs a scope whose recall registry is not live
- **THEN** the request receives an explicit warming or unavailable outcome
- **AND** the request does not walk the scope

#### Scenario: Offline caller retains the walk fallback

- **WHEN** an explicit offline caller has no live registry, or event-maintained indexes are disabled
- **THEN** the caller may compute the projection by walking the source tree
- **AND** the same admission and access policy is applied

#### Scenario: Not-live registry falls back to a walk

- **WHEN** an explicit offline caller's freshness registry has never been seeded, or a deployment runs with event-maintained indexes disabled
- **THEN** the freshness triple for that scope is computed by walking the tree exactly as before this capability existed
- **AND** an activated managed server with event-maintained indexes enabled still declines instead of taking this fallback

#### Scenario: Reconcile replacement is atomic

- **WHEN** periodic reconciliation derives a replacement projection while readers are active
- **THEN** readers observe either the complete previous checkpoint/map or the complete replacement checkpoint/map
- **AND** no reader observes a mixed or empty intermediate generation

#### Scenario: Startup event survives seed replacement

- **WHEN** a create, modify, delete, or move is observed after enumeration begins but before initial replacement publication
- **THEN** the event remains buffered until the replacement is authoritative
- **AND** applying the event advances the resulting live generation before consumers rely on it

#### Scenario: Event path aliases are validated once before publication

- **WHEN** Windows reports a changed file through an 8.3, case, or equivalent alias spelling
- **THEN** event ingress canonicalises and validates that changed identity before publishing it
- **AND** the alias cannot bypass Records/Planning suppression or the vault boundary

#### Scenario: A create, modify, delete, or move updates the registry

- **WHEN** an admitted markdown identity is created, modified, deleted, or moved through an external event
- **THEN** every affected live scope advances to a checkpoint containing that change without a full re-seed

#### Scenario: A rename with a preserved mtime still changes the digest

- **WHEN** an admitted markdown identity is renamed without changing its mtime
- **THEN** every affected registry-derived digest changes because the digest includes the canonical relative path

#### Scenario: A suppressed self-write still updates freshness

- **WHEN** an Exomem writer performs a markdown mutation whose watcher echo is suppressed to avoid duplicate embedding work
- **THEN** every affected live projection advances to a checkpoint containing that mutation independently of watcher suppression

#### Scenario: Event-maintained indexes can be disabled wholesale

- **WHEN** the server runs with event-maintained indexes explicitly disabled
- **THEN** no recall registry is treated as live
- **AND** the declared legacy walk-backed fallback remains available
