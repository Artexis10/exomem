## MODIFIED Requirements

### Requirement: Injected Content Is Bounded, Stub-Only Routing Data

The injected block SHALL show only the `path`, `type` and `updated` fields of each compact hit returned by
`find(detail="compact")` — never `excerpt`, `signals`, or any other page body/text, even when the compact hit
carries one. It SHALL show at most 3 hits and SHALL be truncated to approximately 400
characters if the formatted block would otherwise exceed that.

#### Scenario: Three or fewer hits are shown verbatim

- **WHEN** the active transport returns 1 to 3 compact hits
- **THEN** the injected block contains one line per hit, in the order
  returned, each showing only `path`, `type`, and `updated`
- **AND** no hit's `excerpt` or `signals` (a compact hit may carry an `excerpt`; the block never shows it)
  appears anywhere in the block

#### Scenario: An oversized block is truncated, never hit-count-limited beyond 3

- **WHEN** the formatted routing-stub block (3 hits, long titles/paths) would
  exceed approximately 400 characters
- **THEN** the block is truncated to approximately 400 characters with a
  trailing truncation marker
- **AND** the request itself never asked for more than 3 hits (`limit=3`)
