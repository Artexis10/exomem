## Context

The active mutation stack is FastMCP/REST/CLI → authenticated caller scope → `LeaseManager.invoke` → vault mutation boundary → writer lease/fence → command leaf → canonical transaction and index/log fanout → idempotency result persistence → transport response. The implementation currently enters the vault boundary before it consults idempotency. An overlapping identical retry therefore competes with its original request and can return pre-leaf `MUTATION_BUSY` while the original worker keeps running and commits.

The three preserved incident notes prove three separate canonical creates on the desktop writer at 2026-07-17 14:58:17.824Z, 14:59:03.203Z, and 15:00:43.127Z. Their IDs, content hashes, and payloads differ, so title similarity must not collapse them. The likely identical retries were transport-level contenders within each visible attempt. Current logs have no stable cross-layer request/idempotency correlation, so the exact A/B response pairing is not recoverable.

The Cloudflare HA worker already sends `tools/call` to one eligible origin once and fails closed on timeout/5xx, but personal REST and other mutation-capable POSTs still use generic cross-origin fallback. FastMCP runs synchronous command leaves in worker threads, so cancellation of HTTP response delivery does not necessarily stop the underlying mutation.

## Goals / Non-Goals

**Goals:**

- Make an identical in-flight retry observe the original idempotency record before it can contend for the mutation boundary.
- Persist a completed terminal result before releasing the mutation boundary and before response construction/delivery.
- Return the exact stored terminal result for a completed replay without rerunning writer authority or the leaf.
- Distinguish live acknowledgement uncertainty from pre-commit mutation contention.
- Scope mutation identity to vault/tenant, stable authenticated principal, command, and canonical payload.
- Add deterministic fault injection and privacy-safe phase correlation around terminal persistence and acknowledgement loss.
- Stop personal/local read-only maintenance audit from being misclassified as a mutation.

**Non-Goals:**

- Deduplicate revised payloads or similar titles.
- Promise cross-replica exactly-once recovery after process death between filesystem commit and local receipt persistence. The edge remains single-origin and the per-replica receipt limitation remains explicit.
- Shorten the transaction boundary around required index, log, rollback, and notification work in this correctness change.
- Implement the separate compact envelope, `edit_memory` schema redesign, bootstrap capability filtering, audit output triage, or reranker budget changes.

## Decisions

### Claim idempotency before mutation authority

`IdempotencyStore.run` will reserve or inspect the request identity before entering an injected operation guard. Only the owner of a new pending record may acquire the vault mutation boundary and writer lease. A completed replay returns directly; a mismatched digest fails directly; an identical pending retry waits outside the mutation boundary.

This preserves the existing single-writer/fencing gate for new mutations while preventing a replay from competing with its own original. Merely increasing the lock timeout was rejected because it leaves the ordering bug and ambiguous result intact.

### Bounded-wait for an identical pending request

An identical pending retry will poll the durable SQLite record for a bounded interval, using process-local notifications to avoid unnecessary latency. Completion returns the exact stored result. A pre-commit failure removes the pending row and lets the waiter claim and execute. Expiry of the wait returns `MUTATION_ACKNOWLEDGEMENT_PENDING`, which explicitly says the original outcome is not yet known; it never returns `MUTATION_BUSY` for that identity.

`MUTATION_BUSY` remains reserved for a different/new mutation which could not acquire the boundary and whose leaf never ran.

### Persist the terminal receipt while the mutation boundary is still held

For a claimed request, result serialization and the SQLite transition from `pending` to `completed` occur inside the same operation guard as the command leaf. A post-persistence acknowledgement hook sits after that transition, providing a deterministic test seam for timeout/cancellation without deleting the committed receipt. Recognized committed-cleanup failures keep their sanitized durable terminal marker. The canonical writer also marks its exact non-empty file-commit boundary; any unexpected later exception is converted into a durable `committed_uncertain` receipt instead of escaping as a pre-commit retryable error or deleting the receipt. If exact-result persistence itself fails, the first caller receives committed-uncertain only when that marker was crossed; validation/no-op results report acknowledgement-pending with `committed=null`. Both paths retain a fail-closed pending or uncertain receipt.

This closes the ordinary response-loss window. It cannot make a Markdown filesystem commit and a separate SQLite update atomic across process death; a stranded pending marker remains fail-closed and requires reconciliation.

### Use stable verified principal identity

Personal MCP retry scope will hash verified issuer plus stable subject claims when available, falling back to the current credential/session scope only when no verified principal exists. Hosted implicit scope will use trusted cell plus principal scope rather than request ID. Explicit keys are additionally namespaced by a caller principal scope supplied by authenticated surfaces.

Raw credentials, payloads, and public keys are never logged in identity fields. Public explicit keys are persisted only through their namespaced digest and are echoed only to their caller; the privacy-safe internal fingerprint is separately named `receipt_id`. Exact explicit terminal receipts have a 24-hour retention window, while implicit receipts retain the existing 60-second window. Pending and committed-uncertain receipts remain fail-closed for reconciliation rather than expiring into a possible duplicate.

### Correlate phases without exposing content

Each invocation will log one request-scoped canonical UUIDv4 plus a stable fingerprint of the internal idempotency identity at reservation, pending wait, replay, lock-busy, terminal persistence, and acknowledgement interruption. Middleware binds the UUID through the tool wrapper so direct-origin calls retain one cross-layer identity. Caller-supplied non-UUID log text is rejected and regenerated. Hosted redaction rules remain authoritative. Edge tests prove every mutation-capable POST and public transfer PUT reaches at most one origin.

### Keep required semantic/index work inside the boundary for now

Canonical note creation currently commits before optional corpus diagnostics finish, while transactional index/log fanout is part of the observable mutation. The lock therefore covers more than raw Markdown replacement. This change fixes retry ambiguity without weakening transaction consistency. Moving optional diagnostics out is a separate latency/design change because it changes result consistency and requires its own contract.

### Classify maintenance audit as read-only

`maintain_memory(mode="audit")` will use the common read-only classifier and bypass the hosted consistency guard. Audit is a diagnostic snapshot and may observe a changing corpus, but it cannot retain or contend for the mutation boundary. Other hosted reads keep the consistency guard so they observe pre- or post-transaction state.

## Risks / Trade-offs

- [The original request outlives the pending wait bound] → Return acknowledgement-pending with the same identity; a later same-key retry retrieves the terminal result once persisted.
- [A process dies after canonical commit but before local receipt persistence] → Leave the pending marker fail-closed and require reconciliation; do not automatically rerun or claim success.
- [A retry lands on another replica after failover] → The edge never fans out a mutation, but a later takeover cannot read the former replica's local receipt. Keep this limitation explicit and consider a shared/co-located receipt in a separate HA design.
- [Stable principal claims are absent] → Fall back narrowly to credential or session scope and log the scope kind, never the credential.
- [An intentional identical mutation occurs within a retention window] → Preserve the bounded 60-second implicit behavior and document the 24-hour explicit-key guarantee; a new identity is required for an intentional repeat.

## Migration Plan

No vault migration is required. The local SQLite schema remains compatible. Existing implicit receipts created from bearer scope remain reachable only through their original scope for at most 60 seconds across rollout; new receipts use stable issuer/subject scope. Pre-change CF Access REST receipts are in an unattributed shared namespace and cannot be safely assigned to post-change principals, so quiesce in-flight CF REST mutations for the deployment window; direct personal API-key receipts retain their legacy single-owner namespace. Deploy the code to both replicas, restart the services, verify the deployed Cloudflare worker retains single-origin behavior for MCP, REST, hosted mutation-capable POSTs, and public transfer PUTs, and keep existing pending records fail-closed. Rollback restores prior ordering without modifying vault content or deleting receipt state.

## Open Questions

- A cross-replica crash receipt requires a shared or transactionally co-located design and is deliberately deferred; it is not needed to close the observed live-worker acknowledgement-loss path.
