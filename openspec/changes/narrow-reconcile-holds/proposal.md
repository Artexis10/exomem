## Why

A reconcile holds the vault mutation boundary for its whole run. On a disposable
copy of a vault with about 12,000 Markdown pages, the CLI held the boundary for
830 s on a cold run and 1,005 s on a warm one. During that time every other
writer, local or remote, gets `MUTATION_BUSY`.

`add-remote-maintenance-jobs` lets a remote client start a reconcile as a
background job. That makes a long reconcile routine. The job names itself in the
busy details, but a client still cannot write for a quarter of an hour.

Much of the run may not need the canonical boundary. Embeddings, the lexical
sidecar, the epistemic graph and the stable-reference sidecar are derived state,
and the graph and lexical stores already have their own coordinators. The
canonical writes are smaller: the isolation purges, the index counts,
prepared-lifecycle cleanup and orphan healing. A per-phase profile decides the
split.

## What Changes

- Reconcile takes the mutation boundary once per phase that writes canonical
  state. It releases the boundary between phases, so a foreground write can run
  between them.
- Each canonical phase re-checks its inputs inside its own hold. A phase whose
  inputs moved since the census is skipped and reported, and is never applied
  on stale inputs.
- Derived phases run outside the canonical boundary, under their own
  coordinators, as the write fan-out already does for ordinary writes.
- Reconcile computes its census once per run. Today a write-mode run computes it
  twice.
- The local CLI and the remote job share this behaviour, because both run the
  same leaf.

## Capabilities

### Modified Capabilities

- `maintenance-jobs`: a reconcile job releases the boundary between phases
  instead of holding it for the whole run.

## Impact

- Code: `src/exomem/reconcile.py`, and the job dispatch in
  `src/exomem/maintenance_jobs.py` that keeps the manager's wide guard for
  `WHOLE_RUN_MODES`.
- Behaviour: a reconcile no longer blocks foreground writes for its whole run.
  Its receipt can name phases that it skipped because their inputs moved.
- Depends on `add-remote-maintenance-jobs`, which must be archived first,
  because this change modifies one of its requirements.
- No new tool, mode or parameter.
