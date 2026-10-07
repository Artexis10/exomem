## ADDED Requirements

### Requirement: Bootstrap teaches that every capture names a kind

Bootstrap SHALL tell an agent to name a source kind on every capture: the closest known kind, or a new slug, which registers on capture. It SHALL tell the agent never to choose `other` or `unclassified`, and SHALL NOT describe any kind as a fallback.

Bootstrap SHALL tell an agent to inspect an advisory classification suggestion returned after a capture and to exercise judgement on it rather than repeating advice.

This guidance SHALL fit within the existing compact-profile size budget.

Bootstrap SHALL advance its existing operating-contract version and carry a bounded migration notice. The notice SHALL state that current capture rules supersede historical descriptions permitting an omitted kind or `other`. Current compact, session, full and historical bootstrap profiles SHALL convey the rule from the same source.

#### Scenario: The contract states the kind rule

- **WHEN** an agent reads the source-capture guidance in any bootstrap profile
- **THEN** it learns to name the closest known kind or a new slug
- **AND** it learns never to choose `other` or `unclassified`

#### Scenario: The contract teaches suggestion handling

- **WHEN** an agent reads the post-write guidance
- **THEN** it learns to inspect a returned classification suggestion
- **AND** it learns not to repeat the same advice within one interaction

#### Scenario: Compact guidance stays within budget

- **WHEN** the compact bootstrap payload is produced with this guidance present
- **THEN** its serialized size remains within the established compact ceiling

#### Scenario: Historical instructions receive a versioned correction

- **WHEN** a current or historical client reads bootstrap after this behavioral migration
- **THEN** it sees the operating-contract version and the corrected capture rule
- **AND** it learns that older optional-kind and `other` guidance no longer governs new captures

## REMOVED Requirements

### Requirement: Bootstrap teaches how to treat the fallback and a classification suggestion

**Reason**: There is no fallback kind to teach. The kind rule replaces it.

**Migration**: Read the `kind_rule` entry of bootstrap's source-taxonomy block instead of `fallback_rule`.
