## ADDED Requirements

### Requirement: Caller Can Bound Reranker Candidate Count

The system SHALL expose optional `rerank_max_candidates` on product recall and canonical find paths. When reranking runs, the scorer SHALL receive at most `min(3 * limit, rerank_max_candidates)` candidates. An explicit cap MUST be at least `limit` and no greater than the existing hard candidate ceiling; omission SHALL preserve current behavior.

#### Scenario: Caller selects a small rerank batch
- **WHEN** `limit=5`, reranking is enabled, and `rerank_max_candidates=5`
- **THEN** exactly the leading five available fused candidates are passed to the reranker
- **AND** optional-lane failure still returns deterministic fused results

#### Scenario: Caller supplies an invalid cap
- **WHEN** the cap is smaller than the requested result limit or exceeds the hard ceiling
- **THEN** the request fails locally with a validation error before model invocation

### Requirement: Reranker Bounds Are Observable And Cache-Safe

The requested and effective reranker candidate counts SHALL be included in timing/explanation metadata when requested, and `rerank_max_candidates` MUST participate in hot-cache identity. Ranking evidence MUST distinguish candidates scored by the reranker from fused-order tail candidates.

#### Scenario: Same query uses different caps
- **WHEN** otherwise identical requests use different reranker candidate caps
- **THEN** they do not share a cached ranked result
- **AND** diagnostics report the cap effective for each request

### Requirement: Reranking Remains Optional And Soft-Failing

The candidate cap MUST NOT enable reranking by itself or change the existing mode/device auto policy. A missing, warming, disabled, or failed reranker SHALL continue to return fused results with its existing explicit reason. The system SHALL NOT claim a hard wall-clock budget around a synchronous model call.

#### Scenario: Cap is provided while reranking is off
- **WHEN** `rerank=false` and a valid candidate cap is supplied
- **THEN** no reranker model is loaded or invoked
- **AND** the retrieval profile reports the existing explicit-false reason

#### Scenario: Reranker dependency is unavailable
- **WHEN** reranking is selected but its optional dependency cannot load
- **THEN** the request succeeds with fused results
- **AND** diagnostics report dependency-unavailable rather than a timeout guarantee
