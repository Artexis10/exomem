## ADDED Requirements

### Requirement: schema_memory serves one contract over every vocabulary registry
For the `entity-types`, `relations`, `source-kinds`, `domains` and `categories` subjects, `schema_memory` SHALL serve the same five operations:

- `inspect`: the live entries in the generic entry shape, paginated through `limit` and `continuation`, with usage counts, the content hash, the effective digest and the findings;
- `propose`: read-only; the normalised entry, the attributes it inherits from its parent, exact collisions, near-duplicates with their counts and the `expected_hash` a save needs;
- `save`: a delta of `upsert`, `alias` and `deprecate` with `expected_hash` and a non-empty `why`;
- `history` and `restore`.

The existing operation names (`save-entity-types`, `resolve-entity-type`, `propose-relation`, `save-relations`, `census`, `infer`, `validate`, `diff`) SHALL keep working. The tool description SHALL name the contract once. A new registry SHALL be a new `subject` value and SHALL NOT change the published tool surface or the egress selector table. Live vocabulary SHALL NOT appear in any tool schema. `propose` SHALL be classified as a `structure` selector.

#### Scenario: A stale delta is refused
- **WHEN** two agents read the same `expected_hash` and the first saves a new entry
- **THEN** the second save fails with a stale-hash error and writes nothing
- **AND** a fresh `propose` reports the first agent's entry as a collision

#### Scenario: Meaning is not rewritten in place
- **WHEN** a delta changes the parent of an existing entry or removes one of its aliases
- **THEN** the save is refused and the overlay is unchanged

### Requirement: Registry saves follow the owner governance rule
The governance decision for a registry save SHALL be `owner_only_aggregate`. When it returns no refusal, a save SHALL take effect at once. When it refuses the caller, a `save` SHALL leave the registry unchanged and SHALL record a pending work item in `review_memory(mode="vocabulary")`. That item SHALL carry the reason and delta and remain visible to the owner only. A registry without a review family SHALL refuse with `audience_restricted`. A `restore` by such a caller SHALL refuse with `audience_restricted`. Usage counts and save reasons SHALL follow the same shared admission decision, including RAW protection without configured file policy.

#### Scenario: A delegate's save becomes a pending proposal
- **WHEN** a bound principal refused by `owner_only_aggregate` saves a new entity type, with or without configured file policy
- **THEN** the response reports a pending vocabulary item and the registry hash is unchanged
- **AND** the owner's `review_memory(mode="vocabulary")` lists the item with the delegate's reason and delta
