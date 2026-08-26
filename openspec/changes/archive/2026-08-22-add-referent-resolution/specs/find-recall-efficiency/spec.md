# find-recall-efficiency

## ADDED Requirements

### Requirement: Optional recall stages remain checkpoint-bounded and post-cache
The referents stage SHALL compute from released hits after the shared hit cache, SHALL never change the cache key or cached object, and SHALL record a `referents` timing span only when a deterministic cue is eligible.

#### Scenario: Hot cache hit
- **WHEN** a cue query is served from the find hot cache
- **THEN** referents are recomputed post-cache and match the cold response

#### Scenario: Scale bound
- **WHEN** the warm stage is measured at 2k/125 and 8k/500 pages/entities
- **THEN** 2k remains below 1000 ms and 8k remains within max(1.5x, +25 ms)
