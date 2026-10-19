# hosted-gateway-contract Specification

## Purpose

Define the private authenticated contract between a hosted control-plane gateway and a single-tenant Exomem cell.
## Requirements
### Requirement: Authenticated routing binds one principal to one tenant cell
The hosted gateway contract SHALL derive the destination tenant cell only from an authenticated control-plane principal and its authoritative account-to-cell mapping. A public caller MUST NOT select or override a tenant by request body, URL path, query parameter, cookie, or caller-supplied header, and the destination cell MUST verify that the trusted routing context matches its configured cell identity before executing any operation.

#### Scenario: Authenticated principal has one active cell
- **WHEN** an authenticated principal with one active account-to-cell mapping invokes an Exomem operation
- **THEN** the gateway forwards the operation only to that mapped private cell
- **AND** the cell verifies the trusted cell identity before executing the command

#### Scenario: Public request attempts a tenant override
- **WHEN** a caller supplies a tenant or cell selector in a body, path, query parameter, cookie, or untrusted header
- **THEN** the selector does not influence routing
- **AND** a selector that conflicts with the authenticated mapping is rejected before any cell operation executes

#### Scenario: Principal mapping is absent or ambiguous
- **WHEN** an authenticated principal has no active cell mapping or resolves to more than one active destination
- **THEN** routing fails closed with a stable machine-readable error
- **AND** the gateway does not fall back to a default, previously used, or neighboring tenant cell

### Requirement: Gateway-to-cell forwarding is private and authenticated
Every hosted gateway-to-cell request SHALL travel over a private authenticated channel and SHALL carry trusted routing context sufficient to bind the request to the configured cell and authenticated principal. The trusted context MUST be created by the hosted control plane, MUST NOT be accepted directly from a public caller, and MUST NOT contain payment-provider credentials or mutable product catalog data.

#### Scenario: Valid private forwarding request
- **WHEN** the gateway forwards a request with valid internal authentication and routing context matching the destination cell
- **THEN** the cell evaluates the request through its normal command or transfer boundary

#### Scenario: Cell receives an unauthenticated direct request
- **WHEN** a hosted cell receives a command or transfer request without valid gateway authentication
- **THEN** the cell rejects the request before resolving a vault path or invoking a command leaf

#### Scenario: Trusted context names another cell
- **WHEN** valid internal credentials accompany routing context whose cell identity differs from the destination cell's configured identity
- **THEN** the cell rejects the request as a routing-integrity failure
- **AND** no read, write, upload, download, or readiness detail from either cell is returned

### Requirement: Forwarded commands preserve the registry contract
The hosted gateway SHALL expose and forward Exomem commands from the shared product command registry rather than maintaining an independent command allowlist, schema, coercion layer, or implementation. Command names, input schemas, read/write metadata, result envelopes, and stable error codes MUST remain consistent with the cell's registry-derived MCP and REST surfaces.

#### Scenario: Registry command is forwarded
- **WHEN** a routed caller invokes a command present on the cell's product command registry
- **THEN** the gateway forwards the canonical command name and arguments to the mapped cell
- **AND** the cell's normal registry binding performs coercion, mutation gating, and leaf invocation

#### Scenario: Command is not on the exposed registry surface
- **WHEN** a caller requests a command that the mapped cell does not expose for that surface or tier
- **THEN** the gateway rejects the request without synthesizing or invoking an alternate command

#### Scenario: Cell returns a governed error
- **WHEN** the mapped cell returns a validation, authorization, stale-write, writer, or retry error
- **THEN** the gateway preserves its stable code and structured envelope without replacing it with tenant-specific business logic

### Requirement: Entitlement decisions remain control-plane inputs
Before forwarding a tier-controlled operation, the gateway SHALL require an authoritative entitlement decision for the authenticated account. The Exomem cell MUST consume only provider-neutral capability or resource limits and MUST NOT call Paddle or another billing provider during command execution.

#### Scenario: Account has the required capability
- **WHEN** the trusted control-plane context grants the capability and resource allowance required by an operation
- **THEN** the gateway may forward the request with those provider-neutral limits

#### Scenario: Account lacks the required capability
- **WHEN** an operation requires a capability absent from the authoritative entitlement decision
- **THEN** the gateway rejects it before forwarding with a stable entitlement error
- **AND** no Paddle, price, transaction, or subscription identifier is required by the cell

### Requirement: Idempotency is tenant and principal scoped end to end
The gateway SHALL preserve a caller-supplied idempotency key across forwarding and automatic retries. The effective idempotency namespace MUST include the configured tenant cell and authenticated principal so identical public keys cannot replay or collide across tenants or principals. Existing canonical-payload mismatch rejection and failed-mutation retry behavior SHALL remain authoritative at the cell mutation boundary.

#### Scenario: Gateway retries after a lost acknowledgement
- **WHEN** the gateway retries the same mutation for the same tenant, principal, command, canonical arguments, and idempotency key after losing the first acknowledgement
- **THEN** the cell returns the recorded successful result without executing the mutation leaf twice

#### Scenario: Same key is used by another tenant
- **WHEN** two tenants use the same public idempotency-key value for their own mutations
- **THEN** each tenant has an independent idempotency record and neither can receive or suppress the other's result

#### Scenario: Same scoped key is reused for different input
- **WHEN** one tenant and principal reuse an idempotency key with a different command or canonical payload
- **THEN** the cell rejects the request with `IDEMPOTENCY_KEY_REUSED`
- **AND** the previously recorded result is not disclosed for the mismatched request

#### Scenario: Forwarded mutation fails before completion
- **WHEN** a forwarded mutation fails without committing and is retried with the same scoped key
- **THEN** the retry remains executable rather than replaying a successful result that does not exist

### Requirement: Routing failures never cross tenant boundaries
When the mapped cell is unavailable, not ready, suspended, or inconsistent with trusted routing context, the gateway SHALL fail closed for that request. It MUST NOT retry against another tenant cell, reuse another tenant's warm process, or include another tenant's identifiers, paths, content, transfer metadata, or recorded idempotency result in the response.

#### Scenario: Mapped cell is unavailable
- **WHEN** the authenticated tenant's mapped cell cannot accept the request
- **THEN** the gateway returns a stable unavailable or not-ready error for that tenant
- **AND** no alternate cell is selected

#### Scenario: Concurrent requests target different tenants
- **WHEN** requests for two authenticated tenants are processed concurrently
- **THEN** each request retains its own routing, entitlement, idempotency, and transfer context through completion
- **AND** neither response contains data or replay state from the other tenant

### Requirement: Adoption Studio Is Admitted Generically, Not Intercepted

The hosted command route SHALL admit `adoption_studio` through its generic dispatch path, classifying read versus mutation solely by `commands_module.invocation_is_read_only(command, kwargs)` and routing to `lifecycle.admit_read()` or `admit_mutation()` accordingly. `adoption_studio` SHALL NOT be added to the hosted intercept set (which remains scoped to `transfer_artifact` and `adopt_vault`). Vault-relative path confinement (`resolve_under_vault`) plus the run state machine SHALL be the safety layer, so no hosted-route change is required for adoption command flow and the command's read/write behavior stays consistent with its cell MCP and REST surfaces.

#### Scenario: A mutating adoption action is admitted as a mutation

- **WHEN** the gateway forwards `adoption_studio` with a mutating action (for example `apply`) to a cell
- **THEN** the generic hosted route classifies it via `invocation_is_read_only` and admits it through `admit_mutation()`
- **AND** `adoption_studio` never enters the hosted intercept set

#### Scenario: A read-only adoption action is admitted as a read

- **WHEN** the gateway forwards `adoption_studio` with `status` or `work-item`
- **THEN** the generic hosted route admits it through `admit_read()` without acquiring the mutation boundary

### Requirement: Adoption Uploads Land In Vault-Relative Per-Run Staging

Hosted adoption intake SHALL land uploaded files and expanded archive entries as RAW files under the vault-relative per-run directory `_Staging/adoption/<run_id>/`, outside `Knowledge Base/`, so the engine scans them as legacy input rather than governed content. ZIP archives SHALL be expanded cell-side with zip-slip protection confining every extracted path under the staging directory, and with enforced entry-count and total-size caps. A subsequent `adoption_studio(action="start", path="_Staging/adoption/<run_id>")` SHALL scan the staged material through the same engine used for a local folder, and the intake SHALL return a poll-shaped result Home can consume without cell master credentials or private addresses.

#### Scenario: Staged upload is scannable by the same engine

- **WHEN** files are uploaded for a hosted adoption run and land under `_Staging/adoption/<run_id>/`
- **THEN** `adoption_studio(start, path="_Staging/adoption/<run_id>")` scans them identically to a local subtree
- **AND** the staged files are outside `Knowledge Base/` and no governed Source is created by the intake itself

#### Scenario: Malicious archive entries are rejected

- **WHEN** an uploaded ZIP contains a traversal entry or exceeds the entry-count or total-size cap
- **THEN** the traversal entry is rejected and the caps are enforced, with every accepted entry confined under `_Staging/adoption/<run_id>/`

### Requirement: Session Validation Outage Tolerance

The hosted gateway SHALL maintain a per-replica durable cache of successful
session validations and MAY use a cached validation only when the remote session
store is unavailable and the entry is within the configured stale grace window.
The cache SHALL identify entries by a digest of the token, SHALL encrypt the
validated claims at rest, and SHALL never persist raw tokens or negative
validation results. Token issuance, exchange, refresh, dynamic client
registration, and revocation SHALL continue to require the live store.

#### Scenario: Store outage with a recently validated session

- **WHEN** the remote session store is unavailable
- **AND** the same replica previously validated the session successfully
- **AND** the cached validation is within `EXOMEM_SESSION_STALE_GRACE_SECONDS`
- **THEN** the cached claims are served
- **AND** `stale_served_count` is incremented
- **AND** `/health/ready` reports `session_store.state` as `degraded`

#### Scenario: Store outage without a prior validation

- **WHEN** the remote session store is unavailable
- **AND** no eligible successful validation exists in the local cache
- **THEN** the session is refused with `temporarily_unavailable`

#### Scenario: Authoritative revocation clears stale eligibility

- **WHEN** the remote session store authoritatively reports a session invalid,
  revoked, or expired
- **THEN** the local cache entry is deleted
- **AND** the session is refused
- **AND** that entry is never served stale on a later store outage

#### Scenario: Stale grace kill switch

- **WHEN** `EXOMEM_SESSION_STALE_GRACE_SECONDS=0`
- **AND** the remote session store is unavailable
- **THEN** validation fails closed exactly as it did before this change
- **AND** no cached validation is served

#### Scenario: Session token confidentiality on disk

- **WHEN** a successful validation is persisted locally
- **THEN** its sqlite key is `sha256(token)` rather than the raw token
- **AND** its claims value is encrypted at rest
- **AND** the raw token does not appear in the cache file
