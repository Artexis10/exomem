## ADDED Requirements

### Requirement: Bootstrap Teaches The Semantic Unit Language
The bootstrap contract SHALL explain compact observation syntax, rich semantic blocks, the distinction between open category and governed kind, exact category/kind recall, page/unit structured filters, filter-only retrieval, opt-in ranking explanations, canonical typed relations, logically atomic and crash-recoverable validate-then-commit relation review for disconnected creation, and review-before-governance behavior. It SHALL tell agents to use structured semantic-unit mutation when changing one unit instead of brittle whole-page string surgery, that compact observations cannot carry typed unit relations without selecting a rich governed kind, and that BM25/cosine/fusion/reranker values have different meanings rather than being confidence scores.

#### Scenario: Generic agent can author and retrieve observations
- **WHEN** a generic agent reads the full bootstrap contract
- **THEN** it can author `- [category] content` observations, distinguish categories from tags and kinds, query exact categories, combine safe page/unit filters, request score explanations when needed, and use `observe_memory` for unit mutation

#### Scenario: Bootstrap does not teach automatic semantics
- **WHEN** bootstrap describes open categories and relation suggestions
- **THEN** it states that categories do not imply governed kinds and suggestions require semantic review before typed authoring
