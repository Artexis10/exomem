## Context

The Cloudflare HA Worker currently sends every request to the lease holder with `ORIGIN_TIMEOUT_MS` (2.5 seconds), then replays any timeout or 5xx response to the passive origin. That policy is fine for discovery and initialization but unsafe for `tools/call`: a synchronous Exomem operation can legitimately take 4–9 seconds, and an aborted edge fetch does not cancel the Python mutation. Live evidence showed the desktop returning `tool_success` after the Worker had already forwarded the request to a laptop still running stateful Exomem 0.19.1.

## Goals / Non-Goals

**Goals:**

- Wait long enough for normal MCP tool execution.
- Give every `tools/call` request one unambiguous origin while a writer lease is active.
- Preserve automatic laptop takeover after the desktop lease expires.
- Keep short failover for safe discovery, OAuth, initialization, and listing traffic.
- Make replica release parity an explicit HA deployment gate.

**Non-Goals:**

- Make every MCP tool complete within 2.5 seconds.
- Share transport sessions between replicas; remote Exomem is already stateless.
- Build cross-replica result/idempotency storage.
- Change retrieval ranking, write enrichment, OAuth, or the vault format.

## Decisions

Classify MCP JSON-RPC requests at the edge. A POST to `/mcp` whose JSON body is a `tools/call` request (or a batch containing one) is an ambiguous side-effect boundary and receives the tool-call policy. Malformed/unreadable MCP POST bodies are treated conservatively as tool calls.

Use a separate `MCP_TOOL_TIMEOUT_MS` with a 15-second default. This covers the observed cold deep-read and governed-write envelope. `ORIGIN_TIMEOUT_MS` remains the short connectivity/fallback timeout for safe requests. Correctness does not depend on a timeout/lease ordering because the edge never replays an active-holder tool call; a live origin keeps renewing its lease independently.

When the coordinator names a holder, route a tool call only to that holder. Return its response or an edge error; never replay the call to the passive replica. This is preferable to availability-through-replay because an origin timeout is ambiguous: the tool may already have committed.

When no lease holder exists, probe both origins through the unauthenticated OAuth protected-resource metadata endpoint using the short timeout, choose one healthy origin (desktop preferred), and send the tool call exactly once with the long timeout. A write on the chosen origin acquires the lease; a read remains safe. If neither probe succeeds, return 503 without forwarding the tool call.

Keep the existing bounded fallback loop for non-tool traffic. Initialization, tool discovery, OAuth metadata, and GET/SSE connections are session-independent after Exomem 0.20.1 and can safely try the passive origin.

Replica parity is operationally load-bearing. Both replicas must run a restart-safe release before the stable connector route is enabled. The deployment guide and diagnostics shall compare repo and installed-package versions on both machines; an old passive replica is not considered a valid completed HA deployment.

## Risks / Trade-offs

- [The active holder dies during a tool call] → The request fails once instead of being ambiguously replayed; after lease expiry, the next request selects the healthy laptop.
- [A legitimate tool exceeds 15 seconds] → The client receives an error but the edge still does not replay it cross-replica; the active origin continues renewing its lease and same-replica implicit idempotency protects an identical retry within its bounded window.
- [The no-holder health probe races with an origin failure] → The selected request still goes to one origin only and fails closed.
- [Replica versions drift again] → Deployment parity checks and explicit documentation make the incomplete rollout visible before HA is declared healthy.

## Migration Plan

1. Upgrade desktop and laptop services to the same restart-safe Exomem release.
2. Deploy the Worker routing change with `MCP_TOOL_TIMEOUT_MS=15000` (or higher but below the lease TTL).
3. Verify slow read and write probes remain on one origin and return successfully.
4. Stop the desktop, wait for lease expiry, and verify one laptop tool call succeeds without reconnect/re-auth churn.

Rollback is the previous Worker deployment; doing so restores the known duplicate-risk behavior, so rollback should also disable the passive replica route.

## Open Questions

None before implementation.
