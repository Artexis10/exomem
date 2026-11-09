## MODIFIED Requirements

### Requirement: Background Boundary Holders Step Aside For Waiters In Any Process

Background work that holds the vault mutation boundary across several units, such as graph republication, derived row steps, collection store ticks and maintenance jobs, SHALL end its hold at its next unit boundary when an interactive writer waits for that boundary, whether the waiting writer runs in the holder's process or in another process on the same host. A waiter counts as interactive unless it is a machine hold (a drain, a graph build, a collection read), a maintenance job, or a borrower of a lent boundary; those never make a holder step aside, so background work does not yield to other background work. A keyed write that waits its turn in the capture line behind the head of the line counts as an interactive waiter. A waiting interactive writer SHALL signal its wait through an advisory lock beside the boundary's own lock file. The signal SHALL clear when the process stops waiting, acquires the boundary or exits, so a crashed waiter leaves no signal behind. On a platform without shared advisory locks the signal MAY cover only waiters in the holder's own process. Each unit SHALL still complete before the holder checks for waiters, so background work keeps making progress.

After a hold that ended while an interactive writer waited, the holder SHALL NOT take the boundary again before that writer has had its turn. It SHALL wait while an interactive writer still waits, at most the foreground-priority cap and within the progress share of its pass. That wait SHALL hold no lock, SHALL NOT run on a thread that holds a boundary, and SHALL NOT run on the collection store thread.

#### Scenario: A write in another process waits while background work holds the boundary

- **WHEN** a process holds the boundary for background work that runs in units
- **AND** a write in another process on the same host waits for the boundary
- **THEN** the background work releases the boundary at its next unit boundary
- **AND** the waiting write acquires the boundary within its wait bound

#### Scenario: A waiter that exits leaves no signal

- **WHEN** a process that waited for the boundary gives up, acquires it, or exits
- **THEN** background holders no longer see a waiter from that process
- **AND** they continue their units without stepping aside for it

#### Scenario: A machine hold waiting for the boundary is not a waiter

- **WHEN** a drain, a graph build or a maintenance job waits for the boundary while another holder works
- **THEN** that holder does not step aside for it
- **AND** an interactive write that waits at the same time makes the holder step aside

#### Scenario: A job does not take the boundary back before the waiting writer

- **WHEN** a maintenance job ends its hold because a writer in another process waits
- **THEN** the job waits, at most the foreground-priority cap, while that writer still waits
- **AND** the writer acquires the boundary before the job asks for it again

## ADDED Requirements

### Requirement: Maintenance Phases That Grow With The Vault Hold The Boundary In Short Holds

A maintenance or reconcile phase whose work grows with the vault, such as the refresh of unit sidecars and of embeddings, SHALL apply its items in batches under short holds of the boundary. A hold SHALL end after one second of work, when an interactive writer waits, or when the job is told to stop, and SHALL run at least one batch so the job progresses. Each batch SHALL read again under the hold what it applies. Every item SHALL be applied once across the holds. A hold SHALL carry a label that names its phase as a single token, because a holder label that fails sanitising is shown as `unknown` and hides the cause of a busy refusal. A phase bounded by a constant or by seconds MAY keep one hold.

#### Scenario: A long reconcile phase lets a writer in

- **WHEN** a reconcile phase applies many items under holds of the boundary
- **AND** an interactive write arrives while a batch runs
- **THEN** the write acquires the boundary after that batch, not after the last item
- **AND** the phase applies every item once

#### Scenario: A busy refusal names the phase

- **WHEN** a write is refused because a reconcile phase holds the boundary past its allowance
- **THEN** the holder in the refusal names the phase, not `unknown`
