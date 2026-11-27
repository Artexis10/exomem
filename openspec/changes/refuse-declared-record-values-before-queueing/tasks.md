## 1. Refusal of declared Records values before queueing

- [x] 1.1 Add `record_memory.value_refusal`, which reads a collection's declared fields as a request thread reads them, within a fixed time, without a lock and without recording a release.
- [x] 1.2 Apply it in `commands.declared_refusal_before_queueing`, only for a save about to queue, and pass its refusal through the terminal egress filter. Keep raising an argument refusal when that filter faults.
- [x] 1.3 Keep the save queued while an open queued save may change the declared fields: a command that declares `sets_any_frontmatter_field`, or a Records save that carries a manifest's text.
- [x] 1.4 Tests: an undeclared value behind the queue is refused and never queued, for a row whose real key and guards the commit accepts; a declared value still queues; behind a queued revision, an update of the row by its id queues and its replay saves the value; behind a queued edit of the manifest named by title or reference, the value queues; an argument refusal still comes back when the egress filter faults.
- [x] 1.5 Add `plan_memory.value_refusal`, which runs the Planning commit's rule for each governed field and the per-field rules of the declared fields, through the same declaration read as Records. Keep a deprecated or removed value queued only when the item that the save names stores it, as the commit's rule does.
- [x] 1.6 Tests: a Planning status outside the Planning values, for an add, an update and a triage, and a custom field's undeclared value behind the queue are refused and never queued, as the direct call refuses them; a declared value still queues; behind a queued Planning revision, a value it declares queues and is saved; an update that keeps its own removed status queues and is saved, while an update of another item to that status is refused; a title over Planning's bound still queues; a store-owned Planning item is read beside the busy store thread for both cases.

## 2. Release

- [ ] 2.1 After the release, check on the served vault that a busy `add_record` with an undeclared value, and a busy `plan_memory` add and update with a status outside the Planning values, are refused at once and that the intake holds no copy of either.
