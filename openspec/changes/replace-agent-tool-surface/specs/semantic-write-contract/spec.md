## ADDED Requirements

### Requirement: Guards Ride In A Short Id That The Leaf Compares

A write that guards a version SHALL take one short id in place of hash, fingerprint and token fields. A short id SHALL be 7 base32 characters of the identity, a dot, and 4 base32 characters of the version. A unit id, a row id, a plan item id, a review item id and a page id SHALL follow this rule. The leaf SHALL resolve the identity and compare the version after the write is admitted and under the writer lease, never at the command seam. A stale id SHALL refuse with the current id and a retry that carries it. A restricted caller SHALL receive, for an id that names a withheld unit or row, the refusal that an unknown id receives.

#### Scenario: A stale unit id refuses with the current id
- **WHEN** a caller edits an observation by a unit id issued before the observation changed
- **THEN** the edit refuses and the refusal names the current unit id
- **AND** a retry with the current id succeeds

#### Scenario: A row id guards a record update
- **WHEN** a caller updates a record by the row id that the query returned
- **THEN** the update commits without any hash field

#### Scenario: An id of a withheld row reads as unknown
- **WHEN** a restricted caller names a row id that only a withheld row holds
- **THEN** the refusal equals the refusal for an id that no row holds

### Requirement: The Container Hash Is Filled At Commit

A records or plans write that names a row id SHALL NOT require `expected_container_hash`. The leaf SHALL fill it under the writer lease. REST and CLI SHALL keep accepting it.

#### Scenario: A row update needs no container hash
- **WHEN** a caller updates a row by id and sends no container hash
- **THEN** the update commits against the container version at commit time

### Requirement: An Unlinked Reason Replaces The Relation Review Triple

A note write over MCP SHALL accept one `unlinked_reason`. The server SHALL bind the reviewed-none decision and its reason to the exact committed content. A write that carries no reason and links nothing SHALL refuse with the existing relation-disposition refusal and a retry that names `unlinked_reason`.

#### Scenario: One call writes an unlinked note
- **WHEN** a caller creates a note that links no page and gives an unlinked reason
- **THEN** the note commits in that call with the reviewed-none decision recorded
