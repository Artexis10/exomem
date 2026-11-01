## Context

`reconcile.reconcile` runs these phases in order: planning (the census, only for
a preview or a planned write), purging, units, lifecycle, indexes, embeddings,
lexical, graph, orphans, references, derived receipts, drift, rebaseline and
receipts. Each phase calls `maintenance_jobs.checkpoint`.

The CLI and a remote job both reach the leaf through
`writer_lease.invoke_command`. It takes the wide mutation guard for the whole
leaf, because reconcile has no narrowing row and is in `WHOLE_RUN_MODES`. A
remote job releases that guard before the graph join that follows the leaf.

Facts the design relies on:

- The lexical store serializes its publication with a cross-process lock.
- The graph has no coordinator of its own. It builds privately and takes short
  canonical holds to publish. The graph phase also isolates lineage, recovers a
  checkpoint and sweeps temporaries, which move durable state.
- The reference rebuild scans the corpus, then replaces its table, under an
  in-process lock only. It therefore stays inside a hold.
- The receipts phase repairs the receipt chain after the derived phases, under
  its own lock, and depends on running last.

## Goals / Non-Goals

**Goals:**

- No foreground writer waits for a whole reconcile.
- Nothing applies on inputs that moved since they were read.

**Non-Goals:**

- Making derived rebuilds faster. `bound-reconcile-maintenance` covers graph
  cost.
- Changing the reconcile plan identity or the preview.

## Decisions

### D1. Profile before grouping

A disposable copy of a real-sized vault gives each phase's wall time, cold and
warm. The profile decides which phases share a hold. A phase leaves the
boundary only when a coordinator already serializes it against ordinary writes.
If most of the time sits in phases that must stay inside a hold, this change
stops at the profile and reports that.

### D2. Re-read inside the hold, then apply what is current

Most phases already read their inputs and apply them in one pass, so they
re-read inside their own hold. The purge is the exception: it applies the
planned isolation census. Inside its hold, it reads the census again and
applies only the entries present in both. It reports the other entries and
marks the repair incomplete, so the isolation cursors do not advance.

Run-long values cross phases and go stale once the boundary is released between
them: `semantic_states`, `suppressed_paths`, the unit inclusion sets and
`initial_graph_drift`. Each phase that reads one of them re-reads it inside its
own hold.

Alternative: skip a whole phase when any input moved. Rejected, because a busy
vault would never converge, and a job needs a new preview to run again.

### D3. One narrowing rule for the CLI and the job

Reconcile gets a narrowing row in `writer_lease`, as a chunked job has. The
`EXOMEM_WIDE_MUTATION_BOUNDARY` switch widens it again. Both the CLI and the job
then run the leaf without a whole-run hold, and reconcile leaves
`WHOLE_RUN_MODES`. The graph join and handoff composition in `invoke_command`
must still find no hold held at the join.

### D4. Bounded waits inside the run

Each hold waits out a busy boundary for a bounded time and checks for a stop
between attempts, as `chunk_hold` does. A busy boundary in the middle of a run
no longer fails a half-finished job.

## Risks / Trade-offs

- A foreground write between holds can change what a later phase reads. → D2
  re-reads inside each hold.
- More holds cost more total wall time. → The reconcile pays it, not
  foreground writers. The profile records the before and after.
