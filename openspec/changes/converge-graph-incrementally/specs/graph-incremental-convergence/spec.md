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

### Requirement: A process does not exit with a graph rebuild in flight

Taking the rebuild off the write path SHALL NOT mean a process may end while one
is running. A rebuild runs on a daemon thread, which is correct for a long-lived
server and incorrect for a one-shot invocation that exits as soon as its command
returns — there, the write would report `pending` and nothing would ever make it
true.

A command-line invocation SHALL therefore drain in-flight graph rebuilds before
exiting, on **every** exit path, including those that return before the main
dispatch. The drain SHALL be bounded: a rebuild that will not finish SHALL NOT
hold the process open indefinitely, and SHALL be reported to the operator with
the repair that applies, rather than passed over in silence. The canonical bytes
and the checkpoint are durable either way, so an abandoned rebuild costs a
reconcile, never a write.

#### Scenario: A one-shot invocation waits for the rebuild its write started

- **WHEN** a command-line write commits and its graph rebuild is still running
- **THEN** the process drains that rebuild before exiting

#### Scenario: Every exit path drains

- **WHEN** a command-line invocation returns through an early exit that precedes
  the main dispatch
- **THEN** in-flight graph rebuilds are still drained

#### Scenario: A wedged rebuild is surrendered and reported

- **WHEN** an in-flight rebuild does not finish within the drain bound
- **THEN** the process exits anyway
- **AND** it reports that the change is committed and names reconcile as the repair

### Requirement: Graph-backed reads converge without being asked

Because a write no longer waits for its own rebuild, a reader issuing a
graph-backed request immediately after a write MAY observe the graph
unavailable. That window is the designed cost of taking the rebuild off the
write path, and it SHALL be reported honestly — an unavailable graph SHALL carry
its reason, and the surrounding response SHALL still return the dimensions that
do not depend on the graph.

The graph SHALL then converge with no further request from the caller. No
operation SHALL be required to make a committed write's graph land.

The window SHALL be bounded by the repair actually needed. Until repair is
proportional, that bound is a whole-vault rebuild; once it is, the bound is a
per-path drain. This requirement is what makes that improvement observable
rather than assumed.

#### Scenario: A read straight after a write may see the graph still building

- **WHEN** a client issues a graph-backed context request immediately after a
  write that changed the graph
- **THEN** the response may report the graph unavailable with a stated reason
- **AND** the non-graph dimensions of that response are still returned

#### Scenario: The graph converges with no further request

- **WHEN** a write commits and no subsequent operation is invoked
- **THEN** the graph becomes available on its own
- **AND** a client that polls a graph-backed read observes it become available

### Requirement: An externally fenced graph barrier recovers through durable full debt

When a graph read barrier remains after an external-pending epoch, the drain SHALL
not treat a declined barrier recovery as settled. It SHALL persist the existing
full-rebuild marker and let the guarded marker convergence reconcile and rebuild
the complete recall corpus. It SHALL retain the existing publication-refusal
backoff and active-owner coalescing behavior, and SHALL NOT create duplicate
markers while one is pending.

Before reconciliation clears an observed external epoch, it SHALL evict the
resolver and inbound caches affected by that recall publication. Clearing SHALL
remain limited to the sampled epoch, leaving any newer mark pending and the graph
unavailable until it is itself reconciled and published.
An eviction or cache clear SHALL revoke publication by a resolver or inbound
builder that was already walking the old vault state; that detached request may
complete, but it SHALL NOT repopulate the shared cache.

#### Scenario: An external mark with a barrier converges through the full marker

- **WHEN** the graph has a persisted read barrier, the per-path queue is empty,
  and an external epoch is pending
- **THEN** the drain creates one full-rebuild marker
- **AND** guarded marker convergence publishes a current graph only after a
  complete recall reconciliation

#### Scenario: Cold recovery includes unqueued recall content

- **WHEN** a replacement process has a suspended graph, an observed external
  path, and an additional recall file not named by that event
- **THEN** the recovered graph includes both files before it becomes available

#### Scenario: A newer mark remains fenced

- **WHEN** a newer external mark arrives after reconciliation sampled an older
  mark and before its clear-through
- **THEN** clear-through retires only the older mark
- **AND** the graph remains unavailable until the newer mark is reconciled

#### Scenario: Backoff and active ownership retain one marker

- **WHEN** publication backoff is active, or another rebuild owner holds the
  graph claim
- **THEN** the drain does not spend a duplicate full rebuild
- **AND** at most one durable full-rebuild marker remains pending

#### Scenario: Cache eviction revokes an in-flight builder

- **WHEN** recovery evicts an inbound or recall resolver cache while its builder
  has already read the prior vault state
- **THEN** the builder may return its detached snapshot to its original caller
- **AND** it does not publish that old snapshot for a later request

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

A drain whose queued paths change link topology SHALL also repair the pages whose own
edges change as a result. A link to a page that does not yet exist produces no edge at
all, so re-indexing the target later cannot repair the source; the sources have to be
found and re-indexed. A drain over paths that change no topology SHALL NOT perform that
search.

#### Scenario: A page written before its link target still gains the edge

- **WHEN** a page linking to `C` is drained before `C` exists, and `C` is created and
  drained afterwards
- **THEN** the graph contains the edge from that page to `C`
- **AND** it is the same edge a full rebuild of the resulting vault produces

#### Scenario: An ordinary edit does not pay for topology repair

- **WHEN** every queued path in a drain is already in the graph and still on disk
- **THEN** no search for affected sources is performed

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

### Requirement: A drain retires the generation it converged

Repairing the queued pages is only half of convergence. A drain SHALL acknowledge the
committed graph sync checkpoint it converged, so that the epoch advances and readers
observe a current graph. A drain that repairs pages without moving the acknowledgement
converges nothing observable: the epoch stays stale, the sidecar stays unavailable, and
the next dispatch takes the whole-vault rebuild regardless — which would leave the queue
as overhead beside the expensive path rather than a replacement for it.

Acknowledgement is the only irreversible claim in this path, so a drain SHALL
acknowledge a checkpoint only when the pass processed that checkpoint's whole path set.
A batch truncated by the drain limit, a batch left over from an earlier generation, and
a full-scope marker SHALL each acknowledge nothing and leave the work queued for the
drain that does cover it.

Coverage SHALL be membership in the processed batch rather than in the indexed set. A
deletion named by a checkpoint is processed by removing its rows and has no source bytes
to index, so an indexed-set test would stall every generation containing one
indefinitely. That the indexed pages did not move under the pass remains a separate
proof.

#### Scenario: A covering drain makes the epoch current

- **WHEN** a drain processes every path named by the committed checkpoint
- **THEN** the graph sync epoch reports the committed generation as current
- **AND** the graph reports itself available

#### Scenario: A partial drain acknowledges nothing

- **WHEN** a drain processes only some of the paths the committed checkpoint names
- **THEN** the epoch is not advanced to that generation
- **AND** the remaining paths stay queued

### Requirement: A queued repair is not rescheduled as a whole-vault rebuild

When the incremental pass enqueues the affected paths, the durable queue owns that repair.
The dispatch layer SHALL NOT then register a whole-vault rebuild for the same checkpoint.
Registering one runs the expensive path on exactly the bail-outs this capability exists to
make proportional, leaving the queue as overhead beside the whole-vault rebuild rather
than a replacement for it.

The distinction SHALL rest on an enqueue that actually succeeded, not on the deferral
alone. A fallback that registers its own whole-vault rebuild also reports a deferral, and
a queue write that failed falls back to a rebuild by design; in both cases the queue does
not own the repair and the rebuild is correct.

A write whose repair is queued SHALL report the graph as pending in its terminal, with a
code distinct from a rebuild that is still running. Nothing is rebuilding, so an operator
reading a rebuild-in-progress code would look for a flight that does not exist, and the
two states converge on different timescales. The terminal SHALL prove this from durable
state — a committed checkpoint the sidecar has not acknowledged, with work still queued
against it — because a repair the queue owns has no registration to observe.

A write that has not converged SHALL NOT be reported identically to one whose graph is
current. Absent this outcome the response is byte-identical to a converged write, which is
the single failure the pending outcome exists to prevent.

#### Scenario: A queued repair schedules no rebuild

- **WHEN** the incremental pass enqueues the affected paths for a committed checkpoint
- **THEN** the dispatch reports the repair as queued
- **AND** no whole-vault rebuild is registered for that checkpoint

#### Scenario: A queued repair still reports pending

- **WHEN** a write commits with its graph repair queued and no rebuild registered
- **THEN** the terminal reports the graph as pending with a queued-repair code
- **AND** the response is distinguishable from a write whose graph is current

Only a caller that can report the pending outcome SHALL defer. A caller inside a mutation
request has a terminal that carries the graph outcome; a direct library caller returns a
leaf result with nowhere to put one, and its contract is a converged graph. Deferring for
such a caller does not merely under-report — it changes what the next call in the same
process observes, because the graph that used to be current by the time that call ran no
longer is. The predicate SHALL be the same one that decides whether a caller joins a
registered rebuild: the caller that joins is precisely the caller that must not defer.

#### Scenario: A standalone caller still gets a converged graph

- **WHEN** a direct library caller's incremental pass enqueues its affected paths
- **THEN** the dispatch does not report the repair as queued
- **AND** a rebuild is registered so the caller's contract of a converged graph holds

#### Scenario: A failed enqueue still earns a rebuild

- **WHEN** the queue write fails and the pass falls back to a whole-vault rebuild
- **THEN** the dispatch does not claim the repair is queued

### Requirement: Whole-vault graph repair is paid once and waits out a write burst

Whole-vault graph debt is one durable marker, and more than one owner can run the
rebuild that pays it. Every whole-vault publication SHALL retire the marker it
observed before its epoch sample, and SHALL do so by compare-and-swap on the
marker's raise count as well as its value, so any debt raised after that
observation survives, including a repeat that leaves the value unchanged. The
full-marker dispatcher SHALL clear its marker by the same comparison.

A whole-vault pass publishes only when no write lands while it runs, so the
drain SHALL hold a whole-vault attempt until the vault has been quiet since the
last write's debt signal for one measured whole-vault pass, or for a 5 s floor
before any pass has been measured. The hold SHALL NOT exceed 120 s past the first
debt signal the attempt was held for; after that the attempt runs, paced by the
no-progress backoff. A debt signal the drain raises itself SHALL NOT start the
window, and per-path repair and its drain SHALL NOT be held.

#### Scenario: A publication retires the debt it observed and nothing newer

- **WHEN** a whole-vault publication lands while the full marker it observed
  before its epoch sample stands, and further debt was raised after that
  observation at the same marker value
- **THEN** the marker survives the publication and the drain pays the newer debt
- **AND** without that further debt, the publication retires the marker and no
  owner rebuilds the same graph again

#### Scenario: Whole-vault repair waits one measured pass, for at most 120 s

- **WHEN** a whole-vault marker stands and writes keep arriving
- **THEN** the drain starts no whole-vault attempt until the vault has been quiet
  for one measured pass since the last write
- **AND** if the writes never pause that long, the attempt still starts within
  120 s of the first debt signal it was held for, and later attempts follow the
  no-progress backoff

### Requirement: A drain keeps the queue converging under a steady writer

A drain SHALL record the resolver topology fingerprint it derived its rows under, in the
transaction that writes them, so the next topology-changing write can prove its old
resolver instead of falling back to a whole-vault rebuild.

A drain SHALL land the rows it proved against their own bytes at commit even when the
projection moved elsewhere during its pass, and retire their receipts; its marker,
lineage and acknowledgement wait for a drain the projection holds still for. A page the
drain indexed moving under it SHALL roll the pass back.

A write's incremental refresh that finds its own generation already acknowledged -- a
drain derived its batch first -- and the availability marker current SHALL do nothing
rather than fall back: nothing is owed, and the fallback would withdraw a current marker.
When the marker is not current -- the write's registry update landed after the drain
acknowledged -- the refresh SHALL queue its paths, so a per-path drain republishes the
marker without a whole-vault rebuild.

#### Scenario: A drain that repairs a created page keeps the next write incremental

- **WHEN** a drain repairs a page a write created, and a later write changes topology
- **THEN** the later write proves its old resolver against the stored fingerprint and
  does not fall back on a fingerprint mismatch

#### Scenario: A drain keeps the rows it proved when the vault moves elsewhere

- **WHEN** a write to another page moves the projection during every drain pass
- **THEN** each drain lands the rows it proved and retires their receipts

#### Scenario: A late refresh of an acknowledged generation is a no-op

- **WHEN** a drain has acknowledged a write's generation before that write's own refresh
  runs, and the marker is current
- **THEN** the refresh returns without work and the graph stays readable

#### Scenario: A late refresh behind a late registry update queues its page

- **WHEN** a drain acknowledges a write's generation, the write's registry update lands
  after it, and the write's own refresh then runs
- **THEN** the refresh queues the page, one per-path drain makes the graph readable again,
  and no whole-vault pass runs

### Requirement: An underivable receipt is quarantined, not rotated forever

A queued path whose isolated drain attempt fails on its own bytes -- they are not UTF-8,
or reading them raises an operating-system error that persists past a minimum age of
minutes since its first failure -- SHALL be counted, and after a bounded number of such
failures its receipt SHALL be quarantined: it leaves the queue by exact revision so the
queue can empty around it. Quarantine SHALL only stop hot retries: the rows of a page
that still exists SHALL NOT be deleted. A movement or readiness refusal SHALL NOT count:
anything raised out of the drain pass -- a busy mutation boundary, a locked store -- and
a page that reads and decodes when checked rotate their receipt, however long they last.
A queued path that derived to no rows -- deleted, or no longer recall Markdown -- SHALL
retire its receipt with the deletion.

A quarantined path SHALL be queued again when its stat signature changes, and otherwise
on a slow periodic retry. Any pass that derives the page, including a whole-vault
rebuild, SHALL clear its failure record. The residual lag and the doctor SHALL report
quarantined paths.

#### Scenario: One unreadable page does not keep the queue from emptying

- **WHEN** a queued page's bytes cannot be read on every drain attempt for longer than
  the minimum age
- **THEN** after the bounded attempts its receipt is quarantined, its existing rows stay,
  the rest of the queue converges, and the lag and doctor report one quarantined path

#### Scenario: A busy boundary or a brief lock never quarantines a page

- **WHEN** the drain pass is refused by a busy boundary or a locked store, or a page is
  unreadable for a few drain ticks and then reads normally
- **THEN** its receipt rotates without counting, its rows stay, and the drain repairs it
  once the refusal ends

#### Scenario: A quarantined page is retried and its record cleared once it derives

- **WHEN** a quarantined page changes, its retry interval passes, or a whole-vault
  rebuild derives it
- **THEN** it is queued again or derived, and a pass that derives it clears its record
  and the doctor's warning

### Requirement: Residual graph lag is reported

When graph unavailability is reported because the graph is catching up, the system SHALL
report the residual lag: the acknowledged and committed generations, how many
generations the graph is behind, the queued path count, the age of the oldest queued
debt, whether the gap is covered by durable debt records, whether a full rebuild is
pending, and how many paths are quarantined. It SHALL be readable without a vault walk.
The graph is catching up when it is behind or has work queued, every skipped generation
is receipt-covered, and neither a full-rebuild marker nor a recovery barrier stands; a
deferral's own withdrawal is not a recovery barrier, because the queue is its repair.

When the graph is catching up, `graph_context`'s unavailable payload SHALL say "graph
catching up" and carry the lag; every other refusal keeps its payload. The doctor SHALL
warn rather than fail on a receipt-covered lag younger than its recovery age, and SHALL
still fail an uncovered gap, a standing full marker, a persisted recovery barrier and a
malformed checkpoint. The drain's settled log line SHALL carry the lag.

The read fence stays fail-closed: a lagging graph is reported, not served.

#### Scenario: A catching-up graph says so with its lag

- **WHEN** a committed generation's paths are queued behind the acknowledgement and a
  caller asks for graph context
- **THEN** the unavailable payload says the graph is catching up and carries the lag

#### Scenario: The doctor warns on a covered lag and fails an uncovered gap

- **WHEN** the graph lags behind a receipt-covered gap younger than the recovery age
- **THEN** the doctor reports a warning with the lag
- **AND** when a generation in the gap has neither a durable debt record nor a queued
  receipt, the doctor still fails

### Requirement: A graph rebuild and a canonical write may run concurrently without either losing

A graph rebuild in flight SHALL NOT cause a concurrent canonical write to refuse, and a
concurrent canonical write SHALL NOT be blocked by a rebuild's reads.

The canonical directory census SHALL exclude derived-index residue — the rebuild's
scratch sidecars and their companions, and the dirty-path queue's database and its
journal companions — using the same mechanism already applied to the batch writer's own
workspace residue. Artifacts written by the canonical batch itself, including the graph
sync and floor artifacts, SHALL remain censused.

Excluding the queue database is required by the enqueue ordering, not merely convenient
alongside it: the enqueue is what makes a crash cut safe, so it must happen before the
commit, which places the database's creation and journalling inside the guarded window.
A census that counted it would make the first write to a fresh vault invalidate its own
census, with nothing concurrent involved. Two writers appending their own graph debt is
the monotone behaviour the queue exists to provide, not a canonical conflict.

Each excluded name SHALL be bound by test to the definition it was copied from, so a
rename at the definition cannot leave the census's copy stale and silently start
counting operational bookkeeping as canonical content.

A rebuild's page reads SHALL NOT take custody of the pages they read: on platforms whose
default open mode denies concurrent deletion or replacement, rebuild reads SHALL open
with sharing that permits both.

A replacement refused because of a transient sharing violation SHALL be retried for a
bounded interval, and each attempt SHALL re-evaluate its own precondition rather than
replacing against a stale one.

Publication epoch sampling SHALL observe a canonical batch from outside, never from
within: it SHALL be serialized against the canonical mutation hold so it cannot read a
generation floor installed without its checkpoint.

Graph work that holds the writers' canonical boundary SHALL NOT wait on the in-process
batch commit: a batch holding it may be in its post-commit fan-out waiting for that
boundary. A recovery checkpoint write under the boundary SHALL be skipped while a batch
commits and left for the next attempt. `reconcile` is exempt: it holds the boundary for
its whole command, so a fan-out waiting on that boundary is refused for as long as
reconcile runs whether or not reconcile then takes the batch lock, the inversion adds at
most one bounded wait to reconcile itself, and skipping would fail the repair its caller
asked for.

#### Scenario: The full-marker dispatcher does not queue behind a committing batch

- **WHEN** the dispatcher holds the canonical boundary, finds the epoch recoverable, and
  a canonical batch in the same process is committing
- **THEN** the dispatcher returns without writing the recovery checkpoint, and no
  writer is refused the boundary while it waits

#### Scenario: A rebuild's scratch files do not fail an unrelated write

- **WHEN** a canonical write takes its guarded directory census while a rebuild is
  creating, replacing, and removing its scratch sidecars in the same directory
- **THEN** the census does not change from the rebuild's residue and the write commits

#### Scenario: The first write to a fresh vault does not invalidate itself

- **WHEN** a canonical write enqueues its graph debt and thereby creates the dirty-path
  queue's database inside its own guarded directory census
- **THEN** the census does not change and the write commits

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
