## MODIFIED Requirements

### Requirement: Governed writes are never fenced into a whole-vault rebuild by unattributed filesystem events

The projection-freshness fence raised by filesystem events the process cannot attribute to itself SHALL apply only to reads that require a current projection. A governed write SHALL always compute its predecessor from the checkpoint lineage. An unreadable predecessor SHALL be reported as a distinct outcome and routed to bounded incremental repair of the affected paths, never collapsed into the lineage-gap outcome that schedules a whole-vault rebuild. External marks SHALL be recorded per path and drained by the incremental repair. That drain SHALL itself retire a path-scoped mark whose paths it re-derives, inside its own hold of the mutation boundary, so a mark left standing across a restart converges at the drain's cadence and never waits on the periodic reconcile. A governed write whose own refreshed paths are under a path-scoped mark SHALL queue exactly those paths for that repair and report its own pending outcome, and SHALL NOT schedule a whole-vault rebuild; the coverage SHALL be a durable receipt the drain converges, never an assumption that the observer's own repair will land. A mark that names no scope states that the affected set is unknown and SHALL keep scheduling the whole-vault rebuild. A governed write whose acknowledged lineage is behind its checkpoint SHALL NOT schedule a whole-vault rebuild when every generation the acknowledgement skipped is itself queued as durable repair; it SHALL take the bounded incremental path and report a pending outcome of its own, and SHALL NOT advance the acknowledgement it did not project. Coverage SHALL be proven from the durable receipts, which SHALL record the generation they were queued for; a receipt that does not record one SHALL NOT count as coverage. A process that proves a snapshot it did not publish SHALL adopt it as its delta origin, and SHALL do so even when a bounded set of paths is found unrepaired, by queueing exactly those paths for incremental repair. That adoption SHALL run on the unconditional start-up path, independently of any resource policy governing the preloading of rebuildable caches. A bail-out caused by a cache this process simply does not hold, rather than by stored state it cannot read, SHALL queue the affected paths and report its own pending outcome instead of scheduling a whole-vault rebuild.

#### Scenario: Writes after a worker replacement stay incremental
- **WHEN** a replacement worker starts with an empty self-attribution table and observes filesystem events from the previous worker and the migrator, and then serves ten governed writes
- **THEN** no write schedules a whole-vault rebuild on its own account: an adopted snapshot makes the first write incremental, at most one coalesced rebuild runs per proven lineage divergence between watcher repairs and governed writes, no write joins a rebuild past its budget, each write acknowledges within its bound, and the repair queue drains to zero

#### Scenario: Adoption tolerates a bounded residue
- **WHEN** a replacement process proves the previous snapshot and finds a bounded set of paths whose canonical bytes differ from the snapshot's recorded state
- **THEN** it adopts the snapshot at its checkpoint, queues exactly those paths for incremental repair, reads that require a current projection keep refusing until that repair lands, and the process's first write is incremental

#### Scenario: Adoption does not depend on the cache-preload policy
- **WHEN** a replacement worker starts in a resource mode that does not preload rebuildable CPU caches
- **THEN** start-up still proves and adopts the inherited snapshot, records its residue, and leaves a recall resolver resident, so the first governed write is incremental

#### Scenario: A cold resolver cache is queued repair, not a lineage gap
- **WHEN** a governed write's incremental pass has proven its predecessor and a complete recall delta but this process holds no resident recall resolver for that checkpoint
- **THEN** the delta is queued for incremental repair, the write is acknowledged with a pending outcome naming the cold resolver, no whole-vault rebuild is scheduled, and a later drain converges the queue

#### Scenario: An unrelated external edit does not fence a write
- **WHEN** an unattributed edit lands on one path while a governed write commits to another
- **THEN** the write takes the incremental path, the edited path is queued for repair, and reads that require a current projection continue to refuse until that repair lands

#### Scenario: A mark on the write's own paths is queued repair
- **WHEN** an unattributed edit lands on the very path a governed write is about to commit, the mark names that path, and it is still unrepaired when that write dispatches
- **THEN** the write defers, queues exactly its own paths as durable graph repair, reports a pending outcome naming the unattributed event, schedules no whole-vault rebuild, and a drain converges those paths whether or not the observer's own repair lands first

#### Scenario: A lineage gap the queue already owns is not a rebuild
- **WHEN** governed writes arrive faster than the repair queue drains, so each one's predecessor probe finds the acknowledgement a generation or more behind, and every skipped generation's paths are on the durable queue
- **THEN** each write takes the bounded incremental path, queues its own paths, reports a pending outcome naming the covered gap, schedules no whole-vault rebuild, and leaves the acknowledgement where it was until a drain actually projects it

#### Scenario: A gap the queue cannot account for still rebuilds
- **WHEN** a skipped generation has no durable receipts, or a queued receipt does not record which generation it was queued for
- **THEN** the gap is treated as a proven divergence and the whole-vault rebuild is scheduled

#### Scenario: A real lineage gap still rebuilds
- **WHEN** the checkpoint lineage proves the snapshot cannot be advanced incrementally
- **THEN** a whole-vault rebuild is scheduled and the outcome names the lineage gap, not an unreadable predecessor

#### Scenario: A path-scoped mark left across a restart converges at the drain's cadence
- **WHEN** a process starts with a path-scoped external mark already recorded, the receipts under it are queued, and no watcher event arrives to withdraw the mark
- **THEN** the next drain re-derives those paths, retires the mark for exactly them within its own boundary hold, acknowledges the checkpoint, and readers that require a current projection are served before the periodic reconcile would have run

