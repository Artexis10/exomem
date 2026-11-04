## Purpose

Provide optional, bounded interpretation of candidate relevance without making a model the authority for knowledge, identity or permitted effects.

## ADDED Requirements

### Requirement: Context-fit interpretation has one bounded effect

An enabled interpreter SHALL judge task fit only for the released candidates supplied to it. Its validated output SHALL identify supplied candidates and categorical relevance or abstention. It SHALL NOT invent candidates, generate plans, author facts, grant authority or change canonical state.

The output SHALL contain `items` with `ref`, `source_version` and `fit`. The protocol labels SHALL be `relevant`, `uncertain` and `unrelated`. Missing candidate results SHALL mean uncertain; an empty valid result SHALL preserve baseline order. Unknown identities, mismatched versions, duplicate identities or invalid labels SHALL reject the complete result. Valid judgments SHALL only reorder the supplied pool into relevant, uncertain and unrelated groups, preserving baseline order within each group. They SHALL NOT delete candidates, alter admission, change anchor resolution or reorder method invalidation. Existing packet budgets and overflow SHALL still apply. The packet SHALL distinguish disabled, ready and unavailable interpretation; judgments SHALL remain advisory.

#### Scenario: A model invents a reference
- **WHEN** a model returns a reference absent from its admitted inputs
- **THEN** validation rejects the output and the ordinary candidate path remains available

#### Scenario: A model omits a candidate judgment
- **WHEN** a valid result omits one supplied candidate
- **THEN** that candidate remains eligible in the uncertain group with its baseline relative order

#### Scenario: A model returns conflicting duplicate judgments
- **WHEN** a result judges the same identity more than once
- **THEN** the complete result is rejected and the candidate pool retains baseline order

#### Scenario: A model considers an admitted candidate unrelated
- **WHEN** a valid judgment labels a supplied candidate unrelated
- **THEN** it moves behind relevant and uncertain candidates without deleting its source route or changing admission

#### Scenario: A candidate contains imperative source text
- **WHEN** a candidate contains instructions unrelated to the interpretation protocol
- **THEN** the interpreter treats them as data and gains no tools or mutation authority

### Requirement: Interpretation is optional and replaceable

The interpreter SHALL be default-off. Local and explicitly configured API placements SHALL implement the same typed contract. Each invocation SHALL identify its model, runtime, template, placement and limits. Missing capability, refusal, invalid output, resource pressure or timeout SHALL preserve the agent-only path without an implicit remote fallback.

#### Scenario: Local runtime is unavailable
- **WHEN** the configured local runtime cannot answer within the request allowance
- **THEN** activation returns its admitted candidates and an honest unavailable interpretation state
- **AND** it neither calls an API provider nor waits beyond the request deadline

#### Scenario: API placement replaces local placement
- **WHEN** an authorized deployment selects an API implementation
- **THEN** the same candidate and output contract applies, with explicit provider disclosure and budget configuration

### Requirement: Turn interpretation stays ephemeral

Turn-specific interpretation SHALL remain request-scoped. The system SHALL NOT persist conversation text, conversation hashes, prompts or turn-specific readings in a ledger or cache. Operational measurements SHALL contain only admitted content-free metadata. Readings from the existing epistemic sensing system SHALL remain separate.

#### Scenario: A distinctive conversation is interpreted
- **WHEN** a request includes a distinctive phrase in bounded conversation context
- **THEN** persistent state contains neither the phrase nor its conversation-derived hash
- **AND** no durable epistemic reading is created by that request

### Requirement: Disclosure precedes interpretation

The system SHALL admit inputs for the caller before candidate selection, ranking, caps, counts and model invocation. Provider disclosure SHALL be checked separately from caller access. Restricted callers SHALL NOT consume owner-built interpretation results. Withheld inputs SHALL affect neither their output nor their interpretation metadata.

#### Scenario: A hidden candidate would outrank a visible one
- **WHEN** a hidden candidate is added to a restricted caller's vault view
- **THEN** the caller receives the same candidates and interpretation result as when that candidate is absent

#### Scenario: An API cannot receive an admitted page
- **WHEN** caller access permits a page but provider disclosure does not
- **THEN** the API receives no page content and activation retains the permitted agent-only route

### Requirement: Interpretation must justify its deployment

An enabled deployment SHALL have source-derived real-model evidence comparing the same cases and candidate inputs with and without interpretation. Evidence SHALL report useful and inappropriate selections, paired uncertainty, latency, resource use and failures. Development results SHALL remain separate from held-out claims.

#### Scenario: The optional stage shows no established benefit
- **WHEN** its measured interval includes no change against the agent-only path
- **THEN** the report states no established gain and the stage remains disabled for that deployment
