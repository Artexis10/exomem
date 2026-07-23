## ADDED Requirements

### Requirement: Rich Metadata Is First-Class Semantic Unit Data

The unified semantic parser SHALL project authored rich-block `tags` and `context` metadata into `SemanticUnit.tags` and `SemanticUnit.context`. It MUST preserve the existing parsed generic metadata mapping for compatibility and diagnostics while returning normalized first-class values.

Rich tags MUST be parsed as comma-delimited plain tokens with surrounding whitespace removed, NFKC plus casefold normalization, first-occurrence order, and duplicates removed after normalization. Each tag MUST be 1–64 characters, begin with an alphanumeric, contain only alphanumerics, `_`, `-`, or `/`, and contain neither a trailing `/` nor `//`; `#` prefixes and empty entries are invalid. Rich context MUST be trimmed non-empty single-line Unicode and otherwise preserved. An invalid field MUST emit a stable validation finding, preserve its parsed metadata string, and project no partial first-class value for that field.

#### Scenario: Rich tags and context parse into typed fields

- **WHEN** a governed rich block contains `tags: reliability, runtime/retry` and `context: edge path`
- **THEN** its semantic unit exposes tags `reliability` and `runtime/retry` and context `edge path`
- **AND** the parsed metadata mapping remains available without becoming the only representation

#### Scenario: Rich tags normalize deterministically

- **WHEN** rich metadata declares `tags: Reliability, runtime/retry, reliability`
- **THEN** the first-class tags are `reliability` and `runtime/retry` in that order

#### Scenario: Invalid rich metadata is not partially projected

- **WHEN** rich tags contain an empty entry, `#` prefix, overlong token, trailing slash, doubled slash, or another forbidden character, or rich context is empty
- **THEN** validation emits the stable field-specific diagnostic
- **AND** the parsed metadata mapping is retained while that invalid field projects as empty

#### Scenario: Rich metadata survives every projection

- **WHEN** a rich unit with authored tags and context is indexed and retrieved
- **THEN** structured `unit.tags` and `unit.context` filters match it
- **AND** full hits, exact unit reads, lexical records, graph nodes, and context packs expose the same values

#### Scenario: Explicit post-upgrade reconcile reparses unchanged Markdown

- **WHEN** a pre-change lexical, embedding, graph, or semantic parent sidecar contains a rich unit with empty projected tags/context and the Markdown content and mtime have not changed
- **THEN** the parser-generation or affected sidecar identity marks the record stale after upgrade
- **AND** after mutations are quiesced, `maintain_memory(mode="reconcile")` rebuilds lexical, enabled vector, and graph projections that expose the authored tags and context
- **AND** canonical Markdown bytes and mtimes are unchanged
- **AND** a service restart alone is not required to perform or promise that rebuild

### Requirement: Category, Kind, Tags, Context, And Relations Stay Distinct

The system SHALL preserve the semantic roles of category, governed kind, tags, context, and authored relations. Category filters MUST use canonical category identity, kind filters MUST use the governed block kind, tag/context filters MUST use their first-class fields, and graph traversal MUST follow only governed authored relations rather than inferring edges from category, tags, context, or prose.

#### Scenario: One rich decision uses every semantic dimension

- **WHEN** a `Decision` block declares category `runtime reliability`, tags, context, and governed `mitigates` and `depends_on` relations
- **THEN** it is retrievable as kind `decision` and category `runtime_reliability`
- **AND** its tags and context are independently filterable
- **AND** only its authored governed relations produce typed traversal edges

#### Scenario: Omitted rich category retains three identities

- **WHEN** a rich block omits category metadata and a reviewed category alias applies to its heading-derived category key
- **THEN** `category_raw` and `category_key` retain the heading-derived identity
- **AND** the resolved `category` follows the reviewed alias without changing the governed `kind`
