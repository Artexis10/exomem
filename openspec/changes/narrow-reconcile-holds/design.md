## Context

`reconcile.reconcile` runs these phases in order: planning (the census),
purging, units, lifecycle, indexes, embeddings, lexical, graph, orphans,
references, derived receipts, drift and rebaseline. Each phase already calls
`maintenance_jobs.checkpoint`. The CLI and a remote job both reach the leaf
through `writer_lease.invoke_command`, which takes the wide mutation guard for
the whole leaf. A remote job releases that guard before the graph join that
follows the leaf.

The measured holds (830 s cold, 1,005 s warm) cover the whole run. No per-phase
measurement exists yet, so the design does not assume which phase dominates.

## Goals / Non-Goals

**Goals:**

- No foreground writer waits for the whole reconcile. A writer waits at most for
  the current canonical phase.
- A canonical phase never applies on inputs that moved since the census.
- One census per run.

**Non-Goals:**

- Making derived rebuilds faster. `bound-reconcile-maintenance` covers graph
  cost.
- Changing the reconcile plan identity or the preview.

## Decisions

### D1. Measure each phase's hold before grouping phases

Profile one cold and one warm write-mode reconcile on a disposable copy of a
real-sized vault, and record each phase's wall time. The phases that write
canonical state get their own hold. A derived phase moves outside the boundary
only if its coordinator already serializes it against ordinary writes.

Alternative: split every phase without measuring first. Rejected, because a
phase that is cheap and canonical gains nothing from its own hold.

### D2. Re-check inputs inside each canonical hold

Each canonical phase records the census inputs that it acts on, for example the
indexed paths for the purges and the page hashes for the index counts. Inside
its hold, the phase compares those inputs with disk. When they differ, it skips
the phase and records the skip in the report and the job receipt. The next
reconcile converges the skipped phase.

Alternative: re-plan the whole run when any input moves. Rejected, because a
busy vault would never finish a reconcile.

### D3. Keep the job's plan binding

The job still re-plans before its first write and fails with
`STALE_MAINTENANCE_PLAN` when the destructive census changed. D2 applies after
that check, to inputs that move during the run.

## Risks / Trade-offs

- A phase that reads derived state another phase wrote may see a foreground
  write in between. → D2's input check covers canonical inputs; derived phases
  converge on the next run, as they do after any ordinary write.
- More holds cost more total wall time. → The cost falls on the reconcile, not
  on foreground writers. The phase profile records the before and after.
