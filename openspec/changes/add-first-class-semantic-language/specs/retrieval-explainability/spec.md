## ADDED Requirements

### Requirement: Retrieval Explanation Is Explicit And Backward Compatible
`find` and `ask_memory` SHALL accept `explain: bool`, default `false`. When omitted or false, existing compact and full response bytes, fields, and ordering MUST remain compatible. When true, recall SHALL add a bounded top-level `retrieval_profile` and per-hit `ranking_explanation` without adding note bodies, excerpts, unit content, or other full-detail fields solely because explanation was requested.

#### Scenario: Ordinary compact recall is unchanged
- **WHEN** the same compact request is executed before and after this capability with `explain` omitted
- **THEN** its serialized response and hit ordering are unchanged

#### Scenario: Compact explanation does not leak content
- **WHEN** compact recall uses `explain=true`
- **THEN** diagnostic ranking metadata is added but full excerpts and note bodies remain absent

### Requirement: Retrieval Profile Describes The Effective Plan
The top-level profile SHALL report an explanation schema version, resolved intent, effective result level, requested and effective retrieval modes, normalized shortcuts/filter AST, lane availability and degradation reasons, lane weights, fusion algorithm and constants only when fusion ran, rerank decision, and final ordering/tie-break policy. Each measurement lane SHALL identify its backend or model where relevant, metric name, better-direction/range, and rounding. Unavailable, disabled, warming, failed, non-applicable, and available-but-nonmatching lanes SHALL be represented only in this profile; none SHALL be represented by a fabricated per-hit entry or zero score.

#### Scenario: Vector degradation is explicit
- **WHEN** hybrid retrieval falls back because embeddings are unavailable
- **THEN** the profile reports the vector lane unavailable with its reason and identifies the effective lexical/fusion plan

#### Scenario: Effective filters are inspectable
- **WHEN** category shortcuts, page filters, and query text are combined
- **THEN** the profile returns their normalized AND/OR structure and category alias resolution

### Requirement: Per-Hit Explanation Preserves Lane Evidence
Each explained hit SHALL report only actual candidate-lane participation with lane name, rank, metric name/value when the lane has one, applicable lane weight/contribution only when fusion ran, and applicable provenance. Graph evidence SHALL include seed identity, relation type, direction, hop, and graph rank. Keyword-only evidence SHALL expose rank without inventing a magnitude. Fused retrieval SHALL report the fused value before boosts; single-lane and filter-only retrieval SHALL omit fusion fields and report the actual deterministic lane or filtered-most-recent sort tuple and tie-break values. Each hit SHALL report every applied type/status/recency/usage multiplier, reranker raw/adjusted value when used, the actual final sortable tuple, and final rank.

#### Scenario: RRF contribution is reproducible
- **WHEN** a hit ranks third in a lane weighted two under RRF constant 60
- **THEN** its explanation records rank 3, weight 2, constant 60, contribution `2/(60+3)`, and the contribution participates in the reported fused sum

#### Scenario: Graph expansion identifies why
- **WHEN** a hit enters through a typed one-hop graph expansion
- **THEN** its explanation identifies the seed, canonical relation, direction, hop, graph rank, and graph fusion contribution

#### Scenario: Reranking order is traceable
- **WHEN** reranking and active/type/usage adjustments alter the fused order
- **THEN** each affected hit reports the exact ordered before/after chain needed to reproduce final order within documented rounding

#### Scenario: Filter-only explanation has no invented fusion
- **WHEN** an empty-query filter-only request is explained
- **THEN** each hit omits fusion fields and reports the documented filtered-most-recent sort tuple, tie-break values, and final rank

#### Scenario: Available nonmatching lane is top-level only
- **WHEN** a vector lane is available but does not return a particular BM25 hit
- **THEN** the profile records vector availability while that hit has no vector entry or zero value

### Requirement: Score Names Retain One Meaning
BM25 explanations SHALL include backend, rank, raw backend score, and score direction labelled diagnostic and non-comparable across backends/corpora. Vector and CLIP values SHALL be labelled cosine similarity with model, range, and direction; the existing `vector_score` compatibility field MAY remain but MUST map to that named metric. Reranker values SHALL identify model/backend and direction. RRF fused values, graph ranks, temporal ranks, and reranker values SHALL retain distinct names and MUST NOT be exposed through one mode-dependent generic score. No retrieval measurement SHALL be called confidence unless it is a calibrated confidence value.

#### Scenario: BM25 and cosine cannot be confused
- **WHEN** a hybrid hit participates in both BM25 and vector lanes
- **THEN** its explanation returns separate `bm25.raw_score` and `vector.cosine` values with their ranks and metric caveats

#### Scenario: Mode change does not change field meaning
- **WHEN** equivalent keyword, vector, and hybrid requests are explained
- **THEN** every field retains the same metric definition and absent metrics are omitted or marked non-applicable rather than overloaded

#### Scenario: Negative or inverted backend scores remain interpretable
- **WHEN** a BM25 backend uses lower-is-better or negative raw values
- **THEN** the explanation preserves the raw value, names that backend's direction, and still reports the rank used for fusion

### Requirement: Explanation Fidelity Is Runtime-Tested
Explanation tests SHALL compare public explained responses with isolated lane runs and known deterministic fusion/boost inputs. Returned lane membership, ranks, raw measurements, contributions, degradation, and final ordering MUST agree. Diagnostic metadata SHALL be bounded, SHALL avoid content-derived snippets, and SHALL use the same response schema across MCP, REST, CLI JSON, OpenAPI, and generated documentation.

#### Scenario: Public hybrid explanation matches isolated lanes
- **WHEN** the benchmark runs BM25-only, vector-only, graph-only, and public hybrid queries over the same immutable corpus
- **THEN** the hybrid explanation's lane membership/ranks and fusion contributions agree with the isolated evidence

#### Scenario: Explanation size remains bounded
- **WHEN** a request returns the maximum hit limit with every lane active
- **THEN** explanation contains only fixed-schema scalar/provenance metadata per hit and respects the documented response-size bound
