## ADDED Requirements

### Requirement: The Gateway Forwards The Service API With The MCP Proof

The hosted gateway SHALL forward the service API prefix and the service document path of a cell only to the cell that the authenticated principal maps to.
Each forwarded request SHALL carry the same origin proof and routing context as a request on the MCP path.
The gateway SHALL NOT add, remove or rename a service operation, and SHALL NOT rewrite a service result, refusal or document.

#### Scenario: A service call reaches the mapped cell

- **WHEN** an authenticated principal calls a service operation on the gateway
- **THEN** the gateway forwards the call to the principal's mapped cell with a valid origin proof
- **AND** the cell runs the operation's leaf only after it verifies the proof

#### Scenario: The document passes through unchanged

- **WHEN** an authenticated principal requests the service document path on the gateway
- **THEN** the gateway returns the bytes that the mapped cell serves, without change
- **AND** their SHA-256 digest equals the digest that the cell's discovery advertises
