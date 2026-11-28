## 1. Refusal of declared Records values before queueing

- [x] 1.1 Add `record_memory.value_refusal`, which reads a collection's declared fields as a request thread reads them, within a fixed time, without a lock and without recording a release.
- [x] 1.2 Apply it in `commands.declared_refusal_before_queueing`, only for a save about to queue, and pass its refusal through the terminal egress filter. Keep raising an argument refusal when that filter faults.
- [x] 1.3 Mark the queue and compare with the last possible declaration change after a refusal. Track new inserts and rollback inserts. Cover legacy saves with one seek at upgrade without scanning their arguments or history.
- [x] 1.4 Tests: an undeclared value behind the queue is refused and never queued, for a row whose real key and guards the commit accepts; a declared value still queues; behind a queued revision, an update of the row by its id queues and its replay saves the value; behind a queued edit of the manifest named by title or reference, the value queues; an argument refusal still comes back when the egress filter faults.
- [x] 1.5 Check the Planning and declared field rules on store-owned saves. Share the dispatch route and bound writer arguments, and give the value refusal only when it is the commit’s first. Leave file-backed saves and edits by `item_id` to replay.
- [x] 1.6 Prove direct and busy store saves give the same first refusal. Cover an undeclared value, an explicit null status, guarded edits, and a disabled summary route. Prove a file-backed add queues and its replay gives the direct refusal.
- [x] 1.7 Give the busy decision one budget and cap Planning's store SQL work. Cover upgrade and rollback, a revision that commits during the read, a later ordinary save, slow opens, locked store readers, and a store publication. Cover an authorisation proof that exceeds the SQL allowance.
- [x] 1.8 Refuse a Cloud read-only write before intake or store routing. Prove the refusal with an existing queue and a busy store.
- [x] 1.9 Give a Records value refusal only when it is the writer's first. Prove direct and busy parity for current and stale guards in both storage modes.

## 2. Release

- [ ] 2.1 After the release, check that a busy Records add with an undeclared value and a busy add to a store-owned Planning collection with an undeclared status are refused at once. Confirm that neither enters the intake.
