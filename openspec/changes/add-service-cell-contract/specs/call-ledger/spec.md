## RENAMED Requirements

- FROM: `### Requirement: Every MCP Call Produces Exactly One Ledger Row`
- TO: `### Requirement: Every MCP Or Service Call Produces Exactly One Ledger Row`

## MODIFIED Requirements

### Requirement: Every MCP Or Service Call Produces Exactly One Ledger Row

The system SHALL append exactly one ledger row for each completed MCP tool call — read or
write, success or abstention or refusal or error — recorded at the single point where both the call's
duration and its outcome are known. A call SHALL NOT produce zero rows and SHALL NOT produce
more than one row.

The same rule SHALL apply to each service API call that passes authentication
(`service-cell-contract`), including a call that the cell refuses before the leaf runs. A service
call to an operation that the cell does not serve SHALL record a null operation and its refusal
code. It SHALL NOT record the request path or any caller-supplied operation name.

#### Scenario: Successful write appends one row

- **WHEN** an MCP write tool call completes normally
- **THEN** exactly one ledger row is appended for that call
- **AND** its `outcome` is `ok`
- **AND** its `request_id` matches the correlation id used by the call tracer

#### Scenario: Read call is recorded too

- **WHEN** an MCP read tool call completes normally without activation abstention
- **THEN** exactly one ledger row is appended with `outcome` of `ok`
- **AND** the row is present even though no mutation and no writer lease were involved

#### Scenario: One call is never double-counted

- **WHEN** a single tool call passes through both the middleware seam and the wrapper seam
- **THEN** only one row is emitted for that call, not one per seam

#### Scenario: A call rejected before the leaf is still recorded

- **WHEN** a call is rejected by a pre-check that runs ahead of the tool leaf — the binary-blob
  content guard, or `edit_memory` operation normalization
- **THEN** exactly one ledger row is appended for that call
- **AND** its `outcome` is `refused` carrying the rejecting check's own code
- **AND** the row records the arguments the client actually sent, before any translation

#### Scenario: A service passage search is recorded once

- **WHEN** a service runs a passage search through the service API
- **THEN** exactly one ledger row is appended for that call
- **AND** the row holds the query argument's name, length and hash, and not the query text
- **AND** the response carries the row's `request_id`

#### Scenario: A refused service call is recorded once

- **WHEN** the cell refuses an authenticated service request because the body has an unknown field
- **THEN** exactly one ledger row is appended with `outcome` of `refused` and code `INVALID_REQUEST`

#### Scenario: An unknown operation leaves no caller text

- **WHEN** an authenticated service request names an operation that the cell does not serve
- **THEN** exactly one ledger row is appended with a null operation and code `OPERATION_UNAVAILABLE`
- **AND** no part of the request path appears in the row

### Requirement: Every Row Names The Calling Client

The system SHALL record, on every row, which MCP client made the call: the client name and
version from the initialize handshake's `clientInfo`, the transport, and the session id. These
SHALL be recorded in the clear, not hashed — they identify software, not a person, and hashing
them would destroy the only field that answers "which client?" while protecting nothing. Absence
of an MCP context SHALL yield null fields rather than an error.

A service API row SHALL record the transport as the service API, with the service version and the
operation. Its `clientInfo` and session fields SHALL be null, and its caller SHALL be identified by
the hashed principal scope, as on every row.

#### Scenario: A row identifies which client called

- **WHEN** a call arrives from a client whose handshake declared a name and version
- **THEN** the ledger row records that client name and version in the clear
- **AND** it records the session id, so one client's calls can be followed as a sequence

#### Scenario: Two clients against one vault are distinguishable

- **WHEN** two different MCP clients call the same vault
- **THEN** their rows carry different `client_name` values
- **AND** the ledger can be filtered to one client's calls without consulting any other source

#### Scenario: No MCP context degrades to null, never to a failure

- **WHEN** a call is recorded outside an MCP session — a CLI invocation or a background drain
- **THEN** the client identity fields are null
- **AND** the row is still appended and the call still succeeds

#### Scenario: A service row names its door

- **WHEN** a service calls an operation through the service API
- **THEN** the row records the service transport, the version and the operation
- **AND** its client name, client version and session id are null
