## MODIFIED Requirements

### Requirement: Bootstrap Teaches The Semantic Unit Language
The bootstrap contract SHALL explain the semantic unit language: observation syntax, categories versus governed kinds, filtered and explained retrieval, canonical typed relations and their review. It SHALL tell agents to mutate one unit structurally rather than by string surgery, that a compact observation needs a rich governed kind for a typed relation except a provenance relation, which is recorded on the page, and that ranking scores are not confidence.

#### Scenario: Bootstrap covers the whole language
- **WHEN** a generic agent reads the full bootstrap contract
- **THEN** it covers compact observation syntax, rich semantic blocks, open category versus governed kind, exact category/kind recall, page/unit structured filters, filter-only retrieval, opt-in ranking explanations, canonical typed relations, logically atomic and crash-recoverable validate-then-commit relation review for disconnected creation, and review-before-governance behavior
- **AND** it states that BM25/cosine/fusion/reranker values have different meanings rather than being confidence scores

#### Scenario: Generic agent can author and retrieve observations
- **WHEN** a generic agent reads the full bootstrap contract
- **THEN** it can author `- [category] content` observations, distinguish categories from tags and kinds, query exact categories, combine safe page/unit filters, request score explanations when needed, and use `observe_memory` for unit mutation

#### Scenario: Bootstrap does not teach automatic semantics
- **WHEN** bootstrap describes open categories and relation suggestions
- **THEN** it states that categories do not imply governed kinds and suggestions require semantic review before typed authoring

#### Scenario: Bootstrap names the provenance exception
- **WHEN** a generic agent reads how compact observations treat typed relations
- **THEN** it learns that a link to the Source an observation came from is recorded on the page, and that other typed relations need a rich kind
