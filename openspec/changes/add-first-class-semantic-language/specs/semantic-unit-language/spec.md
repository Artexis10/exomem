## ADDED Requirements

### Requirement: Compact Observation Grammar
The system SHALL parse compact Markdown observations of the form `- [category] content #tags (context) ^anchor` outside fenced code blocks and preserve raw category/source span. After trimming, a valid category SHALL begin with a Unicode letter, contain only Unicode letters/digits, spaces, `_`, or `-`, and contain at most 64 Unicode code points. Its authored canonical value SHALL apply Unicode NFKC and casefold, then collapse runs of spaces, `_`, and `-` to one `_`. Registry alias resolution SHALL remain separate from authored canonicalization. The parser SHALL accept observations anywhere for compatibility, while Exomem writers SHALL author new compact observations under a canonical `## Observations` section.

Suffixes SHALL be parsed from the end in this order: optional terminal Obsidian `^anchor`; one optional balanced, unescaped parenthesized context preceded by whitespace; then a contiguous run of trailing `#slug` tokens. A tag slug SHALL be 1–64 Unicode letters/digits or `_`, `-`, `/`, begin with a letter/digit, and contain neither empty path segments nor trailing `/`. A compact anchor SHALL be 1–64 ASCII letters/digits/hyphens and begin/end alphanumeric. Escaped parentheses, embedded hashes, and non-trailing tag-like text SHALL remain content.

#### Scenario: Compact observation is parsed
- **WHEN** a Markdown page contains `- [config] Session lifetime is 30 days #auth (security review)` outside a fence
- **THEN** the parser returns one compact semantic unit with raw category `config`, canonical category `config`, content, tag `auth`, context `security review`, and the original line/span

#### Scenario: Tasks and fenced examples are not observations
- **WHEN** Markdown contains `[ ]`, `[x]`, `[X]`, or `[-]` task bullets and an observation-shaped bullet inside a fenced code block
- **THEN** none of those bullets is parsed as a semantic unit

#### Scenario: Existing bracketed workflow rows are not observations
- **WHEN** Markdown contains an Exomem `[take: ]` row or another reserved bracketed workflow label containing punctuation outside the category grammar
- **THEN** the row remains ordinary Markdown and does not produce an observation diagnostic

#### Scenario: Unicode category canonicalization is deterministic
- **WHEN** observations use raw categories `Äri Reegel` and `äri-reegel`
- **THEN** both retain their raw labels and normalize to authored canonical category `äri_reegel`

#### Scenario: Suffix punctuation is unambiguous
- **WHEN** an observation ends with content containing escaped parentheses, followed by contiguous trailing tags, a balanced final context, and a terminal anchor
- **THEN** only the trailing tags/context/anchor are structured and the escaped parentheses remain content

#### Scenario: Canonical category collision is a visible union
- **WHEN** two valid raw categories normalize to the same authored canonical category
- **THEN** an exact category filter matches both, results retain both raw forms, and schema profiling reports the collision

#### Scenario: Open category vocabulary is accepted
- **WHEN** valid compact observations use previously unseen categories such as `config`, `rule`, and `term`
- **THEN** each observation is valid and searchable without a registry write
- **AND** no epistemic kind is inferred from those category names

### Requirement: Unified Compact And Rich Semantic Units
The system SHALL normalize compact observations and recognized rich semantic blocks into one semantic-unit result shape. Compact observations SHALL have governed kind `observation`. A rich block SHALL retain its governed heading kind, SHALL default its category to that kind, and MAY override its category through explicit block metadata.

#### Scenario: Decision category spans both forms
- **WHEN** one page contains `- [decision] Use SQLite` and another contains a `## Decision` block with no category metadata
- **THEN** both normalize with category `decision`
- **AND** only the rich block normalizes with kind `decision`

#### Scenario: Rich kind and domain category stay distinct
- **WHEN** a `## Decision` block carries `- category: config`
- **THEN** its kind is `decision` and its category is `config`
- **AND** neither field silently replaces the other

#### Scenario: Rich unit is normalized exactly once
- **WHEN** a recognized rich semantic block is parsed for context, indexing, and graph construction
- **THEN** one normalized rich-unit identity feeds every consumer and preserves the existing semantic-block graph node key
- **AND** no duplicate parse, index row, graph node, or result is emitted

#### Scenario: Legacy semantic-block context remains compatible
- **WHEN** an existing caller requests the bounded `semantic_blocks` context field
- **THEN** it receives a compatibility projection derived from `semantic_units` with existing fields preserved where representable
- **AND** the projection is not stored or ranked as a second semantic object

### Requirement: Semantic Unit Identity And Anchors
The system SHALL identify every semantic unit through its parent memory identity plus either an authored anchor or a fingerprint-bound derived reference. Rich `id` metadata and compact Obsidian block anchors SHALL produce durable anchored unit references only when the parent has a stable `exomem_id`. Anonymous signatures SHALL use authoring form plus normalized authored/raw category and explicitly authored kind/content/tags/context/relation metadata, not mutable registry alias targets. Anonymous duplicate occurrence SHALL be source order among identical authored signatures. References on legacy path-identified parents SHALL change on move, and semantic edits SHALL change anonymous references.

#### Scenario: Anchored compact unit survives a move
- **WHEN** a compact observation with `^session-ttl` belongs to a page with an `exomem_id` and that page moves
- **THEN** its durable unit reference remains the parent `exomem://memory/<uuid>` plus `session-ttl`

#### Scenario: Anonymous unit edit invalidates stale reference
- **WHEN** an anonymous compact observation's category or semantic content changes
- **THEN** its prior derived reference/fingerprint no longer authorizes mutation
- **AND** a stale mutation fails without editing another unit

#### Scenario: Duplicate anonymous units remain distinct
- **WHEN** a page contains two identical anonymous observations
- **THEN** the parser emits two distinct occurrence-qualified references and spans
- **AND** inserting or removing an earlier identical occurrence may invalidate later duplicate references

#### Scenario: Legacy path identity does not promise move stability
- **WHEN** a parent without `exomem_id` moves
- **THEN** its path-fallback unit references change and audit recommends durable identity backfill

#### Scenario: Duplicate authored anchor is rejected
- **WHEN** any compact/rich units in one page reuse the same authored anchor
- **THEN** parsing returns a duplicate-anchor error and no ambiguous durable unit reference

#### Scenario: Registry change preserves authored identity
- **WHEN** a category alias is added or changed without changing Markdown
- **THEN** existing unit references/fingerprints remain stable while resolved-category retrieval may change

### Requirement: Open Category Governance
The system SHALL expose `category_raw`, authored canonical `category_key`, and registry-resolved `category`, which defaults to the key when no alias applies. It SHALL keep unregistered categories valid by default. `schema_memory(subject="categories")` SHALL provide read-only frequency, scope, example, collision, and alias-candidate profiling. Registry aliases, deprecation/replacement, scopes, and category restrictions SHALL take effect only after an explicit reviewed save and SHALL NOT change `category_key` or unit identity.

#### Scenario: Category inference is proposal-only
- **WHEN** category inference observes several spellings or frequent categories
- **THEN** it returns a structured proposal and examples without changing Markdown or the registry

#### Scenario: Reviewed alias preserves authored identity
- **WHEN** a saved registry aliases raw category `configuration` to canonical `config`
- **THEN** retrieval can match resolved category `config` while results still report raw `configuration` and authored key `configuration`

#### Scenario: Conflicting category aliases fail validation
- **WHEN** equal-scope registry entries map one authored canonical category to incompatible targets
- **THEN** validation returns a named conflict and does not choose a target silently

#### Scenario: Unknown category remains valid without restrictive contract
- **WHEN** a unit has an unregistered category and no resolved contract forbids unknown categories
- **THEN** parsing and indexing succeed without a validation error

### Requirement: Registry-Driven Rich Kinds
The system SHALL retain portable built-in semantic-block kinds and SHALL allow reviewed semantic-language registry extensions to add recognized rich kinds without a code release. Unknown headings SHALL remain ordinary Markdown until registered.

#### Scenario: Registered custom heading becomes a rich unit
- **WHEN** a reviewed registry extension adds kind `protocol` and a page contains `## Protocol`
- **THEN** the heading section is parsed and indexed as a rich semantic unit of kind `protocol`

#### Scenario: Unknown heading remains inert Markdown
- **WHEN** no built-in or extension kind matches a heading
- **THEN** the heading remains ordinary Markdown and does not cause a validation error

### Requirement: Deterministic Parsing And Diagnostics
Semantic-unit parsing SHALL be deterministic, SHALL invoke no model, and SHALL return structured errors/warnings with stable code, path, line/span, raw fragment, and remediation. Malformed observation-like content SHALL remain user Markdown and MUST NOT be partially indexed as a valid unit.

#### Scenario: Malformed observation is reported without partial unit
- **WHEN** an observation-shaped bullet has a valid category bracket but empty content or an overlong/invalid category label
- **THEN** parsing returns a span-addressed diagnostic and no semantic unit for that bullet

#### Scenario: Repeated parse is byte-stable
- **WHEN** identical Markdown and registries are parsed twice
- **THEN** the normalized units and diagnostics are byte-identical
