## ADDED Requirements

### Requirement: The Service API Is Served Only For Enabled Versions

A Cloud cell SHALL serve `POST /service/<version>/<operation>` only for a version that the host setting `EXOMEM_SERVICE_API` lists.
When the setting is unset or empty, the cell SHALL serve no service route and SHALL behave as it does without this capability.
The service API SHALL use the cell's existing listener and SHALL NOT accept a request method other than POST.

#### Scenario: The host has not enabled the service API

- **WHEN** a client sends a request to a service path on a cell without the host setting
- **THEN** the cell answers as for any unknown route
- **AND** no command leaf runs and discovery advertises no service version

#### Scenario: The host enables version 1

- **WHEN** the host setting lists version 1
- **THEN** the cell serves each version 1 operation that its composed server implements
- **AND** it serves no operation of an unlisted version

### Requirement: Each Service Operation Renders One Registered Command

Each service operation SHALL name one command in the command registry and SHALL call that command's leaf through the same boundary as the MCP door.
An operation MAY fix or narrow the command's arguments. It SHALL NOT add behaviour, state or side effects that the leaf does not have.
The leaf result SHALL reach the client as data only, without the agent outcome sentence or agent guidance.
An operation SHALL be available only when its command is implemented in the composed server.

#### Scenario: Both doors return the same data

- **WHEN** a conformance run calls one operation through the service API and its command through the MCP door, with equal arguments on equal cell state
- **THEN** the service result holds the same data as the leaf result of the MCP call

#### Scenario: A command is absent from the composed server

- **WHEN** the composed server lacks the command of an operation, such as captured-Source erasure
- **THEN** the service API refuses that operation as an unknown route
- **AND** discovery omits it

### Requirement: Service Requests And Results Follow The Published Document

For each served version, the cell SHALL publish one OpenAPI 3.1 document, generated from the command registry and the operations' request and result models.
Each request model SHALL refuse an unknown field, a missing required field and a value outside its schema, before the leaf runs. The refusal SHALL name the field.
Every successful response SHALL validate against the operation's result schema in that document.
The repository SHALL hold a committed copy of each document, and a test SHALL keep it equal to the generated document.
Each operation SHALL state in the document whether a repeat of the same request is safe and which request field makes it safe.

#### Scenario: A request carries an unknown field

- **WHEN** a client sends a request body with a field that the operation's request schema does not define
- **THEN** the cell refuses the request with status 400 and names the field
- **AND** the leaf does not run

#### Scenario: The registry changes a rendered command

- **WHEN** a change alters the arguments or result of a command that an operation renders
- **THEN** the generated document differs from the committed copy
- **AND** the document test fails until the committed copy is regenerated in the same change

### Requirement: Refusals Carry Closed Codes In Fixed Status Classes

Every refusal SHALL carry one error code that the document lists for that operation. The code SHALL be the code that the leaf raises through the MCP door.
The HTTP status SHALL come from the code's class: 400 invalid request, 401 credential refused, 403 authority refused, 404 unknown route or version, 409 conflict or stale precondition, 500 unexpected failure with unknown outcome, and 503 busy or unavailable.
A 503 refusal SHALL carry a retry hint in its body and in the `Retry-After` header. The cell SHALL NOT queue a request that it refuses as busy.
A pending or uncertain outcome that the leaf reports as a result state SHALL remain a successful result with that state.

#### Scenario: The cell is busy

- **WHEN** the cell refuses a request because its model or mutation capacity is busy
- **THEN** the response has status 503, the leaf's busy code and a `Retry-After` header
- **AND** the request is not queued

#### Scenario: A settlement is still pending

- **WHEN** a capture settlement reports a pending outcome
- **THEN** the response has status 200 and the pending state in its result
- **AND** the client reads the outcome from the result, not from an error

### Requirement: A Service Version Changes Only By Addition

Within one version, a release MAY add operations, optional request fields, response fields and error codes within the fixed status classes.
A release SHALL NOT remove or rename an operation or field, change a type or meaning, add a required request field or refuse a value that the version accepted.
Such a change SHALL ship as a new version. The cell SHALL serve the earlier version until its retirement date, which discovery advertises.
CI SHALL compare each generated document with its committed copy from the base branch and SHALL fail on a breaking difference within one version.

#### Scenario: A change removes a response field

- **WHEN** a change removes a field from a version 1 result schema
- **THEN** the CI compatibility check fails

#### Scenario: A change adds an optional request field

- **WHEN** a change adds an optional field to a version 1 request schema
- **THEN** the CI compatibility check passes
- **AND** a client generated from the earlier document keeps working without the field

### Requirement: The Service API Grants No Authority Beyond The Credential

A service request SHALL pass the same authentication and trusted-routing checks as an MCP request on the same cell. It SHALL resolve the same principal for the same credential.
The leaf SHALL apply the same authorization as for an MCP call by that principal.
No path segment, header, query parameter or body field SHALL select or change the principal, audience, tenant, cell or partition of a request.
Filters, tags and references in a request SHALL only narrow the result that the principal's authority allows.

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
- **AND** the principal of the request stays the one that the credential resolves

### Requirement: Every Service Call Writes One Content-Free Ledger Row

Each service call that passes authentication SHALL append exactly one call-ledger row, including a call that the cell refuses before the leaf runs.
The row SHALL follow the call ledger's rules for arguments, caller identity, outcome, latency and chain continuity.
The row SHALL record the transport as the service API, with the version and the operation. It SHALL leave the MCP client handshake fields null.
The response SHALL carry the row's request id.

#### Scenario: A passage search through the service API

- **WHEN** a service runs a passage search through the service API
- **THEN** the ledger holds exactly one row for that call, with the service transport, the version and the operation
- **AND** the row holds the query argument's name, length and hash, and not the query text

#### Scenario: A refused service call

- **WHEN** the cell refuses a service request for an unknown field
- **THEN** the ledger holds exactly one row for that call with outcome `refused` and the refusal code

### Requirement: Discovery Advertises Each Served Service Version

When the service API is enabled, cell discovery SHALL list each served version with its document path, the SHA-256 digest of its document and, when set, its retirement date.
The document path SHALL be readable without a credential and SHALL contain no tenant content, credential or principal identifier.
When the service API is disabled, discovery SHALL list no service version.

#### Scenario: A client checks the contract before it calls

- **WHEN** a client reads discovery and then the version 1 document
- **THEN** the SHA-256 digest of the document equals the digest that discovery advertises

### Requirement: Service Passage Search Meets The Passage Latency Bars

Passage search through the service API SHALL meet the passage-search latency bars of the passage-evidence capability, measured at the service boundary on the same reference corpus and hardware.
A release that enables the service API SHALL record one measurement of both doors in the same run.

#### Scenario: The service door is measured

- **WHEN** the latency run measures passage search on the reference corpus through both doors
- **THEN** the service door meets the one-caller and four-caller p95 bars
- **AND** the run records both doors' results side by side
