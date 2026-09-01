## ADDED Requirements

### Requirement: Hosted v5 admits exact reviewed curation without a bespoke executor

The Hosted v5 agent surface SHALL expose registry-derived
`maintain_memory(mode="curation")` and forward it to the same curation leaf used
by standalone MCP, REST, and CLI. The gateway and cell MUST NOT add a Hosted-only
plan format, command intercept, executor, persistence path, or semantic model.
All tenant mapping, authenticated principal, entitlement, path confinement,
writer authority, process-safe mutation boundary, terminal receipt, and
tenant-scoped idempotency checks SHALL run normally.

Hosted v1-v4 SHALL retain their pinned legacy `maintain_memory` schema and
actual-wire identities and SHALL refuse curation-only arguments before manager
dispatch.

#### Scenario: Hosted user applies a reviewed plan

- **WHEN** an authenticated Hosted principal submits the exact approved plan id,
  fingerprint, and rationale to its mapped cell
- **THEN** the generic registry route invokes the shared curation executor under
  that cell's ordinary mutation and receipt boundaries

#### Scenario: Cross-tenant plan id is presented

- **WHEN** a principal presents a run or plan identity stored only in another
  tenant cell
- **THEN** resolution fails inside the mapped cell without revealing whether the
  other tenant's identity exists

#### Scenario: Historical Hosted profile receives curation arguments

- **WHEN** a v1-v4 caller supplies `mode="curation"` or another curation-only argument
- **THEN** profile validation refuses the call before manager dispatch
- **AND** the historical descriptor and actual-wire schema remain byte-identical

### Requirement: Remote maintenance refusal remains closed around curation

For a profile whose pinned schema declares curation, the request-bound remote
maintenance gate SHALL admit write execution only for `structured-files` and
`curation`. `fix`, `reconcile`, `backfill-ids`, and every future unknown write
mode SHALL retain the operator-only refusal before manager dispatch. Within
curation, only an exact immutable reviewed plan may reach a content leaf; the
mode MUST NOT become a wrapper for ordinary maintenance or an arbitrary
command. Profiles whose pinned schema omits curation MUST NOT receive this
exception.

#### Scenario: Hosted reconcile remains refused

- **WHEN** a Hosted caller invokes write-mode reconcile after curation ships
- **THEN** it still receives `MAINTENANCE_REQUIRES_CLI` before manager dispatch

#### Scenario: Hosted curation omits its reviewed identity

- **WHEN** a Hosted caller invokes an apply-shaped curation action without the
  exact plan id, fingerprint, or required rationale
- **THEN** the cell refuses before any content leaf executes

### Requirement: Hosted and standalone curation evidence is portable

Canonical curation plans, state, relocation candidates, relocation
authorizations, witnesses, and step receipts SHALL live inside the governed
vault and SHALL use the same schema in Hosted and standalone deployments.
Machine-local retry state MAY accelerate delivery but MUST NOT be the sole
record required to inspect, resume, recover, or compensate a run after a process
restart or Hosted cell replacement. Content-witness recovery remains portable
from governed evidence alone. Placement recovery after replacement SHALL resume
only when the same retained vault filesystem epoch and namespace tokens remain
comparable. A changed or incomparable epoch SHALL still permit read-only status
but SHALL block placement recovery with `CURATION_RENAME_HISTORY_UNPROVABLE`;
portable evidence does not imply that rename history can be reconstructed on a
different filesystem.

#### Scenario: Hosted cell restarts between leaf and terminal receipt

- **WHEN** a cell restarts after a step's effect and witness committed but before
  its terminal receipt was written
- **THEN** read-only status exposes the exact recoverable outcome and the next
  resume reconstructs its terminal receipt from governed evidence without
  control-plane intervention or a second content effect

#### Scenario: Hosted replacement changes the placement filesystem epoch

- **WHEN** a cell is replaced while an authorized placement lacks its terminal
  witness and the retained namespace epoch or token is no longer comparable
- **THEN** status remains available from governed vault evidence
- **AND** resume blocks with `CURATION_RENAME_HISTORY_UNPROVABLE` rather than
  guessing, renaming, or relying on machine-local state
