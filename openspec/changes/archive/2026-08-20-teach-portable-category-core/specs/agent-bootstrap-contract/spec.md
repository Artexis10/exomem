## ADDED Requirements

### Requirement: Bootstrap Teaches Portable Semantic Authoring

The bootstrap contract SHALL project the versioned portable category vocabulary and explain that category is one primary role-or-domain lens, kind is the governed form, tags are secondary facets, and relations are typed links. Compact bootstrap MUST include the core keys, the open-category escape hatch, and one compact example. Full bootstrap MUST add a generic rich example and category-selection guidance. Neither profile may inspect private vault content.

#### Scenario: Compact bootstrap is sufficient to author a reusable unit

- **WHEN** a generic agent calls `bootstrap(profile="compact")`
- **THEN** the response includes the canonical core category keys and one parseable compact unit
- **AND** the response states that an intentional unknown category remains valid

#### Scenario: Full bootstrap teaches rich relations

- **WHEN** a generic agent calls `bootstrap(profile="full")`
- **THEN** the response includes a generic rich semantic block with a stable identifier and typed relation
- **AND** no private path, project name, or vault-derived vocabulary appears
