## Context

Exomem has two coordination layers with different jobs. A strongly consistent writer lease selects one writable replica when the same vault is available on multiple machines; a process-safe OS lock serializes the service, media worker, transfer path, and other writers on that selected host. Hosted deployments still use the same per-cell runtime and therefore keep the second layer even though tenant vaults are physically isolated.

The observed failure is at the local process boundary. The OS lock can be owned by another process while `coordination_status` reads only process-local memory and reports `free`. A waiting command then raises a correctly retryable `MUTATION_BUSY`, but the MCP adapter re-raises the public `OpError`; FastMCP marks it as a failed tool execution and the client proxy can recast it as `INVALID_ARGUMENT`. The exact “tool has been disabled” sentence is host-generated rather than an Exomem response, but protocol-level failure is the server-side trigger we can remove.

Two independent contract gaps amplify the problem: `replace_memory(validate_only=true)` is not classified read-only, and `LeaseConfig.mutation_timeout_seconds` is parsed but not used by the default manager. Separately, rich semantic blocks accept and preserve `tags` and `context` in generic metadata while their first-class fields are hardcoded empty, so downstream filters and projections cannot use authored values.

## Goals / Non-Goals

**Goals:**

- Make expected operational refusals safe for MCP clients without disguising unexpected server faults.
- Make local cross-process lock status authoritative, content-free, crash-safe, and useful in busy diagnostics.
- Apply the configured wait bound and keep every validation-only replacement outside mutation authority.
- Give rich units the same first-class tag/context projection as compact units through every retrieval and graph surface.
- Keep media hashing, bounded scans, and derived fanout outside the global mutation boundary, with writer-fenced per-artifact commits.
- Preserve idempotent retry identity, single-writer fencing, tenant isolation, and existing public tool schemas.

**Non-Goals:**

- Replace the remote writer lease with a synced file lock or use Syncthing as a coordinator.
- Add a durable FIFO mutation service, change background-worker priority, or redesign every derived-index critical section; this repair narrows the measured media discovery, public process/retry, startup, and pure re-embed paths only.
- Suppress unexpected exceptions, authorization failures outside the public operation contract, or true transport/protocol failures.
- Add a server-side reasoning model, confidence score, or inferred semantic relation.

## Decisions

### Treat public operation errors as application outcomes at the MCP boundary

The generated MCP wrapper will convert deliberate public `OpError` instances into the shared `{success:false,error:{...}}` envelope. FastMCP therefore returns normal tool content rather than `isError:true`. The full public details remain intact, including `status`, `committed`, `retry_after_ms`, `request_id`, and `receipt_id` for mutation coordination. Semantic-contract refusals continue using the same application-error plane. Exceptions that are not a deliberate public operation error remain native MCP execution errors. Replay safety remains scoped to the existing effective identity: explicit idempotency identity where supported, otherwise the same principal/bearer/session scope plus canonical command digest in the same idempotency store. A receipt is diagnostic, not a transferable cross-session key.

This is preferred over retrying inside the adapter: an internal retry can consume the edge deadline, obscure caller-controlled idempotency, and still fail behind a long background mutation. It is also preferred over flattening every exception, which would make real defects look like ordinary user-correctable outcomes.

### Bind content-free holder metadata with a two-lock handshake

Each vault coordinator will pair its existing mutation lock with a metadata-mutex lock and a small JSON holder sidecar under the same local runtime-state directory. Acquisition takes the metadata mutex, probes/acquires the mutation lock without blocking, publishes only safe diagnostics while still holding the mutex, and then releases the mutex. Waiting never blocks on the mutation lock while holding the mutex. Release takes the metadata mutex while still owning the mutation lock, clears the holder, releases the mutation lock, and finally releases the mutex.

Status for a concrete vault takes the metadata mutex before probing the mutation lock. If the mutation probe succeeds, it clears stale metadata while both locks remain held, then releases in mutation-lock/mutex order. If the probe fails, the current owner cannot be between acquisition and publication or between cleanup and unlock, so metadata read under the mutex is bound to that ownership generation. Missing or malformed metadata yields `held` with an explicit unknown/unverified external holder rather than false `free`. If the metadata mutex itself cannot be measured within its short bound, status reports contended/unknown rather than guessing free.

This extra mutex is preferred over an uncoordinated sidecar. A lone sidecar cannot distinguish a crashed holder's JSON from a new owner paused before publication and can delete a new owner's metadata during stale cleanup. The handshake has one lock order, never waits on the mutation lock while holding the mutex, and is covered by paused acquire-to-publish and probe-to-cleanup interleaving tests.

The user-facing `coordination_status(vault_root)` path will measure that exact vault. Process-wide readiness may reuse the configured server vault when available; it must not claim an exact boundary from unrelated process-local state. Remote replica ownership remains a separate writer-lease field: local OS metadata is not synced between machines and is not a replacement for the lease coordinator.

### Use configuration as the manager default while retaining explicit test overrides

`LeaseManager` will take an optional mutation-timeout override. When omitted, it uses `LeaseConfig.mutation_timeout_seconds`; focused tests can still pass zero or a short bound explicitly. This makes `EXOMEM_MUTATION_TIMEOUT` operational without changing its current validation or default.

### Generalize validation-only classification to replacement

`replace_memory(validate_only=true)` will join the existing read-only preview paths. A preview is explicitly advisory and may observe a weak snapshot while a multi-file mutation commits; it is never represented as a committed or current vault state. Its draft identity binds the exact reviewed draft and predecessor hash, and the eventual commit remains a separate mutation that revalidates the predecessor, current corpus-dependent semantic checks, and writer authority under the process-safe boundary. This matches the established remember/edit preview contract and avoids idempotency receipts for a non-write without pretending the preview is a transactionally consistent read.

### Normalize rich tags and context once at parse time

The unified semantic parser will derive first-class rich `tags` and `context` from the already parsed leading metadata. Tags are comma-delimited plain tokens: trim each entry, reject empty entries and `#` prefixes, apply NFKC plus casefold, require 1–64 characters beginning with an alphanumeric and containing only alphanumerics, `_`, `-`, or `/`, reject trailing `/` and `//`, and de-duplicate after normalization while preserving first order. Context is trimmed, non-empty, single-line Unicode and otherwise preserved. Invalid fields keep the existing parsed generic metadata mapping for compatibility and diagnostics, emit stable validation findings, and project no partial first-class value for that field.

Indexes, structured filters, full hits, exact reads, graph nodes, and context packs consume the normalized fields from `SemanticUnit`. The parser-generation identity and any sidecar schema/cache identity that does not already incorporate it will be bumped so unchanged Markdown is recognized as stale. After upgrade, the operator quiesces mutations and explicitly runs `maintain_memory(mode="reconcile")`; that pass rebuilds lexical, vector (when enabled), and graph projections without rewriting Markdown bytes or mtimes. Startup alone does not promise this rebuild.

Kind and category stay distinct: a rich heading determines governed `kind`; explicit category metadata supplies authored category identity, and an omitted category retains the existing heading-derived raw/key default before reviewed category-alias resolution. Typed relations remain authored-only governed edges.

### Split media planning from per-artifact commit authority

Watcher, startup, and explicit `process_media(process|retry)` paths classify, scan, and hash media before acquiring the process-safe mutation boundary. Each artifact then enters a named writer-fenced commit guard only long enough to revalidate binary identity, live access policy, sidecar confinement, and exact sidecar content before changing the sidecar or durable job. Pathless process/retry routes pass the same guard factory to each artifact instead of wrapping the whole bounded scan. `process_media` remains mutation-classified and idempotent; its lease-level outer scope validates writer authority without taking the vault lock, while the canonical commit guards retain the active request identity and fencing token.

Reconciliation, transcript, and failure sidecar writes suppress their immediate derived fanout inside the commit guard and dispatch fanout after release; reconciliation also deduplicates multiple writes to one sidecar. Deferred-index draining and pure `_run_reembed` work remain outside the global boundary. Bounded CLIP-vector commits and scene-frame persistence retain named background guards; further narrowing those paths is a measured follow-up rather than an indexing-architecture rewrite.

## Risks / Trade-offs

- **[Risk] Holder metadata survives a process crash.** → The metadata-mutex handshake clears stale state while holding a successful mutation-lock probe, before either lock is released.
- **[Risk] A process pauses between lock acquisition and holder publication.** → Acquisition keeps the metadata mutex through publication, so status cannot attribute the previous generation.
- **[Risk] Returning public errors as normal MCP content could be mistaken for success by a naive caller.** → Keep explicit top-level `success:false`, stable error codes, mutation status, and contract tests; successful results remain unchanged.
- **[Risk] A longer configured wait approaches the edge request deadline.** → Preserve the bounded validated setting and current default; no automatic widening or adapter-level retry is introduced.
- **[Risk] Rich metadata normalization changes previously empty projected fields.** → This is the intended restorative behavior; preserve the existing parsed metadata mapping and add parse, filter, index, exact-read, graph, and context-pack regression coverage.
- **[Risk] Existing sidecars trust old empty projections for unchanged files.** → Bump parser/generation and required sidecar identities and verify rebuild from a pre-change database without touching Markdown mtimes.
- **[Risk] Media or access policy changes while provenance hashing runs outside the lock.** → Revalidate binary identity, access tier, sidecar confinement, and exact sidecar bytes inside each named commit guard before mutation.

## Migration Plan

1. Ship the parser/projection and runtime behavior in one backward-compatible release with unchanged MCP input schemas.
2. After each upgraded vault is quiesced, explicitly run `maintain_memory(mode="reconcile")` to rebuild stale lexical, enabled vector, and graph projections from unchanged Markdown; do not rely on service restart for this migration.
3. Deploy the same release to every self-hosted replica; update the idle follower first, then the active writer, leaving the remote lease coordinator in place.
4. Deploy hosted cells from the same image. Physical tenant isolation prevents cross-tenant contention but does not remove service/background-worker contention inside one cell.
5. Refresh the configured ChatGPT/Codex app only if its previously registered tool-surface digest is stale for unrelated schema changes, then verify busy-as-application-result and a subsequent read from a fresh conversation.
6. Roll back the package if needed; holder sidecars are disposable runtime state and require no vault-data migration.

## Open Questions

None for this repair. Fair queuing and critical-section reduction remain a separately measured follow-up if holder telemetry shows sustained contention after these fixes.
