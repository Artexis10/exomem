## Why

Interactive Exomem writes are already canonically durable long before the caller
gets an answer, but the request still waits for derived-index fanout and
best-effort advisory work. On the live cell, 101 successful write calls measured
26.1 s p50 and 107.4 s p90; `index.upsert_after_write` alone measured 9.6 s p50
and 51.0 s p90, while one representative post-commit advisory added 6.76 s.
That turns healthy committed writes into apparent hangs and risks losing the
acknowledgement at the edge after the bytes are already safe.

## What Changes

- Introduce an exact, machine-local `DerivedBatchReceipt` prepared before the
  canonical replacement. It binds the mutation attempt, canonical generation,
  affected paths, before/after hashes or tombstones, and required derived
  components. After a crash it self-activates only when canonical state proves
  the intended commit; a rolled-back or superseded receipt cannot publish stale
  work.
- Split governed semantic writes into a durable acknowledgement boundary and
  derived convergence. Semantic/governance validation, writer authority,
  fencing, path guards, canonical Markdown and auxiliary files, graph epoch
  custody, the prepared derived receipt, and the exact mutation terminal remain
  synchronous and fail closed. Vector, graph, claim, and default write-advisory
  computation move behind durable component custody.
- Give the whole post-canonical request one shared, hard, non-configurable 2.0 s
  budget. The fast path returns immediately when no work is pending; it MUST NOT
  sleep out the budget. A deadline alone yields a visible pending outcome, while
  a real registration, publication, or proof failure remains a failure and is
  never laundered into deferral.
- Preserve read-your-write: direct reads return the committed bytes immediately;
  keyword and hybrid recall merge an exact bounded pending delta and suppress
  stale pre-write rows until durable lexical publication catches up. Vector and
  graph participation may be pending, and that limitation is disclosed.
- Make the default near-duplicate/overlap advisory durable background work and
  return a stable opaque result reference. Exact lookup reports pending, ready,
  failed, or superseded without enrolling the result in attention, due-state,
  bootstrap, or an unrelated later response. Reuse the page vectors produced by
  background embedding instead of encoding the same page twice.
  `suggestions=true` remains the explicit enriched, potentially slow synchronous
  opt-in.
- Drain component receipts promptly, fairly, and in bounded batches on the
  running server, with restart recovery and periodic reconciliation as
  backstops. New work MUST NOT revive whole-vault full receipts when exact
  per-component custody already covers it.
- Extend timing and correctness gates across the complete public mutation and
  the immediately following read, including cold/recovery conditions, so moving
  work outside the mutation boundary cannot masquerade as a speedup.

This change depends on completing the already-planned
`bound-corpus-context-flight-join` fast-path-safe 2.0 s bound and reconciling the
near-closure deferred-work and graph-recovery changes. It does not absorb or
redefine those changes.

## Capabilities

### New Capabilities

- `durable-write-acknowledgement`: defines the acknowledgement cut, exact derived
  work custody, one shared post-canonical budget, background advisory semantics,
  recovery, and end-to-end latency evidence.

### Modified Capabilities

- `hosted-mutation-safety`: mutation authority must prepare exact derived-work
  custody without exposing uncommitted bytes or extending canonical authority
  across derived work.
- `transactional-vault-writes`: caught rollback, process death, superseding
  writes, and the canonical/sidecar crash cut gain explicit prepared-receipt
  semantics.
- `mutation-terminal-contract`: compact committed terminals expose bounded
  non-graph component convergence plus advisory state/reference, including a
  visible pending outcome; the existing `graph_sync` field remains authoritative
  for graph convergence.
- `live-index-freshness`: exact component receipts become the normal handoff for
  expensive writer fanout and must converge through bounded server-owned drains.
- `recall-read-path`: immediate direct and lexical read-your-write remains exact
  while vector or graph projections are still pending.
- `command-surface`: `review_memory(mode="write-advisory-result", ref=...)`
  resolves exactly one opaque result and has no listing form.
- `attention-queue`: deferred advisory jobs and results remain outside every
  queue, category, count, rank, and carrier; ready advisory references keep the
  existing triage behavior.
- `release-gate`: exact advisory-result retrieval reauthorizes every candidate
  and makes withheld candidates indistinguishable from absence.

## Impact

- `src/exomem/vault.py`, `writer_lease.py`, `mutation_terminal.py`: prepared
  receipt lifecycle, acknowledgement cut, terminal projection, and crash cuts.
- `src/exomem/deferred_index.py`, `index_sync.py`, `file_watcher.py`,
  `graph_drain.py`: per-component custody, bounded dispatch, fair restart-safe
  draining, and exact completion CAS.
- `src/exomem/freshness.py`, `find.py`, `lexstore.py`, `memory_refs.py`: bounded
  pending-delta recall and stale-row suppression.
- `src/exomem/note.py`, `corpus_aware.py`, `embeddings.py`: background advisory
  handoff, exact result lookup, and one-pass vector reuse; no new model and no
  server-side reasoning. Existing frozen embedding models continue only to
  rank/measure and remain optional and soft-fail.
- Call-ledger/metrics and `scripts/semantic_write_latency.py`: full-operation,
  acknowledgement, pending-age, convergence, and read-after-write evidence.
- No change to semantic acceptance rules, canonical Markdown schemas, retrieval
  ranking policy, or explicit reconcile/index operations that promise
  convergence.
