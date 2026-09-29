## ADDED Requirements

### Requirement: A separate loopback listener, off by default

A managed service SHALL open a second HTTP listener on `127.0.0.1` only when
`EXOMEM_LOCAL_PORT` names a valid TCP port different from the public port. The listener
SHALL forward through the same ingress as the public listener. A missing, malformed or
unbindable local port SHALL leave the local listener closed with a logged reason and SHALL
NOT stop the public listener.

#### Scenario: The port is unset
- **WHEN** the manager starts without `EXOMEM_LOCAL_PORT`
- **THEN** only the public listener is open and no worker receives an ingress proof

#### Scenario: The port cannot be used
- **WHEN** `EXOMEM_LOCAL_PORT` is malformed, equals the public port, or is already bound
- **THEN** the manager serves the public listener and reports the local listener as closed

### Requirement: Internal headers are stripped on both listeners and stamped on one

The ingress SHALL remove every inbound header whose name starts with `x-exomem-internal-`
before forwarding, on both listeners. Only a request admitted by the local listener SHALL
be forwarded with `x-exomem-internal-ingress: local` and the manager's per-process ingress
proof. A worker SHALL treat a stamp as local only when it serves a supervisor-owned socket
and the proof matches the key its manager gave it; any other stamp SHALL be refused.

#### Scenario: A remote caller forges the stamp through the public listener
- **WHEN** a request to the public listener carries `x-exomem-internal-ingress: local`
- **THEN** the header is removed before forwarding and the worker handles the request on the public path

#### Scenario: An older manager forwards a forged stamp
- **WHEN** a worker from this release, running under a manager that does not strip internal headers, receives a stamp without a valid proof
- **THEN** the worker refuses the request and no local credential check runs

### Requirement: The local listener fails closed on transit, browsers and unknown paths

The local listener SHALL refuse, without forwarding, any request that carries `cf-ray` or
`cf-connecting-ip`, any `Origin`, a missing `Host` or a `Host` that is not literally
`127.0.0.1` or `[::1]` with an optional port, and any raw path that contains `%`, `\`, or
an empty or dot segment or that is not `/mcp`, under `/api/`, `/upload`, `/health` or under
`/health/`. A worker SHALL apply the same predicate to a stamped request.

#### Scenario: Cloudflare-transited request on the local port
- **WHEN** a request with `cf-ray` reaches the local listener
- **THEN** it is refused and never reaches the worker

#### Scenario: A browser page reaches the local port
- **WHEN** a request carries an `Origin` header, or a `Host` such as `localhost` or a rebinding hostname
- **THEN** it is refused and never reaches the worker

#### Scenario: A path outside the allowlist
- **WHEN** a request targets `/control/promote`, `/download`, `/metrics.json`, an OAuth route, or an encoded path
- **THEN** it is refused and never reaches the worker

### Requirement: The audience is the ingress

Local client sessions SHALL carry a local issuer and audience distinct from every public
issuer and audience. On local ingress a worker SHALL validate the bearer only against the
local audience, and its refusal SHALL carry no OAuth `resource_metadata`. On the public
path a worker SHALL validate exactly as before, so it refuses a local session.

#### Scenario: A local token via the public route
- **WHEN** a request presenting a valid local token arrives through the public listener
- **THEN** it receives the public path's ordinary 401

#### Scenario: An OAuth token on the local listener
- **WHEN** a request presenting a valid OAuth session token arrives through the local listener
- **THEN** it receives a 401 with no `resource_metadata`, and neither the REST key nor the static upload token is accepted there either

#### Scenario: The public path is unchanged
- **WHEN** an OAuth session token arrives through the public listener
- **THEN** it is accepted exactly as before this change

### Requirement: Local tokens are issued, listed and revoked by the operator

`exomem auth issue-local --client <name> --output <file>` SHALL mint a non-expiring local
session for the configured owner identity, create the file exclusively with mode 0600 and
never print or log the token. A failure to write the file SHALL revoke the new session.
`exomem auth sessions` SHALL list local sessions and name their ingress, `exomem auth revoke
<id>` SHALL end one, and `exomem auth revoke --all` SHALL end them with every other session.

#### Scenario: Issue a token
- **WHEN** the operator issues a token for client `home`
- **THEN** a 0600 file holds it, the command prints only its session id and client, and the next local request with it is accepted

#### Scenario: Revoke a token
- **WHEN** the operator revokes that session, or revokes all sessions
- **THEN** the next local request with the token receives a 401

### Requirement: A local token resolves to the owner, labelled local

A request authenticated on local ingress SHALL resolve to the owner audience with issuer
family `mcp-local` and the audit label `owner-local`. It SHALL never be labelled as a
remote owner, and its provenance SHALL require both the token type the local branch
creates and the request's live local grant.

#### Scenario: A tool call over the local listener
- **WHEN** a local client calls a tool with a valid local token
- **THEN** the call ledger records `principal_kind: owner-local`

#### Scenario: Claims copied into another token
- **WHEN** a token without local provenance carries claims identical to a local token's
- **THEN** it does not resolve to the owner through the local rule

### Requirement: REST and upload accept the local token on local ingress

On local ingress the REST facade and `/upload` SHALL accept a valid local token as the
owner, even when the REST key or the upload token is unset, and SHALL keep their existing
gates on the public path. `/upload` SHALL keep its multipart form, its size cap and its
default evidence lane. `exomem attach <file>` SHALL send the file's bytes to the local
`/upload` with a local token and print the returned handle; no tool argument SHALL name a
local path.

#### Scenario: Upload over local ingress
- **WHEN** `exomem attach` sends a file with a valid local token
- **THEN** the service preserves the bytes and the command prints the handle

#### Scenario: REST over local ingress
- **WHEN** a local client posts to `/api/<tool>` with a valid local token
- **THEN** the call runs as `owner-local`

### Requirement: Ingress logs are content-free and attributed

A request served on local ingress SHALL be logged with `ingress=local` and its session's
client id, and a refusal SHALL name only its reason. No log line SHALL contain a bearer, a
proof, a header value, or request content. Use of the owner REST key or the static upload
token on a Cloudflare-transited request SHALL be recorded in a content-free log event
without being refused, and SHALL NOT appear in the unauthenticated metrics.

#### Scenario: Local request logged
- **WHEN** a local client's request completes
- **THEN** its access record names `ingress=local` and the client id and nothing secret

#### Scenario: Owner key used through the tunnel
- **WHEN** the owner REST key authorizes a request carrying `cf-ray`
- **THEN** the request is served as before and a content-free log event records it
- **AND** `/metrics.json` carries nothing about it
