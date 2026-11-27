## 1. Intake store and drainer

- [x] 1.1 Add `write_intake.py`: the `.write-intake.sqlite` store under the private-state ceremony (WAL, `synchronous=FULL`), accept with origin-key de-duplication, claim, finish, list, read, dismiss and pruning of applied rows after 7 days.
- [x] 1.2 Add the drainer thread: acceptance-order application through `invoke_command` as the stored principal with the `intake:<id>` identity, contention back-off from 1 s to 30 s, failure recording, owner liveness for rows left applying by a dead process, a sticky commit-reached marker per replay identity written at the commit gate (fail closed), a fresh identity for a replay that stopped before its commit, a login recheck before each replay, an in-memory origin index, and no polling while idle.
- [x] 1.3 Register the store in `reserved_paths` and start and stop the drainer in the serving HTTP runtime.
- [x] 1.4 Start the drainer before activation's start-up waits (after a cloud cell's staged catalogue adoption), so a save that meets a busy boundary while a new or promoted worker still warms up is queued too.
- [x] 1.5 End a replay's waits for a busy boundary, a warm-up or its own preparation when the drainer stops; keep the save queued when the refusal proves nothing committed; keep the writer lease while a replay still runs at the end of the stop.

## 2. Acceptance in the dispatcher

- [x] 2.1 Add the `busy_intake` registry field and declare it on the knowledge saves.
- [x] 2.2 Add the `invoke_command` decorator: eligibility, waiting behind a non-empty queue, acceptance of a contention refusal, and the queued receipt; return the same receipt to a resend of a queued origin, derived from the dispatcher's own key and payload digest; fall through to the save on any intake fault.
- [x] 2.3 Cap the busy wait at the intake wait when the save is eligible.
- [x] 2.4 Carry eligibility per operation (`OperationEntry.queues_when_busy`) so the episode workflow steps and a Records import job keep the busy refusal.

## 3. Visibility

- [x] 3.1 Add `review_memory(mode="queued-write")` with and without `ref`.
- [x] 3.2 Add `write_intake` to `coordination_status`, `unknown` when unreadable.
- [x] 3.3 Add `exomem intake list`, `exomem intake show <ref>` and `exomem intake dismiss <id>`.
- [x] 3.4 Describe the queued receipt in the shipped `mutation-results` reference and regenerate the derived client artifacts.
- [x] 3.5 Name failed queued saves once in the next successful reply to the owner's eligible save (`write_intake.with_failure_notice`).

## 4. Proof

- [x] 4.1 Tests: a busy eligible save returns a queued receipt and lands once after the hold; an ineligible write keeps `MUTATION_BUSY`; a resend while queued returns the same receipt; a replay after a crash that followed the commit writes nothing new; a stale queued edit fails visibly with its arguments; a later save never overtakes a queued one; only the owner reads queued saves, never a delegated session; a typed MCP edit is queued and applied; the same explicit key with two payloads lands two pages; a commit marker that cannot be written commits nothing; an outcome-unknown replay fails visibly and the queue moves on; a replay fault before the dispatcher fails one row only; an unreadable intake store does not block an ordinary save; an idle drainer leaves identities alone; a revoked login fails its save.
- [x] 4.2 Outcome check on a disposable vault copy with a real service: hold the boundary with a long synthetic writer, send saves from two clients, kill the service mid-queue, restart, and record that every save landed exactly once.
- [x] 4.3 After the release is live, the personal service's journal shows queued receipts where it showed `MUTATION_BUSY` on eligible saves, and the intake drains to zero.
