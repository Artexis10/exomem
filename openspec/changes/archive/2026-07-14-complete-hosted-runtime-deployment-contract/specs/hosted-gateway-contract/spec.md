## MODIFIED Requirements

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
