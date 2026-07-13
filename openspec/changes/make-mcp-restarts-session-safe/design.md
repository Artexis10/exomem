## Context

FastMCP's default Streamable HTTP transport is stateful: initialization creates a process-local transport session and later requests carry `Mcp-Session-Id`. Exomem's OAuth and writer state are durable and shared across replicas, but the transport session is not. A Windows service restart or HA route change therefore returns 404 for a previously valid session and can push clients into unnecessary connection and authorization recovery.

## Goals / Non-Goals

**Goals:**

- Remove process-local MCP transport sessions from remote HTTP operation.
- Preserve OAuth enforcement, tool semantics, retry safety, and writer fencing.
- Make the behavior an Exomem product default rather than a private deployment tweak.

**Non-Goals:**

- Persist FastMCP transport sessions externally.
- Change token lifetime, signing, shared OAuth storage, or the MCP endpoint.
- Change stdio transport behavior.

## Decisions

Pass `stateless_http=True` explicitly when Exomem runs an HTTP transport. FastMCP then creates a fresh transport for each request and does not require a process-local session record. Explicit construction is preferable to relying on `FASTMCP_STATELESS_HTTP` because restart/failover safety is part of Exomem's remote-server contract and should not depend on operator configuration.

Preserve GET/SSE compatibility on the same OAuth-protected MCP route. FastMCP omits GET from stateless routes because server-initiated notifications are optional, but Codex and Claude still open that channel. The underlying MCP SDK stateless transport already handles GET without issuing a session ID, so an Exomem FastMCP subclass adds GET back to the protected route rather than implementing a parallel or unauthenticated endpoint.

Keep OAuth unchanged. Stateless transport removes only the MCP session dependency; every request still passes through the existing OAuth middleware and durable token swap.

Expose `/.well-known/openid-configuration` as an RFC 8414-compatible alias of Exomem's authorization-server metadata. Live Codex evidence showed a successful `/token` response immediately followed by a 404 at this alias, after which the client abandoned the connection. The alias contains OAuth endpoint metadata only; Exomem does not claim to issue OIDC ID tokens.

Cover construction with a focused fake-server test that asserts remote runs enable stateless mode and stdio runs do not receive the option. Integration verification will exercise repeated authenticated calls before and after one controlled service restart.

## Risks / Trade-offs

- [FastMCP changes its internal route shape] → Keep a focused route-method regression test and fail construction visibly if the protected MCP route cannot be found.
- [Per-request transport setup adds overhead] → FastMCP exposes this mode for horizontally scaled deployments; measure consecutive call latency during live verification.
- [Stateless mode does not repair an actually invalid OAuth token] → OAuth remains independently diagnosable; the change targets the observed 404/reconnect cascade after restart.

## Migration Plan

Deploy as a normal Exomem service update and perform one controlled restart. Existing clients should establish a stateless request on their next call. Roll back by removing the explicit option if a supported connector proves incompatible.

## Open Questions

None before implementation.
