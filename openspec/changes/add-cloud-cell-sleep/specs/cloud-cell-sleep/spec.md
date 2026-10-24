## ADDED Requirements

### Requirement: An idle, quiescent Cloud cell sleeps

cellctl SHALL put a Cloud cell to sleep when both of these hold:
- no tenant request has reached it for its configured idle period;
- the cell reports itself quiescent.

A sleeping cell SHALL run no pod. It SHALL keep its volume, its entitlement and its desired state. Sleep SHALL be recorded in an activity state separate from the desired state, and only cellctl SHALL write that activity state.

The idle period SHALL be deployment configuration with a per-cell override, and a cell MAY be configured never to sleep.

A cell SHALL report itself not quiescent while any of these is in progress: an initial or re-embed build, an import or upload session, a media job, derived, semantic or graph drain debt, or an unacknowledged write. Health probes SHALL NOT count as requests.

#### Scenario: An idle cell sleeps

- **WHEN** a cell's last tenant request is older than its idle period and the cell reports itself quiescent
- **THEN** cellctl scales the cell's pod to zero and records it asleep
- **AND** its volume and desired state are unchanged

#### Scenario: A cell with pending work stays awake

- **WHEN** a cell's idle period has passed while its initial embedding build is still running
- **THEN** the cell stays awake until the build finishes and it reports itself quiescent

#### Scenario: A never-sleep cell stays awake

- **WHEN** a cell is configured never to sleep
- **THEN** cellctl never records it asleep, however long it is idle

### Requirement: A request wakes a sleeping cell

The gateway SHALL record each cell's last request time and SHALL request a wake when a tenant request arrives for an asleep cell. The gateway SHALL NOT write Kubernetes objects, the cell's activity state or its desired state.

cellctl SHALL start the cell's pod after a wake request. When the cell is ready, cellctl SHALL record it awake.

The gateway SHALL hold the request, up to a configured hold budget, until the cell is ready, then forward it. If the budget runs out first, the gateway SHALL answer with a typed error, `CELL_WAKING`, that marks the request retryable and gives a retry delay.

#### Scenario: A tool call wakes the cell and succeeds

- **WHEN** a tenant's `tools/call` reaches an asleep cell and the cell is ready within the hold budget
- **THEN** the gateway forwards the call and the tenant receives its answer

#### Scenario: A slow wake answers with a retryable error

- **WHEN** an asleep cell is not ready when the hold budget runs out
- **THEN** the gateway answers `CELL_WAKING` with a retry delay
- **AND** the cell keeps waking, so a retry after the delay reaches it

### Requirement: Discovery does not wait for a wake

The gateway SHALL answer `initialize` and `tools/list` for an asleep cell from the last responses that cell gave on the same image. In the same request, it SHALL start a wake of the cell. If no such responses are recorded for that image, the gateway SHALL hold the request as for any other request.

The recorded responses SHALL contain no tenant content.

#### Scenario: Listing tools warms the cell

- **WHEN** a client lists tools for an asleep cell whose responses for its current image are recorded
- **THEN** the gateway answers at once from the recorded responses
- **AND** the cell starts waking before the client's first tool call

### Requirement: Holds run on sleeping cells

A sleeping cell SHALL remain eligible for backups, and its backup SHALL run against its volume without starting its pod.

An image change for a sleeping cell SHALL run its upgrade while the cell sleeps: the pre-upgrade backup, then the new image's state migration and readiness, then sleep again.

A wake that arrives during a hold SHALL wait for the hold within the hold budget.

#### Scenario: A sleeping cell is backed up

- **WHEN** a sleeping cell's backup is due
- **THEN** the backup runs against its volume and the cell stays asleep

#### Scenario: A sleeping cell upgrades before its next wake

- **WHEN** a sleeping cell's image changes
- **THEN** cellctl upgrades it while it sleeps and returns it to sleep
- **AND** its next wake starts the new image with no state migration

### Requirement: Capacity follows awake cells

cellctl SHALL publish two capacities:
- the total number of cells the node can hold, bounded by storage and volume limits;
- the number of cells that can be awake at once, bounded by node memory.

Substrate admission SHALL count non-deleted cells against the total. When a wake needs room and the awake set is full, cellctl SHALL first put the least recently used quiescent awake cell to sleep. It SHALL NOT put a cell that is not quiescent to sleep to make room. A wake that finds no room SHALL wait in arrival order.

#### Scenario: A wake makes room by sleeping the least recently used cell

- **WHEN** the awake set is full and a request wakes another cell
- **THEN** cellctl puts the least recently used quiescent awake cell to sleep first
- **AND** then wakes the requested cell

#### Scenario: A busy cell is never evicted

- **WHEN** the awake set is full and every awake cell has pending work
- **THEN** no awake cell is put to sleep
- **AND** the wake waits, and the gateway answers `CELL_WAKING` when its hold budget runs out

### Requirement: Sleep is visible

The tenant home SHALL show when the tenant's memory is asleep and wakes on the next request, and when it is waking. Operators SHALL see each cell's activity state, last request time, wake count and wake latency. Each value SHALL be computed when it is shown.

#### Scenario: A tenant sees a sleeping memory

- **WHEN** a tenant opens the home page while their cell is asleep
- **THEN** the page says the memory is asleep and wakes on the next request
