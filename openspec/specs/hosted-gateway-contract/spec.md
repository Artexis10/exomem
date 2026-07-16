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

### Requirement: Transfer authority is tenant bound and narrowly scoped

Hosted browser upload/download authority SHALL implement the checked-in normative `contracts/hosted-transfer-v2.json` capability, request, response, error, and route schema on exactly `PUT|OPTIONS /public/exomem/v2/transfers/upload` and `GET|OPTIONS /public/exomem/v2/transfers/download` at the configured transfer hostname. OPTIONS SHALL be bodyless, unauthenticated, and non-consuming. A canonical grant SHALL bind its schema/audience, signing credential version, exact HTTPS browser origin, operation/method, immutable cell/principal, normalized target, byte limit, issue/not-before/expiry, and UUIDv4 JTI. The verifier SHALL enforce the exact clock equations in the normative artifact, including strict `now < exp`; a transfer admitted/consumed while unexpired may finish without mid-stream expiry recheck. The cell SHALL validate all request-level invariants, acquire lifecycle transfer admission, and then atomically persist JTI consumption in the cell security authority before reading upload bytes or opening/statting a download. Consumption SHALL survive restart, and JTI cleanup MUST NOT resurrect an expired grant. The capability MUST NOT expose/require the long-lived service bearer or private cell address, authenticate another route, change target/operation, or cross tenant boundaries.

An upload target SHALL be the SHA-256 of canonical JSON containing exact filename, content type, optional scope/category/description, signed size, and file SHA-256; direct uploads SHALL be raw bodies and SHALL NOT accept extracted text or request-selected metadata. A download target SHALL be one normalized vault-relative file path. The entire upload body MUST fit both the grant and 90 MiB hosted ceiling; download size MUST fit the explicit grant up to the cell storage entitlement and is not subject to the upload ceiling. Grant headers SHALL be bounded before decoding. `Content-Length` SHALL be optional: when present it MUST exactly equal signed size before consumption; absent/chunked streaming is allowed, and any streamed mismatch/overflow after consumption burns the JTI.

#### Scenario: Tenant performs an authorized direct upload

- **WHEN** the configured origin sends raw `PUT` with an unexpired grant whose metadata digest, content type, signed/optional-declared/streamed size, and file SHA-256 match a body no larger than 94371840 bytes
- **THEN** lifecycle admission precedes durable JTI consumption, body streaming uses bounded private temp, and final canonical publication alone enters the shared vault mutation boundary
- **AND** caller fields cannot change metadata, target, principal, tenant, operation, content allowance, or hash encoded by the grant

#### Scenario: Tenant performs an authorized direct download

- **WHEN** the configured origin sends `GET` with an unexpired grant for one normalized file and the file fits the grant allowance
- **THEN** lifecycle admission precedes JTI consumption, and only then may the cell resolve/open/stream the exact grant-bound file until iterator close
- **AND** query/path selectors, range requests, traversal, alternate paths, or resolution outside the mapped vault are rejected

#### Scenario: Pre-consumption validation fails

- **WHEN** host/method/origin/query/auth/cookie, grant size/encoding/signature/claim/time, signed metadata/limit, content type, present Content-Length, framing, or lifecycle admission is invalid
- **THEN** the request is rejected before JTI consumption and before body read/file existence resolution
- **AND** no response discloses whether a target file exists

#### Scenario: Consumed transfer aborts or fails

- **WHEN** missing download, upload/download disconnect, cancellation, absent-length/chunked overflow, metadata/hash/streamed-size mismatch, temp failure, or later commit/open error occurs after consumption
- **THEN** lifecycle admission is released in a finally-equivalent path, upload temp is removed, and no partial canonical bytes are published
- **AND** the JTI remains burned across current process/restart and a retry requires a fresh grant

#### Scenario: Transfer grant binding is altered or replayed

- **WHEN** a grant is reused, expired, oversized, signed by a finalized old credential, presented from another origin/host, used for another route/method/operation/path/cell/principal, or paired with mismatched metadata/body
- **THEN** transfer is rejected without reading/writing canonical bytes or disclosing file existence

#### Scenario: Grant crosses a clock boundary

- **WHEN** `nbf` is at the allowed 30-second future-skew boundary and strict `now < exp` still holds
- **THEN** the grant may be admitted, consumed, and completed even if `exp` passes during streaming
- **AND WHEN** `nbf` is farther ahead, `exp <= now`, or an expired grant is replayed after JTI cleanup
- **THEN** it is rejected before consumption with no expiration grace

#### Scenario: Concurrent requests consume the same grant

- **WHEN** two valid requests race the same JTI
- **THEN** the process-safe security authority admits exactly one consumption
- **AND** only that request can proceed to body read or file open

#### Scenario: Browser performs CORS preflight

- **WHEN** the configured HTTPS origin preflights exact `PUT`/`GET` and the documented grant/content-type headers
- **THEN** the cell echoes only that origin, no credentials/wildcard, exact methods/headers, `Vary: Origin`, and max-age no greater than 300 seconds
- **AND** null/hostile/malformed origin or overbroad method/header receives no CORS authority and does not consume a grant

#### Scenario: Browser receives an actual transfer response

- **WHEN** a syntactically valid configured origin receives any v2 success, error, or streaming response
- **THEN** the response includes exact-origin CORS, `Vary: Origin`, and `Cache-Control: private, no-store`
- **AND** download exposes only bounded content type/length/disposition headers while errors remain existence-neutral

#### Scenario: Upload commits successfully

- **WHEN** a consumed upload passes streamed size/hash verification and governed canonical commit
- **THEN** the cell returns the normative `201 application/json` success envelope with only operation, byte count, SHA-256, and `committed=true`
- **AND** it returns no governed path, filename, user metadata, grant, or JTI

#### Scenario: Download opens successfully

- **WHEN** a consumed download resolves the exact safe file and size
- **THEN** the cell returns `200` raw bytes with exact length, `application/octet-stream`, no-store policy, and the normative bounded RFC 8187 attachment disposition
- **AND** it emits no JSON success envelope or vault path

#### Scenario: Transfer returns a modeled error

- **WHEN** a pre- or post-consumption transfer failure occurs
- **THEN** status, stable code/message, retryability, and `requires_new_grant` match the normative artifact and the envelope contains no target/user/grant/JTI content
- **AND** pre-consumption busy/unavailable preserves the JTI, while every post-consumption error requires a fresh grant

#### Scenario: Private gateway transfer compatibility is exercised during rollout

- **WHEN** a coordinated rollout supplies an immutable RFC3339 compatibility deadline no later than seven days after the signed image build time and the current time is before it
- **THEN** the cell may accept version 1 only on private service-authenticated routes, with one active upload and a 4 MiB body cap under general runtime-temp accounting
- **AND** absent, expired, malformed, or overlong compatibility configuration disables both v1 routes, while public routes accept only v2 and reject service bearer/cookies

#### Scenario: Browser performs preflight on an exact public path

- **WHEN** the configured origin sends bodyless OPTIONS to the upload or download path
- **THEN** the route returns only its exact method/header CORS policy without requiring or consuming a grant
- **AND** OPTIONS on any other path remains private or absent

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

