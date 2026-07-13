## Why

The active/passive Cloudflare edge currently applies a 2.5-second origin timeout to every request and blindly replays slow MCP POSTs to the passive replica. Exomem calls taking 3.9–8.9 seconds therefore finish successfully on the desktop while the edge forwards the same session-bound request to an out-of-date laptop, Claude reports failure, and mutating retries create duplicate governed notes.

## What Changes

- Give MCP tool calls a separate, realistic origin deadline instead of the edge's short connectivity timeout.
- Never replay an in-flight tool call to the passive replica while the coordinator names an active writer; an ambiguous timeout or server error returns to the client without duplicating the request.
- Preserve fast fallback for discovery, OAuth, initialization, and other safe transport requests.
- Allow replica fallback after the writer lease expires so the laptop still takes over when the desktop is intentionally shut down.
- Document and verify that every HA replica must run the same restart-safe Exomem release before being admitted to the stable connector route.
- Add Worker regression tests for slow successful calls, active-writer routing, lease-expiry takeover, and mixed-version deployment diagnostics.

## Capabilities

### New Capabilities

- `remote-mcp-deadline-safety`: The HA edge waits for legitimate MCP tool execution and never blindly replays an ambiguous in-flight tool call across replicas.

### Modified Capabilities

None.

## Impact

Affected areas are the Cloudflare HA Worker routing policy, its configuration and tests, HA deployment documentation, and replica parity verification. Core MCP tool schemas, retrieval quality, vault format, OAuth semantics, and pure-substrate behavior remain unchanged.
