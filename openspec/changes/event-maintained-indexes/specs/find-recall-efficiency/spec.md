## MODIFIED Requirements

### Requirement: Per-Request Freshness Snapshot

The system SHALL compute markdown freshness for a single `find` request at most once per scope: at
most one KB markdown stat-walk and at most one vault markdown stat-walk per request, shared by every
consumer that needs that scope's freshness within the same request (the BM25 index and the wikilink
resolver). A `scope="kb-only"` request that triggers no vault-scope work MUST NOT perform a
vault-wide stat-walk. When the event-maintained freshness registry for a scope is live, the request
SHALL consult the registry instead of walking — satisfying this requirement's "at most one walk"
bound with zero walks for that scope — and the freshness triple obtained from the registry MUST be
identical to the triple a walk would have produced. When the registry is not live, the request
SHALL fall back to the walk-based computation exactly as before, still bounded to at most one walk
per scope per request.

#### Scenario: One KB walk and one vault walk per request

- **WHEN** `find` is called with `scope="kb"` and a non-empty query that also triggers auto-widen's
  vault-scope check, and the event-maintained freshness registry is not live
- **THEN** the KB markdown tree is stat-walked at most once for that request
- **AND** the vault markdown tree is stat-walked at most once for that request, shared between
  auto-widen and any other vault-scope freshness check
- **AND** the returned hits are identical to the same request today

#### Scenario: kb-only scope never walks the vault

- **WHEN** `find` is called with `scope="kb-only"`
- **THEN** no vault-wide markdown stat-walk occurs for that request

#### Scenario: A live registry answers freshness with no walk and identical results

- **WHEN** `find` is called for a scope whose event-maintained freshness registry is live
- **THEN** that scope's freshness is obtained from the registry with no filesystem stat-walk
- **AND** the returned hits are identical to the same request served by a walk-based freshness
  computation over the same vault state
