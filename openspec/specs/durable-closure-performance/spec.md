# durable-closure-performance Specification

## Purpose
Measure whether an agent can complete a multi-note evidence workflow promptly while preserving durable state, useful retrieval, and projection correctness.

## Requirements

### Requirement: Whole-workflow latency and correctness are measured together
The benchmark SHALL exercise a persistent public product surface over isolated realistic corpora near personal-vault scale and 8,000 pages. It SHALL preserve one PDF and two images, process them, update an active tracker and related notes, capture multiple semantic observations, attach evidence, repair a stale relation, and verify final direct and recalled state. The optimized workflow and per-write-probe stress workflow SHALL be separate timed variants. It SHALL fail if ordinary retrieval returns a call-level refusal between successful ordinary writes, or if final read-your-write assertions fail, even when individual acknowledgement bounds pass. Successful hits with optional graph warming SHALL pass useful-closure acceptance and record delayed optional convergence.

The model-free media variant SHALL prove preservation and durable enqueue only. The real-extraction variant SHALL await public completion, assert unique PDF/image extracted content, and report engine versions and extraction-convergence time, or explicitly report blocked/null; fixture presence or pending sidecars SHALL NOT count as extraction.

#### Scenario: Disabled extraction retains durable job custody
- **WHEN** the model-free profile disables extraction and processing reports `MEDIA_BLOCKED`
- **THEN** the harness proves each exact artifact's durable job through public status and its binary hash through the public sidecar before dependent note work; it records the disabled blocked state and does not claim runnable work or extraction completion

#### Scenario: Fast writes with unavailable recall fail acceptance
- **WHEN** all write acknowledgements meet their latency bounds but an intervening ordinary lookup returns a call-level RETRIEVAL_INDEX_WARMING refusal
- **THEN** the workflow correctness gate fails and records the refusal window

#### Scenario: Exact closure with asynchronous projections
- **WHEN** canonical writes are durable and final exact recall verifies their new state while optional projections remain pending
- **THEN** useful durable closure and later full convergence are reported as distinct times

#### Scenario: Evidence-backed writes depend on media completion
- **WHEN** extraction can change a cited media sidecar while unrelated tracker or background-note work is ready
- **THEN** independent work proceeds during extraction, while the evidence-backed write validates its sources after the relevant media completion without bypassing source-version or backlink guards

### Requirement: Measurements declare their provenance
The report SHALL include workflow wall time, public call count, summed and union server durations, connector overhead when observed, write ACK p50/p95, warming/refusal observations and windows, graph incremental/rebuild counts, derived scan pages/bytes, and final read-your-write correctness. Missing measurements SHALL be explicit nulls with reasons, never invented zeroes. Historical workflow reconstruction SHALL use call-ledger rows and distinguish client gaps from measured connector or model time.

#### Scenario: Concurrent calls overlap
- **WHEN** two server calls overlap in wall time
- **THEN** their summed work is reported separately from interval-union occupancy and no negative client overhead is derived

### Requirement: Comparative workflows use equivalent public semantics
A Basic Memory comparison SHALL pin immutable tested artifact bytes and resolved dependencies, run persistent public clients on the same common Markdown write/edit/read/text-search workload, and measure accepted writes separately from search convergence. Non-common governance, provenance, evidence and media capabilities SHALL be documented separately. An internal diagnostic SHALL NOT be presented as a public competitive-suite ranking; publication SHALL follow the existing benchmark-fairness programme, including configuration/glue provenance, paired own-harness/direct variants, fault invalidation and independent review.

#### Scenario: Competitor search is asynchronous
- **WHEN** an accepted write is immediately readable but not yet searchable
- **THEN** exact read-your-write succeeds and search-convergence latency remains pending until the public search proves it

### Requirement: Shared-workload improvement is repeatable and work-bounded

Claims of shared Markdown workflow parity SHALL use at least three fresh paired
runs per declared corpus size with identical fixed bodies, public operations,
runtime pins, and isolated state. Product order SHALL alternate. The report SHALL
retain individual observations and compare median verified-closure times;
startup and optional graph convergence SHALL remain separate measurements.
Deterministic regression checks SHALL also measure unrelated body reads during
warm writer resolver preparation and topology dependency discovery. A timing
improvement SHALL NOT substitute for full-rebuild parity or durability evidence.

#### Scenario: One unusually fast run
- **WHEN** one observation meets the target but the paired median does not
- **THEN** the report records the observations and does not claim the parity target was met

#### Scenario: Background graph completion is delayed
- **WHEN** exact public reads and text search verify closure before graph convergence
- **THEN** the report names the two states separately and does not treat useful closure as proof that all derived work finished

#### Scenario: Work-count regression despite fast hardware
- **WHEN** a warm resolver or dependency-discovery path rereads unrelated Markdown
- **THEN** its deterministic regression check fails even if elapsed time remains under a wall-clock threshold

### Requirement: Comparative startup proves the indexed corpus size
Before the timed shared workflow, each product adapter SHALL prove that every
generated fixture path is represented exactly once in its Markdown metadata and
text-search projection in one coherent, read-only snapshot of the current run's
disposable derived store. It SHALL bind the proof to the expected fixture path
set and product/project identity, retain membership counts and digests, and
preserve independent public readiness gates. It SHALL NOT use directory file
counts, total row counts or one successful sentinel lookup as completeness proof.
The adapter SHALL report startup separately, invalidate unsupported schemas, and
produce no timed workflow result when its bounded completeness wait expires.
The proof SHALL NOT mutate the index or substitute internal operations for timed
public calls.

#### Scenario: Sentinel appears in the first indexing batch
- **WHEN** the sentinel is searchable but only 100 of 3,800 fixture notes are indexed
- **THEN** setup remains pending and the workflow clock does not start

#### Scenario: Counts match but identities or search rows do not
- **WHEN** a missing fixture path is replaced by an unexpected or duplicate row, a wrong-project row, or metadata without its text-search row
- **THEN** the completeness proof fails even if the aggregate row count matches

#### Scenario: Full indexed fixture becomes ready
- **WHEN** exact fixture membership and corresponding search rows are proven in one current-run snapshot and public readiness succeeds
- **THEN** the unchanged shared public workflow starts and records the completeness evidence outside its timing
