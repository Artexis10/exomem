## MODIFIED Requirements

### Requirement: Idempotent append and conflict-safe update
The substrate SHALL make exact append retries idempotent where a stable item identity is available. When a caller omits the item identity and every field of the manifest's declared natural key is present in the validated values, the substrate SHALL derive the identity deterministically from the collection identity and the natural-key serialisation the read path already uses, and stamp it explicitly like any other item key; an explicit identity SHALL still win, and a payload that lacks a natural-key field SHALL receive a random identity as before. Reusing one identity with different content SHALL refuse as an identity conflict. An append whose derived or supplied identity differs from an existing item's while its serialised natural key equals that item's SHALL refuse as a natural-key conflict naming every such existing item, so the new item cannot shadow an older one keyed differently. Targeted update SHALL change only the resolved item and SHALL never fall back from a missing identifier to fuzzy text matching. Both profiles SHALL inherit these rules through the shared mechanics.

#### Scenario: Exact append retry produces one item
- **WHEN** a client retries the same append with the same collection, item identity, and normalized payload
- **THEN** the substrate returns the committed item without adding a duplicate

#### Scenario: Re-stated append without identity replays
- **WHEN** a client appends the same observation twice without supplying an item identity and the payloads are identical
- **THEN** the second append returns the committed item as a replay and the collection holds one item

#### Scenario: Reused identity with different content refuses
- **WHEN** an append supplies an existing item identity with materially different content, or omits the identity and the derived identity already exists with different content
- **THEN** it refuses with a record identity conflict and preserves the existing item

#### Scenario: Natural-key twin of an older item refuses
- **WHEN** an append's natural-key values equal those of an existing item whose identity was minted before derivation existed
- **THEN** it refuses with a natural-key conflict that names the existing item and writes nothing

#### Scenario: Planning titles stop duplicating
- **WHEN** a Planning collection declares `[title]` as its natural key and an agent adds a work item whose title already exists
- **THEN** the add replays when the payload is identical and refuses otherwise, so the agent updates the existing item instead of filing a twin

#### Scenario: Missing identifier does not select a similar item
- **WHEN** targeted update names an identifier that no longer exists
- **THEN** it refuses as missing or stale and does not update an item with similar text or fields
