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

### Requirement: Validity-Stamp Admission Governs Pre-Boundary Reuse

A pre-boundary preflight result SHALL carry a validity stamp composed of:
the corpus census the preflight's own corpus build already walked (never a
second stat-walk — the CI write-latency gate holds preflight to one walk
total), a scandir-census of the relation-review artifact and lifecycle
sidecar directories, and the boundary commit-generation read at preflight
ENTRY. The commit-generation is a monotonic per-vault counter advanced on
every mutation-guard exit while the boundary is still held, so every
governed writer moves it. Inside the boundary, admission SHALL compare only
the commit-generation and a fresh sidecar census against the stamp — an
O(sidecars) check with no corpus stat-walk; the pre-boundary corpus build,
evaluation, and validation results MAY be reused only when both match.
Identity, artifact-reservation, and destination-occupancy checks MUST always
be re-executed against live filesystem state inside the boundary regardless
of stamp match. A stamp mismatch, or a stamp that is `None` because an input
could not be censused cheaply or the commit-generation could not be read
(fail closed), MUST fall back to a full, bounded in-boundary revalidation
rather than reusing any pre-boundary result, and MUST produce the same
semantic-contract verdict a fresh evaluation would produce. Edits made
outside any governed writer (external tools, sync) are outside the stamp's
detection — for those, the same write-time protections the wide boundary
relied on (path guards, `create_only`, target freshness) still apply.

#### Scenario: A current stamp reuses pre-boundary validation

- **WHEN** the commit-generation inside the boundary equals the one captured
  at preflight entry and the fresh sidecar census matches the stamp
- **THEN** the commit reuses the pre-boundary corpus build, evaluation, and
  validation results instead of rebuilding them, without a corpus stat-walk
- **AND** the identity, artifact-reservation, and destination-occupancy
  checks still run fresh against live filesystem state

#### Scenario: A governed commit between preflight and commit invalidates reuse

- **WHEN** any governed writer commits (exiting a mutation guard) between
  the pre-boundary preflight and the in-boundary admission check
- **THEN** the commit-generation no longer matches the captured stamp
- **AND** the commit falls back to a full in-boundary revalidation that
  produces the same verdict a fresh evaluation would have produced, rather
  than reusing stale results

#### Scenario: An uncensusable tree or unreadable counter never reuses

- **WHEN** the corpus tree or the relation-review artifact and lifecycle
  sidecar directories cannot be censused cheaply (for example an unsafe
  symlink or reparse point), or the boundary commit-generation cannot be
  read for any reason other than never having been written
- **THEN** the validity stamp is `None`
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
