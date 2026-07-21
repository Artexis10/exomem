## ADDED Requirements

### Requirement: One Declarative Registry Defines Entity Kinds
The system SHALL resolve entity kinds from one declarative registry used by entity validation, folder routing, rendering, index maintenance, bootstrap guidance, and pack validation. Built-in definitions MUST include the existing `person`, `concept`, `library`, and `decision` kinds unchanged and MUST add `organization` routed to `Entities/Organizations/`.

#### Scenario: Organization is created through the canonical entity route
- **WHEN** a caller creates a valid entity with `entity_type="organization"`
- **THEN** Exomem writes one governed entity beneath `Knowledge Base/Entities/Organizations/`
- **AND** the page, entity index, activity record, and returned metadata identify the registered `organization` kind

#### Scenario: Existing kinds retain their paths
- **WHEN** a caller creates a person, concept, library, or decision after the registry migration
- **THEN** the entity uses the same folder, frontmatter, validation, and command route as before
- **AND** no existing entity page is rewritten merely because the registry exists

### Requirement: Knowledge Packs Prioritize Only Registered Entity Kinds
Knowledge-pack `default_entity_types` SHALL be validated against the entity registry and SHALL act as capture priorities rather than a separate validity list. A pack MUST NOT activate an unknown folder or entity type, and selecting or changing a pack MUST NOT make existing registered entity pages unreadable or unwritable.

#### Scenario: Business pack prioritizes organizations
- **WHEN** a selected business-oriented pack includes `organization` in `default_entity_types`
- **THEN** bootstrap prioritizes organization candidates in its bounded entity-capture guidance
- **AND** the registry remains the authority for validation and folder routing

#### Scenario: Pack names an unknown entity kind
- **WHEN** pack validation encounters a `default_entity_types` value absent from the registry
- **THEN** it fails with a stable pack-validation error before that pack can become active
- **AND** no unknown entity folder or page is created

### Requirement: Entity Discovery Is Alias-Aware And Non-Destructive
The registry SHALL expose normalized IDs, labels, and aliases for deterministic candidate lookup. Candidate resolution MUST return an exact active entity, no match, or an ambiguity; it MUST NOT silently merge, rename, supersede, or create an entity.

#### Scenario: Existing entity matches an alias
- **WHEN** a capture workflow checks a durable name that matches one registered alias of one active entity page
- **THEN** Exomem returns that existing entity as the update/link target
- **AND** a duplicate page is not proposed as the default action

#### Scenario: Alias is ambiguous
- **WHEN** one name or alias resolves to multiple active entity pages
- **THEN** Exomem returns an ambiguity with the bounded candidate set
- **AND** it requires agent/user reconciliation rather than choosing a page silently

### Requirement: Entity Indexes Follow The Registry
Initialization and index refresh SHALL derive entity subfolders and count entries from the registry. Adding a supported kind in a release SHALL create or refresh only the required index structure and MUST preserve unrelated human-authored index content.

#### Scenario: New supported registry kind is installed
- **WHEN** a release adds a registry kind whose folder/index entry is absent in an existing vault
- **THEN** Exomem creates the missing governed folder/index structure or adds the bounded generated count entry
- **AND** existing index prose and unrelated entries remain unchanged
