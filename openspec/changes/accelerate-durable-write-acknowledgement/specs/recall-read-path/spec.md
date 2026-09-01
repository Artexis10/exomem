## ADDED Requirements

### Requirement: Pending Derived Work Preserves Exact Read-Your-Write

After a committed acknowledgement, direct path and stable-reference reads SHALL
resolve the exact committed canonical generation immediately. Keyword and hybrid
recall SHALL merge a bounded exact pending-delta projection with the last
published lexical catalogue, exclude stale catalogue rows for every changed or
deleted pending path, and deduplicate by current canonical identity. The pending
projection SHALL survive process restart from durable receipt state before
managed recall is declared ready. If complete pending coverage cannot be proven
within its bound, managed recall MUST return a typed warming or unavailable
outcome rather than stale or silently incomplete results. Vector and graph lanes
MAY omit the new generation while their receipts are pending, but the default
recall response SHALL disclose that projection state.

#### Scenario: Query immediately follows a create

- **WHEN** a caller acknowledges a new governed page and immediately searches for exact terms from it
- **THEN** keyword and hybrid recall can return the new canonical page from the pending delta before vector or graph publication
- **AND** the response identifies vector or graph coverage as pending when applicable

#### Scenario: Query immediately follows an edit

- **WHEN** a committed edit replaces terms that still exist in the last lexical or vector generation
- **THEN** recall suppresses the stale pre-edit row and considers the exact new canonical generation
- **AND** it never returns the old excerpt as current

#### Scenario: Query immediately follows a delete or move

- **WHEN** a committed mutation deletes a path or moves its identity while derived catalogues remain pending
- **THEN** recall suppresses the old path immediately
- **AND** a moved current identity is returned at most once under its current path

#### Scenario: Restart cannot reconstruct a complete pending projection

- **WHEN** managed recall starts with pending receipts but cannot prove or hydrate their complete current delta
- **THEN** recall reports warming or unavailable
- **AND** it does not serve the last catalogue as if no pending mutation existed

