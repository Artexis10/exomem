## Context

`maintain_memory` has ten modes. Remote surfaces already run the write modes
`structured-files`, `tag-variants` and `curation`, each bound to a reviewed plan
identity (`REMOTE_WRITE_MAINTENANCE_MODES` in `writer_lease.py`). The gate in
`invoke_command` refuses every other remote write with
`MAINTENANCE_REQUIRES_CLI`.

The CLI does not ask the service to run maintenance. `exomem maintain` calls
`invoke_command` in its own process and takes the OS boundary lock itself.
`fix`, `backfill-ids` and `reconcile` hold the wide outer mutation boundary for
their whole run. `fix` already commits in 100-page batches, and its content
writes carry a path guard. Its sidecar repair writes carry no guard.
`backfill-ids` writes carry an expected hash. `reconcile` is exempt from the
request budget.

No generic job store exists. The media job ledger shows the durable pattern
(machine-local SQLite, atomic claim, restart recovery). Adoption runs show
cancellation between steps.

## Goals / Non-Goals

**Goals:**

- A remote owner can preview, start, follow, cancel and read the receipt of
  `fix` (including `rebuild_embeddings`), `backfill-ids` and `reconcile`
  (including `rebuild_graph`).
- A job applies only the change set its preview showed.
- A running `fix` or `backfill-ids` job leaves room for foreground writes.
- Remote `collections-store-adopt-local` applies while the service runs.

**Non-Goals:**

- Narrow `reconcile`'s critical sections. The job keeps the CLI's whole-run
  hold. A measurement of the hold on a real-sized vault decides that follow-up.
- Change the local CLI. It stays synchronous and does not register jobs.
- Run `collections-store-export` apply in a live service.
- Store receipts in the vault. Receipts match the CLI, which keeps none there.

## Decisions

### D1. The job runs in a service thread through the shared dispatcher

The start leaf records the job and starts a daemon thread. The thread calls
`invoke_command` for the real leaf with an internal `maintenance-job` surface,
under the starting principal's request scope. Writer authority, egress and the
mutation boundary therefore apply exactly as for any other write.

A child process is wrong here. Adopt-local uses the service's serving session.
Reconcile rebaselines in-process indexes and hands embedding drift to the
service's drain. A separate process would leave the service's state stale.

### D2. A small new store, not the media ledger

`maintenance_jobs.py` owns `.maintenance-jobs.sqlite` in the vault's
machine-local state directory (WAL), registered as external state in
`reserved_paths.py`. The media ledger's columns are media-specific.

The store never lived inside the vault, so its descriptor is marked
`migrates_from_vault=False` and stays out of the migration manifest's
descriptor set. Otherwise, the first release with the store would need a state
migration at upgrade, and an older release would refuse the vault on rollback.

- States form a closed set: `running`, `completed`, `failed`, `cancelled` and
  `interrupted`. A `cancel_requested` flag sits beside the state.
- One write job runs per vault. The claim runs under `BEGIN IMMEDIATE`.
- A start with the same mode, flags and `plan_id` as the running job returns
  that job, which also covers a lost start acknowledgement.
- The row stores the owner process identity. A running row whose owner is gone
  becomes `interrupted` when the service activates and when a client reads it.
  The job never re-runs by itself, because an automatic re-run would be
  unattended maintenance on a plan that is probably stale.
- Each claim prunes terminal rows older than 30 days beyond the newest 200.

### D3. Plan identity binds the change set

The preview returns `plan_id`: a SHA-256 over a canonical JSON document of the
mode, its flags and the change set. Counts and other derived output stay out of
the digest. The job re-plans before its first write and compares.

- `fix`: the content rewrites as `(path, before hash, after hash)`. Index
  refresh writes stay out, because any page creation changes them.
- `backfill-ids`: the pages without an ID as `(path, content hash)`. The IDs
  are random at apply time, so the receipt records them. The duplicate-ID
  refusal stays.
- `reconcile`: what it would remove or quarantine, plus `rebuild_graph`. The
  census of those sets moves before the first write. Refreshes that converge
  derived state to Markdown stay out, so a body edit elsewhere does not make
  the plan stale.

### D4. Chunked holds for content modes

`fix` and `backfill-ids` plan without the boundary. Each chunk of at most 100
pages then takes its own `mutation_guard` (the `tag_variants.apply` pattern),
re-checks each page's content hash, records changed pages as `skipped_changed`
and commits the rest. The index refresh comes last in one short hold. Sidecar
repair writes gain a content guard, because a media commit can now run between
chunks. The dispatcher narrows these job starts, and cancel, to writer
authority only, as it does for `tag-variants`.

`reconcile` keeps the whole-run hold. Its top-index write is unguarded and its
write seams depend on serialisation.

Each hold carries `holder_kind="maintenance-job"` and the job's `run_id` as
its request id. `MUTATION_BUSY` already returns the holder snapshot, so a
blocked writer can follow the job.

### D5. Jobs run behind foreground requests

`_foreground_priority_request` marks every dispatched call as foreground. In
the job thread a `foreground_priority.background_job()` scope suppresses that
mark, and the thread runs inside `foreground_priority.bulk()`. Boundary waits
and holds stay foreground, because a holder never parks. The rule that owns
foreground priority is still in the active `prioritize-foreground-writes`
change, so `maintenance-jobs` states the job's exception itself.

### D6. Checkpointed cancellation

Cancel sets the row flag and an in-process event. Work outside the boundary
(planning, embedding encode) runs under `foreground_priority.cancellable`.
Under the boundary the job stops only at `checkpoint(phase)` calls between
chunks and phases. Committed chunks stay, and the receipt lists them. A skipped
index refresh is named in the receipt; the next `reconcile` heals it. A
cancelled embedding rebuild drops its staging rows and leaves the serving
vectors as they were.

### D7. The tool surface grows variants, not tools

- `fix`, `backfill-ids` and `reconcile` with `dry_run=false` accept `plan_id`
  and `why`. On `mcp` and `rest` both are required and the call returns the job
  handle. `invoke_command` replaces the leaf with `maintenance_jobs.start_leaf`.
- `maintenance-job` is read-only: one job by `run_id`, or the list.
- `maintenance-job-cancel` is a write: it takes `run_id` and `why`.
- `run_id` keeps its name and now also identifies a maintenance job.
- Start, status, list and cancel are owner-only (`raw_protection.is_owner`
  with unrestricted access), because these modes touch every page.

### D8. Cells

A read-only cell refuses a start through the existing write check. In a cell,
each checkpoint reads `media_brakes.read_cell` and `pressure_exceeded`, and the
job fails with `MEMORY_PRESSURE` under pressure. `rebuild_embeddings` is
admitted with that brake, because it is a cell's only route to rebuild
embeddings.

### D9. Scope beyond `maintain_memory`

The CLI-only subcommands need no remote route:

- `index` and `backfill-media` are covered by `reconcile`,
  `fix --rebuild-embeddings` and `process_media`.
- `reclaim-schema`, `governance-schema`, and collections backup, create,
  migrate and export are offline migrations that need the service stopped.
- `lease`, `warm`, `mode`, `dreamer`, `prominence` and speaker enrollment are
  host administration.

### D10. A remote preview runs as a read-only job

A remote preview of `fix`, `backfill-ids` or `reconcile` reads every page. On a
vault with about 12,000 pages, the `backfill-ids` preview took 61 s and the
`fix` preview about 485 s, in-process on a disposable copy. A request through
the public ingress ends after about 100 s, so a remote client never received
the `plan_id` that starts a job.

On a request-bound surface, the owner's preview of one of these modes therefore
starts a read-only job and returns its handle, as a write start does. The job
runs the same dry-run leaf in the service thread, takes no mutation boundary
and runs behind foreground requests. Its receipt holds the preview report with
the `plan_id`, and the planned pages page by cursor like any other entries. The
owner reads the receipt, then starts the write job with that `plan_id`.

- A preview job is a job: it counts toward one job per vault, an identical
  running preview returns its handle, and the owner can cancel it.
- A caller other than the owner keeps the synchronous preview.
- The local CLI keeps its synchronous preview.

## Risks / Trade-offs

- A remote `reconcile` blocks other writers for its run, as a CLI `reconcile`
  does today. `MUTATION_BUSY` names the job. Mitigation: measure the hold on a
  real-sized vault; narrow it in a follow-up if it runs for minutes.
- An embedding rebuild under steady writes can refuse to publish, because its
  source changed. The receipt shows that outcome.
- The cell memory brake is a checkpoint, not a hard cap. One encoding batch
  can still exceed it.
- Old vocabulary debt stays and is reported: `audit._REQUIRED_FIELDS_BY_TYPE`
  and the `page_type == "pattern"` branch in `audit_fix.py`.
