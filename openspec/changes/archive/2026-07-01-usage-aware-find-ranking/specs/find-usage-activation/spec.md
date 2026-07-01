## ADDED Requirements

### Requirement: Opt-In Usage-Activation Boost Defaults Off

The system SHALL support an opt-in `prefer_used` parameter on `find` that applies a
usage-activation boost derived from existing access logs (find-surfacings, `get` reads, and
cited writes). `prefer_used` SHALL default to `False`. When `prefer_used` is `False`, `find`
SHALL return byte-identical results, ordering, and `signals` to a `find` call made before this
capability existed.

#### Scenario: Default find behavior is unaffected

- **WHEN** `find` is called without specifying `prefer_used`
- **THEN** the returned hits, their order, and their `signals` are identical to the same call
  made without usage-activation logic present
- **AND** no usage-activation signal is computed or read from disk

#### Scenario: Usage boost only applies when explicitly requested

- **WHEN** `find` is called with `prefer_used=True`
- **THEN** hits may be reordered relative to the `prefer_used=False` call for the same query
- **AND** the same set of candidate paths is still passed through the existing content-matching
  rules before usage activation is considered

### Requirement: Usage Boost Is Bounded, Positive-Only, and Below The Compiled Boost

The system SHALL compute the usage-activation multiplier as strictly greater than or equal to
`1.0` and strictly less than the configured `usage_boost` ceiling. `usage_boost` SHALL default
to a value strictly less than `compiled_boost` (the existing `prefer_compiled` multiplier). The
multiplier MUST NEVER be less than `1.0` for any page, regardless of how little that page has
been accessed.

#### Scenario: Unused page keeps the neutral multiplier

- **WHEN** a page has no find-surfacing, read, or citation events within the usage horizon
- **THEN** its usage multiplier is exactly `1.0`
- **AND** its ranking is unaffected by usage activation

#### Scenario: Heavily-used page never exceeds the configured ceiling

- **WHEN** a page has the maximum plausible usage activation
- **THEN** its usage multiplier is strictly less than `usage_boost`
- **AND** its usage multiplier never equals or exceeds `compiled_boost`

#### Scenario: A superseded page cannot outrank its active successor via usage alone

- **WHEN** a `status: superseded` page has maximum usage activation
- **AND** its active successor has baseline (unused) usage activation
- **THEN** the superseded page's combined supersession-penalty-times-usage-boost score remains
  below the active successor's baseline score

### Requirement: Usage Activation Is Transparent In Signals

The system SHALL surface the raw activation value and the applied usage multiplier in a hit's
`signals` whenever `prefer_used` is active and that hit's usage multiplier is not `1.0`. When
`prefer_used` is `False`, or a hit's usage multiplier is exactly `1.0`, these fields MUST be
absent so unaffected hits carry no new noise.

#### Scenario: Boosted hit shows its activation and multiplier

- **WHEN** `find` is called with `prefer_used=True`
- **AND** a returned hit's usage multiplier is not `1.0`
- **THEN** that hit's `signals` includes `activation` (the raw base-level activation)
- **AND** that hit's `signals` includes `usage_boost` (the multiplier applied)

#### Scenario: Unaffected hits carry no usage signals

- **WHEN** `find` is called with `prefer_used=True`
- **AND** a returned hit's usage multiplier is exactly `1.0`
- **THEN** that hit's `signals` does not include `activation` or `usage_boost`

### Requirement: Usage Snapshot Freshness And Strict No-Op Conditions

The system SHALL memoize usage-activation data in a snapshot invalidated by access-log file
size/mtime changes and by the current date, refreshed at most every configurable interval. The
system SHALL treat usage activation as a strict no-op — every usage multiplier exactly `1.0` —
under each of: no access logs present, all access logs empty, query logging disabled, embeddings
disabled, or an explicit usage-boost kill-switch enabled.

#### Scenario: Cold start produces no boost

- **WHEN** `find` is called with `prefer_used=True` and no access logs exist yet
- **THEN** every returned hit's usage multiplier is exactly `1.0`
- **AND** no hit's `signals` includes `activation` or `usage_boost`

#### Scenario: Disabled logging or an explicit kill-switch forces a no-op

- **WHEN** query logging is disabled, embeddings are disabled, or the usage-boost kill-switch is
  set
- **THEN** `find` calls with `prefer_used=True` behave identically to `prefer_used=False`

#### Scenario: Snapshot reflects new access-log activity

- **WHEN** an access log file grows after the snapshot's refresh interval has elapsed, or the
  snapshot is explicitly reset
- **THEN** the next `find` call with `prefer_used=True` computes usage multipliers from the
  updated log contents

### Requirement: Usage Activation Never Creates Candidates

The system SHALL apply the usage-activation multiplier only to candidates already surfaced by
the existing content-matching lanes (keyword, BM25, vector, graph, CLIP) or already present in
the fused/reranked hit list. Usage activation MUST NOT introduce a path into the results that
those lanes did not surface, regardless of that path's usage activation.

#### Scenario: An irrelevant but heavily-used page stays absent

- **WHEN** a page has high usage activation but does not match the query on any content-matching
  lane
- **THEN** that page does not appear in the `find` results, with or without `prefer_used`

#### Scenario: Usage activation is not a fusion lane

- **WHEN** `find` runs with `prefer_used=True`
- **THEN** usage activation is applied only as a post-fusion multiplier to already-ranked
  candidates
- **AND** usage activation is not included as an input to the reciprocal rank fusion of content
  lanes

### Requirement: Default Find Ranking Remains Usage-Blind

The system SHALL NOT consult usage-activation data when `prefer_used` is not explicitly set to
`True`. This capability SHALL NOT change the default value of any existing `find` parameter, and
SHALL NOT be enabled by any mechanism other than an explicit `prefer_used=True` on a given call.

#### Scenario: Existing callers are unaffected by this capability's existence

- **WHEN** an existing caller invokes `find` without knowledge of `prefer_used`
- **THEN** its results, ordering, and `signals` are unchanged by this capability shipping

#### Scenario: No configuration can silently enable usage ranking by default

- **WHEN** `ranking_config.json` sets `usage_boost`, `usage_decay`, `usage_horizon_days`, or the
  usage weight knobs
- **THEN** those values only take effect on a `find` call made with `prefer_used=True`
- **AND** they have no effect on any `find` call made with `prefer_used=False` (the default)
