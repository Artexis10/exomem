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
