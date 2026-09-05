# relation-acceptance-queue

## ADDED Requirements

### Requirement: Batched deterministic relation queue
`review_memory(mode="relation-queue")` SHALL return a read-only, deterministic
batch of relation candidates produced by the existing deterministic
suggestion methods, grouped by source page, each item carrying a stable
review ref (`exomem://review/relation/<id>`), a signal fingerprint, the
candidate triple (from, to, relation_type), its method, and its evidence.
The queue SHALL exclude: candidates whose edge already exists as an authored
relation; candidates targeting unresolved placeholders; and candidates whose
fingerprint has an unexpired dismiss/snooze decision. The read SHALL be
propose-only (`mutated: false`) and SHALL emit coverage counters aligned with
the activation denominators.

#### Scenario: Deterministic batch with filtering
- **WHEN** the queue is read twice on an unchanged corpus
- **THEN** both reads return identical items in identical order, none of
  which duplicate an authored edge or target a placeholder

#### Scenario: Read never mutates
- **WHEN** the queue is read
- **THEN** no Markdown, sidecar edge, or review-state entry changes and the
  response reports `mutated: false`

### Requirement: Fingerprint-bound rejection via triage
`triage_memory` SHALL accept relation-queue refs with the existing
`dismiss`/`snooze`/`reopen` actions, persisting decisions keyed by
`review_id:signal_fingerprint` in the existing review-state store. A
dismissed candidate SHALL NOT reappear in the queue while its fingerprint is
unchanged, and SHALL reappear when the underlying signal materially changes.
Relation-queue identities SHALL be namespaced so triaging a relation
candidate never resolves an activation or attention item, and vice versa.

#### Scenario: Dismissed candidate stays gone
- **WHEN** a candidate is dismissed and the queue is re-read on an unchanged
  corpus
- **THEN** the candidate is absent

#### Scenario: Changed signal resurfaces
- **WHEN** a dismissed candidate's source page changes such that the
  suggestion's fingerprint changes
- **THEN** the candidate reappears with the new fingerprint

### Requirement: Governed server-side accept
`connect_memory(operation="accept-relation")` SHALL accept exactly one queue
item by ref, validate that the item's fingerprint still matches the live
signal and that the caller-supplied `expected_hash` matches the target page,
and then author the canonical relation via the existing governed edit path —
appending `- relation_type [[Target]]` under the `## Relations` heading,
identical in effect to the Studio's existing single-proposal write. On
fingerprint or hash mismatch the operation SHALL refuse with the existing
drift error contract and write nothing. An accepted candidate SHALL
disappear from subsequent queue reads because its edge now exists.

#### Scenario: Accept writes one canonical bullet
- **WHEN** accept-relation is called with a valid ref, matching fingerprint,
  and matching expected_hash
- **THEN** exactly one `- relation_type [[Target]]` bullet is appended under
  the page's `## Relations` section and the graph dual-write indexes it

#### Scenario: Drift refuses the write
- **WHEN** the target page changed after the queue was read (hash mismatch)
  or the candidate signal changed (fingerprint mismatch)
- **THEN** the operation writes nothing and returns the drift error contract

#### Scenario: Accepted item leaves the queue
- **WHEN** a candidate is accepted and the queue is re-read
- **THEN** the candidate is absent (its edge is now authored)

### Requirement: Studio batched queue panel
The Review Studio SHALL present the relation queue as a batched panel grouped
by page with per-candidate accept and reject actions, using the existing
fingerprint-guarded write flow, requiring an audit reason for accepts, and
recomputing no ranking client-side. The existing one-at-a-time relation
proposal modal SHALL remain available unchanged.

#### Scenario: Batch accept and reject in one session
- **WHEN** a user accepts two candidates and dismisses one from the panel
- **THEN** two canonical bullets are written via the governed accept
  operation, one review-state dismissal is recorded, and the panel refreshes
  without the three handled items
