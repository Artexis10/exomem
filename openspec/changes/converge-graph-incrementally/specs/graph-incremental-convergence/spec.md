## ADDED Requirements

### Requirement: An interactive write never waits for graph convergence

An interactive canonical mutation SHALL report a graph outcome that has already
settled and SHALL NOT wait for one that has not. The join SHALL be a check whose
latency contribution is zero, not a wait bounded by a configured interval; no
interactive graph join timeout constant SHALL exist.

Operations whose purpose is convergence — `reconcile`, and maintenance invoked in
reconcile mode — SHALL retain an unbounded join, because waiting is what the caller
asked for.

Every call site that joins a registered graph flight SHALL be either check-only or a
declared convergence opt-out, and the declared set SHALL be enforced by test so a
third unbounded site cannot appear silently.

#### Scenario: A write whose rebuild is still running does not block

- **WHEN** a canonical mutation commits and its graph rebuild is still running
- **THEN** the mutation returns without waiting for that rebuild
- **AND** the elapsed time attributable to the graph join is zero

#### Scenario: A write whose rebuild already finished reports its real outcome

- **WHEN** a canonical mutation commits and its graph rebuild has already reached a
  terminal outcome
- **THEN** the mutation reports that outcome rather than downgrading it to `pending`

#### Scenario: Reconcile still waits

- **WHEN** a user invokes `reconcile`, or maintenance in reconcile mode, and a graph
  rebuild is in flight
- **THEN** the operation waits for that rebuild to reach a terminal outcome before
  returning, with no timeout

#### Scenario: A new unbounded join site fails the suite

- **WHEN** a call site joins a registered graph flight without a bound and without
  being a declared convergence opt-out
- **THEN** the test that enumerates graph join sites fails

### Requirement: A deferred graph outcome is reported, not hidden

The terminal contract SHALL express a fourth graph outcome, `pending`, alongside
absent, `completed`, and `failed`, meaning the graph is converging and the caller did
not wait for it. It SHALL carry a stable machine-readable reason distinguishing it
from a failure.

`pending` SHALL survive the compact terminal projection. A response whose graph is
still converging SHALL NOT be byte-identical to one whose graph is current.

Every consumer that validates the terminal `graph_sync` field SHALL accept `pending`,
including consumers outside continuous integration. The permitted-outcome set SHALL
have exactly one definition in the codebase; a consumer SHALL import it rather than
restate it.

#### Scenario: A non-blocking write reports pending

- **WHEN** a canonical mutation returns without waiting for an in-flight rebuild
- **THEN** its terminal reports `graph_sync: "pending"` with a stable reason code

#### Scenario: The compact projection preserves pending

- **WHEN** a terminal carrying `graph_sync: "pending"` is projected to its compact form
- **THEN** the compact form still reports `pending`, distinguishably from `completed`

#### Scenario: Live acceptance accepts pending

- **WHEN** the live acceptance script validates a response whose graph is converging
- **THEN** it accepts `pending` as a valid outcome rather than rejecting the response

### Requirement: The changed-path set is enqueued durably, never discarded

When a canonical batch writes a graph sync checkpoint, the checkpoint's changed and
created paths SHALL be enqueued into a durable per-path graph work queue **in the same
durable step as the checkpoint write**, so that no crash cut can leave the markdown
committed and the dirty set lost.

The queue SHALL reuse the existing deferred-index machinery — its table shape,
receipts, generation triggers, and poison-isolating receipt rotation. A parallel store
SHALL NOT be introduced.

A full-scope batch, one whose path count exceeds the checkpoint path limit, SHALL
enqueue a full-rebuild marker rather than an unbounded path list.

#### Scenario: A crash between markdown and drain loses no work

- **WHEN** a canonical batch commits its markdown and checkpoint and the process dies
  before any drain runs
- **THEN** on restart the changed paths are still queued and the next drain repairs them

#### Scenario: A full-scope batch enqueues a rebuild marker

- **WHEN** a canonical batch's changed-path count exceeds the checkpoint path limit and
  its scope is recorded as full
- **THEN** a full-rebuild marker is enqueued instead of a path list

#### Scenario: A poisoned path does not stall the queue

- **WHEN** one queued path fails to index repeatedly
- **THEN** its receipt is rotated into isolation and the remaining queued paths still drain

### Requirement: Graph repair is proportional to the change

A bail-out from the incremental refresh path SHALL enqueue the affected paths and
return a deferred result. It SHALL NOT trigger a whole-vault rebuild.

The affected set SHALL be the paths the refresh path has already computed: its delta
paths, together with the resolver-affected sources it folds in.

A whole-vault rebuild SHALL remain reachable, and SHALL be reserved for the cases that
genuinely require it: no graph sidecar exists, the schema or registry version changed,
the batch scope is full, a lineage reset occurred, or the user invoked reconcile
explicitly.

A drain SHALL re-index queued paths through the existing per-path indexing primitive
and publish through the existing incremental availability marker. It SHALL NOT delete
the node, edge, or parent-reference tables.

#### Scenario: An ordinary bail-out costs O(changed), not O(vault)

- **WHEN** the incremental refresh path bails out on an ordinary write
- **THEN** the affected paths are enqueued and the call returns deferred
- **AND** no whole-vault walk is performed

#### Scenario: A missing sidecar still rebuilds fully

- **WHEN** no graph sidecar exists for the vault
- **THEN** a whole-vault rebuild runs

#### Scenario: A drain leaves the graph equal to a full rebuild

- **WHEN** a sequence of changes is applied once through queued incremental drains and
  once through a full rebuild from the resulting vault state
- **THEN** the resulting nodes, edges, and parent references are identical

#### Scenario: A concurrent write appends rather than invalidating

- **WHEN** a write lands while a drain is in progress
- **THEN** the written path is enqueued and repaired by a subsequent drain
- **AND** the in-progress drain's completed work is not discarded

### Requirement: A graph rebuild and a canonical write may run concurrently without either losing

A graph rebuild in flight SHALL NOT cause a concurrent canonical write to refuse, and a
concurrent canonical write SHALL NOT be blocked by a rebuild's reads.

The canonical directory census SHALL exclude derived-index residue — the rebuild's
scratch sidecars and their companions — using the same mechanism already applied to the
batch writer's own workspace residue. Artifacts written by the canonical batch itself,
including the graph sync and floor artifacts, SHALL remain censused.

A rebuild's page reads SHALL NOT take custody of the pages they read: on platforms whose
default open mode denies concurrent deletion or replacement, rebuild reads SHALL open
with sharing that permits both.

A replacement refused because of a transient sharing violation SHALL be retried for a
bounded interval, and each attempt SHALL re-evaluate its own precondition rather than
replacing against a stale one.

Publication epoch sampling SHALL observe a canonical batch from outside, never from
within: it SHALL be serialized against the canonical mutation hold so it cannot read a
generation floor installed without its checkpoint.

#### Scenario: A rebuild's scratch files do not fail an unrelated write

- **WHEN** a canonical write takes its guarded directory census while a rebuild is
  creating, replacing, and removing its scratch sidecars in the same directory
- **THEN** the census does not change from the rebuild's residue and the write commits

#### Scenario: A rebuild's reads do not block a replacement

- **WHEN** a canonical write replaces a page that an in-flight rebuild is reading
- **THEN** the replacement succeeds

#### Scenario: Epoch sampling never sees a batch's interior

- **WHEN** a rebuild samples the publication epoch while a canonical batch is between
  its floor install and its checkpoint write
- **THEN** the sample reflects the state before or after that batch, and no lineage
  conflict is raised

### Requirement: No graph rebuild outlives the test that started it

Because an interactive write no longer joins its rebuild, a rebuild is a daemon thread
that outlives its request. The test suite runs many vaults through one process, so such
a thread would touch process-global projections while a later test runs against a
different vault.

The suite SHALL drain active graph rebuilds at test teardown, and SHALL fail rather than
leak a rebuild that will not stop. That drain SHALL guarantee only that nothing is still
running; it SHALL NOT be used as a convergence helper.

A test that requires the graph to be current SHALL join the flight explicitly. An
assertion on a graph outcome SHALL NOT be weakened to accept `completed` or `pending`
interchangeably.

#### Scenario: A leaked rebuild fails rather than contaminating the next test

- **WHEN** a graph rebuild started by a test is still running at that test's teardown
- **THEN** teardown joins it before the next test begins
- **AND** a rebuild that will not stop within the quiesce timeout fails the run

#### Scenario: A test needing a current graph joins explicitly

- **WHEN** a test asserts that a write's graph outcome is `completed`
- **THEN** it joins the active rebuild itself rather than relying on the write to have waited
