## Context

`reconcile.reconcile` runs these phases in order: planning (the census, only for
a preview or a planned write), purging, units, lifecycle, indexes, embeddings,
lexical, graph, orphans, references, derived receipts, drift, rebaseline and
receipts. `commands.op_reconcile` then runs a projections phase: the due-state
projection, the review-state compaction and the vocabulary review projection.
Each phase calls `maintenance_jobs.checkpoint`.

The CLI and a remote job both reach the leaf through
`writer_lease.invoke_command`. Before this change, it took the wide mutation
guard for the whole leaf, because reconcile had no narrowing row and was in
`WHOLE_RUN_MODES`. A remote job released that guard before the graph join that
follows the leaf.

Facts the design relies on, each checked in the code:

- The lexical store serializes its publication with a cross-process lock
  (`lexstore._publication_lock`).
- The graph has its own coordinator for publication (`holder_kind` "graph" in
  `epistemic_graph`). It builds in a registered flight and publishes under that
  coordinator. The graph phase also isolates lineage, recovers a checkpoint and
  sweeps temporaries, which move durable state.
- The reference rebuild scans the corpus, then replaces its table, under an
  in-process lock only. It therefore stays inside a hold.
- The receipts phase closes open intents and commits or aborts tombstones from
  the vault as it reads it. A governed write holds the boundary from its intent
  to its terminal, so the receipts phase must hold the boundary too. It runs
  last.
- The projections phase needs no hold. The due-state rebuild is a projection
  that ordinary writes also schedule outside the boundary. The review-state
  compaction and the vocabulary projection take the review-state coordinator.

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
warm. The profile decides which phases hold the boundary. A phase leaves the
boundary only when it is read-only, or when a coordinator already serializes it
against ordinary writes.

The profile (13,665 pages, models disabled, tasks 1.1) puts most of the old
hold in phases that need no boundary: lexical, projections, drift and the units
audit. The phases that must hold it take about 90 s of the 2,899 s warm hold.
The grouping is:

- No hold: the planning census, the posthoc evaluation, the units audit, the
  lifecycle inspection, the lexical refresh, the drift audit, the lexical
  lineage rebase and the projections. The lineage rebase serializes on the
  lexical publication barrier, as warm-up and the watcher seed call it.
- One short hold each: the activation manifest, the purge, the units repair,
  the lifecycle cleanup, the indexes, the embeddings refresh, the graph phase,
  the orphans repair, the reference rebuild, the derived receipts, the
  rebaseline and the receipts.

The grouping is code structure: each phase wraps its writes in `_phase_hold`.

### D2. Re-read inside the hold, then apply what is current

Each phase reads again inside its hold what it applies, or checks that what it
read before the hold has not moved:

- The purge reads the isolation census again inside its hold. A planned write
  applies only the entries that the plan and the fresh read share. It reports
  the other entries (`semantic_isolation_changed`) and marks the repair
  incomplete, so the isolation cursors do not advance.
- The orphans repair of a planned write does the same for CLIP orphans and frame
  directories, and reports the rest in `orphans_changed`. A run without a plan
  applies its fresh read.
- The units repair and the lifecycle cleanup compare each page with the bytes
  that the run read: a corpus page with the bytes that the corpus read, and a
  unit parent outside the corpus with the bytes recorded at the unit audit. A
  page whose bytes moved belongs to the write that moved it: the units repair
  leaves it and reports it (`semantic_unit_parents_moved`), and the lifecycle
  cleanup reports it as `LIFECYCLE_PRIMARY_RACE`. A parent outside the corpus
  that did not move loses its unit rows, as before this change.
- A parent outside the corpus whose unit rows carry the hash of its bytes at
  the unit audit was indexed for those bytes by a write that the corpus read
  missed. The units repair leaves it and reports it as moved.
- The indexes are counted inside their hold. The embeddings refresh and the
  orphans repair check each found file again inside their hold.

Three kinds of plan entry are not split against the plan. The extraction parts,
the lifecycle slots and the graph-quarantine members apply the fresh read inside
their hold, even for a planned write. This is safe for each:

- Parts: the sweep reads every part again inside the orphans hold. It removes
  only a part beyond the count that its Source page lists now. An extraction
  write that shortens a page retires the same surplus parts itself, so the
  sweep agrees with that write.
- Lifecycle slots: the cleanup takes a fresh identity census inside its hold
  and re-checks each slot's owners. The run also compares the owners' bytes with
  the corpus read. A slot whose owners changed is reported as a race and left.
- Graph-quarantine members: the isolation reads its members again under the
  graph coordinator, and only while the graph lineage is still unavailable. It
  moves them aside, with a manifest, into a reset directory that
  `graph_quarantine_id` names. It removes that directory only after a rebuilt
  graph is current, so the members stay recoverable until then.

Run-long values that a later phase reads go stale once the boundary is released
between phases. `semantic_states` is used only for parents whose bytes did not
move, so it still describes them. Each sidecar also re-parses a supplied state
whose hash differs from the committed bytes
(`semantic_index.current_parent_index_state`), so a stale state cannot write old
units over an edit. `suppressed_paths` and `missing_paths` come
from the census read inside the purge hold. `missing_clip_orphans` is checked
again against the disk inside the orphans hold. The unit inclusion sets only
scope what the units audit compares, and `initial_graph_drift` only decides
whether the graph phase looks again: that phase reads the drift again inside its
hold before it repairs.

Alternative: skip a whole phase when any input moved. Rejected, because a busy
vault would never converge, and a job needs a new preview to run again.

### D3. One narrowing rule for the CLI and the job

Reconcile gets a narrowing row in `writer_lease`, as a chunked job has. The
`EXOMEM_WIDE_MUTATION_BOUNDARY` switch widens it again, and each phase hold then
re-enters the whole-run hold. Both the CLI and the job run the leaf without a
whole-run hold. `WHOLE_RUN_MODES` and its dispatch retry are gone. The graph
join and handoff composition in `invoke_command` still find no hold held at the
join.

### D4. Bounded waits inside the run

Each hold waits out a busy boundary for a bounded time and checks for a stop
between attempts, through `maintenance_jobs.chunk_hold`. A busy boundary in the
middle of a run no longer fails a half-finished job. A stop lands between holds,
never inside a hold's write.

### D5. One reconcile write run at a time

The whole-run hold also kept two reconciles from interleaving their phases. A
write run now holds a vault-scoped run lock for its whole run, and no ordinary
writer takes that lock. A second write run refuses at once with
`MUTATION_BUSY` and `committed: false`, before it reads or writes anything. It
does not wait, because the running reconcile can be a job parked at a phase
hold's foreground yield. A foreground request that waited for the lock would
wait on a parked bulk pass, outside `foreground_priority.awaiting_bulk()`. A
preview takes no run lock.

The lock is a file lock in the vault's private state, as the one archive
expansion per vault holds. It is not a mutation coordinator: a thread that holds
a coordinator counts as holding a boundary, so its foreground yields would
return at once.

## Risks / Trade-offs

- A foreground write between holds can change what a later phase reads. → D2
  re-reads inside each hold.
- More holds cost more total wall time. → The reconcile pays it, not
  foreground writers. The profile records the before and after.
- A file that comes back outside the corpus after the corpus read, over unit
  rows of older bytes, loses its derived rows in the units repair, as before
  this change. A restore copied in from a backup does this. → Its next write
  or the next reconcile indexes it again.
- A hold that waits out its busy budget fails the run with `MUTATION_BUSY`. The
  phases before it keep their effect. The CLI does not run it again, because one
  attempt outlasts the CLI's wait budget. → Run the reconcile again.
- A phase that repairs many pages holds the boundary for that whole repair: the
  units repair, the embeddings refresh and the orphans repair, which can rebuild
  extraction parts. → The profile puts each well under the old hold. Chunking
  them is follow-up work.
- The parts sweep walks the whole knowledge base inside the orphans hold. A
  planned write applies the sweep's fresh read, not the parts that its plan
  names. → A sweep scoped to the planned parts is follow-up work.
- A caller that absorbs contention, such as the CLI or a keyed request,
  retries the run lock's refusal until its wait budget ends (40 s by default).
  In the service, that retry slows a running reconcile job to its progress
  floor meanwhile. → A `maintain_memory` reconcile write from a request
  surface runs only as a job, and a job thread is not a foreground request.
