## MODIFIED Requirements

### Requirement: Compact Success Is The Default Projection

Committed product mutations SHALL return a compact default projection led by
`ok`, `status`, `mutated`, primary `path`, original `request_id`, stable receipt
identity, `warnings_count`, and a closed `derived_sync` outcome. The outcome
SHALL be `completed`, `pending`, or `failed`; pending SHALL carry only bounded
component names and stable remediation, and failed SHALL remain distinguishable
from deadline expiry. `derived_sync` SHALL summarize non-graph, non-advisory
derived components and SHALL NOT replace or reinterpret the existing
`graph_sync` outcome. When default write-advisory work applies, the compact
projection SHALL expose `advisory_sync` as `completed`, `pending`,
`not_required`, or `failed`; pending or failed SHALL carry the stable
`advisory_result_ref`, and completed SHALL also carry that reference whenever an
applicable job completed before acknowledgement. `not_required` SHALL carry no
result reference. Failure MAY add only a closed code and fixed bounded next
action. These advisory fields SHALL remain outside the mutation-outcome key set
and SHALL NOT embed advisory content. A referenced result SHALL remain exactly
resolvable for at least as long as the corresponding mutation terminal remains
replayable; cleanup MUST NOT strand a replayed reference. `response_detail="full"`
SHALL add the complete leaf result under `diagnostics`;
`response_detail="legacy"` SHALL return the pre-change raw leaf result during
the compatibility window.

#### Scenario: Default committed response is decisive

- **WHEN** a governed product mutation succeeds without an explicit response detail
- **THEN** the first-level response identifies it as committed and mutated
- **AND** it distinguishes completed derived work from durable pending work without mixing verbose semantic, index, warning, or transition diagnostics into the compact terminal fields
- **AND** the existing `graph_sync`, the non-graph `derived_sync`, and `advisory_sync` outcomes cannot contradict or overwrite one another

#### Scenario: Full diagnostics are requested

- **WHEN** the same mutation is requested with `response_detail="full"`
- **THEN** the compact terminal fields and derived outcome are unchanged
- **AND** the complete existing leaf payload and bounded component diagnostics are available under `diagnostics`

#### Scenario: Pending is not failed

- **WHEN** the shared post-canonical deadline expires with exact unfinished work still durably covered
- **THEN** the compact terminal reports `derived_sync="pending"`
- **AND** it does not report `failed` or hide a real component error behind the deadline

## ADDED Requirements

### Requirement: Derived Convergence Does Not Rewrite The Original Terminal

The canonical terminal SHALL retain the exact component and advisory state
observed when the original acknowledgement was persisted. Later background
success or failure MUST NOT rewrite that completed idempotency result. Current
derived health MAY be read through status, recall, or reconciliation surfaces,
while an exact retry continues replaying the original terminal byte-for-byte
under the existing response-detail projection rules.

#### Scenario: Pending work completes before retry

- **WHEN** an original terminal recorded pending derived work and the background worker later completes it
- **THEN** an exact mutation retry replays the original pending terminal without executing the leaf
- **AND** current status independently reports the now-completed derived generation
