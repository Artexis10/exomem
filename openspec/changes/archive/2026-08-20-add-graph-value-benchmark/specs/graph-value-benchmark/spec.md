## ADDED Requirements

### Requirement: Product-Neutral Graph Task Corpus

The repository SHALL define a deterministic product-neutral manifest of graph facts and tasks and SHALL render semantically equivalent Exomem and Basic Memory corpora using each product's documented native Markdown grammar. The renderers MUST preserve note identities, observations, directed relation facts, lifecycle facts, provenance facts, block facts, expected targets, and distractors, and MUST verify that contender execution does not mutate either generated corpus.

#### Scenario: Native renderers preserve one semantic manifest

- **WHEN** the fixture corpus is generated for both contenders
- **THEN** both rendered trees identify the same neutral notes and note-level relation facts
- **AND** contender-specific provenance, lifecycle, or block representations are declared explicitly rather than silently dropped

#### Scenario: Benchmark corpus is deterministic and non-mutating

- **WHEN** the same manifest version is rendered twice and exercised by a contender
- **THEN** its pre-run corpus hashes are identical across renders
- **AND** its pre-run and post-run Markdown hashes are identical

### Requirement: Independent Graph-Value Metrics

The evaluator SHALL report separate numerator, denominator, and ratio values for one-hop reachability, multi-hop reachability, distractor precision, relation-type fidelity, direction fidelity, traversal-lens filtering, provenance traceability, supersession/active-conclusion handling, and semantic-block relational precision. It MUST report response bytes and latency separately and MUST NOT collapse correctness dimensions into a weighted aggregate score.

#### Scenario: A graph mistake remains visible

- **WHEN** a contender reaches the expected target but returns the wrong relation type, wrong direction, or forbidden distractor
- **THEN** reachability, semantic fidelity, and precision are scored independently
- **AND** a high value in one dimension does not erase the failed dimension

#### Scenario: Unsupported capability is explicit

- **WHEN** a contender cannot represent or return a required provenance, lifecycle, or semantic-block fact
- **THEN** the dimension records unsupported status and a zero result with a reason
- **AND** the denominator is not silently removed

### Requirement: Falsifiable Graph Superiority Contract

The comparison SHALL declare Exomem dominant only when Exomem is no worse than Basic Memory on common note-level one-hop and multi-hop reachability, distractor precision, relation-type fidelity, and direction fidelity; passes every Exomem fixture invariant; and strictly exceeds Basic Memory on provenance traceability, supersession handling, and semantic-block relational precision. Every failed criterion MUST identify the affected dimensions and cases.

#### Scenario: Governance cannot hide a reachability regression

- **WHEN** Exomem wins provenance and lifecycle dimensions but misses a note-level target that Basic Memory reaches
- **THEN** the dominance result is false
- **AND** the report names the reachability criterion and failed case

#### Scenario: Strict governed-graph advantage is demonstrated

- **WHEN** Exomem matches or exceeds all common graph dimensions and exceeds Basic Memory on all three governed dimensions
- **THEN** the dominance result is true
- **AND** the report scopes the claim to graph-dependent tasks

### Requirement: Fast Exomem Gate And Optional Direct Contender

The repository SHALL provide a model-free Exomem fixture gate suitable for the lean test suite and an explicit desk-side mode that invokes current Exomem and Basic Memory through persistent MCP server sessions. The Basic Memory session MUST use an isolated home, config, and database; disable semantic/model features and corpus mutation; and record its executable version and git revision when available. Basic Memory unavailability MUST soft-fail with setup guidance outside direct-comparison mode and MUST NOT add a required Exomem dependency.

#### Scenario: Lean tests require no external contender

- **WHEN** the normal Exomem test suite runs without Basic Memory installed
- **THEN** deterministic Exomem graph cases and evaluator tests run successfully
- **AND** no network, embedding model, or external database is required

#### Scenario: Direct comparison uses persistent isolated servers

- **WHEN** desk-side direct comparison is explicitly requested
- **THEN** each contender is invoked through one persistent MCP session over its generated corpus
- **AND** Basic Memory state is confined to an isolated benchmark directory with mutation-disabled configuration

#### Scenario: Missing Basic Memory is actionable

- **WHEN** direct comparison is not requested and no Basic Memory executable or checkout is available
- **THEN** the report marks the contender unavailable and prints the exact opt-in setup requirement
- **AND** the Exomem fixture gate still completes

### Requirement: Privacy-Safe Reproducible Reports

The benchmark SHALL emit JSON and Markdown reports containing manifest and corpus versions, contender versions/revisions, aggregate per-dimension results, dominance criteria, response-size and latency measurements, fairness notes, and reproduction commands. Reports MUST NOT contain private vault paths, personal note content, environment values, or personal-vault query text.

#### Scenario: Report is safe to commit

- **WHEN** a fixture or direct comparison report is rendered
- **THEN** it contains only fixture case identifiers and aggregate measurements
- **AND** repository tests reject absolute home paths, environment secrets, or generated note bodies in the report

### Requirement: Benchmark-Driven Runtime Changes

Any runtime graph change made in this work SHALL be tied to a recorded failed benchmark criterion and SHALL add a regression case demonstrating the public graph behavior. Runtime code MUST NOT branch on benchmark case identifiers, fixture paths, or benchmark execution state.

#### Scenario: Measured failure drives a general fix

- **WHEN** the initial benchmark reveals an Exomem graph failure
- **THEN** the change records the failed criterion, adds a regression case, and implements a content-agnostic fix
- **AND** the benchmark passes without special-casing fixture identifiers
