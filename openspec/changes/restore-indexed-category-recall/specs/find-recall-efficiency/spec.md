## ADDED Requirements

### Requirement: Reproducible Structured Category Latency Gate

The workstation retrieval harness SHALL run page-level and unit-level exact-category lanes in a live service process with semantic catalog and OS file cache warm. Each cold sample SHALL clear the in-process result and parsed-page caches, then run `scope=kb-only`, `mode=keyword`, empty query, graph/rerank/pack disabled, and two indexed candidates. It SHALL collect 30 samples and compute nearest-rank p95, excluding connector RTT and startup/catalog construction. Hot samples SHALL repeat an unchanged request with the result cache live. Cold `filter_eligibility` p95 MUST be below 100 ms, cold total p95 below 250 ms, and hot total p95 below 10 ms.

#### Scenario: Page and unit lanes expose equivalent stages

- **WHEN** the latency harness runs both result levels
- **THEN** each report contains comparable `filter_eligibility` and total stage distributions
- **AND** its sample count, cache reset policy, candidate bucket, and percentile method are recorded

### Requirement: Structural Scaling Is The CI Gate

CI SHALL use operation-count tests proving Markdown hydration tracks candidate count plus fixed overfetch rather than corpus size. Timing thresholds SHALL remain workstation release evidence and MUST NOT make shared-runner tests flaky.

#### Scenario: Corpus growth cannot hide a scan regression

- **WHEN** the same two candidates are embedded in 2,000-page and 8,000-page fixtures
- **THEN** parent opens remain within the same fixed bound
- **AND** the test fails if eligibility invokes a corpus walk

### Requirement: Real-Vault Reports Are Aggregate And Anonymized

Committed or shared real-vault performance evidence SHALL use synthetic category labels, anonymous run IDs, corpus-size buckets rounded to 500, candidate-count buckets, and latency distributions only. It MUST NOT contain exact category values or frequencies, query text, paths, excerpts, project names, or exact candidate counts.

#### Scenario: Report cannot reveal category usage

- **WHEN** a real-vault category benchmark report is rendered
- **THEN** category and candidate identities are replaced by synthetic labels and buckets
- **AND** no source path, excerpt, query text, or exact category frequency appears
