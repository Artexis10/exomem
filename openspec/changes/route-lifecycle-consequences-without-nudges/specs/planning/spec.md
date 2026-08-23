## ADDED Requirements

### Requirement: Planning inventory is available before a selector is known
Planning SHALL expose a bounded inventory of Planning collections — every manifest whose profile is `planning`, in the same shape and under the same disclosure filtering as the Records inventory — through `plan_memory(action="inspect")` with no collection named. Reading the inventory SHALL create nothing and SHALL NOT resolve evidence descriptors, execution pointers, or Records. With a collection named, `inspect` SHALL behave exactly as before.

#### Scenario: Fresh session lists Planning collections
- **WHEN** an agent calls `inspect` without a collection in a vault holding two Planning collections and one Records collection
- **THEN** the response lists exactly the two Planning collections with their manifest references and declared natural keys

#### Scenario: Withheld collections are absent
- **WHEN** a Planning manifest is withheld from the requesting audience by the release plane
- **THEN** the inventory omits it and its count equals the vault with it absent

#### Scenario: Inventory does not widen other actions
- **WHEN** `query`, `add`, `update` or `triage` is called without a collection
- **THEN** validation refuses as before
