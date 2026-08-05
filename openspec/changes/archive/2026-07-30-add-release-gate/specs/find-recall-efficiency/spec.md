# find-recall-efficiency

## ADDED Requirements

### Requirement: Release decisions do not fragment the recall cache

Governance release decisions SHALL NOT be stored in the `find` hot cache and SHALL
NOT be part of its key. The hot cache SHALL remain keyed on content and policy
fingerprints only, storing principal-free candidates; release decisions SHALL be
computed per request after a cache hit and memoized separately. Declared purpose
SHALL NOT enter the recall cache key.

#### Scenario: Cache hit still decides per principal

- **WHEN** a query result is served from the hot cache to a second audience
- **THEN** decisions are recomputed for that audience and no cached candidate
  carries a prior audience's decision

#### Scenario: Purpose does not bust the recall cache

- **WHEN** the same query is issued with different declared purposes
- **THEN** both are served from the same cached candidate set, with decisions
  applied afterward
