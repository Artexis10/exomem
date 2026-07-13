## ADDED Requirements

### Requirement: Product-Neutral Semantic Language Manifest
The repository SHALL define a deterministic product-neutral manifest covering notes, compact observations, categories, duplicate content, tags/context, schemas, relations, provenance, lifecycle, distractors, mutations, and expected retrieval/context results. Native renderers SHALL express equivalent facts using each contender's documented grammar and SHALL record any unsupported fact explicitly.

#### Scenario: Renderer parity is inspectable
- **WHEN** the benchmark renders Exomem and Basic Memory corpora
- **THEN** the report lists how every neutral fact was represented or why it is unsupported
- **AND** neither renderer silently drops a required common fact

### Requirement: Isolated Public-Path Execution
The direct benchmark SHALL run each contender against isolated temporary corpora and state, perform required full indexing, and execute through persistent public MCP sessions. It SHALL record contender version/revision, configuration, corpus hashes before/after, raw response envelopes, latency, and mutation diffs. It MUST NOT point Basic Memory at a live user vault.

#### Scenario: Direct run leaves source vault untouched
- **WHEN** the direct benchmark runs with a sibling Basic Memory checkout
- **THEN** all Basic Memory files, database, home, and configuration live under disposable benchmark paths
- **AND** no configured live Exomem vault path is passed as its project

#### Scenario: Mutations are confined to throwaway corpus
- **WHEN** edit, move, delete, schema, and reindex cases execute
- **THEN** only the contender's temporary corpus/state may change and the mutation diff is reported

### Requirement: Contender-Neutral User Outcomes Are Predeclared
Before execution, the benchmark manifest SHALL pin corpus, contender revisions/configuration, dimensions, pass criteria, latency/response-size thresholds, normalization, and unsupported/error handling. It SHALL evaluate contender-neutral outcomes for exact knowledge-unit retrieval, source-location citation, current/history distinction, safe schema enforcement, external-edit repair without content loss, typed relation direction fidelity, bounded context, and complete mutation cleanup. Supporting measures SHALL cover open category parsing, same-content/different-category identity, category-only/text/hybrid recall, edits, moves, deletes, and multi-hop traversal. Source/docs claims alone MUST NOT count as a pass, and criteria MUST NOT be revised after observing a run.

#### Scenario: Strict schema behavior is measured
- **WHEN** contender documentation and source suggest strict schema enforcement
- **THEN** the benchmark attempts public write, filesystem-sync, and full-reindex paths and scores only the observed runtime result

#### Scenario: Edit cleanup is measured end to end
- **WHEN** an observation's category/content changes through the public mutation path
- **THEN** the benchmark verifies the old category/text result disappears and the new result appears

#### Scenario: Criteria are fixed before contender output
- **WHEN** a benchmark run begins
- **THEN** its report records the manifest hash and rejects post-run threshold or expected-result changes from that run's claim

### Requirement: Governed Outcome Evidence Is Returned, Not Assumed
The benchmark SHALL separately record durable parent/unit identity, source anchors, provenance, lifecycle/supersession state, governed relation semantics, bounded semantic-unit context, writer-enforced saved contracts, out-of-band repair safety, and complete derived-index cleanup. Credit SHALL require evidence returned through public product behavior, not an implementation inference.

#### Scenario: Provenance and lifecycle require returned evidence
- **WHEN** a contender stores semantically equivalent unit facts
- **THEN** it receives governed-dimension credit only when its public response returns the required provenance anchor and active/superseded state

### Requirement: No-Regression And Scoped Semantic-Governance Gate
The benchmark SHALL report independent dimensions without a compensating weighted aggregate. It MAY report a scoped semantic-governance advantage for the recorded revisions only when Exomem passes every required contender-neutral outcome, remains within every predeclared common no-regression threshold, every Exomem fixture invariant passes, and Exomem demonstrates strictly more of the required governed outcomes. Unsupported behavior and execution failure SHALL remain visible. The report MUST NOT convert this result into an unscoped or overall-product superiority claim.

#### Scenario: One common regression fails the scoped gate
- **WHEN** Exomem loses category fidelity or common observation retrieval even while winning governance dimensions
- **THEN** the scoped semantic-governance gate is false

#### Scenario: Complete outcomes plus governed differentiation passes
- **WHEN** both contenders pass all common dimensions and Exomem passes the required governed dimensions that Basic Memory cannot return
- **THEN** the report may mark a scoped semantic-governance advantage for the recorded corpus and revisions only

### Requirement: Fast Fixture Gate And Optional Direct Gate
The repository SHALL provide a fast Exomem-only fixture gate suitable for normal tests and an explicit desk-side direct-contender command. The direct gate SHALL soft-report an unavailable sibling checkout rather than making the normal suite depend on it.

#### Scenario: Normal suite has no external contender dependency
- **WHEN** the focused/lean Exomem test suite runs without a Basic Memory checkout
- **THEN** native semantic-language fixtures and invariants run and the direct contender is not required
