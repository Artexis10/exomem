## MODIFIED Requirements

### Requirement: SQL-Native Vector Search Backend

The system SHALL be able to serve vector KNN search from vec0 virtual tables co-located in
the existing embedding and CLIP sidecars, selected by `EXOMEM_VEC_BACKEND` (`numpy` |
`sqlite-vec`, default `numpy`). The default backend is the in-memory numpy scan; the vec0
backend MUST activate ONLY when `EXOMEM_VEC_BACKEND` is set explicitly to `sqlite-vec`. Any
other value — unset, the legacy `auto`, or an unrecognized string — MUST resolve to the
numpy scan. Under the default, the system MUST NOT probe for or load the sqlite-vec
extension, so installing the package MUST NOT change which backend serves search. When the
vec0 backend is opted into, in full-precision mode it MUST be exact: the ranked (path,
chunk) results and cosine scores MUST match what the in-memory scan returns for the same
sidecar state, within floating-point tolerance. When the opted-in backend is unavailable in
any way — the package is not installed, the Python build cannot load SQLite extensions, or a
runtime error occurs — vector search MUST soft-fail to the in-memory scan with unchanged
results and without recording a lane degradation. `EXOMEM_VEC_BACKEND=numpy` MUST force the
in-memory scan unconditionally. When the vec0 backend serves search, the process MUST NOT
need to hold the full vector matrix resident in Python memory for that search path. While
the vec0 backend is off, sidecar writers MAY skip vec dual-writes; a later opt-in MUST heal
any resulting blob-vs-vec drift from the stored blobs before serving vec0 results.

#### Scenario: Numpy is the default and the extension is never probed

- **WHEN** `EXOMEM_VEC_BACKEND` is unset (or set to `auto` or any unrecognized value)
- **THEN** vector search is served by the in-memory numpy scan
- **AND** the sqlite-vec extension is neither probed nor loaded, and no vec tables are
  written

#### Scenario: The vec0 backend is opt-in and exact

- **WHEN** `EXOMEM_VEC_BACKEND=sqlite-vec` is set and the same query runs once under the
  vec0 full-precision backend and once under the in-memory scan, over an unchanged sidecar
- **THEN** the ordered (path, chunk) results are identical
- **AND** the scores match within floating-point tolerance

#### Scenario: Installing sqlite-vec does not change the serving backend

- **WHEN** the sqlite-vec package becomes importable but `EXOMEM_VEC_BACKEND` is not set to
  `sqlite-vec`
- **THEN** vector search continues to use the in-memory numpy scan
- **AND** no vec tables are created by the search or write paths

#### Scenario: Unavailable extension falls back silently when opted in

- **WHEN** `EXOMEM_VEC_BACKEND=sqlite-vec` is set but sqlite-vec is not importable or the
  connection cannot load extensions
- **THEN** vector search returns the in-memory scan's results
- **AND** `find` records no vector-lane degradation for the fallback itself

#### Scenario: Kill switch forces the in-memory scan

- **WHEN** `EXOMEM_VEC_BACKEND=numpy` is set
- **THEN** vector search uses the in-memory scan even where the vec0 backend is available

#### Scenario: A runtime vec failure degrades to the scan for the process

- **WHEN** the vec0 backend is opted in and a vec0 KNN query raises at runtime
- **THEN** that search call returns correct results via the in-memory scan
- **AND** subsequent searches in the process stop attempting the vec0 backend

#### Scenario: Re-enabling vec0 heals drifted shadow tables

- **WHEN** a sidecar was advanced while the numpy default was in effect (vec shadow tables
  drifted from the blob tables) and a process later opts into `EXOMEM_VEC_BACKEND=sqlite-vec`
- **THEN** the first opt-in use rebuilds the vec rows from the stored blobs in pure SQL
- **AND** the vec0 backend then serves results identical to the in-memory scan
