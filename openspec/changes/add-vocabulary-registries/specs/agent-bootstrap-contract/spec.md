## ADDED Requirements

### Requirement: Bootstrap serves the live vocabulary by use
The compact bootstrap SHALL serve a `vocabulary` section. For each registry it SHALL list at most 12 active keys ordered by usage count with their counts, a `+n more` line when more keys exist, the number of registry findings and the `schema_memory(operation="inspect")` route for that registry. A registry MAY declare that the section omits its keys. For such a registry the section SHALL still report a findings count that is not zero, its recently added keys under `new`, and `unavailable` when the registry refuses the caller; it SHALL leave the registry out only when none of these applies. Usage counts SHALL come from maintained projections. When a projection is absent, warming, stale or refused, that registry's counts SHALL read `unavailable` with a reason and its keys SHALL be listed in registry order; the section SHALL NOT report a zero it did not count. Usage counts SHALL follow `owner_only_aggregate`, including RAW protection without configured file policy. A caller that it refuses SHALL receive `unavailable` with `audience_restricted`. The always-served core SHALL carry a pointer to the section instead of entity-type ids. `section="entities"` SHALL remain accepted and SHALL serve the `vocabulary` section's blocks.

#### Scenario: A promoted type appears with its count
- **WHEN** an agent promotes an entity type and the graph projection is current
- **THEN** `bootstrap(section="vocabulary")` lists the type under `entity-types` with a counted value
- **AND** after an entity page of that type is indexed, its count is 1

#### Scenario: A cold projection is unavailable, not zero
- **WHEN** the graph projection has not been built
- **THEN** the entity-type and relation counts read `unavailable` with a reason
- **AND** no key carries a count of 0

#### Scenario: The core points at the section
- **WHEN** a client reads the compact core
- **THEN** `capture_semantics` names the `vocabulary` section instead of listing entity types
- **AND** the core stays under its byte ceiling

### Requirement: A vocabulary delta rides activation and mutating responses
The system SHALL serve one bounded `vocabulary_delta` block that tells a session about vocabulary added since that session last received the caller's admitted effective digest. The block SHALL list at most 5 entries in about 700 characters and SHALL name, for each entry, its registry, key, display label, a one-line description and its parent where present. The block SHALL read the registry-history `added` producer that the bootstrap `new` marker reads, and SHALL list only active entries of instances that the caller is admitted to. A session that has received no digest SHALL see the additions of the bootstrap `new` window. The block SHALL ride the activation packet and the response of a committed mutating call. The system SHALL record delivery in the due-state emission ledger, keyed by session, audience and vault, and only when the response carried the block. A session SHALL receive one block for each change of its admitted digest, the session that made the change included. A call that carries no session, by transport or by the caller's `session` argument, SHALL receive no block. A digest change that adds no entry SHALL emit nothing. An activation SHALL count the block inside `limit_chars` and SHALL omit it when it does not fit. The block SHALL list the newest entries first, SHALL order ties by registry and key, and SHALL report a `more` count of the admitted entries that it omitted. A caller SHALL learn nothing from the block about an instance that it is not admitted to, including through counts, order or truncation. The retrieval hook SHALL render the block in working-set mode and SHALL show it again after the client compacts or resumes the session.

#### Scenario: A promotion reaches another session once
- **WHEN** one session promotes a relation and another session next calls activation
- **THEN** the activation packet carries a `vocabulary_delta` that names the relation with its registry, key, label, description and parent
- **AND** the second session's following activation and mutating responses carry no `vocabulary_delta`
- **AND** a further promotion makes the next response carry a delta that names only the new entry

#### Scenario: A mutating response carries the delta
- **WHEN** another session promotes an entity type and this session next completes a committed write
- **THEN** the write response carries the `vocabulary_delta` once
- **AND** a `response_detail` of `legacy` carries none and records no delivery

#### Scenario: A restricted caller learns nothing of a withheld promotion
- **WHEN** a promotion lands in an instance that the caller is not admitted to
- **THEN** the caller's activation packet and mutating responses carry no `vocabulary_delta` and no count
- **AND** the presence, content and count of the block are those that a vault without the promotion produces

#### Scenario: The block is bounded and counted
- **WHEN** more than 5 admitted entries are new to the session
- **THEN** the block lists the 5 newest by version, registry and key, and reports `more` for the rest
- **AND** the activation packet's `used_chars` grows by the characters of the block, and the block is omitted when `limit_chars` leaves no room for it

#### Scenario: Compaction shows the delta again
- **WHEN** the client compacts or resumes the session after the hook rendered a delta
- **THEN** the next prompt in working-set mode renders that delta once more
