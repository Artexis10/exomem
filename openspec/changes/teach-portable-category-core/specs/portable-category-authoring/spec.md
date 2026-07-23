## ADDED Requirements

### Requirement: Portable Core Category Vocabulary

The system SHALL expose a versioned immutable core category vocabulary containing exactly `decision`, `fact`, `finding`, `insight`, `constraint`, `requirement`, `assumption`, `risk`, `problem`, `question`, `action`, `technique`, `preference`, `code`, `design`, and `config`. It SHALL resolve only these built-in aliases: `decisions`, `facts`, `findings`, `insights`, `constraints`, `requirements`, `assumptions`, `risks`, `problems`, `questions`, `open_question`, `open_questions`, `actions`, `techniques`, `preferences`, `designs`, `configs`, `configuration`, and `configurations`, mapped to their singular canonical keys as defined by the design. Key and alias normalization MUST use the existing NFKC, case-fold, and whitespace/hyphen/underscore rules.

#### Scenario: Core vocabulary works without an extension registry

- **WHEN** a semantic unit uses `[constraints]` with no semantic-language registry file
- **THEN** the category resolves to core key `constraint`
- **AND** the unit remains authored and retrievable without creating a registry file

#### Scenario: Unknown spelling is not fuzzily coerced

- **WHEN** a syntactically valid category is absent from the exact core alias table and extension registry
- **THEN** resolution preserves its normalized unknown key with status `unregistered`
- **AND** the system does not infer a semantically similar core label

### Requirement: Legacy Core Collisions Do Not Invalidate The Registry

When an existing extension registry defines a key or alias reserved by the core vocabulary, core resolution SHALL win and the extension entry SHALL remain preserved in extension-only serialization. The registry SHALL emit a non-fatal `core_category_shadowed` warning. A warning MUST NOT cause unrelated category resolution to return `registry_invalid`; only error-severity findings may do so.

#### Scenario: Existing config definition survives an upgrade

- **WHEN** a pre-upgrade registry contains a custom `config` category or `configuration` alias
- **THEN** loading after upgrade resolves those labels through core `config`
- **AND** the original extension entry remains present in the serialized proposal with a non-fatal warning
- **AND** an unrelated extension category still resolves normally

### Requirement: Role-First Category Selection With Domain Escape

The authoring contract SHALL define category as one primary open-vocabulary lens. It SHALL instruct agents to prefer a meaningful epistemic or operational role and put domain in tags, while using a domain category when the role would otherwise be generic and the domain is the durable retrieval lens. Kind SHALL remain the governed form, tags secondary facets, and relations typed edges. Unknown well-formed categories MUST remain valid and MUST NOT receive a default ranking boost or write rejection.

#### Scenario: Meaningful role wins over domain

- **WHEN** an agent authors a code-related operating constraint
- **THEN** guidance uses a unit such as `- [constraint] Keep retry windows bounded #code`
- **AND** it does not alternate the same fact between `[constraint]` and `[code]`

#### Scenario: Domain wins when role would be generic

- **WHEN** a durable observation is primarily useful as design knowledge and has no more specific role
- **THEN** guidance permits a unit such as `- [design] Keep the public adapter stateless #api`
- **AND** `design` is the category, `observation` the compact kind, and `api` a secondary tag

### Requirement: Rich Semantic Teaching Examples

The canonical full guidance SHALL include compact paired examples and a rich example demonstrating category, governed kind, tags, stable identifier, and typed relations. It SHALL discourage a redundant explicit rich category when category equals kind, and SHALL encourage several non-duplicative observations and relations only when the note actually contains them.

#### Scenario: Rich kind and category do not duplicate accidentally

- **WHEN** an agent writes a rich `## Decision` whose intended category is also `decision`
- **THEN** guidance tells it to omit redundant `- category: decision`
- **AND** inference and retrieval still treat the defaulted category as core `decision`

### Requirement: Bounded Advisory Category Feedback

Shared semantic write results SHALL provide at most eight deterministic `category_feedback` entries plus `category_feedback_omitted`. Each entry MUST contain `unit_ref`, `authored`, `normalized`, `canonical`, `status`, and nullable `replacement`; `status` MUST be one of `alias`, `deprecated`, `scope_violation`, or `open`. Feedback MUST NOT rewrite the committed note or change default write acceptance.

#### Scenario: Alias feedback encourages canonical reuse

- **WHEN** a successful write authors built-in alias `constraints`
- **THEN** feedback reports normalized `constraints`, canonical `constraint`, and status `alias`
- **AND** the committed authored text is unchanged

#### Scenario: Unknown category is explicitly open

- **WHEN** a successful default write uses a well-formed unknown category
- **THEN** feedback reports status `open` and preserves its normalized key
- **AND** no replacement is invented

### Requirement: Reviewed Corpus Vocabulary Evolution

Category inference SHALL emit a deterministic `register_category` candidate for an unregistered normalized category used on at least five distinct selected pages. It MUST include exact counts, at most five bounded examples, and a complete extension-registry proposal using description `User-defined semantic category observed across multiple pages.` It MUST NOT infer aliases, scope, or semantic equivalence and MUST NOT save without an explicit reviewed operation. Core categories and defaulted core rich kinds MUST NOT become candidates.

#### Scenario: Recurring unknown category becomes saveable for review

- **WHEN** the same unregistered category occurs on at least five distinct selected pages
- **THEN** inference returns one stable `register_category` candidate whose proposal passes registry validation
- **AND** the active registry remains unchanged until explicitly saved

#### Scenario: Incidental and core categories stay observational

- **WHEN** an unregistered category appears on fewer than five pages or the observed category is core
- **THEN** inference may report frequency
- **AND** it produces no registration candidate for that category
