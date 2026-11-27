## ADDED Requirements

### Requirement: Planning create assigns a new collection's id when the manifest names none
The Planning command's `create` and `validate` of a new collection SHALL accept a manifest whose frontmatter has no `exomem_id` line, as the Records command does. `create` SHALL assign a new id and return it as `collection_id`, and a present but malformed id SHALL still be refused.

#### Scenario: A Planning collection without an id
- **WHEN** a client creates a Planning collection from a manifest that has no `exomem_id` line
- **THEN** the create commits and the receipt's `collection_id` names the id written into the manifest
