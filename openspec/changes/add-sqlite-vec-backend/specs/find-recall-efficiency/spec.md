## ADDED Requirements

### Requirement: SQL-Native Vector Search Backend

The system SHALL be able to serve vector KNN search from vec0 virtual tables co-located in
the existing embedding and CLIP sidecars, selected by `EXOMEM_VEC_BACKEND` (`auto` |
`sqlite-vec` | `numpy`, default `auto`). In full-precision mode the vec0 backend MUST be
exact: the ranked (path, chunk) results and cosine scores MUST match what the in-memory
scan returns for the same sidecar state, within floating-point tolerance. When the backend
is unavailable in any way — the package is not installed, the Python build cannot load
SQLite extensions, or a runtime error occurs — vector search MUST soft-fail to the
in-memory scan with unchanged results and without recording a lane degradation.
`EXOMEM_VEC_BACKEND=numpy` MUST force the in-memory scan unconditionally. When the vec0
backend serves search, the process MUST NOT need to hold the full vector matrix resident
in Python memory for that search path.

#### Scenario: Full-precision backend returns identical ranking

- **WHEN** the same query runs once under the vec0 full-precision backend and once under
  the in-memory scan, over an unchanged sidecar
- **THEN** the ordered (path, chunk) results are identical
- **AND** the scores match within floating-point tolerance

#### Scenario: Unavailable extension falls back silently

- **WHEN** sqlite-vec is not importable or the connection cannot load extensions
- **THEN** vector search returns the in-memory scan's results
- **AND** `find` records no vector-lane degradation for the fallback itself

#### Scenario: Kill switch forces the in-memory scan

- **WHEN** `EXOMEM_VEC_BACKEND=numpy` is set
- **THEN** vector search uses the in-memory scan even where the vec0 backend is available

#### Scenario: A runtime vec failure degrades to the scan for the process

- **WHEN** a vec0 KNN query raises at runtime
- **THEN** that search call returns correct results via the in-memory scan
- **AND** subsequent searches in the process stop attempting the vec0 backend

### Requirement: Opt-In Quantized Vector Mode

The system SHALL support a binary-quantized vector search mode, enabled only by
`EXOMEM_VEC_QUANT=binary` (default `off`). Quantized search MUST rescore its candidate set
against the stored full-precision vectors and return true cosine scores, so downstream
score semantics are unchanged. The quantized configuration MUST clear the golden retrieval
floors (the same NDCG/MRR/recall floors and per-query zero-recall guard the default
configuration is held to) before being recommended, and the mode MUST NOT be enabled
implicitly by corpus size or any other heuristic.

#### Scenario: Quantized mode passes the golden retrieval gate

- **WHEN** the golden retrieval evaluation runs with `EXOMEM_VEC_QUANT=binary`
- **THEN** mean NDCG@10, MRR, and recall@10 clear the same floors as the default
  configuration
- **AND** no golden query drops to zero recall

#### Scenario: Quantized scores are full-precision cosine

- **WHEN** a query runs in quantized mode
- **THEN** returned scores are cosine similarities computed from full-precision vectors,
  not quantized distances

#### Scenario: Quantization is never implicit

- **WHEN** `EXOMEM_VEC_QUANT` is unset
- **THEN** vector search never uses the quantized tables, at any corpus size

## MODIFIED Requirements

### Requirement: Retrieval Architecture Changes Are Deferred Until Measured

The system SHALL NOT adopt a retrieval-architecture change (a new vector index backend,
LSH, ANN, or a new vector database) without per-lane timing measurement identifying the
lane and cost the change addresses. The vec0 SQL-native backend is adopted under this
rule: the per-lane timing diagnostics and the latency-vs-scale curve identified the vector
lane's in-memory O(N) matrix load and scan as the corpus-linear cost, and the backend is
held to exactness (full precision) or the golden retrieval floors (quantized). Any FURTHER
retrieval-architecture change (including an ANN index) SHALL be considered only with the
same evidence: a measured lane cost it addresses, plus the golden floors as its recall
gate.

#### Scenario: Backend adoption is evidence-gated

- **WHEN** this change adopts the vec0 backend for the vector lane
- **THEN** the latency-vs-scale harness records the in-memory scan's cost curve before the
  swap and the backend's curve after it
- **AND** the golden retrieval floors hold in every shipped configuration

#### Scenario: Further rewrites remain deferred until measured

- **WHEN** a future ANN index or retrieval rewrite is proposed
- **THEN** it is adopted only with per-lane measurement identifying the cost it addresses
- **AND** the golden retrieval floors gate its recall

### Requirement: Process-Lifetime Embedding Matrix Cache

The system SHALL load the embedding (and CLIP) vector matrix from its sidecar at most once
per unchanged sidecar state and reuse it across `find` calls, via a process-shared
per-vault index instance, whenever the in-memory scan serves vector search. A brand-new
call site MUST NOT construct an index whose in-memory matrix starts empty and forces a
full reload. Startup warm-up MUST prime the backend that `find` will actually use: the
shared matrix when the in-memory scan serves search, or the vec0 tables' readiness (sync
check and first-touch) when the vec0 backend serves search — in which case the matrix MAY
remain unloaded and no `find` call may force a matrix load for search purposes.

#### Scenario: Repeated finds reuse a single matrix load

- **WHEN** two or more `find` requests run under the in-memory scan against a vault whose
  embedding sidecar has not changed between them
- **THEN** the vector matrix is loaded from the sidecar at most once for that unchanged
  state
- **AND** the later requests reuse the already-loaded matrix rather than re-reading and
  re-stacking every row

#### Scenario: Warm-up primes the backend find actually uses

- **WHEN** startup warm-up runs and a subsequent `find` executes with the sidecar
  unchanged
- **THEN** that `find` is served by a backend warm-up already primed — the shared matrix
  under the in-memory scan, or synced vec0 tables under the vec0 backend — without paying
  first-touch construction cost

#### Scenario: The vec0 backend does not hold the matrix resident

- **WHEN** the vec0 backend serves vector search for a process
- **THEN** `find` calls do not load the full vector matrix into Python memory for search
