## ADDED Requirements

### Requirement: Parent families are operational derived views
For each active entity type, the registry SHALL derive exactly one family: a core type belongs to its own family, a vault-defined extension with an explicit core `parent` belongs to that parent family, and an existing valid extension with no parent belongs to a self-family named by its leaf ID. Parent SHALL remain optional and the derived view SHALL NOT invalidate or rewrite a parentless v1 extension. The registry SHALL expose deterministic family identity, family membership, and family matching. Canonical Entity frontmatter and folder routing SHALL continue to store and use only the singular leaf ID and that leaf's plural folder.

Exact leaf filtering SHALL remain available. Family-aware query SHALL use the derived `page.entity_family` field, and family-aware Entity traversal SHALL use an explicit `entity_type_families` selector rather than reinterpret generic graph `node_types`. Results SHALL report canonical `entity_type` and derived `entity_family`.

#### Scenario: Child kind rolls up to its core parent
- **GIVEN** active `place` and `venue` extensions whose parent is `concept`
- **WHEN** registry family membership for `concept` is requested
- **THEN** it deterministically contains `concept`, `place`, and `venue`
- **AND** each extension retains its own leaf ID and folder

#### Scenario: Community preserves its specific kind beneath the organization family
- **GIVEN** a vault registers singular leaf kind `community` with plural folder `Communities` and parent `organization`
- **WHEN** an Entity is created with `entity_type="community"`
- **THEN** its canonical kind remains `community`, its path is beneath `Entities/Communities/`, and its derived family is `organization`
- **AND** adding that legitimate kind requires no Exomem code release

#### Scenario: Parentless extension remains compatible as a self-family
- **GIVEN** an active v1 vault extension `initiative` with folder `Initiatives` and no parent
- **WHEN** family identity, exact query, or family query is requested
- **THEN** the extension remains valid with leaf and family `initiative`
- **AND** its canonical frontmatter and `Entities/Initiatives/` projection are unchanged

#### Scenario: Organization family query includes communities
- **GIVEN** active organization and community Entities and a registered `community` child of `organization`
- **WHEN** a caller filters `page.entity_family="organization"`
- **THEN** both leaf kinds match while each result retains its singular canonical `entity_type`

#### Scenario: Family filter is broader than exact leaf filter
- **WHEN** a caller filters `page.entity_family="concept"`
- **THEN** active concept, place, and venue Entities match
- **AND** an exact leaf filter for `entity_type="place"` still returns only place Entities

#### Scenario: Family-aware traversal preserves leaf identity
- **WHEN** Entity traversal is narrowed with `entity_type_families=["concept"]`
- **THEN** eligible concept-family Entity neighbours may be traversed
- **AND** every returned node reports its canonical leaf type and derived family
- **AND** generic `node_types` semantics remain unchanged

#### Scenario: Referent cue accepts a child of the requested family
- **WHEN** a query supplies a concept-family cue and one candidate has leaf type `place`
- **THEN** family matching admits that candidate
- **AND** exact identity ambiguity still refuses automatic selection

#### Scenario: Registry change invalidates family projections
- **WHEN** an extension's active state, parent, or registry identity changes
- **THEN** cached family filters, candidate indexes, and Entity traversal metadata rebuild
- **AND** no Entity Markdown page is rewritten
