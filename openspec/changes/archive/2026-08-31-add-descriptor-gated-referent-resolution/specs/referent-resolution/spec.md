## MODIFIED Requirements

### Requirement: Resolution Requires Exact Name Or Two Independent Evidence Kinds
An active entity SHALL resolve by exact title/alias alone, or by cue-type match plus at least two distinct non-exact evidence kinds. A cue's qualifiers SHALL be the deduplicated contiguous run of non-stopword, non-count tokens immediately preceding the cue noun within a three-token window. When qualifiers are non-empty, a non-exact resolution SHALL additionally require qualifier-bearing evidence: attribute evidence matching at least one qualifier, or graph evidence seeded by an active top-ten anchor whose title or body matches at least one qualifier using the attribute stem/prefix rules. The wider descriptor set SHALL continue to supply attribute evidence but SHALL NOT activate the gate. One kind or evidence that fails the qualifier gate SHALL remain a candidate; inactive or mismatched entities SHALL be dropped unless exact-name rules apply. Fuzzy-name evidence SHALL NOT be qualifier-bearing by itself. When qualifiers are empty, including post-nominal phrasing, the pre-change exact-name-or-two-kinds rule SHALL apply unchanged.

#### Scenario: Retrieval alone abstains
- **WHEN** a person entity appears only as a recall hit
- **THEN** it is a candidate and is not resolved

#### Scenario: Counted qualifier distractor remains unresolved across trailing topic words
- **WHEN** "my two verdant friends route timing" expects two friends and carries the qualifier "verdant"
- **AND** one friend has qualifier-bearing attribute evidence
- **AND** another friend has cue-noun attribute evidence plus graph evidence from a route-and-timing anchor without "verdant"
- **THEN** only the qualifier-bearing friend resolves
- **AND** the other friend remains a candidate
- **AND** status is partial with unresolved_count one

#### Scenario: Qualifier-bearing graph anchor qualifies
- **WHEN** a non-exact candidate has two independent evidence kinds including graph evidence
- **AND** the graph seed anchor title or body carries a cue qualifier under the attribute stem/prefix rules
- **THEN** the graph evidence satisfies the qualifier gate

#### Scenario: Unqualified cue preserves two-kind resolution
- **WHEN** a cue has no qualifiers
- **AND** a type-matching entity has two distinct non-exact evidence kinds
- **THEN** the entity resolves under the existing two-kind rule

#### Scenario: Post-nominal words fall back to the existing rule
- **WHEN** qualifying or topical words occur only after the cue noun
- **THEN** they remain wide descriptors but do not become qualifiers
- **AND** non-exact promotion uses the existing two-kind rule

#### Scenario: Exact name bypasses qualifier gate
- **WHEN** an active entity title or alias exactly matches the query
- **THEN** it resolves regardless of qualifier-bearing evidence
- **AND** graph evidence retains the first edge under the pre-change deterministic order

#### Scenario: Fuzzy name alone does not bear a qualifier
- **WHEN** a fuzzy entity-name match and one other non-qualifier evidence kind are present for a cue with qualifiers
- **THEN** the entity remains a candidate unless attribute or graph evidence bears a cue qualifier

#### Scenario: Either genuine qualifier can satisfy the gate
- **WHEN** a cue has the pre-nominal qualifiers "japanese" and "hiking"
- **AND** separate candidates have two evidence kinds and attribute evidence matching either qualifier
- **THEN** either qualifier satisfies the gate for its candidate
