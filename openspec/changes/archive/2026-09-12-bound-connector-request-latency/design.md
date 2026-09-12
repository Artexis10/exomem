## Context

Facts gathered on 2026-09-11 from the tree at origin/main a9262d73 and the serving host's ledger.

- One MCP dispatch path serves every connector: `server.py:CallTraceMiddleware.on_call_tool` (from L170) mints the request id, opens `command_surface.mcp_request_context(request_id)` (L698 to L711), which sets the per-call `MCP_CALL_TOKEN`, runs the tool, and records the ledger row with `spans=pop_call_spans(call_token)` at four exits (L201, L248, L304, L323). Hosted and REST routes add routes beside it; they do not dispatch tool calls.
- No request-scoped deadline exists. The nearest bounds are local: `client_artifacts._BATCH_DEADLINE_SECONDS = 60.0` for handle retrieval, `writer_lease._MUTATION_REQUEST_BUDGET_SECONDS = 60.0` minus `_TERMINAL_DELIVERY_RESERVE_SECONDS = 5.0` measured from lease entry and folded into the derived wait, `semantic_contract._CORPUS_CONTEXT_JOIN_TIMEOUT_SECONDS = 2.0` whose comment (L1309 to L1311) cites a 15-second connector timeout that no longer exists. The canonical `write-path-resilience` spec pins the edge's 60-second single-origin mutation budget; `remote-mcp-deadline-safety` requires a dedicated MCP tool-call deadline at the edge without a number; the in-flight `deterministic-edge-ingress` names the edge variable `MCP_TOOL_TIMEOUT_MS=60000`.
- Recall: `op_ask_memory` (commands.py L5393) → `op_find` (L1994) → `find.find` (find.py L991). `rerank` is decided at find.py L4464 to L4501 and runs at L4508; the `deep` pack and `graph_enrich` run only in `op_find` at commands.py L2436 to L2439 through `context_pack.assemble_pack`. `FindTimings` exists only when `include_timings` is set (commands.py L2294) and its stages never reach `call_spans`, which is registered only in write modules. Model singletons are unloaded after 15 idle minutes (`model_reaper.DEFAULT_IDLE_SECONDS = 900`) and reloaded synchronously inside the next request.
- Preservation: `client_artifacts` calls `media_processing.reconcile_media` without a `commit_guard` at L1206 and L1487, inside `manager.mutation_guard(..., operation="preserve_artifacts_media")`, so `_write_sidecar` runs with `post_commit_fanout=True` and the index and graph fan-out executes inline while the guard is held. `commands._process_media` (L6447) passes `_commit_guard` (L6461 to L6467) to the same function, deferring guard acquisition and setting `post_commit_fanout=False`.
- Ledger rows: `call_ledger.record_call` already accepts `spans` (L270, L314, clipped by `_clip_spans` L327) and the canonical spec requires `duration_ms` and `total_ms`; no requirement describes `spans`, and nothing records a budget outcome.
- Six active changes own neighbouring work and are not duplicated here: `accelerate-governed-recall` (p50/p95 recall ceilings, span completeness; 22 of 28), `bound-corpus-context-flight-join` (the 2-second bound; 18 of 21), `fix-startup-retrieval-liveness` (13 of 15), `shorten-mutation-critical-section` (20 of 24), `bound-remote-maintenance` (6 of 7), `fix-deferred-work-drain` (39 of 42), plus `accelerate-durable-write-acknowledgement` (one shared 2-second post-canonical budget) and `deterministic-edge-ingress` (the edge timeout variable).

## Goals / Non-Goals

Goals: a connector call returns before the client stops listening, says what it left out when it had to leave something out, and leaves a ledger row that attributes its time by stage; a preservation batch returns its receipt without paying for media fan-out inside the critical section; local behaviour is unchanged.

Non-goals: recall speed itself (owned by `accelerate-governed-recall`); the corpus-context bound and the post-canonical acknowledgement budget (owned by their changes; they nest inside this budget); the ChatGPT application's own timeout and disable policy; the edge variable's value; model residency policy.

## Decisions

### D1 A request-scoped deadline is created where the call enters

`mcp_request_context` creates a `RequestBudget` for every MCP tool call: `deadline = entry + MCP_REQUEST_BUDGET_SECONDS`, held in a context variable beside the call token, readable anywhere on the request through `request_budget.current()`. The default is 50 seconds, PROVISIONAL, in one module (`src/exomem/request_budget.py`) with the other reserves; it sits below the client's 60 seconds and the edge's 100 with a delivery reserve for serialization and transport. Operators override it with `EXOMEM_MCP_REQUEST_BUDGET_SECONDS`; a client cannot loosen it per call, because the budget exists to protect the client from work it will never receive. Calls with no MCP token (CLI, tests, in-process callers) get no budget and behave exactly as today. Reconcile-class commands (`reconcile`, `maintain_memory` in reconcile mode) are exempt because their terminal is the derived state and `wait_for_graph_sync` joins unbounded by design.

Control accounting: what it prevents is work that completes after the caller has gone, which the ledger shows poisoning the conversation; what it costs when wrong is a narrower result for a call that would have finished a few seconds late, and that result says so; the payer is the caller who asked for a heavy option and can re-ask narrower. Alternative rejected: a per-call `budget_seconds` argument. The client that needs it most cannot be trusted to set it, and a knob on the tool surface moves the fingerprint and forces another action-schema refresh for a control almost nobody should touch.

### D2 Heavy recall stages yield to the remaining budget

Inside `op_find` and `find.find`, each heavy stage checks `budget.can_afford(reserve)` before it starts: `rerank` (reserve `RERANK_RESERVE_SECONDS = 8.0`, or `RERANK_COLD_RESERVE_SECONDS = 25.0` when the reranker singleton is not resident, because the cold load runs inside the request), the `deep` pack (`PACK_RESERVE_SECONDS = 10.0`), `graph_enrich` (`GRAPH_ENRICH_RESERVE_SECONDS = 5.0`). A stage that cannot be afforded is skipped and named in `budget.skipped`; the pack assembly also takes the deadline and stops adding items when it is reached, naming `pack` in `budget.truncated`. Ranking of the hits that were produced is identical to the unbudgeted call: yielding removes stages, it never reorders. The reserves are PROVISIONAL constants in `request_budget.py`, chosen from the ledger's stage spans; the change records the values it shipped with.

Alternative rejected: cancelling a stage mid-flight with a thread interrupt. The reranker and the pack are library calls that do not check flags, and a half-applied rerank would reorder hits unpredictably. Skipping before the stage starts keeps the result explainable.

### D3 A budgeted response names what it skipped

When a budget applied and at least one stage was skipped or truncated, the recall response carries `budget: {"applied": true, "seconds": <budget>, "remaining_ms_at_return": <int>, "skipped": [...], "truncated": [...]}`; when nothing was skipped the block is omitted so the default response shape is unchanged. The block reaches compact detail. The scaffold reference for recall gains one sentence: a `budget` block means the result is complete for the stages listed as run and the agent may re-ask with a narrower option set; it is not an error. The `ask_memory` tool description is not changed in this change, so the tool-surface fingerprint and the ChatGPT action schema registered after 0.78.0 stay valid; the block is self-describing when present.

### D4 Preservation fan-out leaves the critical section

`preserve_artifacts` and the evidence adoption lane pass `reconcile_media` a `commit_guard` built the way `commands._process_media` builds its own, so `_write_sidecar` runs with `post_commit_fanout=False` and the per-file `preserve_artifacts_media` guard wraps only the sidecar write. The index and graph work for the media sidecar then takes the derived path note writes already use (the fast-acknowledgement session's derived batches, drained behind the response and reported through `derived_sync`). Invariant: no index or graph fan-out runs while a preservation mutation guard is held, and none runs before the batch terminal is persisted. When no derived path is available for a sidecar (no fast-acknowledgement session), the fan-out runs after the terminal is persisted, in the position the acknowledgement occupies since `repair-artifact-preservation-receipts-and-idempotent-retry`, never inside the guard. The implementing lane verifies which of the two mechanisms carries each sidecar and records it in tasks.md.

Without a fast-acknowledgement session, media reconciliation also queues a durable full refresh before terminal persistence. Canonical recovery evidence carries `derived_sync=pending`, including the optional, enum-validated field in the signed graph commit receipt. If terminal persistence fails, an exact retry recovers that pending outcome without restaging the artifact; the background drain retains custody of the refresh. Foreground fan-out reports its observed component outcomes but leaves retirement of the backup refresh to the background drain. This can repeat an idempotent refresh after foreground success; it avoids deleting newer work when a full-queue revision is reused after deletion.

### D5 Recall stage timings become ledger spans

`FindTimings` is created for every `find` that runs inside an MCP call, not only when `include_timings` is set, and `find_types.timing_span` mirrors each stage into `call_spans.record_span` under the name `recall.<stage>` (`recall.bm25`, `recall.vector`, `recall.rerank`, `recall.pack`, `recall.graph_enrich`, `recall.serialize`, and the other stages the existing diagnostics already name). Spans carry names and milliseconds only, never query text, hit paths or excerpts; the existing 64-name bound per call holds. Response inclusion of `timings` stays opt-in as the canonical requirement says. The ledger row gains `budget: {"seconds", "remaining_ms", "skipped"}` whenever a budget applied, so a slow row is attributable without a second call.

### D6 The writer lease's budget derives from the request deadline

`writer_lease.invoke` computes its acknowledgement deadline as the earlier of its existing entry-based value and `request_budget.current().deadline - DELIVERY_RESERVE_SECONDS` when a request budget exists. The budget bounds only the derived waits, exactly as the archived receipts repair left it: a canonical commit that is already under way is never interrupted, and a terminal is always persisted. The comment on `_CORPUS_CONTEXT_JOIN_TIMEOUT_SECONDS` is corrected to name the 60-second client, the 100-second edge and this 50-second origin budget; the constant's value is untouched because `bound-corpus-context-flight-join` owns it.

### D7 What stays out

The `ask_memory` and `preserve_artifacts` tool descriptions are unchanged (see D3). `accelerate-durable-write-acknowledgement`'s 2-second post-canonical budget and `bound-corpus-context-flight-join`'s 2-second join bound are not touched; their waits nest inside the remaining request budget. The edge variable `MCP_TOOL_TIMEOUT_MS` belongs to `deterministic-edge-ingress`; this change documents `EXOMEM_MCP_REQUEST_BUDGET_SECONDS` beside it. The origin budget must be below both the edge and client timeouts. Where the edge is configurable, it should also be below the client's timeout so an edge timeout response can arrive. The reference worker's 60-second limit and a direct Cloudflare Tunnel's 100-second cap are distinct deployments; neither implies the client waits longer.

## Risks / Trade-offs

- A reserve set too high skips a stage that would have finished; too low lets a stage overrun. The constants are PROVISIONAL and the ledger's new `recall.*` spans are the instrument for tuning them; the first live week's spans are the follow-up.
- The cold-model reserve keys on singleton residency, which is a process-local fact; a second worker process would not share it. The personal cell runs one process per cell today.
- Deferring media fan-out means a freshly preserved image is searchable a few seconds after the receipt rather than at the receipt; `derived_sync` already communicates that state for notes, and the receipt is what the client needs to stop retrying.
- Always creating `FindTimings` inside MCP calls costs a few microseconds per stage; the existing `unattributed_ms` bookkeeping shows the cost stays negligible.

## Open questions

- Whether `graph_enrich` should also yield inside `assemble_pack` per neighbourhood rather than only at stage entry; start with stage entry and let the spans decide.
- Whether hosted cells with a different edge need a per-cell budget; the environment override covers it until a second edge exists.
