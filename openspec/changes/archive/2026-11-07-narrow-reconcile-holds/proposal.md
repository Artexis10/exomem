## Why

A reconcile holds the vault mutation boundary for its whole run. On 2026-10-30,
a CLI reconcile on a disposable copy of a vault with about 12,000 Markdown
pages held it for 830 s cold and 1,005 s warm. The figures are the `hold_ms`
values of the `mutation_lock_released` events in that run's CLI log. During a
reconcile, every other writer, local or remote, gets `MUTATION_BUSY`.

`add-remote-maintenance-jobs` lets a remote client start a reconcile as a
background job. That makes a long reconcile routine. The job names itself in the
busy details, but a client still cannot write for a quarter of an hour.

No per-phase measurement exists yet. Some phases rebuild derived state, and the
lexical store already serializes its own publication. If the time sits in those
phases, holding the boundary only for the canonical phases shortens every hold.

## What Changes

- A reconcile, from the CLI or as a job, runs its leaf without one boundary hold
  for the whole run. The boundary narrows for reconcile as it already does for a
  chunked job.
- The leaf takes the boundary in shorter holds. No hold spans a derived rebuild
  that a separate coordinator already serializes. A per-phase profile decides
  how the phases group into holds.
- Each hold re-reads its inputs inside the hold and applies what is still
  current. The destructive purge applies only the entries that both the plan
  and the fresh read contain, and reports the rest, as `fix` reports
  `skipped_changed`.
- A hold that meets a busy boundary waits a bounded time, as a chunked job
  does, instead of failing the run.

## Capabilities

### Modified Capabilities

- `maintenance-jobs`: a reconcile job no longer holds the boundary for its
  whole run.

## Impact

- Code: `src/exomem/reconcile.py`; `src/exomem/maintenance_jobs.py`
  (`WHOLE_RUN_MODES` and the dispatch retry); `src/exomem/writer_lease.py` (the
  narrowing row for reconcile, and the graph join and handoff composition that
  now assume a wide guard).
- Behaviour: a reconcile no longer blocks foreground writes for its whole run.
  Its receipt can name purge entries that it skipped because they moved.
- Depends on `add-remote-maintenance-jobs`, which must be archived first,
  because this change modifies one of its requirements.
- No new tool, mode or parameter.
