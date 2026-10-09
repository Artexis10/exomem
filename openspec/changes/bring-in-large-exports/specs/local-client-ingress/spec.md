## MODIFIED Requirements

### Requirement: The local listener fails closed on transit, browsers and unknown paths

The local listener SHALL refuse, without forwarding, any request that carries `cf-ray` or
`cf-connecting-ip`, any `Origin`, a missing `Host` or a `Host` that is not literally
`127.0.0.1` or `[::1]` with an optional port, and any raw path that contains `%`, `\`, or
an empty or dot segment or that is not `/mcp`, under `/api/`, `/upload`, `/upload/sessions`
or under `/upload/sessions/`, `/health` or under `/health/`. A worker SHALL apply the same
predicate to a stamped request.

#### Scenario: Cloudflare-transited request on the local port
- **WHEN** a request with `cf-ray` reaches the local listener
- **THEN** it is refused and never reaches the worker

#### Scenario: A browser page reaches the local port
- **WHEN** a request carries an `Origin` header, or a `Host` such as `localhost` or a rebinding hostname
- **THEN** it is refused and never reaches the worker

#### Scenario: A path outside the allowlist
- **WHEN** a request targets `/control/promote`, `/download`, `/metrics.json`, an OAuth route, or an encoded path
- **THEN** it is refused and never reaches the worker

## ADDED Requirements

### Requirement: Uploads resume through sessions

Both listeners SHALL serve resumable upload sessions under `/upload/sessions` with the tus
1.0 core protocol and its creation, expiration and termination extensions. Creating a
session SHALL take the credentials that `/upload` takes on that listener and SHALL declare
the whole file's length and SHA-256. Creation SHALL return a session secret that every
later request on the session presents in a header; the secret SHALL never appear in a URL
or a log, and Exomem SHALL store only its hash. Each request SHALL carry at most 64 MiB,
and a session's length SHALL be bounded by `EXOMEM_UPLOAD_SESSION_MAX_BYTES`. A session
SHALL resume after a lost connection or a service restart at the offset the server
reports. Exomem SHALL preserve the bytes only after their SHA-256 equals the declared one,
SHALL fail the session and delete its bytes on a mismatch, and SHALL delete a session's
bytes when the session is cancelled or expires. `exomem attach` SHALL use a session when a
file exceeds the single-request cap and SHALL resume an interrupted upload on its next run.

#### Scenario: An interrupted upload resumes
- **WHEN** the connection drops after part of a file is sent and the client asks for the offset
- **THEN** the server reports the bytes it holds and the client sends only the rest

#### Scenario: The bytes do not match the declared hash
- **WHEN** the last chunk arrives and the file's SHA-256 differs from the declared one
- **THEN** the session fails with a stable code, nothing is preserved and the bytes are deleted

#### Scenario: A request without the session secret
- **WHEN** a request names an existing session but omits its secret or presents another
- **THEN** the server refuses it as if the session did not exist
