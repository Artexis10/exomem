## 1. Intake store and drainer

- [x] 1.1 Add `write_intake.py`: the `.write-intake.sqlite` store under the private-state ceremony (WAL, `synchronous=FULL`), accept with origin-key de-duplication, claim, finish, list, read, dismiss and pruning of applied rows after 7 days.
- [x] 1.2 Add the drainer thread: acceptance-order application through `invoke_command` as the stored principal with the `intake:<id>` identity, contention back-off from 1 s to 30 s, failure recording, owner liveness for rows left applying by a dead process, a sticky boundary-held marker per replay identity, and a fresh identity for a replay that stopped before it held the boundary.
- [x] 1.3 Register the store in `reserved_paths` and start and stop the drainer in the serving HTTP runtime.

## 2. Acceptance in the dispatcher

- [x] 2.1 Add the `busy_intake` registry field and declare it on the knowledge saves.
- [x] 2.2 Add the `invoke_command` decorator: eligibility, waiting behind a non-empty queue, acceptance of a contention refusal, and the queued receipt; return the same receipt to a resend of a queued origin identity.
- [x] 2.3 Cap the busy wait at the intake wait when the save is eligible.

## 3. Visibility

- [x] 3.1 Add `review_memory(mode="queued-write")` with and without `ref`.
- [x] 3.2 Add `write_intake` to `coordination_status`, `unknown` when unreadable.
- [x] 3.3 Add `exomem intake list`, `exomem intake show <ref>` and `exomem intake dismiss <id>`.
- [x] 3.4 Describe the queued receipt in the shipped `mutation-results` reference and regenerate the derived client artifacts.

## 4. Proof

- [x] 4.1 Tests: a busy eligible save returns a queued receipt and lands once after the hold; an ineligible write keeps `MUTATION_BUSY`; a resend while queued returns the same receipt; a replay after a crash that followed the commit writes nothing new; a stale queued edit fails visibly with its arguments; a later save never overtakes a queued one; only the owner reads queued saves.
- [x] 4.2 Outcome check on a disposable vault copy with a real service: hold the boundary with a long synthetic writer, send saves from two clients, kill the service mid-queue, restart, and record that every save landed exactly once.
- [ ] 4.3 After the release is live, the personal service's journal shows queued receipts where it showed `MUTATION_BUSY` on eligible saves, and the intake drains to zero.
