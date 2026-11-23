## 1. Fan-out after the release

- [x] 1.1 Add `writer_lease.fanout_after_release(vault_root)`: bind a deferred-work queue around a door's body, drain it on exit, start the graph rebuild that the fan-out registered, and expose `derived_sync`.
- [x] 1.2 Let `add.deferred_fanout` take custody that its caller recorded before the commit, retire that custody by exact revision when the fan-out completes, and note its outcome for the enclosing scope.
- [x] 1.3 Add `add.full_refresh_custody`, which records full-refresh receipts for a batch's Markdown pages before the install, and records none in store mode or under fast-acknowledgement custody.

## 2. Upload doors

- [x] 2.1 Give `add.add` the `custody_before_commit` and `corpus_advisories` options, and pass the retained import original through the deferred commit.
- [x] 2.2 Give `preserve.preserve`, `preserve.preserve_stream` and `archive_members.preserve_members` the `defer_fanout_to_terminal` option.
- [x] 2.3 Wrap the Evidence upload, Source capture, archive manifest, Evidence session and Source session doors in `fanout_after_release`, and add `derived_sync` to their receipts.

## 3. Proof

- [x] 3.1 For Evidence, Source capture and Source session uploads, a fan-out slowed by 3 s leaves the boundary hold unchanged, a note save made meanwhile does not wait, the receipt says `completed`, and no custody is left behind.
- [x] 3.2 A fan-out stopped after the release leaves the page's custody and a `pending` receipt, and the next drain refreshes the page and retires the custody.

## 4. Live acceptance

- [ ] 4.1 After the release is live, the personal service's `mutation_lock_released` records show upload holds (`upload_preserve`, `upload_capture`, `upload_source_session`, `upload_manifest`) at a p99 of 1 s or less and a maximum of 2 s or less over at least 20 uploads.

## 5. Closure

- [ ] 5.1 When task 4.1 holds, synchronize the delta into `openspec/specs/hosted-mutation-safety/spec.md` and archive this change with `openspec archive`.
