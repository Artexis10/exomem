## MODIFIED Requirements

### Requirement: Content maintenance commits in short guarded chunks

A `fix` or `backfill-ids` job SHALL commit its page writes in chunks of at most
100 pages. Each chunk SHALL take its own mutation boundary hold and SHALL
re-check each page's content hash inside that hold. The job SHALL skip a page
that changed since planning and SHALL record it as `skipped_changed`. The job
SHALL commit derived index refreshes last, in one short hold. A `reconcile`
job SHALL NOT hold the boundary for its whole run, and no hold SHALL span a
derived rebuild that another coordinator serializes. A `reconcile` job SHALL
re-read inside each hold what that hold applies, and SHALL report the purge
entries that it left unchanged because they moved. Every hold SHALL carry
holder kind `maintenance-job` and the job's `run_id`.

#### Scenario: A foreground write runs between chunks
- **WHEN** a `fix` job commits several chunks and a client writes a page outside the change set
- **THEN** the client's write commits between two chunks without `MUTATION_BUSY`

#### Scenario: A page changes during the job
- **WHEN** a page in the change set changes after planning and before its chunk commits
- **THEN** the job leaves that page unchanged and records it as `skipped_changed` in the receipt

#### Scenario: A blocked writer finds the job
- **WHEN** a client write receives `MUTATION_BUSY` while a job holds the boundary
- **THEN** the busy details name holder kind `maintenance-job` and the job's `run_id`

#### Scenario: A foreground write runs between reconcile holds
- **WHEN** a `reconcile` job runs and a client writes a page while the job holds no boundary
- **THEN** the client's write commits without `MUTATION_BUSY`

#### Scenario: A purge entry that moved is left unchanged
- **WHEN** a planned purge entry is no longer in the isolation census read inside the purge hold
- **THEN** the job leaves that entry unchanged and names it in its receipt
