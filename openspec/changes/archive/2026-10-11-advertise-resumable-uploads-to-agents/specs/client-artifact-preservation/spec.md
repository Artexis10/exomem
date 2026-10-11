## ADDED Requirements

### Requirement: The upload handoff advertises resumable sessions

On a server that serves upload sessions, `transfer_artifact(operation="upload")` for the Evidence lane SHALL return a `resumable` object beside `upload_url`. The object SHALL name the session creation URL, the protocol and its version, the header that a client resends on every later request, the most bytes that one request may carry, the most bytes that one session may hold, the `Upload-Metadata` keys that creation requires, and a link to a documented shell recipe. It SHALL NOT carry the recipe itself. The token that the handoff mints SHALL create a session through the same credential check and lane binding that `/upload` applies. Session creation SHALL refuse a request that lacks an advertised key with `INVALID_UPLOAD`, name every missing key, and open no session. A Sources-lane handoff SHALL NOT advertise sessions, because a session preserves Evidence.

#### Scenario: An agent with a shell uploads a file larger than the proxy edge cap
- **WHEN** an agent calls `transfer_artifact(operation="upload")`, creates a session at the advertised URL with the minted token, sends the file in parts and loses the connection once
- **THEN** it continues from the offset that the server reports
- **AND** the receipt names the SHA-256 of the whole file

#### Scenario: Creation lacks a required metadata key
- **WHEN** a client creates a session without one of the advertised `Upload-Metadata` keys
- **THEN** creation is refused with `INVALID_UPLOAD` that names every missing key, and no session opens

#### Scenario: A Sources-lane handoff
- **WHEN** an agent mints an upload capability for the Sources lane
- **THEN** the handoff carries `upload_url` and no `resumable` object

### Requirement: A standalone server pulls file handles up to the upload session maximum

On a standalone server, `preserve_artifacts` and `capture_source` SHALL accept one client file handle, and all the file handles of one call together, up to the upload session maximum that `EXOMEM_UPLOAD_SESSION_MAX_BYTES` sets. A held upload SHALL be held only up to the same cap, so that its redeeming command can fetch it. A pull SHALL stop with a stable timed-out failure and remove its temporary bytes when no byte arrives within its per-read timeout, or when, after a fixed allowance, its average rate falls below the documented minimum. Each byte that a pull receives SHALL extend the deadline of its file and of its call. Hosted and Cloud cells SHALL keep the per-file and per-call cap that the artifact broker's grants carry, and fixed deadlines. A call that carries an adoption key SHALL keep that cell cap on every server, because replaying it re-verifies the stored artifact in memory.

#### Scenario: A file handle larger than 100 MiB
- **WHEN** a standalone server pulls a client file handle larger than 100 MiB and within the session maximum
- **THEN** it streams the bytes to private temporary storage and preserves them

#### Scenario: A slow but healthy pull
- **WHEN** a pull takes longer than the fixed allowance and its average rate stays above the minimum
- **THEN** the pull completes

#### Scenario: A later file in a call
- **WHEN** one call pulls a long first file and then a second file
- **THEN** the second file starts with the time the first file earned for the call

#### Scenario: A trickling pull
- **WHEN** the bytes of a pull arrive slower than the minimum rate after the fixed allowance
- **THEN** the pull stops with a timed-out failure and its temporary bytes are removed

#### Scenario: A Cloud cell
- **WHEN** a Cloud cell pulls a file handle through the artifact broker
- **THEN** the broker grant's cap and the fixed deadlines apply as before
