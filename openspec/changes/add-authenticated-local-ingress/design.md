## Context

The managed service is a supervisor (`service_manager serve`) that owns the public TCP
listener and forwards every request through one `ServiceIngress` to a worker bound to a
private Unix socket. Request headers are relayed as received. Cloudflare tunnels
terminate on that same public loopback port, so a worker cannot use the peer address to
decide anything.

FastMCP installs the auth provider's middleware outside the middleware list the server
passes to `run()`, so a fact the auth decision needs must be established either in the
provider's own `get_middleware()` or inside `load_access_token()`.

`local_http_allowed` (loopback bind, no base URL, a REST key) is a different mode and stays
untouched.

## Goals / Non-Goals

**Goals**

- Same-machine clients get their own door with a per-client, revocable credential.
- The public path is byte-identical for every request that does not carry a forged
  internal header.
- Nothing changes until an operator sets `EXOMEM_LOCAL_PORT` and restarts the manager.

**Non-Goals**

- Isolation between same-UID processes. A process that can read the token file, or the
  service environment, can act as the owner; tokens buy attribution and revocation.
- Refusing the owner REST key or static upload token on Cloudflare-transited requests.
  That needs log evidence first; this change only counts it.
- Client configuration and the attachment-custody hand-off.

## Decisions

- **D1. A second listener on the same ingress.** A separate uvicorn server on
  `127.0.0.1:EXOMEM_LOCAL_PORT` wraps the one `ServiceIngress`, so pause, bounded queueing,
  drain and stream reattachment already cover it. The manager binds the socket itself
  before serving: a busy port or a malformed value disables the local listener with a
  logged reason instead of taking the public listener down, since the local listener is an
  optional door and the public one is the service.
- **D2. Strip on both, stamp on one, with a proof.** The ingress drops every inbound
  `x-exomem-internal-*` header before forwarding, whichever listener received it. The
  local listener marks its requests through an ASGI scope key a client cannot set, and the
  ingress then adds `x-exomem-internal-ingress: local` plus
  `x-exomem-internal-ingress-proof: <key>`. The key is random per manager process and
  reaches workers only through `EXOMEM_INTERNAL_INGRESS_KEY` in the environment the
  manager builds for each child, overriding any value in the service environment file.
  The worker takes it out of its own environment first thing at startup, so none of
  its descendants (the media worker, converter subprocesses) inherit it.
  The proof closes the upgrade window: a new worker can be deployed by seamless handoff
  under an old manager that does not strip internal headers, and without a proof a remote
  caller could forge the stamp through it. Under an old manager the worker has no key and
  refuses any stamp.
- **D3. Fail closed at the door.** The local listener refuses `cf-ray` or
  `cf-connecting-ip` (Cloudflare transit), any `Origin` (browser-originated, including DNS
  rebinding), a missing or non-literal-loopback `Host` (`localhost` is a name, not an
  address), a raw path containing `%`, `\`, an empty or dot segment, and every path outside
  `/mcp`, `/api/*`, `/upload` and `/health*`. The worker re-applies the same predicate to a
  stamped request as defence in depth. These rules cost nothing for a supported client:
  no existing client uses this listener.
- **D4. The audience is the ingress.** Local sessions use the issuer
  `urn:exomem:local-ingress` and the audience `urn:exomem:local-ingress/mcp`. The public
  authority already refuses any record whose issuer or audience differs from its own, so a
  local token on the public path is an ordinary 401. On local ingress the worker validates
  the bearer against the local authority only, so an OAuth token or the REST key is a 401
  there, and the 401 carries no OAuth `resource_metadata`.
- **D5. The worker seam.** `LocalIngressMiddleware` runs first in the OAuth proxy's
  `get_middleware()` list, and at the front of the server's own list for a no-auth
  worker. It removes the stamp from the scope, so the second instance is inert. On a
  verified local credential it binds a request-local grant and the authenticated user;
  `load_access_token()` returns the grant's token only for that exact bearer and otherwise
  keeps its existing body. The middleware is installed only on a supervisor-owned worker.
- **D6. Tokens are ordinary durable sessions.** `issue-local` calls
  `SessionAuthority.issue()` with the configured owner GitHub identity, so the allowed-
  account recheck, `revoke <id>` and the generation bump behind `revoke --all` apply
  unchanged, and `exomem auth sessions` lists them from the shared collection. Tokens do
  not expire; the file is created exclusively with mode 0600, and a failed write tombstones
  the new session so no unheld credential survives.
- **D7. `owner-local`.** A request whose verified token came from the local grant resolves
  to `OWNER_AUDIENCE` with issuer family `mcp-local`, `local_owner=True` and the audit
  label `owner-local`. Provenance is a token type only the local branch constructs plus the
  live grant, mirroring the remote-owner rule. It never sets `remote_owner`.
- **D8. Bytes, not paths.** `exomem attach` uploads bytes to the local `/upload`; no tool
  argument names a local path.

## Risks / Trade-offs

- A rolled-back worker release without this change ignores the stamp, so on the local
  listener it behaves as the public path: local tokens fail closed, public credentials
  work as they already do on the public listener.
- A second listener is a second bind on loopback. It is off by default and only accepts
  the local audience.
