## Why

Remote clients cannot repair a vault. A request-bound MCP or REST call that asks
for write-mode `fix`, `backfill-ids` or `reconcile` returns
`MAINTENANCE_REQUIRES_CLI`, so only the local CLI can run them. This costs the
remote client three things:

- An error such as `RELATION_REVIEW_STABLE_ID_REQUIRED` names `backfill-ids` as
  its remedy, and a remote agent cannot apply it.
- An Exomem Cloud cell has no host CLI, so the refusal leaves a cell owner no
  route at all.
- `collections-store-adopt-local` has no working route while the service runs.
  The CLI refuses because the service holds the writer lease, and the remote
  mode it points to is refused too.

The refusal exists because a request-bound call held the vault mutation boundary
for the whole run and kept it after the transport gave up. Every other session
then failed with `MUTATION_BUSY`. A background job with a reviewed plan, chunked
commits, progress, cancellation and a receipt removes that cause, so remote
clients can get the same maintenance as the CLI.

## What Changes

- A remote write-mode `fix`, `backfill-ids` or `reconcile` with the `plan_id`
  from its preview and a `why` starts a maintenance job and returns its handle at
  once. The service runs the job in the background.
- An owner's remote preview of these modes runs as a read-only job, because a
  preview of a large vault outlasts a request. Its receipt holds the `plan_id`.
- Each preview returns a `plan_id` that binds the change set. The job re-plans
  and fails with `STALE_MAINTENANCE_PLAN` before its first write when the
  change set differs.
- `fix` and `backfill-ids` commit in short guarded chunks, so foreground writes
  run between chunks. `reconcile` keeps the whole-run hold that the CLI uses.
- New `maintain_memory` modes `maintenance-job` (status and list) and
  `maintenance-job-cancel` let the owner follow and cancel a job. Every job ends
  with a receipt.
- Remote `collections-store-adopt-local` applies through its existing sealed
  preview.
- Remote writes through the legacy `audit_fix` and `reconcile` aliases are
  refused before vault admission.
- `collections-store-export` apply stays host work: it runs only with the
  service stopped.

## Capabilities

### New Capabilities

- `maintenance-jobs`: the durable maintenance job, its plan identity, chunked
  commits, priority, cancellation, receipt, restart behaviour, owner-only
  access and cell memory brake.

### Modified Capabilities

- `remote-maintenance-admission`: remote maintenance writes run as governed
  jobs instead of failing; adopt-local is admitted; unadmitted modes and legacy
  aliases still fail before vault admission.

The active `add-governed-curation-lane` delta for `hosted-gateway-contract` is
corrected in place. It keeps its curation-only rules and no longer lists the
remote admission set, which `remote-maintenance-admission` now owns.

## Impact

- Code: `writer_lease.py` (admission, leaf swap, holder labels, priority),
  `commands.py`, `governance/egress.py` and `operation_entries.py` (modes and
  variants), a new `maintenance_jobs.py`, `reserved_paths.py`, `audit_fix.py`,
  `memory_refs.py`, `reconcile.py` and `server_runtime.py`.
- Remote tool contract: `check_memory` and `repair_memory` gain variants, so
  the schema fixtures, the ChatGPT pending digest, the plugins, the cloud
  packages and the capabilities document regenerate, and the tool-schema budget
  rises with a stated reason.
- State: one new SQLite file in machine-local vault state. No Markdown,
  sidecar or vault schema change.
- The local CLI keeps its synchronous behaviour.
