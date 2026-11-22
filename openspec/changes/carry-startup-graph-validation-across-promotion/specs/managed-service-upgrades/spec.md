## MODIFIED Requirements

### Requirement: A promoted standby does not repeat the warm it already ran

A promoted worker SHALL NOT repeat warm-up work its own standby completed and promotion either re-verified or cannot invalidate, and SHALL record which components were carried forward. A component SHALL be carried only where the standby completed it; the adopted graph snapshot SHALL be carried only where the checkpoint the standby holds a proof for is current at promotion. Where the serving worker published again after the standby's proof, promotion SHALL re-prove the new checkpoint from the page reads the standby's proof ended on, re-reading only the pages that moved wherever a page's stat generation binds its bytes, under the same invariant as the standby's proof and inside a share of the budget the supervisor sends with the promotion request, or of the legacy ten-second request cap when the supervisor sends none; a re-proof that adopts makes the checkpoint current, and one that declines or runs out of time SHALL leave the graph handoff uncarried. Carried components SHALL be marked ready before the promoted worker's warm begins serving requests, so a governed write arriving immediately after promotion is admitted rather than refused as warming. A worker that starts cold SHALL carry nothing and SHALL run its whole warm unchanged. The watcher's start-up graph validation SHALL count as carried only after promotion bridges the unwatched interval with live observation and a bounded replay of the standby's guarded source, membership and policy proof. A matching sidecar checkpoint pair alone SHALL NOT establish that bridge. The promoted worker SHALL NOT repeat that sealed proof in watcher startup. Buffered observations SHALL remain pending through seeding and catch-up. A carried residue SHALL remain durably queued and fenced until exact repair publishes; an unrelated governed write SHALL NOT erase that debt or acknowledge rows it did not project. Missing observation, unavailable proof or an expired promotion budget SHALL leave both graph handoff and startup validation uncarried. Canonical writes SHALL queue graph work while fallback proof establishes a valid source origin; no graph publication SHALL bypass that proof. Where start-up validation does run and finds the durable checkpoint unacknowledged while a governed mutation holds the boundary, it SHALL treat that as contention and wait for that mutation's own acknowledgement rather than a fixed budget, and SHALL NOT suspend reads or schedule a whole-vault rebuild on that evidence alone; only a proof that fails with the boundary free is incoherence. A graph snapshot whose proof declined because it is unusable for the standby's build SHALL be reported settled, so it does not hold cutover, and SHALL NOT be carried.

#### Scenario: A governed write arrives immediately after promotion
- **WHEN** a governed write reaches a worker that was promoted from a standby whose warm completed
- **THEN** it is admitted rather than refused as warming, and it is served incrementally

#### Scenario: The promoted worker's warm subtracts what it carried
- **WHEN** a promoted worker runs its own warm-up after a promotion that re-verified the snapshot as current
- **THEN** it neither adopts the snapshot again nor rebuilds the semantic corpus context, and its completion record names the carried components

#### Scenario: The snapshot moved under the standby
- **WHEN** the serving worker published again after the standby's proof, and promotion re-proves the new checkpoint from the standby's page reads inside its bound
- **THEN** promotion records the snapshot as current and the re-proof as run, the graph handoff is carried forward, and a governed write arriving immediately after promotion is admitted and served incrementally

#### Scenario: The promotion re-proof does not finish
- **WHEN** promotion's re-proof of a moved checkpoint declines or runs past its bound
- **THEN** promotion records the snapshot as advanced with the decline reason, the graph handoff is not carried forward, and the promoted worker's warm runs it in full

#### Scenario: The inherited graph snapshot belongs to an older graph schema
- **WHEN** a standby proves that the inherited graph sidecar has an older graph schema version than its own build
- **THEN** the graph snapshot component is reported settled so the upgrade is not held
- **AND** promotion records the snapshot as unproven, and the promoted worker runs the whole graph handoff

#### Scenario: A cold start carries nothing
- **WHEN** a worker starts without having been a standby
- **THEN** it carries no components and runs every warm-up step

#### Scenario: A warm step failed rather than not running
- **WHEN** a standby's semantic corpus build runs and fails
- **THEN** the component is reported settled so the upgrade is not held, and it is not carried forward, so the promoted worker runs that step itself

#### Scenario: A governed write during the promoted worker's start-up validation is not incoherence
- **WHEN** the promoted worker's start-up validation observes an unacknowledged durable checkpoint while a governed write holds the mutation boundary for longer than the admission budget
- **THEN** validation waits for that write's acknowledgement, admits the graph it publishes, suspends no reads and schedules no whole-vault rebuild

#### Scenario: The promoted worker does not re-prove the carried graph
- **WHEN** promotion seals the current graph proof through the normal watcher's observation and guarded replay
- **THEN** watcher startup reuses that proof and records start-up validation as carried
- **AND** exact queued residue remains fenced until the ordinary drain publishes it


#### Scenario: Canonical bytes moved without a sidecar publication
- **WHEN** a source changes after the standby proof without changing the sidecar checkpoint pair, and an unrelated governed write arrives before the first graph read
- **THEN** the observation bridge and source replay account for that change before graph publication, or the worker takes the uncarried proof path
- **AND** no graph publication certifies stale rows as current

#### Scenario: Observation cannot arm within the request budget
- **WHEN** promotion cannot establish covering observation and guarded source proof within its existing budget
- **THEN** graph handoff and startup validation remain uncarried, and the established startup path performs its proof
- **AND** an unrelated first write remains durably queued for graph work without publishing stale graph rows
