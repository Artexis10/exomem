## ADDED Requirements

### Requirement: Remote maintenance writes run as governed jobs

On a request-bound MCP or REST surface, the system SHALL admit a write-mode
`fix`, `backfill-ids` or `reconcile` invocation only when it carries the
`plan_id` of a preview with the same mode and flags, and a one-line `why`. An
admitted invocation SHALL start a maintenance job and return its handle
without waiting for the work. The request SHALL NOT hold the vault mutation
boundary while the job runs.

#### Scenario: Remote client starts a reviewed reconcile
- **WHEN** a remote caller previews `reconcile` with `dry_run=true` and then requests write-mode `reconcile` with the returned `plan_id` and a `why`
- **THEN** the system returns a job handle with a `run_id` and state `running`
- **AND** the call returns before the job finishes

#### Scenario: Remote write omits its reviewed plan
- **WHEN** a remote caller requests `fix` or `backfill-ids` with `dry_run=false`, or write-mode `reconcile`, without a `plan_id` or a `why`
- **THEN** the system returns `MAINTENANCE_PLAN_REQUIRED` as not committed
- **AND** it creates no job and acquires no vault mutation boundary

### Requirement: Remote adopt-local applies through its sealed preview

On a request-bound surface, the system SHALL admit
`collections-store-adopt-local` with `apply=true` as an ordinary write that
the existing preview seal (`plan_id`, `why` and `acknowledge_skipped`) binds.

#### Scenario: Remote owner adopts the local collection store
- **WHEN** a remote caller applies `collections-store-adopt-local` with the `plan_id` from its preview and a `why` while the service runs
- **THEN** the system applies the adoption under the preview seal instead of returning `MAINTENANCE_REQUIRES_CLI`

### Requirement: Unadmitted remote maintenance writes fail before vault admission

The system SHALL reject a request-bound remote write of a maintenance mode that
this capability does not admit before it acquires writer authority,
idempotency state or the vault mutation boundary. The refusal SHALL use the
stable code `MAINTENANCE_REQUIRES_CLI`, SHALL identify the invocation as not
committed and SHALL name the host procedure. A request-bound write through a
legacy leaf alias of maintenance (`audit_fix` or `reconcile`) SHALL fail the
same way with `MAINTENANCE_PLAN_REQUIRED` and SHALL name the job route.

#### Scenario: Remote export apply is refused
- **WHEN** a remote caller applies `collections-store-export`
- **THEN** the system returns `MAINTENANCE_REQUIRES_CLI` before writer-manager dispatch
- **AND** the remediation says that export runs on the host with the service stopped

#### Scenario: Legacy alias write is refused
- **WHEN** a local MCP caller with legacy aliases enabled requests a write through the `reconcile` or `audit_fix` alias
- **THEN** the system returns `MAINTENANCE_PLAN_REQUIRED` before writer-manager dispatch
- **AND** no vault mutation boundary is acquired

### Requirement: Tool guidance teaches the remote job route

The generated `maintain_memory` contract and the installed generic Exomem
guidance SHALL tell agents how to preview, start, follow, cancel and read a
maintenance job remotely. They SHALL name `collections-store-export` apply as
host work that runs with the service stopped.

#### Scenario: Agent plans a remote repair
- **WHEN** an agent reads the generated tool description or the installed maintenance guidance
- **THEN** it can preview a repair, start it with the `plan_id`, follow it to a terminal state and read its receipt without first attempting a write

## MODIFIED Requirements

### Requirement: Remote inspection and local maintenance remain available

The system SHALL continue to admit read-only maintenance audit and explicit dry
runs on remote surfaces. It SHALL continue to run write-mode maintenance
synchronously from the local CLI and from direct trusted Python integrations
that have no request-bound remote surface descriptor. Those callers SHALL NOT
need a `plan_id`.

#### Scenario: Remote maintenance inspection remains callable
- **WHEN** an MCP or REST caller requests audit or an explicitly read-only maintenance preview
- **THEN** the canonical maintenance leaf executes through the ordinary read-only admission path

#### Scenario: Local operator repair remains callable
- **WHEN** the local CLI requests a write-mode maintenance operation without a `plan_id`
- **THEN** the canonical maintenance leaf runs synchronously through ordinary writer and mutation admission

## REMOVED Requirements

### Requirement: Remote maintenance writes fail before vault admission

**Reason**: Remote `fix`, `backfill-ids` and `reconcile` writes now run as governed background jobs, which remove the unbounded request-bound hold that the refusal prevented.

**Migration**: Replaced by "Remote maintenance writes run as governed jobs" and "Unadmitted remote maintenance writes fail before vault admission".

### Requirement: Tool guidance prevents unsafe remote routing

**Reason**: Remote repair is no longer limited to audit and dry runs.

**Migration**: Replaced by "Tool guidance teaches the remote job route".
