## ADDED Requirements

### Requirement: Hosted admits exact reviewed curation without a bespoke executor

The current Hosted agent surface SHALL expose registry-derived
`maintain_memory(mode="curation")` and forward it to the same curation leaf used
by standalone MCP, REST, and CLI. The gateway and cell MUST NOT add a Hosted-only
plan format, command intercept, executor, persistence path, or semantic model.
All tenant mapping, authenticated principal, entitlement, path confinement,
writer authority, process-safe mutation boundary, terminal receipt, and
tenant-scoped idempotency checks SHALL run normally.

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

### Requirement: Remote maintenance refusal remains closed around curation

The request-bound remote maintenance gate SHALL admit write execution only for
`structured-files` and `curation`. `fix`, `reconcile`, `backfill-ids`, and every
future unknown write mode SHALL retain the operator-only refusal before manager
dispatch. Within curation, only an exact immutable reviewed plan may reach a
content leaf; the mode MUST NOT become a wrapper for ordinary maintenance or an
arbitrary command.

#### Scenario: Hosted reconcile remains refused

- **WHEN** a Hosted caller invokes write-mode reconcile after curation ships
- **THEN** it still receives `MAINTENANCE_REQUIRES_CLI` before manager dispatch

#### Scenario: Hosted curation omits its reviewed identity

- **WHEN** a Hosted caller invokes an apply-shaped curation action without the
  exact plan id, fingerprint, or required rationale
- **THEN** the cell refuses before any content leaf executes

### Requirement: Hosted and standalone curation evidence is portable

Canonical curation plans, state, witnesses, and step receipts SHALL live inside
the governed vault and SHALL use the same schema in Hosted and standalone
deployments. Machine-local retry state MAY accelerate delivery but MUST NOT be
the sole record required to inspect, resume, recover, or compensate a run after
a process restart or Hosted cell replacement.

#### Scenario: Hosted cell restarts between leaf and terminal receipt

- **WHEN** a cell restarts after a step's effect and witness committed but before
  its terminal receipt was written
- **THEN** read-only status exposes the exact recoverable outcome and the next
  resume reconstructs its terminal receipt from governed evidence without
  control-plane intervention or a second content effect
