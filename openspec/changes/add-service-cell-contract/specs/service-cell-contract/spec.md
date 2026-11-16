## Purpose

The service cell contract defines how an outside service, such as a product or an enterprise system, uses an Exomem cell as its governed memory layer.
It fixes the versioned service API, its published document, its refusal codes and status classes, its change rules and its performance budgets.

## ADDED Requirements

### Requirement: The Service API Is Served Only For Enabled Versions

A Cloud cell SHALL serve `POST /service/<version>/<operation>` only for a version that the host setting `EXOMEM_SERVICE_API` lists.
A version SHALL be named by the month in which it is cut, in the form `YYYY-MM`.
When the setting is unset or empty, the cell SHALL serve no service route and SHALL behave as it does without this capability.
The service API SHALL use the cell's existing listener. Every operation SHALL accept POST only.

#### Scenario: The host has not enabled the service API

- **WHEN** a client sends a request to a service path on a cell without the host setting
- **THEN** the cell answers as for any unknown route
- **AND** no command leaf runs and discovery advertises no service version

#### Scenario: The host enables the first version

- **WHEN** the host setting lists the first version
- **THEN** the cell serves each operation of that version that its composed server implements
- **AND** it serves no operation of an unlisted version

### Requirement: Each Service Operation Renders One Registered Command

Each service operation SHALL name one command in the command registry and SHALL call that command's leaf.
An operation MAY fix or narrow the command's arguments. It SHALL NOT add behaviour, state or side effects that the leaf does not have.
The leaf result SHALL reach the client as data only, without the agent outcome sentence or agent guidance.
The personal REST facade's paths, replies, statuses and document SHALL stay unchanged when the cell serves the service API.

#### Scenario: Both doors return the same data

- **WHEN** a conformance run calls one operation through the service API and its command through the MCP door, with equal arguments on equal cell state
- **THEN** the service result holds the same data as the leaf result of the MCP call, after per-call identifiers and timestamps are removed from both

#### Scenario: The personal facade keeps its replies

- **WHEN** a client calls the personal REST facade on a cell that serves the service API
- **THEN** each path returns the reply and status that it returns on a cell without the service API
- **AND** the facade's document is the same on both cells

### Requirement: One Fixed Document Describes Each Version

For each version that a release serves, the release SHALL hold one OpenAPI 3.1 document, generated from the command registry and the operations' request and result models.
Every cell of one release SHALL serve the same bytes for one version at `GET /.well-known/exomem-cell/service-api/<version>.json`, without a credential.
The repository SHALL hold a committed copy of each document, and a test SHALL keep it equal to the generated document.
Each result schema SHALL allow unlisted fields and SHALL mark every listed field required, with an explicit `null` for an absent value.
Each request schema SHALL refuse unlisted fields.
An error `code` and every result-state field SHALL have type `string`. The document SHALL list their known values in the description and in the `x-exomem-known-values` extension.
Each operation SHALL state in the document whether a repeat of the same request is safe and which request field makes it safe.

#### Scenario: A request carries an unknown field

- **WHEN** a client sends a request body with a field that the operation's request schema does not define
- **THEN** the cell refuses the request with status 400 and code `INVALID_REQUEST`, and names the field
- **AND** the leaf does not run

#### Scenario: The registry changes a rendered command

- **WHEN** a change alters the arguments or result of a command that an operation renders
- **THEN** the generated document differs from the committed copy
- **AND** the document test fails until the committed copy is regenerated in the same change

#### Scenario: A cell does not serve an operation of the document

- **WHEN** a client calls an operation that the document defines but the cell's composed server lacks
- **THEN** the cell refuses it with status 404 and code `OPERATION_UNAVAILABLE`
- **AND** discovery does not list that operation for the cell

### Requirement: Refusals Carry Codes In Fixed Status Classes

Every refusal SHALL carry one error code. A refusal from a leaf SHALL carry the code that the leaf raises through the MCP door.
The HTTP status SHALL come from the code-to-status map that every HTTP door of the cell shares.
The service API SHALL read that map through a service overlay that adds the 401, 403 and 500 classes.
The overlay SHALL NOT change a status that the personal REST facade, edge ingress or a transfer route returns.
The service classes SHALL be: 400 invalid request, 401 credential or proof refused, 403 authority refused, 404 resource not found or operation unavailable, 409 conflict or stale precondition, 500 unexpected failure with unknown outcome, and 503 busy or unavailable.
A refusal that the service renderer makes before the leaf SHALL carry one of these codes: `CLOUD_PROOF_REQUIRED`, `UNAUTHORIZED`, `OPERATION_UNAVAILABLE`, `INVALID_REQUEST`, or the leaf's existing busy code.
A refusal from the shared request boundary SHALL carry the code that the boundary raises on every door. Examples are a read-only cell, a quiescing cell and a held writer lease.
A leaf code that the operation does not declare SHALL reach the client with its class status, and the cell SHALL count it by operation.
A 503 refusal SHALL carry a retry hint in its body and in the `Retry-After` header. The cell SHALL NOT queue a request that it refuses as busy.
A pending or uncertain outcome that the leaf reports as a result state SHALL remain a successful result with that state.

#### Scenario: The cell is busy

- **WHEN** the cell refuses a request because its model or mutation capacity is busy
- **THEN** the response has status 503, the leaf's busy code and a `Retry-After` header
- **AND** the request is not queued

#### Scenario: An authority refusal keeps its personal status

- **WHEN** a credential without authority for a command calls it through the personal REST facade and through the service API
- **THEN** the personal REST facade answers with the status that it returned before the service API shipped
- **AND** the service API answers with status 403 and the same code

#### Scenario: A settlement is still pending

- **WHEN** a capture settlement reports a pending outcome
- **THEN** the response has status 200 and the pending state in its result
- **AND** the client reads the outcome from the result, not from an error

#### Scenario: A leaf raises an undeclared code

- **WHEN** a leaf raises a code that its operation does not declare
- **THEN** the client receives that code with the status of its class
- **AND** the cell counts the code for that operation

### Requirement: A Service Version Changes Only By Addition

Within one version, a release MAY add operations, optional request fields, result fields, error codes and result-state values.
A release SHALL NOT remove or rename an operation or field, change a type or meaning, add a required request field or refuse a value that the version accepted.
Such a change SHALL ship as a new version. The cell SHALL serve the earlier version until its retirement date, which discovery advertises.
CI SHALL compare each generated document with its committed copy from the base branch with `oasdiff breaking` and SHALL fail at its `ERR` level.

#### Scenario: A change removes a result field

- **WHEN** a change removes a field from a result schema of a served version
- **THEN** the CI compatibility check fails

#### Scenario: A change adds an error code

- **WHEN** a release adds an error code to an operation
- **THEN** the CI compatibility check passes
- **AND** a client generated from the earlier document receives the new code as a string without failing

### Requirement: Service Requests Pass The Full MCP Boundary

A service request SHALL pass the same bearer verification, gateway proof check, containment verdict and trusted-routing checks as an MCP request on the same cell.
It SHALL resolve the same principal for the same verified credential and proof. The leaf SHALL apply the same authorization as for an MCP call by that principal.
Only the verified credential and the verified gateway proof SHALL determine the principal, audience, tenant and cell of a request.
No path segment, query parameter, body field or unverified header SHALL select or change them, or a partition.
Filters, tags and references in a request SHALL only narrow the result that the principal's authority allows.

#### Scenario: A valid bearer arrives without a gateway proof under enforcement

- **WHEN** the cell enforces gateway proofs and a service request carries a valid cell bearer but no valid gateway proof
- **THEN** the cell refuses it with status 401 and code `CLOUD_PROOF_REQUIRED`, exactly as it refuses that request on `/mcp`
- **AND** no leaf runs

#### Scenario: The origin session was revoked

- **WHEN** a service request arrives after the gateway revoked the login session of its origin
- **THEN** the gateway signs no proof for it, and the cell refuses any earlier proof for that session once the proof's lifetime ends, exactly as on `/mcp`

#### Scenario: A credential belongs to a different cell

- **WHEN** a credential issued for another cell reaches a service operation on this cell
- **THEN** the cell refuses the request with status 401 before the leaf handles content

#### Scenario: A principal lacks content authority

- **WHEN** a principal without the authority that an operation's leaf requires calls that operation
- **THEN** the cell refuses it with status 403 and the leaf's own authority code
- **AND** the response contains no content or count from the cell

#### Scenario: A request body names a different principal

- **WHEN** a request body carries a field that names an audience, tenant or principal
- **THEN** the cell refuses the request as an unknown field
- **AND** the principal of the request stays the one that the verified credential and proof resolve

### Requirement: Discovery Advertises Each Served Service Version

When the service API is enabled, cell discovery SHALL list each served version with these fields:

- its document path;
- the SHA-256 digest of the document bytes that the cell serves;
- the operations that the cell serves;
- the version's retirement date, when one is set.
When the service API is disabled, discovery SHALL list no service version.

#### Scenario: A client checks the contract before it calls

- **WHEN** a client reads discovery and then a served version's document
- **THEN** the SHA-256 digest of the document bytes equals the digest that discovery advertises
- **AND** every cell of the same release advertises the same digest for that version

### Requirement: The Service Door Meets The Passage And Capture Budgets

Passage search through the service API SHALL meet the passage-search latency bars of the passage-evidence capability, measured at the service door on the same reference corpus and hardware.
Capture through the service API SHALL have a p95 no higher than capture through the MCP door in the same run. Reservation and settlement SHALL have a p95 below 1 s on that reference.
Before a host enables the service API on a tenant's cell, one run SHALL have measured both doors for the release that the cell runs.

#### Scenario: The service door is measured

- **WHEN** the latency run measures passage search, capture, reservation and settlement on the reference corpus through both doors
- **THEN** the service door meets each bar, and its capture p95 is no higher than the MCP door's
- **AND** the run records both doors' results side by side
