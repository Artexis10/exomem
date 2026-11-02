## Why

Writes still fail with `MUTATION_BUSY` when another writer holds the vault mutation boundary. On 2026-11-01, between 07:40 and 08:08 UTC, the personal service refused 20 writes this way: 10 `capture_source`, 6 `transfer_artifact`, 2 `edit_memory` and 2 `remember`. Source captures held the boundary back to back, for up to 31 seconds per source. From 07:00 to 09:00 UTC, 150 capture commits held it for 2,075 seconds in total.

Each refused path has its own reason:

- `capture_source` with files never waits, because a second attempt would fetch its files again. It refuses after one 5-second acquire.
- `remember` and `edit_memory` wait in line for up to 40 seconds and then refuse. These four calls ran for 59 to 125 seconds and still failed.
- `transfer_artifact` issues a credential. It carries no retry identity, so it refuses after one acquire.

A busy refusal gives the caller nothing to act on except to send the write again. The server can do that wait itself and keep the write safe meanwhile. Longer waits cannot remove the refusal. A holder can always outlast the wait, a waiting request holds a worker thread, and a wait does not survive a restart.

## What Changes

- **Accept instead of refuse.** An eligible write that cannot commit inline becomes an intent in a durable per-vault intent journal. The caller gets the committed result when the intent commits within 2.0 seconds of waiting. Otherwise the caller gets `state: "accepted"` and an `intent_id`.
- **One accept deadline.** Every wait before the answer ends 2.0 seconds after the request arrives: the inline acquire, the warm-up wait, the wait on an identical in-flight retry, and the collection-store admission. Preflight work that the synchronous path does anyway, such as validation and file fetches, is not waiting.
- **Uncontended writes do not change.** A write that finds no queued intent and a free boundary runs inline, as today. A capture with files also runs inline first. It joins the journal only when its first file meets a held boundary, and then it keeps the bytes it already fetched.
- **Accept checks everything that the caller controls.** Authorization, arguments, size limits, reserved paths, idempotency, file fetches and the command's own validation run before accept. A failed check returns its existing error, and the server records nothing. A write that depends on the caller's own queued write skips validation at accept, and the applier validates it later.
- **One retry identity.** The intent keeps the caller's retry key, the digest of the original arguments and the replay window. The applier commits under that key, so the existing receipts commit each intent at most once, also across a crash. A crash during an apply becomes an owner decision, never a silent re-run.
- **One applier per vault.** The applier applies intents in arrival order. It enters through the same command entry point as a request, with the caller's full principal, and it checks that the caller's login is still active before each dispatch.
- **Reconcile on the fly.** The applier runs each intent against the current vault. When the caller's own basis changed, the intent becomes a write-conflict review item with state `conflicted`. The owner then retries or discards it. The applier never overwrites or drops an intent silently.
- **Observe completion.** `review_memory(mode="write-intent", ref=...)` returns the state of an intent and can wait for it. A resend with the same retry identity returns the same intent, also after it ends. Direct reads and the recall status name the reader's own accepted writes that are not applied yet.
- **Busy only when the journal cannot accept.** `MUTATION_BUSY` remains for eligible writes only with `cause: journal_full` or `cause: journal_unavailable`. The caller-visible refusal journal records the cause of every busy refusal.

Delivered before this change, by the change that shortens capture holds, without a flag:

- Credential issuance (`transfer_artifact` upload and download, session open and rotate) takes writer authority and its own store lock instead of the vault boundary. This removes the six `transfer_artifact` refusals.
- Files 2 and later of a capture wait for the boundary through `absorbing_mutation_guard`, because their bytes are local. This fixes today's partial-capture outcome.

Out of scope: per-page locks, one intent per capture file, moving intents between machines, push notifications and the remote writer lease.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: The server accepts contended eligible writes into a durable intent journal within one accept deadline. One applier per vault commits each intent once under the caller's retry identity and principal. Accept validates what the caller controls. Conflicts become review items. A busy refusal remains only for a journal that cannot accept.
- `mutation-terminal-contract`: adds the nonterminal states `accepted` and `conflicted`, and limits `MUTATION_BUSY` for eligible writes to a journal that cannot accept.

## Impact

- Code: a new `src/exomem/write_intents.py` (journal and applier); `writer_lease.py` (accept deadline, accept path, applier dispatch, the receipt expiry, idle release); `client_artifacts.py` (moving staged inputs into the journal); `mutation_terminal.py`; `mutation_journal.py`; `commands.py` (`review_memory` and `triage_memory` modes); `vocabulary_state.py` (binding release); read and recall status; `studio/`; the CLI.
- Behaviour: a contended write returns committed or accepted, never busy. Agents must handle `accepted`, so the tool instructions and the scaffold reference change.
- Rollout: `EXOMEM_WRITE_INTENTS` (`off`, `on`, `always`). The applier drains existing intents in every mode.
- Order: lands after the auxiliary re-plan fix (#2297) and the capture-hold change. It removes the request-side contention loop for eligible commands, so net weight falls.
