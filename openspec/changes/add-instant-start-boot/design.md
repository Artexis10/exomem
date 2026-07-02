# Design — instant-start boot

## Context

`build_server()` (`src/exomem/server.py`) currently preloads `embeddings.get_model()`,
`embeddings.get_reranker()`, and (when enabled) `embeddings.get_clip_model()` inline, then calls
`warmup.warm_caches(vault_root)` for the lexical caches (parsed pages, BM25 both scopes, wikilink
resolver, embedding/CLIP matrices) — all before `mcp.run()` is reached. Every step already
soft-fails and logs a warning, but the failure mode this change targets isn't exceptions, it's
wall-clock: `get_model()` / `get_reranker()` / `get_clip_model()` are lazy singletons behind
double-checked locks (`_MODEL_LOCK`, `_RERANKER_LOCK`, `_CLIP_LOCK` in `embeddings.py`), and the
whole boot sequence runs before the transport accepts a single request. A stdio client executes
its MCP `initialize` handshake against a process that hasn't started listening yet; a first-ever
run pays HF download time (minutes) in that same window.

## Decisions

### Events, not locks: request paths must check readiness, not rely on try/except

The three model getters already have a correct, safe concurrency primitive for the *load* itself —
double-checked locking. The problem this change solves is upstream of that: a request thread that
calls `get_model()` while the warm thread is inside the `with _MODEL_LOCK:` block blocks on lock
*acquisition* for the full remaining load time. That block is synchronous Python — there is no
exception to catch, no `ImportError` to soft-fail on, nothing for the existing
`except Exception: log.warning(...); fall back` pattern in `_find_semantic` to observe. The
request simply hangs until the warm thread releases the lock.

`readiness.should_defer(component)` is therefore checked *before* the lane touches a model getter,
not around it. It answers a different question than the lock does: not "is the model loaded yet"
(that's what the lock protects) but "is it safe to ask for the model without paying an
indeterminate wait." The event-based check is non-blocking by construction — reading a
`threading.Event.is_set()` under `readiness._lock` never itself waits on the model load.

The narrow semantics matter here: `should_defer` is true *only* while `_warm_active and not
_warm_finished and not event.is_set()`. The moment `finish_warm()` runs — success or failure — it
is permanently false. A component whose preload failed (network down, disk full, HF outage) never
gets its event set, but once the warm window closes, `should_defer` stops gating it and the lane
falls through to today's inline lazy-load, which pays the load cost once and then soft-fails via
the existing `except ImportError` / `except Exception` handling exactly as it does now. No new
failure mode is introduced; the defer gate only exists *during* the warm window, where the
existing exception-based soft-fail cannot see the hazard.

### Nothing warms synchronously: one code path serves both transports

The alternative of warming lexical caches synchronously and only backgrounding the model preloads
was considered and rejected: a stdio client still runs its `initialize` handshake against
`build_server()`'s return value, so *any* synchronous work in that function — even the ~1-3s
lexical warm — is still dead air the client has to wait through, and on a slow disk or a large
vault that number isn't bounded the way "a few seconds" implies. More importantly, splitting sync
lexical work from async model work means two boot code paths to keep correct and tested (a
lexical-sync branch for `build_server`, an all-background branch inside `warmup`), doubling the
surface for a boot-ordering bug. Putting the *entire* warm sequence — lexical first, models after —
on one background thread means `build_server()` itself does zero blocking work beyond schema/env
setup, `mcp.run()` is reached immediately regardless of transport, and there is exactly one
ordering to test (`warm_all`'s stage sequence), not two.

The trade this preserves rather than removes: a cold `find` issued the instant the process starts
(warm-up disabled, or a request racing a still-open warm window with no readiness protection)
already pays the identical inline BM25/resolver build cost it pays today whenever
`EXOMEM_DISABLE_WARMUP` is set — that path is unchanged and already covered by existing tests. This
change adds a background thread that races to finish that same work before a request needs it, and
a readiness layer so a request that loses the race degrades instead of blocking.

### Rejected alternatives

- **Block hybrid `find` until warm-up finishes.** Simplest to reason about, but a first-ever
  download is minutes — indistinguishable from a hung server to the caller. Rejected: reintroduces
  exactly the dead-air problem this change exists to remove, just moved from boot to first-query.
- **Silent degrade (serve lexical-only results with no marker).** Removes the lock-blocking hazard
  but leaves the agent unable to tell "this is the full hybrid recall" from "this is BM25-only
  because models aren't warm yet" — a caller doing a low-recall query during the ~30s window would
  silently get worse results with no signal to retry or widen. Rejected in favor of the `warming`
  marker, which is measurement (what lanes ran), not judgment.
- **Warm lexical caches synchronously, background only the models.** Discussed above — still risks
  an `initialize` timeout on slow disks/large vaults, and creates two boot code paths (sync
  lexical + async models) instead of one (`warm_all` on a background thread). Rejected for the same
  reason as full synchronous boot: the goal is a single, fully-tested ordering.

## Deferred-write recovery

A write that lands during the warm window has its re-embed work item appended to
`readiness._deferred["embeddings"]` (in-memory only) instead of being embedded inline. If the
process dies before `warm_all` reaches `mark_ready("embeddings")` and drains that list, the item is
lost from memory — but the markdown file itself was already written with a fresh mtime, and its
sidecar row was not updated, which is exactly the `embedding_drift` condition `audit` already
detects (on-disk mtime newer than the sidecar row) and `reconcile` already heals. No new recovery
path is needed; the existing audit/reconcile pair is the safety net for a crash mid-drain.

## Risks / Trade-offs

- Double-checked build locks in `BM25Index._fresh_corpus` and `find._get_query_resolver` add lock
  acquisition to every call. Kept cheap: the fast path (already-fresh cache) only needs the lock
  around the freshness-key comparison, matching the existing `get_model`-style pattern, so a
  warmed, unchanging corpus pays one uncontended lock per call, not a rebuild.
- A request that defers a lane and then finds the component still not ready on retry has no
  built-in backoff — callers see `warming` and choose whether to retry. This is a query-time UX
  decision, not a server-side one, consistent with `find`'s existing opt-in diagnostics pattern.
- `EXOMEM_EAGER_BOOT=1` exists specifically so a deploy that hits an unexpected background-warm
  regression can roll back to the old fully-synchronous boot without a code revert.

## Migration Plan

No data migration. `EXOMEM_DISABLE_WARMUP` continues to mean "no warm at all, fully lazy" exactly
as today. Existing deploys pick up background warm-up automatically; `EXOMEM_EAGER_BOOT=1` restores
the previous blocking boot as an explicit opt-in rollback lever, not a default.
