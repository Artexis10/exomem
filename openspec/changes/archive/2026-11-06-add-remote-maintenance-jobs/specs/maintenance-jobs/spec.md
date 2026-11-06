## ADDED Requirements

### Requirement: One durable maintenance job runs per vault

The system SHALL record each maintenance job in a machine-local store outside
the vault. A job SHALL be in exactly one of the states `running`, `completed`,
`failed`, `cancelled` and `interrupted`. The system SHALL run at most one
maintenance job per vault. A start with the same mode, flags and `plan_id` as
the running job SHALL return that job. Any other start while a job runs SHALL
return `MAINTENANCE_JOB_ACTIVE` with the running job's `run_id`.

#### Scenario: A second job is refused
- **WHEN** a `reconcile` job runs and the owner starts a `fix` job
- **THEN** the system returns `MAINTENANCE_JOB_ACTIVE` with the running job's `run_id`
- **AND** it creates no second job

#### Scenario: A repeated start returns the running job
- **WHEN** the owner repeats a start with the same mode, flags and `plan_id` as the running job
- **THEN** the system returns the running job's handle and starts no new work

### Requirement: A job applies only the previewed change set

The preview of `fix`, `backfill-ids` and `reconcile` SHALL return a `plan_id`
that binds the mode, its flags and the change set:

- for `fix`, each content rewrite as path, content hash before and content
  hash after;
- for `backfill-ids`, each page without an ID as path and content hash;
- for `reconcile`, the derived state it would remove or quarantine, and
  `rebuild_graph`.

Writes that only converge derived state to Markdown SHALL NOT be part of the
identity. Before its first write, the job SHALL plan again. When the new
`plan_id` differs, the job SHALL fail with `STALE_MAINTENANCE_PLAN`, SHALL
write nothing and SHALL report the new `plan_id`.

#### Scenario: A page changes between preview and start
- **WHEN** a page in a `fix` preview changes before the job's first write
- **THEN** the job fails with `STALE_MAINTENANCE_PLAN`, writes nothing and reports the new `plan_id`

#### Scenario: An unrelated edit does not stale a reconcile
- **WHEN** another client edits a page body between a `reconcile` preview and its start, and the edit removes no indexed path
- **THEN** the job runs with the previewed `plan_id`

### Requirement: Content maintenance commits in short guarded chunks

A `fix` or `backfill-ids` job SHALL commit its page writes in chunks of at most
100 pages. Each chunk SHALL take its own mutation boundary hold and SHALL
re-check each page's content hash inside that hold. The job SHALL skip a page
that changed since planning and SHALL record it as `skipped_changed`. The job
SHALL commit derived index refreshes last, in one short hold. A `reconcile`
job SHALL hold the boundary for its run, as the local CLI does. Every hold
SHALL carry holder kind `maintenance-job` and the job's `run_id`.

#### Scenario: A foreground write runs between chunks
- **WHEN** a `fix` job commits several chunks and a client writes a page outside the change set
- **THEN** the client's write commits between two chunks without `MUTATION_BUSY`

#### Scenario: A page changes during the job
- **WHEN** a page in the change set changes after planning and before its chunk commits
- **THEN** the job leaves that page unchanged and records it as `skipped_changed` in the receipt

#### Scenario: A blocked writer finds the job
- **WHEN** a client write receives `MUTATION_BUSY` while a job holds the boundary
- **THEN** the busy details name holder kind `maintenance-job` and the job's `run_id`

### Requirement: Jobs run behind foreground requests

A job SHALL run its work outside the mutation boundary as in-process bulk work
that yields to foreground requests. The dispatcher SHALL NOT mark the job
thread's invocation as a foreground request. Waits for and holds of the
mutation boundary SHALL stay foreground.

#### Scenario: A request overlaps the job's planning
- **WHEN** a client request runs while a job plans without the boundary
- **THEN** the job yields to the request within the bulk-pass budget

### Requirement: The owner follows and cancels a job

The `maintenance-job` mode SHALL return one job by `run_id`, or the list of
retained jobs, with state, phase, progress, start and end times in UTC, and
`cancel_requested`. Progress SHALL report an unknown total as null, never as
zero. The `maintenance-job-cancel` mode SHALL take `run_id` and a `why`. It
SHALL make the job stop at its next checkpoint between chunks or phases. The
job SHALL keep its committed chunks and SHALL end `cancelled`. A cancel of a
job in a terminal state SHALL return that state unchanged.

#### Scenario: The owner cancels a running fix
- **WHEN** the owner cancels a `fix` job between two chunks
- **THEN** the job commits no further chunk, ends `cancelled` and lists its committed chunks in the receipt

#### Scenario: The owner follows a job to completion
- **WHEN** the owner reads `maintenance-job` for a `backfill-ids` job until it is terminal
- **THEN** the state moves from `running` to `completed` and the progress total is null or the true count, never zero while unknown

### Requirement: Every job ends with a receipt

A job in a terminal state SHALL carry a receipt with its `run_id`, mode,
flags, `plan_id`, `why`, surface, principal audience, start and end times in
UTC, terminal state, error code, and the leaf report. The receipt SHALL list
the paths of each committed chunk, every `skipped_changed` page, every write
whose outcome is unknown, and for `backfill-ids` each minted ID. The system
SHALL retain receipts for at least 30 days and SHALL always retain the newest
200.

#### Scenario: The owner reads a backfill receipt
- **WHEN** a `backfill-ids` job completes
- **THEN** its receipt lists each page path with the ID the job added

### Requirement: An interrupted job is never resumed unattended

When the process that owns a running job is gone, the system SHALL mark the
job `interrupted` with its receipt when the service activates or a client
reads the job. The system SHALL NOT re-run an interrupted job. A new job needs
a new start.

#### Scenario: The service restarts during a job
- **WHEN** the service stops while a job runs and then starts again
- **THEN** the job reads as `interrupted`, its committed chunks are listed, and no maintenance work resumes

### Requirement: Only the vault owner runs maintenance jobs

The system SHALL require the vault owner with unrestricted access to start,
read, list or cancel a maintenance job. A refusal SHALL reveal no job.

#### Scenario: A delegated session asks for a job
- **WHEN** a principal that is not the owner with unrestricted access starts, reads or cancels a job
- **THEN** the system refuses without revealing whether the job exists

### Requirement: A cell stops a job under memory pressure

In a cloud cell, each job checkpoint SHALL read the cell's memory pressure. The
job SHALL stop as `failed` with `MEMORY_PRESSURE` when the pressure exceeds the
cell's limit. A cell in read-only mode SHALL refuse a start.

#### Scenario: A cell runs short of memory during an embedding rebuild
- **WHEN** a `fix` job with `rebuild_embeddings` reaches a checkpoint while the cell's memory pressure exceeds its limit
- **THEN** the job ends `failed` with `MEMORY_PRESSURE` and the serving vectors stay as they were
