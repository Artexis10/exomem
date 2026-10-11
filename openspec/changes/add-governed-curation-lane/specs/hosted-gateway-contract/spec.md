## ADDED Requirements

### Requirement: Remote maintenance refusal remains closed around curation

For an active surface whose resolved schema declares
curation, the request-bound remote
maintenance gate SHALL admit write execution only for `structured-files` and
`curation`. `fix`, `reconcile`, `backfill-ids`, and every future unknown write
mode SHALL retain the operator-only refusal before manager dispatch. Within
curation, only an exact immutable reviewed plan may reach a content leaf; the
mode MUST NOT become a wrapper for ordinary maintenance or an arbitrary
command. Surfaces whose resolved schema omits curation MUST NOT receive this
exception.

#### Scenario: Remote reconcile remains refused

- **WHEN** a remote caller invokes write-mode reconcile after curation ships
- **THEN** it still receives `MAINTENANCE_REQUIRES_CLI` before manager dispatch

#### Scenario: Remote curation omits its reviewed identity

- **WHEN** a remote caller invokes an apply-shaped curation action without the
  exact plan id, fingerprint, or required rationale
- **THEN** the cell refuses before any content leaf executes

### Requirement: Curation evidence is portable

Canonical curation plans, state, relocation candidates, relocation
authorizations, witnesses, and step receipts SHALL live inside the governed
vault and SHALL use the same schema in local and Cloud deployments.
Machine-local retry state MAY accelerate delivery but MUST NOT be the sole
record required to inspect, resume, recover, or compensate a run after a process
restart or Cloud cell replacement. Content-witness recovery remains portable
from governed evidence alone. Placement recovery after replacement SHALL resume
only when the same retained vault filesystem epoch and namespace tokens remain
comparable. A changed or incomparable epoch SHALL still permit read-only status
but SHALL block placement recovery with `CURATION_RENAME_HISTORY_UNPROVABLE`;
portable evidence does not imply that rename history can be reconstructed on a
different filesystem.

#### Scenario: Cloud cell restarts between leaf and terminal receipt

- **WHEN** a cell restarts after a step's effect and witness committed but before
  its terminal receipt was written
- **THEN** read-only status exposes the exact recoverable outcome and the next
  resume reconstructs its terminal receipt from governed evidence without
  control-plane intervention or a second content effect

#### Scenario: Cloud replacement changes the placement filesystem epoch

- **WHEN** a cell is replaced while an authorized placement lacks its terminal
  witness and the retained namespace epoch or token is no longer comparable
- **THEN** status remains available from governed vault evidence
- **AND** resume blocks with `CURATION_RENAME_HISTORY_UNPROVABLE` rather than
  guessing, renaming, or relying on machine-local state
