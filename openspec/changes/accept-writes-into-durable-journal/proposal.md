## Why

Writes still fail with `MUTATION_BUSY` when another writer holds the vault mutation boundary. On 2026-11-01, between 07:40 and 08:08 UTC, the personal service refused 20 writes this way: 10 `capture_source`, 6 `transfer_artifact`, 2 `edit_memory` and 2 `remember`. Source captures held the boundary back to back, for up to 31 seconds per source. From 07:00 to 09:00 UTC, 150 capture commits held it for 2,075 seconds in total.

Each refused path has its own reason:

- `capture_source` with files never waits, because a second attempt would fetch its files again. It refuses after one 5-second acquire.
- `remember` and `edit_memory` wait in line for up to 40 seconds and then refuse. These four calls ran for 59 to 125 seconds and still failed.
- `transfer_artifact` issues a credential. It carries no retry identity, so it refuses after one acquire.

A busy refusal gives the caller nothing to act on except to send the write again. The server can do that wait itself and keep the write safe meanwhile. Longer waits cannot remove the refusal. A holder can always outlast the wait, a waiting request holds a worker thread, and a wait does not survive a restart.

## What Changes

- **Accept instead of refuse.** Some writes meet a held boundary, a full waiting line, an overdue holder or a warming component. The server records each such write as an intent in a durable per-vault intent journal. The caller gets the committed result when the intent commits within the existing wait. Otherwise the caller gets `state: "accepted"` and an `intent_id`.
- **Uncontended writes do not change.** A write that finds the journal empty and the boundary free runs inline, as today.
- **Accept checks everything that the caller controls.** Authorization, arguments, size limits, reserved paths, idempotency, file fetches and the command's own semantic validation run before accept. A failed check returns its existing error, and the server records nothing.
- **One applier per vault.** The applier applies intents in arrival order when the boundary is free. It sends each intent through the existing command path with the intent's retry identity. Existing receipts then commit each intent once, also across a crash.
- **Reconcile on the fly.** The applier runs each intent against the current vault. It plans navigation files again, applies content-relative edits to current content and follows a moved page through its stable reference. When the caller's own basis changed, the intent stops as a write-conflict review item. The owner then retries or discards it. The applier never overwrites or drops an intent silently.
- **Observe completion.** `review_memory(mode="write-intent", ref=...)` returns the state of an intent and can wait for it. A resend with the same retry identity returns the same intent. Direct reads and the recall status name the reader's own accepted writes that are not applied yet.
- **Busy only when the journal cannot accept.** `MUTATION_BUSY` remains only with `cause: journal_full` or `cause: journal_unavailable`. The caller-visible refusal journal records the cause of every busy refusal.
- **Captures stop blocking other writes.** A capture with files stages the fetched bytes into the journal at accept. The applier commits one file per step, so other intents commit between the files of a long capture.
- **Credential issuance stays synchronous.** The server never journals `transfer_artifact` or session open and rotate, because their result is a bearer. They take writer authority and their own store lock instead of the vault boundary.

Out of scope: per-page locks, moving intents between machines, push notifications and the remote writer lease.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: The server accepts contended eligible writes into a durable intent journal. One applier per vault commits each intent once. Accept validates what the caller controls. Conflicts become review items. A busy refusal remains only for a journal that cannot accept. Credential issuance leaves the vault boundary.
- `mutation-terminal-contract`: adds the nonterminal `accepted` state, and limits `MUTATION_BUSY` for eligible writes to a journal that cannot accept.

## Impact

- Code: a new `src/exomem/write_intents.py` (journal and applier); `writer_lease.py` (accept path, applier dispatch, idle release); `client_artifacts.py` (staged inputs); `mutation_terminal.py`; `mutation_journal.py`; `commands.py` (`review_memory` and `triage_memory` modes); read and recall status; `studio/`; the CLI.
- Behaviour: a contended write returns committed or accepted, never busy. Agents must handle `accepted`, so the tool instructions and the scaffold reference change.
- Rollout: `EXOMEM_WRITE_INTENTS` (`off`, `on`, `always`). The applier drains existing intents in every mode.
- Order: lands after the auxiliary re-plan fix (#2297) and the change that shortens capture holds. It removes the request-side contention loop for eligible commands, so net weight falls.
