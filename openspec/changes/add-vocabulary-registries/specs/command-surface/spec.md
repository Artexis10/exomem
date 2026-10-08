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
Registry mutation authorization SHALL use existing bound-principal ownership and canonical vocabulary writer authority. Whole-vault disclosure admission SHALL NOT confer or remove write authority. In v1, a valid owner's save SHALL take effect without an approval item. A content restriction alone SHALL NOT route that owner for review. A resolved nonowner's save SHALL preserve the existing pending-item route and leave the registry unchanged. The item SHALL carry the reason and delta and remain visible only to its authorized owner. A registry without a review family SHALL refuse with `audience_restricted`.

Restore SHALL require owner write authority independently of disclosure permission. Missing principal context and the hosted RAW exemption SHALL NOT confer owner write authority. Trusted internal calls SHALL establish their existing explicit library scope without elevating a bound remote principal.

Explicitly activated v2 vaults SHALL retain canonical effect classification and writer authority. Registry code SHALL NOT replace that gate with disclosure admission or a new S1 queue. S1 SHALL NOT activate v2 or require its opt-in owner-control process for v1 owners.

Counts, history reasons, collision details and other private-dependent results SHALL follow content admission independently, including RAW protection without configured file policy. An operation requiring unavailable private information SHALL report unavailable until an admitted or domain-aware implementation exists.

#### Scenario: A delegate's save becomes a pending proposal
- **WHEN** a resolved nonowner saves a new entity type in v1, with or without configured file policy
- **THEN** the response reports a pending vocabulary item and the registry hash is unchanged
- **AND** the owner's `review_memory(mode="vocabulary")` lists the item with the delegate's reason and delta

#### Scenario: A limited owner performs a permitted save
- **WHEN** a verified owner in v1 saves admitted vocabulary without depending on private definitions
- **THEN** the valid save takes effect without an approval item
- **AND** unavailable private-dependent counts remain unavailable independently of the write

#### Scenario: A hosted exemption supplies no owner authority
- **WHEN** a resolved hosted nonowner is exempt from RAW admission and saves vocabulary in v1
- **THEN** the operation uses the existing nonowner route
- **AND** unrestricted aggregate disclosure cannot turn the caller into the owner

#### Scenario: An activated v2 write uses its existing authority
- **WHEN** a caller saves vocabulary in an explicitly activated v2 vault
- **THEN** the canonical writer applies its existing classification and authority checks
- **AND** registry routing adds no S1 approval queue and changes no activation mode
