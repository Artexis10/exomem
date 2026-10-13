# semantic-unit-language Specification

## Purpose
Define how rich semantic blocks are parsed: they follow heading hierarchy, empty units are diagnosed and excluded, and unit spans never overlap. It also governs `prediction` as a core kind and the `verdict` and `check_by` unit-metadata keys, with hierarchy changes rebuilding only derived state.
## Requirements

### Requirement: Rich Semantic Blocks Follow Heading Hierarchy

A recognized rich heading at numeric ATX level `N` SHALL own content until the
next non-fenced ATX heading whose numeric level is `<= N`. Headings with numeric
level `> N` SHALL remain inside
the parent body, even when their label is a recognized rich kind. A recognized
rich heading nested beneath an unknown structural heading MAY start a unit when
no recognized ancestor is open. Authors SHALL use sibling heading levels for
sibling rich units.

#### Scenario: Nested subsections remain in the rich body
- **WHEN** `## Finding` contains `### Mechanism` and `### Consequence` subsections before the next level-two heading
- **THEN** one finding unit spans both subsections and the nested headings neither truncate it nor become sibling units

#### Scenario: Sibling heading ends the block
- **WHEN** `## Decision` is followed by `## Risk`
- **THEN** the decision ends before the risk and the parser emits two independently validated rich units

#### Scenario: Unknown structural parent can contain a rich child
- **WHEN** an unknown level-two container contains a recognized level-three rich heading
- **THEN** the recognized child starts a rich unit because no recognized ancestor owns its span

#### Scenario: Fenced headings remain content
- **WHEN** a rich block body contains an ATX-looking line inside a fenced code block
- **THEN** that line does not end the rich block or start a nested semantic unit

### Requirement: Empty Rich Units Are Diagnosed And Excluded

After leading metadata, relation rows, descendant heading markers, and whitespace
are removed, a recognized rich block with no substantive body SHALL emit stable
diagnostic `empty_rich_unit` at the heading span and SHALL NOT produce a normalized
or indexable semantic unit. Metadata, relations, or descendant headings alone
SHALL NOT make a block non-empty.

#### Scenario: Metadata-only rich heading is empty
- **WHEN** a recognized rich heading contains only `id`, `category`, `tags`, `context`, or `relations` metadata before its boundary
- **THEN** parsing emits `empty_rich_unit` and no index, graph, pack, count, or recall unit for that block

#### Scenario: Nested outline without prose is empty
- **WHEN** a recognized rich heading contains descendant heading labels but no substantive body text
- **THEN** descendant labels do not fabricate content and the parent is excluded with `empty_rich_unit`

#### Scenario: Posthoc parsing preserves the source
- **WHEN** watcher or reconcile parses a directly authored empty rich block
- **THEN** the Markdown remains byte-for-byte unchanged while the invalid unit is excluded and the finding is surfaced

### Requirement: Normalized Semantic Unit Spans Do Not Overlap

One source span SHALL contribute to at most one normalized semantic unit. While a
recognized rich block is open, deeper recognized headings and compact-shaped
bullets SHALL remain part of that rich body rather than producing nested units.
Compact observations intended as independent units SHALL be authored outside rich
spans, canonically under `## Observations`.

#### Scenario: Compact-shaped bullet inside rich body is not duplicated
- **WHEN** a non-empty rich block contains a `- [category] content` bullet
- **THEN** the bullet remains rich body content and does not create a second compact index row, graph node, count, fingerprint, or recall hit

#### Scenario: Compact observation under Observations remains independent
- **WHEN** a valid compact bullet is under `## Observations` outside any recognized rich block
- **THEN** it produces one compact semantic unit with governed kind `observation`

#### Scenario: Reparse has non-overlapping stable spans
- **WHEN** the same nested Markdown is parsed repeatedly
- **THEN** unit spans are non-overlapping and normalized units, diagnostics, identities, and fingerprints are byte-stable

### Requirement: Hierarchy Migration Rebuilds Only Derived State

The hierarchy change SHALL increment the semantic parser/index schema version.
Rebuild and reconcile SHALL replace affected derived lexical, vector, graph, pack,
and count state from Markdown without rewriting source pages. Anonymous unit
references and expected fingerprints MAY become stale; stable authored anchors
SHALL retain their parent-qualified identity while still requiring the current
fingerprint for mutation.

#### Scenario: Upgrade rebuilds rich derived records
- **WHEN** a vault indexed under the earlier heading-boundary behavior opens under the new parser version
- **THEN** derived semantic-unit state is rebuilt from Markdown and stale empty or overlapping rows disappear without a source-file write

#### Scenario: Stale anonymous reference fails safely
- **WHEN** hierarchy migration changes an anonymous rich unit span or body and a caller submits its former reference or fingerprint
- **THEN** mutation fails as stale and does not select or edit a different unit

### Requirement: Prediction Is A Governed Core Semantic Kind
The semantic-unit language SHALL recognize `prediction` as a portable, code-owned core rich kind with the heading alias `Predictions`. It SHALL resolve through the same core kind ring as every other built-in kind, so registry resolution, kind filters, schema profiling, and governed writers accept it without any per-vault registry entry. A vault-owned registry entry that shadows the built-in `prediction` kind SHALL report the existing canonical-collision finding rather than silently overriding product vocabulary.

#### Scenario: A prediction heading parses as a governed rich unit
- **WHEN** a compiled page contains `## Prediction` followed by a substantive body
- **THEN** the parsed document contains one rich semantic unit whose kind is `prediction`

#### Scenario: The plural heading resolves to the singular kind
- **WHEN** a compiled page contains `## Predictions` followed by a substantive body
- **THEN** the parsed unit's kind is `prediction`

#### Scenario: Prediction needs no registry entry
- **WHEN** the core semantic-language registry resolves the kind label `prediction` against a vault with no semantic-language registry file
- **THEN** resolution succeeds with core status and a definition rather than an unregistered status

#### Scenario: A registry extension may not shadow the built-in kind
- **WHEN** a semantic-language registry proposal declares a `prediction` kind
- **THEN** validation reports a `canonical_collision` error finding naming that key

### Requirement: Verdict Is A Governed Categorical Unit-Metadata Key
The semantic-unit language SHALL recognize `verdict` as a governed rich unit-metadata key whose value is exactly one of `abandoned`, `confirmed`, `inconclusive`, `qualified`, or `refuted`, matched after Unicode NFKC normalization and casefolding. Any other value, including any numeric value, SHALL produce a deterministic source-addressed error whose remediation names the closed set and states that a numeric confidence is not a stored field. The normalized value SHALL be projected onto the parsed unit and included in its serialized form. A verdict SHALL NOT change a unit's lifecycle, standing, or rank.

#### Scenario: A valid verdict is normalized and projected
- **WHEN** a rich unit carries the metadata row `- verdict: Refuted`
- **THEN** the parsed unit's verdict is `refuted` and the document reports no error

#### Scenario: An unknown verdict value is rejected
- **WHEN** a rich unit carries the metadata row `- verdict: probably-wrong`
- **THEN** the document reports an `invalid_rich_verdict` error bound to that source line

#### Scenario: A numeric verdict is refused with the no-confidence reason
- **WHEN** a rich unit carries the metadata row `- verdict: 0.7`
- **THEN** the document reports an `invalid_rich_verdict` error whose remediation states that confidence is not a stored field

#### Scenario: A refuted unit keeps active standing
- **WHEN** a rich unit on an active page carries `- verdict: refuted`
- **THEN** the unit remains an ordinary active unit and its parent page status is unchanged

### Requirement: Check By Is A Governed Date Unit-Metadata Key
The semantic-unit language SHALL recognize `check_by` as a governed rich unit-metadata key whose value is a strict ISO calendar date spelled `YYYY-MM-DD`. A value that is not an exact ISO calendar date, including a timestamp or an abbreviated date, SHALL produce a deterministic source-addressed error. The validated value SHALL be projected onto the parsed unit and included in its serialized form.

#### Scenario: A valid check-by date is projected
- **WHEN** a rich unit carries the metadata row `- check_by: 2026-11-01`
- **THEN** the parsed unit's check-by value is `2026-11-01` and the document reports no error

#### Scenario: A non-canonical date is rejected
- **WHEN** a rich unit carries the metadata row `- check_by: 2026-1-1`
- **THEN** the document reports an `invalid_rich_check_by` error bound to that source line

#### Scenario: A timestamp is rejected
- **WHEN** a rich unit carries the metadata row `- check_by: 2026-11-01T09:00:00Z`
- **THEN** the document reports an `invalid_rich_check_by` error

### Requirement: Governed Unit Metadata Is Rich-Form Only And Reserved
Governed unit-metadata keys SHALL be available only in the rich authoring form, because compact observations carry no metadata rows. `verdict` and `check_by` SHALL be treated as reserved metadata rows, so a rich unit whose only content is governed metadata SHALL still be reported as an empty rich unit.

#### Scenario: A metadata-only prediction is still empty
- **WHEN** a page contains `## Prediction` followed only by `- verdict: refuted` and `- check_by: 2026-11-01`
- **THEN** the document reports an `empty_rich_unit` error for that heading

#### Scenario: Compact observations carry no governed metadata
- **WHEN** a compact observation line is parsed
- **THEN** the resulting unit has no verdict and no check-by value

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
