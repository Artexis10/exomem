## MODIFIED Requirements

### Requirement: Content maintenance commits in short guarded chunks

A `fix` or `backfill-ids` job SHALL commit its page writes in chunks of at most
100 pages. Each chunk SHALL take its own mutation boundary hold and SHALL
re-check each page's content hash inside that hold. The job SHALL skip a page
that changed since planning and SHALL record it as `skipped_changed`. The job
SHALL commit derived index refreshes last, in one short hold. A `reconcile`
job SHALL take the boundary once per phase that writes canonical state and
SHALL release it between phases. Every hold SHALL carry holder kind
`maintenance-job` and the job's `run_id`.

#### Scenario: A foreground write runs between chunks
- **WHEN** a `fix` job commits several chunks and a client writes a page outside the change set
- **THEN** the client's write commits between two chunks without `MUTATION_BUSY`

#### Scenario: A page changes during the job
- **WHEN** a page in the change set changes after planning and before its chunk commits
- **THEN** the job leaves that page unchanged and records it as `skipped_changed` in the receipt

#### Scenario: A blocked writer finds the job
- **WHEN** a client write receives `MUTATION_BUSY` while a job holds the boundary
- **THEN** the busy details name holder kind `maintenance-job` and the job's `run_id`

#### Scenario: A foreground write runs between reconcile phases
- **WHEN** a `reconcile` job runs and a client writes a page while the job is between two phases
- **THEN** the client's write commits without `MUTATION_BUSY`

#### Scenario: A reconcile phase whose inputs moved is skipped
- **WHEN** a canonical reconcile phase finds inside its hold that its inputs changed since the census
- **THEN** the job applies nothing for that phase and names the phase as skipped in its receipt
