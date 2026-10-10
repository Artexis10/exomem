## 1. Upkeep says why it is unavailable

- [ ] 1.1 Record the gate's waiting reason in the Dreamer's sidecar health when it changes, and on the first idle tick.
- [ ] 1.2 Add the closed `reason` to the unavailable upkeep response, from the hosting process's live state or the recorded health.
- [ ] 1.3 Prove a worker held by its gate reports `no_tick_yet` to a process without the worker, and that schema-mismatched and unreadable sidecars name themselves.

## 2. Per-family effect

- [ ] 2.1 Stamp the family at the due-state carrier and the attention surface.
- [ ] 2.2 Add the pure per-family aggregation over a window.
- [ ] 2.3 Serve the `effect` block in the dispositions view, owner-only, with its window and sources.
- [ ] 2.4 Prove the counts on a vault with an open, a dismissed, a deleted, an unattributed and an audit-only item.

## 3. A due row's fingerprint addresses its own signal

- [ ] 3.1 Publish each due-state row's stored fingerprint on the block and through the write terminal.
- [ ] 3.2 Resolve a request carrying that fingerprint from the stored entry and one page re-check, falling back to the whole-vault path when the re-check does not reproduce it.
- [ ] 3.3 Scope a fingerprint-bound reopen of a single-category item to that item's records.
- [ ] 3.4 Prove a due row is read and triaged with its fingerprint with no whole-vault audit, a bare reference still resolves the fused item, and reopening the due signal leaves a fused dismissal alone.
