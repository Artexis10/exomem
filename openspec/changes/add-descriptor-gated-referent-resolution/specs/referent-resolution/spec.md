## MODIFIED Requirements

### Requirement: Resolution Requires Exact Name Or Two Independent Evidence Kinds
An active entity SHALL resolve by exact title/alias alone, or by cue-type match plus at least two distinct non-exact evidence kinds. When the cue contains descriptors, a non-exact resolution SHALL additionally require descriptor-bearing evidence: attribute evidence matching at least one cue descriptor, or graph evidence seeded by an active top-ten anchor whose title or body matches at least one cue descriptor using the attribute stem/prefix rules. One kind or evidence that fails the descriptor gate SHALL remain a candidate; inactive or mismatched entities SHALL be dropped unless exact-name rules apply. Fuzzy-name evidence SHALL NOT be descriptor-bearing by itself.

#### Scenario: Retrieval alone abstains
- **WHEN** a person entity appears only as a recall hit
- **THEN** it is a candidate and is not resolved

#### Scenario: Counted descriptor distractor remains unresolved
- **WHEN** a cue expects two friends and carries a descriptor
- **AND** one friend has descriptor-bearing attribute evidence
- **AND** another friend has two non-exact evidence kinds but only cue-noun attribute evidence and graph evidence from an anchor without that descriptor
- **THEN** only the descriptor-bearing friend resolves
- **AND** the other friend remains a candidate
- **AND** status is partial with unresolved_count one

#### Scenario: Descriptor-bearing graph anchor qualifies
- **WHEN** a non-exact candidate has two independent evidence kinds including graph evidence
- **AND** the graph seed anchor title or body carries a cue descriptor under the attribute stem/prefix rules
- **THEN** the graph evidence satisfies the descriptor gate

#### Scenario: Descriptorless cue preserves two-kind resolution
- **WHEN** a cue has no descriptors
- **AND** a type-matching entity has two distinct non-exact evidence kinds
- **THEN** the entity resolves under the existing two-kind rule

#### Scenario: Exact name bypasses descriptor gate
- **WHEN** an active entity title or alias exactly matches the query
- **THEN** it resolves regardless of descriptor-bearing evidence

#### Scenario: Fuzzy name alone does not bear a descriptor
- **WHEN** a fuzzy entity-name match and one other non-descriptor evidence kind are present for a cue with descriptors
- **THEN** the entity remains a candidate unless attribute or graph evidence bears a cue descriptor
