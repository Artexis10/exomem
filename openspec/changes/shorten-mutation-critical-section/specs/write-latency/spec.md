## ADDED Requirements

### Requirement: Bounded Mutation-Boundary Hold For Governed Semantic Writes

For a command on the narrowed-boundary set, the vault mutation boundary
SHALL be held only across the commit seam of a governed semantic write —
canonical write, index/log updates, and fencing — not across corpus
validation, relation-review evaluation, or embedding-model loading. The
worst-case in-boundary time SHALL be bounded by one census check plus, on a
census mismatch, one warm delta-reconciled revalidation; it MUST NOT include
a cold embedding-model load.

#### Scenario: A slow validator does not extend the held boundary

- **WHEN** pre-commit corpus validation for a narrowed command takes several
  seconds
- **THEN** that time is spent before the mutation boundary is acquired
- **AND** the measured boundary hold for the commit does not include it

#### Scenario: A cold embedding-model load never happens inside the boundary

- **WHEN** the embedding model has not yet been loaded in this process and a
  narrowed command's commit path would otherwise trigger a cold load
- **THEN** the load is attempted, best-effort, before the boundary is
  acquired
- **AND** the mutation snapshot observed at the moment of that load attempt
  reports no in-flight mutation for this write

### Requirement: Census-Token Validity Governs Pre-Boundary Reuse

A pre-boundary preflight result SHALL carry a census-validity token —
derived from a stat-level census of every corpus input plus a scandir-census
of the relation-review artifact and lifecycle sidecar directories — captured
as a before/after sandwich around the preflight. Inside the boundary, a
freshly computed token SHALL be compared against the captured one; the
pre-boundary corpus build, evaluation, and validation results MAY be reused
only on an exact match. Identity, artifact-reservation, and
destination-occupancy checks MUST always be re-executed against live
filesystem state inside the boundary regardless of token match. A token
mismatch, or a token that is `None` because some input could not be censused
cheaply or the sandwich disagreed, MUST fall back to a full, bounded
in-boundary revalidation rather than reusing any pre-boundary result, and
MUST produce the same semantic-contract verdict a fresh evaluation would
produce.

#### Scenario: An exact census match reuses pre-boundary validation

- **WHEN** the census token recomputed inside the boundary exactly matches
  the token captured for the pre-boundary preflight
- **THEN** the commit reuses the pre-boundary corpus build, evaluation, and
  validation results instead of rebuilding them
- **AND** the identity, artifact-reservation, and destination-occupancy
  checks still run fresh against live filesystem state

#### Scenario: A sibling write between preflight and commit invalidates reuse

- **WHEN** another write changes corpus, relation-review, or lifecycle state
  between the pre-boundary preflight and the in-boundary commit
- **THEN** the freshly computed census token does not match the captured one
- **AND** the commit falls back to a full in-boundary revalidation that
  produces the same verdict a fresh evaluation would have produced, rather
  than reusing stale results

#### Scenario: An uncensusable tree never reuses pre-boundary validation

- **WHEN** the corpus tree, or the relation-review artifact and lifecycle
  sidecar directories, cannot be censused cheaply (for example an unsafe
  symlink or reparse point)
- **THEN** the census token is `None`
- **AND** the commit always performs a full in-boundary revalidation, never
  a reuse

### Requirement: Revalidation-Outcome Telemetry

Every commit for a narrowed-boundary command SHALL record whether its
pre-boundary validation was reused, revalidated on a census mismatch, or
unavailable (no pre-boundary result to compare), as a counter labeled by
outcome and an accompanying log event. This telemetry, together with the
existing `exomem_boundary_hold_ms` measurement and the mutation journal's
hold fields, is the mechanism for observing whether the narrowed boundary
achieves its latency goal in production.

#### Scenario: A reused commit is counted separately from a revalidated one

- **WHEN** a commit reuses pre-boundary validation under an exact census
  match
- **THEN** `exomem_prevalidated_commit_total{outcome="reused"}` increments
- **AND** a commit that instead falls back to in-boundary revalidation
  increments `exomem_prevalidated_commit_total{outcome="revalidated"}`
  instead
