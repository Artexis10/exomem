## MODIFIED Requirements

### Requirement: Event-Maintained Markdown Freshness Keys

The system SHALL maintain, in memory, a per-scope registry of policy-admitted `{path: signature}` rows for markdown under each recall scope (`kb`, `vault`), updated from a safely enumerated seed, the live file watcher, and in-process writer events. A live registry SHALL answer a scope's freshness triple and exact allowed-path projection without a request-time filesystem walk. Seed and reconcile SHALL publish a complete replacement map and its checkpoint atomically; readers SHALL continue observing the last proven map until the replacement is authoritative. Observation events received before initial seed publication SHALL be retained and replayed against the published generation. Event-derived paths SHALL retain Windows long-name canonicalisation, reparse/no-follow validation, access-policy checks, and Records/Planning admission before publication.

Activated server consumers MUST NOT fall back to a walk when a registry is not live; they SHALL report retrieval warming/unavailable and allow background recovery to establish authority. Explicit offline callers and deployments with event indexes disabled MAY use the prior walk fallback. A rename MUST change the scope digest even when mtime is preserved, and a suppressed self-write MUST still update every affected live projection.

#### Scenario: Live registry answers freshness and paths without a walk

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

#### Scenario: A create, modify, delete, move, or suppressed self-write updates the registry

- **WHEN** an admitted markdown identity changes through an external event or an Exomem writer
- **THEN** every affected live scope advances to a checkpoint containing that change without a full re-seed
