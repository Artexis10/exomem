## Why

FastMCP 4 is now stable and supports the current MCP protocol while retaining
legacy clients. Exomem is pinned to FastMCP 3.4.4 and carries transport and OAuth
integration code that must be migrated deliberately. The upgrade targets
protocol compatibility and maintainability, not an assumed latency improvement.

## What Changes

- Pin stable FastMCP 4.0.3 and reconcile its MCP SDK v2 and HTTP client contracts.
- Preserve raw credential redaction, per-request authorization, durable OAuth
  sessions, stateless HTTP compatibility, and sanitized stdio transport.
- Exercise both legacy initialized clients and modern sessionless clients.
- Remove compatibility code only where the new dependency demonstrably supplies
  its behavior; keep Exomem-owned security boundaries.
- Verify public tool behavior and durable-closure regressions independently of
  deployment. No graph/index, writer-ordering, or acknowledgement changes.

## Capabilities

### New Capabilities

- `mcp-protocol-compatibility`: Supported MCP transport generations preserve the
  same command and authorization semantics across framework upgrades.

### Modified Capabilities

None. Existing authorization-session and OAuth longevity requirements remain
unchanged and are acceptance constraints on this migration.

## Impact

Dependency manifest and lockfile; FastMCP composition, sanitized stdio adapter,
GitHub verification and OAuth integration; transport/authentication tests.
No canonical data migration, new model-backed feature, or service rollout is
part of the code change. Existing release and rollback procedures apply.
