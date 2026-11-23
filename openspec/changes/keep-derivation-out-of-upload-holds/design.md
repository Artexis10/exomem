## Context

An upload door copies its bytes into a private stage before it takes the vault mutation boundary. Under the boundary it plans the pages, installs the stage and writes the batch. Until this change, `batch_atomic_write` then ran `post_commit_batch_fanout` inside the same hold. That fan-out refreshes the lexical store, the resolver, the epistemic graph and the embeddings, and its cost grows with the corpus.

The background media worker already uses a shorter shape:

1. It records full-refresh receipts for the pages it will write.
2. It commits with the fan-out off.
3. It runs the fan-out after the release.
4. It retires the receipts by exact revision.

This change gives the upload doors the same shape.

## Decisions

### One scope runs deferred work after the release

`writer_lease.fanout_after_release(vault_root)` binds a deferred-work queue around a door's body. The leaf queues its fan-out through `defer_until_terminal_persisted`, the seam that governed commands already drain after their terminal. When the scope exits, the boundary has been released, so the scope drains the queue and starts any graph rebuild that the fan-out registered. A direct guard does the same on its own release.

The Source upload session commits through `invoke_command`. That command binds and drains its own queue after its own release, so the scope cannot see the work in its queue. The fan-out therefore notes its outcome on a second context variable, which the scope reads to build `derived_sync`.

### Custody comes before the install

`add.full_refresh_custody` records full-refresh receipts for the batch's Markdown pages while the boundary is held and before the batch installs them. `deferred_fanout(receipts=...)` uses these receipts for the graph hand-off and clears them only when the fan-out completes. A failed or stopped fan-out leaves them for the drain. A store-mode vault and an active fast-acknowledgement session record nothing, because something else already owns their derived work.

### Recall admission is read at fan-out time

The fan-out runs the ordinary `upsert_after_write` pipeline on the committed paths. That pipeline partitions the paths by recall policy from current disk. It also revalidates the partition before dispatch, re-checks admission per path in the lexical store, the embeddings and the graph, and requeues a batch whose identity drifted during the dispatch. A write that lands between the release and the fan-out is therefore indexed with the admission of the bytes that the fan-out reads.

### `derived_sync` is honest per request

The receipt says `completed` only when this request's fan-out ran and every component finished. It says `pending` in every other case: a component deferred or failed, the fan-out did not run, or the request replayed a stored result. A fan-out that reported nothing, as in store mode, counts as unverified. A failed fan-out also maps to `pending`, because its custody stays queued and the next drain finishes it.

## Risks

- Full-refresh revisions restart at 1 after a drain deletes a row. Suppose the drain retires an upload's receipt, and a later failed refresh of the same page re-creates it at revision 1 before the upload's fan-out clears its custody. The clear can then remove that newer receipt. The window needs a drain and a failed refresh of the same page inside one upload's fan-out. A monotonic queue clock on the full-refresh queue closes it.
- The upload response still waits for the fan-out. This change shortens the hold, not the request.
