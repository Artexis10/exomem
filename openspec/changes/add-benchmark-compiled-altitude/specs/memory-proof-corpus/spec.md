## ADDED Requirements

### Requirement: Corpus Emits A Deterministic Compile Plan
Generation SHALL emit a compile plan alongside the claim, source and query
streams: one conclusion record per compiled conclusion, carrying its title,
body, the source ids it draws from, the conclusion it supersedes when the
underlying claim was superseded, and the conclusions it disputes when the
corpus records an incompatible assertion. Every field SHALL be derived from
oracle-computable records, with no model in the derivation. The plan SHALL be
seeded, byte-reproducible across generations, and hashed into the release
manifest with the rest of the corpus.

#### Scenario: Plan derives only from oracle-computable records
- **WHEN** a corpus is generated
- **THEN** every conclusion's cited sources equal the sources the oracle records
  as asserting that claim, every supersession edge matches the claim's
  supersession chain, and generation completes without invoking any model

#### Scenario: Plan is deterministic
- **WHEN** the same seed is generated twice
- **THEN** the compile plan is byte-identical and its hash appears in the
  release manifest

#### Scenario: Plan is neutral about product grammar
- **WHEN** a conclusion record is inspected
- **THEN** it carries only a conclusion, its cited sources and its lineage, and
  no field named after or shaped by any single product's API

### Requirement: Compile Plan Provides Conflicting Conclusions
The compile plan SHALL include conclusions that assert incompatible values for
the same underlying claim, so that conflict surfacing is measurable rather than
structurally absent. A corpus whose plan contains no disputed pair SHALL fail
generation with a named error rather than silently producing a dimension that
nothing can pass.

#### Scenario: Disputed pair present
- **WHEN** a corpus is generated from a seed whose templates include an
  authority conflict or equal-authority dispute
- **THEN** the plan contains at least one pair of conclusions marked as
  disputing each other, drawn from the claim the corpus records as contested

#### Scenario: Missing dispute refuses generation
- **WHEN** a plan would contain no disputed pair
- **THEN** generation fails with a named error rather than emitting a corpus in
  which the contradiction dimension has a ceiling of zero
