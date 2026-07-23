## ADDED Requirements

### Requirement: Foreground Operations Have Explicit Latency Ceilings

The system SHALL keep warm persistent-runtime semantic-unit validation and guarded semantic-unit commit below explicit latency ceilings on deterministic 2,000- and 8,000-page reference corpora. Local validation SHALL have median below 500 ms and p95 below 1,000 ms; local commit SHALL have median below 750 ms and p95 below 1,500 ms. Cold semantic warm-up, model download, first interpreter startup, and explicitly requested heavy reranking are excluded; ordinary graph, relation-governance, logging, and coordination work are included.

#### Scenario: Warm semantic-unit write meets the ceiling

- **WHEN** the benchmark runs repeated guarded `observe_memory` validate and commit operations against a warmed 2,000-page vault
- **THEN** validation and commit each meet the local median and p95 ceilings

#### Scenario: Optional models do not redefine the write ceiling

- **WHEN** embedding, CLIP, OCR, ASR, or other optional measurement models are absent or warming
- **THEN** required canonical and semantic write acknowledgement still meets the foreground ceiling
- **AND** optional lanes report disabled, warming, or unavailable without blocking the write

### Requirement: Foreground Work Avoids Corpus-Wide Markdown Parsing

Optimized warm unit recall, validate, and commit paths MUST NOT open or parse every Markdown page. They MAY rederive bounded in-memory structures from cached metadata. On equivalent 2,000- and 8,000-page reference corpora, both the 8,000-page validation and commit medians SHALL remain below `2 * the corresponding 2,000-page median + 200 ms`; each size SHALL also satisfy the tighter absolute ceilings.

#### Scenario: Larger unchanged corpus remains bounded

- **WHEN** the benchmark repeats the same selected-page query and semantic-unit update on 2,000- and 8,000-page vaults
- **THEN** each operation satisfies the scaling formula
- **AND** instrumentation shows Markdown I/O bounded to indexed candidates and changed paths rather than every Markdown page

### Requirement: Unrelated Changes Do Not Reintroduce A Cold Corpus Rebuild

One unrelated Markdown create, edit, delete, move, or synchronized replacement SHALL be incorporated as a bounded delta before the next foreground request. It MUST NOT force that request to rebuild the complete semantic, lexical, resolver, or identity corpus.

#### Scenario: Unrelated synced edit precedes validation

- **WHEN** one unrelated page changes after the semantic snapshot is warm and the watcher reports that exact path
- **THEN** the next guarded validation applies or awaits only the bounded delta
- **AND** it still meets the foreground p95 ceiling

#### Scenario: Incremental state cannot be trusted

- **WHEN** the freshness token cannot prove current semantic state
- **THEN** the existing exact census identifies the changed inputs
- **AND** Markdown-only changes are reconciled by path while registry/config changes retain the cold full-build fallback

### Requirement: Latency Gates Combine Timing With Structural Evidence

CI SHALL run model-free regression tests that assert the absence of full-corpus foreground parses and a reproducible benchmark SHALL report cold warm-up, median, p95, and scaling-bound timings. Reports MUST contain aggregate counts/timings only and MUST NOT contain private paths, queries, or note content.

#### Scenario: O-corpus implementation fails even on a fast machine

- **WHEN** a candidate implementation meets the wall-clock threshold only by running on faster hardware but opens or parses the full reference corpus
- **THEN** the structural latency gate fails

#### Scenario: Benchmark output is safe to publish

- **WHEN** the foreground latency report is generated
- **THEN** it contains only aggregate timing and operation-count evidence
- **AND** it contains no authored query, Markdown body, title, or vault-relative path
