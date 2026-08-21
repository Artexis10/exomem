# graph-find-ranking

## MODIFIED Requirements

### Requirement: Referent corroboration respects release decisions
The optional referent composition stage MAY reuse the typed sidecar for one-hop corroboration, but SHALL use released anchors only, SHALL drop withheld entity and seed paths, and SHALL not modify graph-lane scoring or hit ordering.

#### Scenario: Withheld anchor
- **WHEN** graph evidence names an anchor withheld for the current audience
- **THEN** that evidence is removed before the referents block is emitted
