## Why

The ChatGPT connector gives a tool call about 60 seconds and Cloudflare's edge about 100; the serving host's call ledger shows `ask_memory` rows with `deep`, `rerank` and `graph_enrich` running 1 to 15 minutes and `preserve_artifacts` rows running 14 to 95 seconds. The work finishes after the client has given up, the conversation marks the tool disabled, and the agent has no way to know which stage consumed the time because recall stage timings never reach the ledger. Nothing on the single MCP dispatch path creates a request-scoped deadline; the only bounds are local constants, one of which still cites a 15-second connector timeout that no longer exists, and the preservation path runs its media index and graph fan-out inline while it holds the mutation guard.

## What Changes

- Every MCP tool call carries a request-scoped deadline created at the dispatch entry, defaulting to 50 seconds so the response leaves the origin before the client's 60 seconds and the edge's 100. Operators may override it by environment; clients cannot loosen it. Local CLI calls, tests and reconcile-class maintenance are exempt.
- Heavy recall stages (`rerank`, the `deep` pack, `graph_enrich`) yield to the remaining budget: a stage that cannot finish inside its reserve is skipped or truncated, and the response carries a `budget` block naming exactly what was skipped so the agent can re-ask narrower. Calls that fit the budget are unchanged.
- `preserve_artifacts` and `preserve_evidence` hand media reconciliation a commit guard, as `process_media` already does, so index and graph fan-out no longer runs inside the held mutation guard or before the batch terminal is persisted; `derived_sync` reports it.
- Recall stage timings are collected inside every MCP call and mirrored into the call ledger as `recall.<stage>` spans; the ledger row also records the budget outcome. Response inclusion of timings stays opt-in.
- The writer lease's acknowledgement budget derives from the request deadline instead of its own clock, the stale connector-timeout comment is corrected, and the operator variable is documented beside the edge's tool-call timeout.

## Capabilities

### New Capabilities

- `connector-request-budget`: the request-scoped deadline, the stage-yield rule, the `budget` block, and the boundary between bounding waits and never interrupting canonical commits.

### Modified Capabilities

- `find-recall-efficiency`: timing diagnostics are always collected inside an MCP call and mirrored to the ledger; the response stays opt-in.
- `call-ledger`: rows carry bounded stage `spans` and a `budget` outcome beside `duration_ms` and `total_ms`.
- `client-artifact-preservation`: media fan-out leaves the preservation critical section and the pre-terminal window.
- `write-path-resilience`: the origin budgets its own work below the edge's 60-second mutation budget.

## Impact

- Code: `src/exomem/command_surface.py` (deadline creation in the per-call context), new `src/exomem/request_budget.py` (constants and the budget object), `src/exomem/commands.py` (`op_find` stage decisions and the `budget` block), `src/exomem/find.py` (rerank yield, span mirroring), `src/exomem/context_pack.py` (deadline-aware pack assembly), `src/exomem/find_types.py` (span bridge), `src/exomem/client_artifacts.py` (commit guard for media reconciliation), `src/exomem/writer_lease.py` (budget derivation), `src/exomem/call_ledger.py` (row fields), `src/exomem/semantic_contract.py` (comment), scaffold reference text and its plugin copy, `docs/remote-quickstart.md`.
- Behaviour: connector callers see bounded responses that say what was skipped instead of timeouts; local CLI behaviour is unchanged.
- Related in-flight changes are amended, not duplicated: `accelerate-governed-recall` (recall ceilings and span completeness), `bound-corpus-context-flight-join` (the 2-second corpus-context bound nests inside the request budget), `accelerate-durable-write-acknowledgement` (its 2-second post-canonical budget nests inside the writer lease's derived budget), `deterministic-edge-ingress` (owns the edge variable this change documents next to).
