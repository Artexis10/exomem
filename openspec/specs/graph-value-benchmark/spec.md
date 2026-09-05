# graph-value-benchmark Specification

## Purpose
TBD - created by archiving change add-graph-value-benchmark. Update Purpose after archive.
## Requirements
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

### Requirement: Graph-value corpus covers relation vocabulary evolution

The product-neutral synthetic graph corpus SHALL include policy applicability requiring a vault extension, child-to-parent membership satisfied by core `part_of`, an existing extension reached through a different clean alias, a legitimately generic relation, topical proximity that must not become a directional claim, extension parent-family roll-up, survivor-directed deprecation replacement, and two isolated vault registries. The Exomem gate SHALL exercise each case through the public resolve, propose, save, graph, filter, and explain surfaces that apply. Because resolution is non-authoritative, the gate SHALL distinguish candidate evidence from an explicit scripted caller decision and SHALL verify that resolution alone performs no registry or Markdown mutation.

#### Scenario: Accepted applicability extension works end to end
- **WHEN** the synthetic organisation policy is connected to a specific employee case through reviewed `vault.applies_to`
- **THEN** the benchmark finds the edge by clean alias, exact canonical key, and parent-family filter
- **AND** raw identity, canonical identity, direction, provenance, and match mode are scored independently

#### Scenario: Existing and generic semantics are not penalized
- **WHEN** the membership, generic, and false-specificity cases run
- **THEN** resolution exposes `part_of` without proposing or writing, the scripted caller explicitly authors it, and an explicitly authored truthful `relates_to` passes
- **AND** topical evidence alone produces no selected or authored directional relation, while any scripted false directional choice scores as a false positive

### Requirement: Relation review has a scale and concurrency regression gate

The repository SHALL provide a deterministic model-free relation-review benchmark over at least 3,600 eligible synthetic pages and a workload of twenty concurrent request streams containing graph reads and mutations that change canonical Markdown on eligible graph pages. Validation-only calls, no-op edits, and graph-excluded writes SHALL NOT count as mutations. Every successful mutation SHALL advance a real graph checkpoint, and an explicit barrier SHALL prove queue reads overlap graph-relevant commits during the mixed phase. The harness SHALL report separate available and typed-unavailable queue latency distributions; mutation committed, busy, and failed outcomes; graph checkpoint generations; graph availability ratio; time from the final graph-relevant commit to a current available queue completion; graph snapshot and query counts; Markdown parse counts; embedding-call counts; and corpus-size-normalized candidate counts. A current-graph queue request for at most twenty source groups MUST use one snapshot, zero full-vault Markdown parses, zero embedding calls, and a bounded query count independent of corpus size. A passing calibrated Linux run MUST include at least two committed graph-relevant mutations, at least 90 percent available queue completions, and a current available queue completion within 5,000 milliseconds after the final graph-relevant commit. Available queue p95 MUST remain below 1,000 milliseconds and no available sample may exceed 2,000 milliseconds; typed-unavailable queue p95 MUST remain below 250 milliseconds. An all-busy mutation run, all-warming reader run, or non-overlapping mixed phase MUST fail.

#### Scenario: Concurrent review remains usable
- **WHEN** the calibrated scale fixture runs with twenty concurrent request streams
- **THEN** relation queue reads satisfy the availability, recovery, structural, and state-specific latency gates while some mutations may report legitimate retryable busy states
- **AND** at least two eligible-page Markdown mutations commit, each advances a graph checkpoint, their mixed phase overlaps queue reads, and the queue becomes current and available again within the recovery bound after the final commit
- **AND** no queue read triggers graph reconstruction, a Markdown census, or embedding materialization

#### Scenario: Semantic tests synchronize on behavior rather than tight clocks
- **WHEN** unit and integration tests exercise queue reads beside an injected held mutation boundary or graph publication
- **THEN** they assert snapshot isolation, bounded calls, and typed availability states using explicit synchronization
- **AND** absolute timing assertions remain confined to the dedicated calibrated benchmark
