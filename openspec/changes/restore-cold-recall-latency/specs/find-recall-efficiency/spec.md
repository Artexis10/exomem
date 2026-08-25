## MODIFIED Requirements

### Requirement: Per-Request Freshness Snapshot

The system SHALL compute markdown freshness for a single `find` request at most once per scope and SHALL distinguish activated server requests from explicit offline callers. An activated server request MUST use a live checkpoint-bound recall projection and MUST NOT perform a KB or vault markdown stat-walk to construct recall admission. When a required server projection is not live, the request SHALL return the retryable retrieval-warming outcome and schedule or observe repair without walking. An offline/CLI caller with no activated runtime MAY fall back to the walk-based computation, still bounded to at most one KB walk and one vault walk per request. A `scope="kb-only"` request MUST NOT perform a vault-wide walk in either execution context. A live registry's triple and path set MUST equal a fresh policy-projected walk over the same state.

#### Scenario: Activated server request never walks for recall admission

- **WHEN** any keyword, hybrid, or vector `find` request runs in an activated server process and its required recall projection is not live
- **THEN** the request returns the retryable retrieval-warming outcome
- **AND** no KB or vault markdown stat-walk is performed by that request

#### Scenario: Offline caller retains one-walk correctness fallback

- **WHEN** `find` is invoked by an explicit offline/CLI caller with no activated runtime and the event-maintained registry is not live
- **THEN** the KB markdown tree is stat-walked at most once for the request
- **AND** the vault markdown tree is stat-walked at most once when vault scope is required
- **AND** the result reflects the same policy-projected source state as before this change

#### Scenario: kb-only scope never walks the vault

- **WHEN** `find` is called with `scope="kb-only"`
- **THEN** no vault-wide markdown stat-walk occurs for that request

#### Scenario: Live registry answers freshness with no walk and identical results

- **WHEN** `find` is called for a scope whose event-maintained recall projection is live
- **THEN** that scope's freshness and allowed paths are obtained from the registry with no filesystem stat-walk
- **AND** the returned hits are identical to a policy-projected walk over the same vault state

## ADDED Requirements

### Requirement: Recall Projection Timing Is Attributed

Timing diagnostics SHALL attribute recall admission/projection acquisition, watcher-seed waiting observed by a request, resolver acquisition, and referent enrichment as named stages. Material time spent in those stages MUST NOT appear only as `unattributed_ms`.

#### Scenario: Projection acquisition appears in timings

- **WHEN** a timed `find` acquires a recall projection or declines because it is not live
- **THEN** timing diagnostics include a recall-projection stage with its elapsed time and outcome
- **AND** the same elapsed work is not counted only as unattributed time
