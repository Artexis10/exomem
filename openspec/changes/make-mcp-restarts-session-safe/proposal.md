## Why

Remote MCP clients currently depend on process-local Streamable HTTP session IDs. A normal Exomem restart or HA failover invalidates those IDs, causing repeated 404/reconnect/authentication churn even though the OAuth credential and shared state remain valid.

## What Changes

- Run remote Streamable HTTP in FastMCP stateless mode so each request gets a fresh transport and no process-local session survives between calls.
- Serve the OIDC discovery alias used by Codex after OAuth token exchange.
- Preserve OAuth, shared encrypted token storage, writer fencing, and tool behavior.
- Add regression coverage proving remote HTTP construction is stateless while stdio remains unchanged.
- Document restart/failover continuity for self-hosted deployments.

## Capabilities

### New Capabilities

- `mcp-session-continuity`: Remote MCP calls do not depend on process-local transport session state and can recover cleanly across Exomem restart or replica failover.

### Modified Capabilities

None.

## Impact

Affected areas are `src/exomem/server.py`, `src/exomem/server_assets.py`, focused server transport tests, and deployment documentation. No MCP tool schema, OAuth token semantics, vault format, dependency, or stdio behavior changes.
