## ADDED Requirements

### Requirement: Create assigns a new collection's id when the manifest names none
The Records command's `create` and `validate` of a new collection SHALL accept a manifest with no `exomem_id` line. `create` SHALL assign an id and return it as `collection_id`; `validate` SHALL warn `COLLECTION_ID_ASSIGNED_AT_CREATE`. A present `exomem_id` that is not a UUID SHALL still be refused with `INVALID_COLLECTION_ID`. `describe` SHALL mark `exomem_id` optional for a new collection and show no fixed id in its examples.

#### Scenario: A client with no UUID source creates a collection
- **WHEN** a client creates a collection from a manifest that has no `exomem_id` line
- **THEN** the create commits, the written manifest carries a new id, and the receipt's `collection_id` names it

#### Scenario: A malformed id is refused, not replaced
- **WHEN** a client creates a collection from a manifest whose `exomem_id` is not a UUID
- **THEN** the create is refused with `INVALID_COLLECTION_ID` and nothing is written

#### Scenario: A revision with the text the collection was created from
- **WHEN** a client validates or revises an existing collection with a manifest that has no `exomem_id` line
- **THEN** the manifest takes that collection's own id, and a manifest that names another id is still refused
