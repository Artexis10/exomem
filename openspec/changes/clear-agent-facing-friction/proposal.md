## Why

The July 17 ChatGPT incident exposed a second layer of failures after write acknowledgement safety: agents still struggle to identify a successful mutation, construct a valid edit, trust bootstrap recommendations, and act on large audit reports. Current reranking is optional but its caller cannot reduce the fixed `3 * limit` scoring batch when latency matters.

## What Changes

- Return a compact, decisive terminal envelope for governed product mutations by default, while retaining the exact persisted terminal result for replay and exposing full leaf diagnostics only when requested.
- Keep one `edit_memory` tool, but advertise a discriminated operation object whose variants expose only relevant fields. Preserve legacy top-level arguments as a bounded runtime compatibility shim without continuing to advertise the overloaded schema.
- Filter bootstrap tool references and catalogs against the actual invoking surface, and prove every advertised tool exists on MCP, REST, and CLI.
- Make audit output action-first: current blockers lead, malformed and unregistered semantic relations follow, and grandfathered missing-disposition debt is downgraded and grouped with counts plus bounded samples. Full enumeration becomes explicit.
- Keep reranking optional and soft-failing. Add a caller-controlled candidate cap; do not claim a hard wall-clock budget around the synchronous CrossEncoder call.
- Refresh the intentional MCP schema fixture and published discovery fingerprint. No additional top-level tools are introduced.

## Capabilities

### New Capabilities

- `mutation-terminal-contract`: Compact committed/replayed mutation results, full diagnostics on request, and replay-stable terminal identity.
- `action-first-audit`: Actionable-first audit projection with grouped grandfathered backlog and explicit full enumeration.

### Modified Capabilities

- `command-surface`: `edit_memory` advertises a discriminated single-tool schema while retaining one-release legacy runtime compatibility.
- `agent-bootstrap-contract`: Bootstrap recommendations are filtered to the actual invoking surface and conformance-tested.
- `find-recall-efficiency`: Explicit reranking accepts a bounded caller-selected candidate cap while remaining optional and soft-failing.

## Impact

Affected areas include the shared command registry and adapters, writer-lease/idempotency result persistence, FastMCP discovery generation, audit serialization, hybrid reranking, OpenAPI/CLI coercion, the generic skill contract, and schema/golden tests. Default mutation and audit response shapes intentionally change; legacy edit calls remain accepted during the compatibility window. No new model or dependency is added: the existing frozen reranker remains a pure measurement lane and runs only when explicitly selected or allowed by the existing accelerated auto policy.
