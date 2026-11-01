## MODIFIED Requirements

### Requirement: Managed Lexical Repair Converges Under Live Traffic

While managed retrieval is unavailable, the system SHALL allow a detached full
lexical repair to publish under ordinary writer and watcher traffic without
weakening authoritative source, projection, policy, or semantic-identity
validation.

#### Scenario: Concurrent live write is rebased before publication

- **WHEN** a watcher generation changes or deletes Markdown paths while a
  detached full repair is building
- **AND** the complete bounded delta from the repair checkpoints to the current
  live checkpoints is retained
- **THEN** the system applies that delta to the completed replacement under the
  publication barrier
- **AND** publishes only after the replacement proves its target checkpoints
- **AND** managed retrieval can become ready without a process restart

#### Scenario: Large batch landing during publication wait catches up off-barrier

- **WHEN** a foreground watcher batch holds the publication barrier while a
  completed detached repair waits
- **AND** the retained final suffix is complete but exceeds the barrier replay cap
- **THEN** the system preserves the completed replacement and releases the barrier
- **AND** replays that suffix off-barrier without the foreground cap
- **AND** repeats the independent source proof before retrying publication
- **AND** limits catch-up retries so sustained live churn cannot monopolize repair

#### Scenario: Published handoff does not repeat a current full scan

- **WHEN** a successfully published and promoted repair leaves one generation
  request pending at its bounded idle handoff
- **AND** the next repair flight proves the persisted catalogue already covers
  the current live projection
- **THEN** the system acknowledges that handoff without another full-vault scan
- **AND** a stale proof or a repair request arriving during the proof still
  follows the normal full-repair path

#### Scenario: Safety-net reconcile proof survives restart

- **WHEN** the periodic safety-net walk discovers a filesystem event the live
  watcher missed
- **AND** it holds complete before and after recall maps under one unchanged
  policy identity
- **THEN** the system retains their exact changed/deleted set as a bridgeable
  recall delta carrying explicit reconcile provenance, never as a trusted
  watcher transition
- **AND** the lexical replay persists the resulting current checkpoint only
  after an independent off-barrier source proof matches that exact checkpoint
- **AND** a fresh process admits the current catalogue without a full rebuild

#### Scenario: Mixed reconcile walk cannot bless a stale catalogue

- **WHEN** the off-lock safety-net walk mixes pre- and post-change
  observations because a source path changed after the walk observed it
- **AND** the lexical replay applies the reconcile delta's changed/deleted set
- **THEN** the independent source proof fails to match the reconcile-derived
  checkpoint
- **AND** the system refuses to persist that scope's checkpoint and preserves
  the conservative repair path

#### Scenario: Source proof never holds the publication barrier

- **WHEN** a watcher batch must prove a reconcile-tainted checkpoint against
  the complete current source
- **THEN** the O(vault) proof walk executes before the publication barrier is
  acquired, with no locks held
- **AND** validation under the barrier is an O(1) exact-checkpoint comparison
- **AND** request and readiness paths refuse a reconcile-tainted delta without
  ever walking the vault

#### Scenario: Invalidated source proof refuses only the affected scope and converges

- **WHEN** an observed event, reconcile, or policy change lands between the
  off-barrier source proof and the publication barrier
- **THEN** the superseded proof fails closed and only the affected scope's
  checkpoint is refused
- **AND** sibling scopes with exact observed witnesses still persist
- **AND** the batch's rows still apply, and a later batch covering the current
  delta re-proves and persists the checkpoint without a full rebuild
- **AND** proof outcomes are counted in stable, content-free telemetry

#### Scenario: SQLite token-only churn does not veto a current replacement

- **WHEN** the live SQLite main, WAL, or SHM token changes during a detached
  repair
- **AND** source/projection checkpoints, policy, and semantic identity still
  match the replacement proof
- **THEN** token-only churn SHALL NOT decline publication

#### Scenario: Unprovable catch-up fails closed

- **WHEN** the required delta is incomplete
- **OR** the final suffix remains oversized after the bounded catch-up retries
- **OR** source, policy, projection identity, or semantic identity cannot be
  proven current
- **THEN** the system preserves the live catalogue
- **AND** leaves the repair request pending for a later bounded flight

#### Scenario: Repair telemetry preserves vault privacy

- **WHEN** a detached repair advances, publishes, or declines
- **THEN** telemetry reports a bounded phase, duration, and stable result reason
- **AND** contains no vault path, note name, or note content

#### Scenario: Observed writes during a detached replay do not void it

- **WHEN** an observed event on another path lands while a completed detached
  replacement replays a delta
- **AND** every replayed path still proves its signature or its absence
- **THEN** the replay stands and the replacement records the replay's target
  checkpoint
- **AND** the next replay or a later repair applies the newer event
- **AND** a replay into the live catalogue still requires the registry to sit at
  its target checkpoint

#### Scenario: Observed writes on both sides of the census walk still publish

- **WHEN** the final source census walk sees one observed write and misses
  another
- **AND** the census replay re-proves each path in its delta
- **AND** every path outside that delta matches one registry snapshot taken
  after the walk
- **THEN** the system publishes the replacement
- **AND** counts the excused paths in the repair telemetry

#### Scenario: Drift no event reported still aborts the repair

- **WHEN** the census walk disagrees with the registry on a path outside the
  census replay's delta
- **OR** a path changed again after its own observed event without a new event
- **THEN** the system declines publication as `source_changed`
- **AND** preserves the live catalogue

#### Scenario: A delta that needs a source proof excuses nothing

- **WHEN** the census replay absorbs a delta from the safety-net reconcile walk
- **THEN** the census excuses no path
- **AND** publishes only when the whole walk matches the rebased target

#### Scenario: A write still publishing during a detached replay is replayed, not discarded

- **WHEN** a governed write changes a replayed page on disk after the replay's
  target checkpoint
- **AND** the detached replay runs off the publication barrier
- **THEN** the replay waits once for the registry to observe a newer state of
  that page
- **AND** waits only while a write in the process holds a mutation boundary or
  has a publication intent still open for one of the pages, and for at most a
  bounded time shared by every path of the replay
- **AND** once the registry names the page after the target, the replay records
  its original target checkpoint and leaves the page to the next replay as
  ordinary lag
- **AND** never replays the newer delta inside the same replay
- **AND** declines the pass as `source_changed` when the registry does not
  observe the newer state in time, or when no write is in flight and the page
  still differs
- **AND** a replay under the publication barrier never waits: it defers only
  pages that one registry read already names after its target, and declines as
  before otherwise
- **AND** a delta that needs a source proof defers no page
- **AND** a page whose rows both scopes share is deferred only when every scope
  that covers it either names it after its target or proved the page in its own
  delta
- **AND** the repair telemetry counts the deferred pages of each pass that
  published, and reports none for a pass that declined
- **AND** `lexical_repair_finished` names which bound ended a pass that
  declined, as `declined_for`

#### Scenario: A deferred page never sits behind a current checkpoint

- **WHEN** a replacement publishes with deferred pages
- **THEN** each deferred page has an observed event after the published
  checkpoint
- **AND** readers that require a current catalogue treat the catalogue as
  lagging until a later replay applies that event
- **AND** the next replay or catch-up serves the page's newest content

#### Scenario: A deleted page never outlives its deletion behind a current checkpoint

- **WHEN** a page is absent at the checkpoint that a replacement or a catch-up
  records, whether the checkpoint's delta deleted it or the page was created
  after the checkpoint
- **THEN** the catalogue holds no row for that page in that scope, because
  readers compare the content state of a checkpoint without its generation and
  absence is the one state that can recur exactly
- **AND** a replay never defers a page that is absent at its target while its
  row holds a present state
- **AND** a catch-up round records no checkpoint for a scope while a page that
  one of its steps applied and that is absent at that checkpoint has such a row

#### Scenario: A full repair publishes under a sustained import

- **WHEN** an import writes new pages and rewrites the same index pages on every
  call, for longer than several repair passes
- **THEN** a full repair still publishes, with the rewritten pages deferred
- **AND** the published catalogue lags the registry by about one pass, not by
  the whole import

#### Scenario: A catch-up outpaced by sustained writes still advances

- **WHEN** an off-barrier catch-up round runs out of steps because new events
  keep arriving
- **AND** a scope lags, and a registry checkpoint that the round planned for it
  has every path of its delta applied by a committed step that read the page
  after that checkpoint, or that no event has touched since the step began
- **AND** no page that a step of the round applied and that is absent at that
  checkpoint has a row in the scope
- **THEN** the catch-up records the newest such checkpoint for that scope
- **AND** queues a lag-only catch-up for the newer events instead of a full
  repair
- **AND** the worker's finish line reports the result `advanced`, not `targeted`
- **AND** a scope without lag records nothing, and the round counts as advanced
  only when every lagging scope moved a stored checkpoint
- **AND** a round in which a lagging scope stays frozen declines as outpaced,
  and the full pass runs

#### Scenario: A write still publishing during the census walk is replayed, not refused

- **WHEN** the final census walk sees a page that a governed write changed,
  created or deleted before its event reached the registry
- **THEN** the census waits, while a write in the process holds a mutation
  boundary or has a publication intent still open for the path, and for a
  bounded time, for the registry to observe each path where the walk and the
  registry disagree
- **AND** replays and re-proves those paths before the census judges the walk
- **AND** declines the pass as `source_changed` at the census when the registry
  does not observe such a path in time, or when no write is in flight
- **AND** a delta that needs a source proof still excuses no path
