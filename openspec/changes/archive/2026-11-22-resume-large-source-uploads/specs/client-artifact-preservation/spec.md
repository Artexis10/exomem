## MODIFIED Requirements

### Requirement: The upload handoff advertises resumable sessions

On a server that serves upload sessions, `transfer_artifact(operation="upload")` for the Evidence lane SHALL return a `resumable` object beside `upload_url`. The object SHALL name the session creation URL, the protocol and its version, the header that a client resends on every later request, the most bytes that one request may carry, the most bytes that one session may hold, the `Upload-Metadata` keys that creation requires, and a link to a documented shell recipe. It SHALL NOT carry the recipe itself. The token that the handoff mints SHALL create a session through the same credential check and lane binding that `/upload` applies. Session creation SHALL refuse a request that lacks an advertised key with `INVALID_UPLOAD`, name every missing key, and open no session. A Sources-lane bearer handoff SHALL advertise sessions when the same listener serves Source upload sessions.

#### Scenario: An agent with a shell uploads a file larger than the proxy edge cap
- **WHEN** an agent calls `transfer_artifact(operation="upload")`, creates a session at the advertised URL with the minted token, sends the file in parts and loses the connection once
- **THEN** it continues from the offset that the server reports
- **AND** the receipt names the SHA-256 of the whole file

#### Scenario: Creation lacks a required metadata key
- **WHEN** a client creates a session without one of the advertised `Upload-Metadata` keys
- **THEN** creation is refused with `INVALID_UPLOAD` that names every missing key, and no session opens

#### Scenario: A Sources-lane handoff
- **WHEN** an agent mints a bearer upload capability for the Sources lane on a listener that serves sessions
- **THEN** the handoff advertises the Source session metadata and the resulting session preserves a Source

### Requirement: A digest holds an upload

`upload_files` with the lane `source` or `evidence` and `expected_sha256` SHALL imply a held upload, as the lane `import` does. `transfer_artifact` with operation `upload` and `expected_sha256` and no `artifact` SHALL do the same. A call without `expected_sha256` and without `hold` SHALL keep returning the bearer-token handoff. The reply SHALL be the held handoff that `hold=true` returns, with `committed=false`, except when a Source request selects the resumable handoff.

#### Scenario: A digest without hold

- **WHEN** an agent calls `upload_files` with the lane `source`, an `expected_sha256` and a request within the one-shot limit, and omits `hold`
- **THEN** the reply is a held handoff for that lane with `hold=true` and `handoff_status=handoff_prepared`, and not an `INVALID_MODE` refusal

## ADDED Requirements

### Requirement: A personal Source handoff can carry a large original

When a Source request to `upload_files` specifies `size_bytes` above the one-shot limit on a verified personal HTTP listener, the tool SHALL offer a tus resumable handoff up to the configured session maximum. The descriptor SHALL name the endpoint, required metadata, limits and next action. The prepared response SHALL report `committed=false`.

#### Scenario: The client requests a Source larger than the one-shot limit

- **WHEN** the owner requests a Source above the one-shot limit and supplies its expected SHA-256
- **THEN** the tool returns a resumable handoff with the exact declared size and the session limits
- **AND** the handoff accepts chunks no larger than the listener's request limit

#### Scenario: The requested maximum exceeds the session ceiling

- **WHEN** the owner declares a Source larger than the listener's upload session maximum
- **THEN** the tool reports that ceiling without opening a session

#### Scenario: The caller requires a hold

- **WHEN** a Source request specifies `hold=true` or an import identity and exceeds the one-shot limit
- **THEN** the tool reports that limit without issuing an upload that would publish directly

#### Scenario: The listener does not serve resumable Source handoffs

- **WHEN** an owner requests a large Source on an ingress that offers only the multipart handoff
- **THEN** the tool reports that ingress's size limit without accepting an upload

### Requirement: A resumable Source preserves the upload's issuing authority

A resumable Source handoff SHALL bind the expected digest, byte limit, owner, issuing MCP login and deployment. One ticket SHALL open at most one session. The session SHALL accept only the bound digest and permitted Source metadata. The service SHALL recheck the current owner binding and issuing login before session operations and at canonical publication. Refreshing an access token SHALL preserve authority while its login remains active. An unreadable authority SHALL leave an unspent ticket available for retry.

#### Scenario: A transfer stops before the final byte

- **WHEN** an upload stops partway or the listener restarts while the issuing login remains valid
- **THEN** the next valid request receives the durable offset and can send the remaining bytes

#### Scenario: The issuing login closes or loses owner access before publication

- **WHEN** the login has closed or the account has lost its owner binding by the final authorization check
- **THEN** the listener refuses session access and does not publish any pending Source

#### Scenario: A client replays the ticket or sends a different digest

- **WHEN** a client tries to reuse the ticket or sends a declaration or bytes with another digest
- **THEN** it cannot open a second session or publish a different Source

### Requirement: A verified resumable Source enters custody once

The listener SHALL verify the complete SHA-256 and publish the original through the governed Source commit. Sending every byte SHALL not by itself report that a Source is stored. The terminal receipt SHALL identify the Source, artifact and verified digest.

#### Scenario: Canonical publication completed before the acknowledgement was interrupted

- **WHEN** the canonical Source commit completed before an interruption
- **THEN** the listener reuses the exact receipt without duplicating the Source
- **AND** the same command can retrieve its receipt for 24 hours after settlement

#### Scenario: A partial publication cannot prove its outcome

- **WHEN** a stop leaves canonical files without complete evidence of their commit
- **THEN** the listener retains the uploaded bytes and reports an unknown Source outcome
