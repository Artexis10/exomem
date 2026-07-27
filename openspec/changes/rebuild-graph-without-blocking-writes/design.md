## Context

`EpistemicGraphIndex.refresh_paths` takes the vault mutation boundary and calls
`_refresh_paths_locked`, which escalates to `_rebuild_all_locked()` whenever
`available()` is false. `_rebuild_all_locked` runs a stabilization loop:
snapshot `_disk_vault_freshness`, run `_rebuild_all_pass`, and accept the result
only if freshness is unchanged; otherwise retry, up to
`REBUILD_STABILIZATION_ATTEMPTS`, then fail and mark the graph unavailable.

Two properties of the current implementation matter here.

`_rebuild_all_pass` mutates the **live** sidecar: it deletes every row from
`graph_edges`, `graph_nodes`, and `graph_parent_refs` before refilling. A reader
arriving mid-pass would see an empty or partial graph. That is safe today only
because the boundary excludes everyone for the whole rebuild.

The stabilization loop is therefore currently a guard against *out-of-band*
edits — a user editing files directly in Obsidian — rather than against
concurrent Exomem writes, which the boundary already prevents.

Moving the rebuild outside the boundary changes the status of both: the wipe
becomes visible, and the stabilization loop becomes the primary consistency
mechanism rather than a backstop.

## Goals / Non-Goals

**Goals:**

- A full rebuild must not block unrelated vault mutations for its duration.
- A reader must never observe a partially rebuilt graph.
- Preserve the write-path contract: a write against an unusable sidecar returns
  with the graph built and available.
- Preserve the failure contract: a rebuild that cannot observe a stable vault
  marks the graph unavailable rather than publishing a bad one.

**Non-Goals:**

- Changing the sidecar schema or file format.
- Making incremental (`available()`-true) refresh faster.
- Removing the escalation itself. #346 established that it is load-bearing.
- Reducing how often the sidecar is invalidated. Fewer `SCHEMA_VERSION` bumps or
  a registry-hash change that does not invalidate would both reduce exposure,
  but they are separate questions from how a rebuild behaves.

## Decisions

Build into a temporary database in the sidecar's own directory, then swap. Same
directory so the publish step is an atomic rename on one filesystem. The rebuild
passes run against the temp database with no boundary held; only the swap takes
it. The boundary hold drops from the length of a full rebuild — 32 s at 2,000
pages, 172 s at 8,000 — to a rename.

Re-verify freshness under the boundary immediately before the swap. Outside the
boundary the vault can change during the final pass, so the pre-swap check is
what makes the published graph trustworthy. If freshness moved, the rebuild
retries within its existing attempt budget rather than publishing.

Make rebuilds single-flight per vault. Today the boundary serialises them
implicitly: the second writer blocks, and by the time it runs the graph is
available so it takes the incremental path. Once rebuilds no longer hold the
boundary, N concurrent writers would otherwise each start a full rebuild. A
rebuild-in-progress marker keeps one running and lets the others wait for its
result.

Keep `_mark_unavailable()` on stabilization failure. The failure contract is
unchanged; only the location of the work moves.

Sweep abandoned temp databases in `reconcile`. A crash mid-rebuild leaves a temp
file that no longer has an owner. Reconcile already walks sidecar state and
already owns `rebuild_all`, so it is the natural place. Temp databases use a
reserved name prefix so a sweep cannot mistake one for the live sidecar.

## Risks / Trade-offs

- [The stabilization budget may be too small outside the boundary] → Concurrent
  Exomem writes can now perturb freshness mid-pass, where previously only
  out-of-band edits could. If `REBUILD_STABILIZATION_ATTEMPTS` proves
  insufficient under sustained writes, a rebuild that used to succeed slowly
  would fail instead. Covered by a test that writes continuously during a
  rebuild; the attempt budget may need raising with the sweep.
- [Disk usage doubles transiently during a rebuild] → One extra sidecar-sized
  file for the duration. Acceptable, and bounded by the sweep.
- [A waiting writer still waits] → Single-flight means a concurrent writer that
  needs the graph still waits for the in-flight rebuild. It no longer blocks
  *unrelated* mutations, which is the reported harm, but the first writer after
  invalidation still pays the rebuild cost. Reducing that is a separate problem.
- [Atomic rename semantics on Windows] → Replacing an open SQLite file differs
  from POSIX. The swap must tolerate readers holding the old file, and the
  implementation must verify behaviour on the Windows service path rather than
  assuming POSIX rename.

## Migration Plan

No data migration. The first rebuild after this change writes a temp database
and swaps it; existing sidecars are read normally until invalidated. Roll back
by restoring the in-boundary rebuild, which changes no on-disk format.

## Open Questions

- What should a writer do while a rebuild is in flight — block on the
  single-flight result, or return deferred and let the rebuild cover its path?
  Deferring keeps the write bounded but means the writer's own edit may not be
  in the published graph, which the current contract does not allow.
- Does `REBUILD_STABILIZATION_ATTEMPTS` need to rise now that concurrent Exomem
  writes can perturb freshness, and what is the right ceiling before failing?
