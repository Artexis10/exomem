## 1. Attribution

- [x] 1.1 Attribute the `materialize_replay` declines to the per-path proof on pages whose newer write the registry observes later.

## 2. Convergence

- [x] 2.1 Retarget an off-barrier detached replay to the registry's newer observation of each unproven path, a bounded number of times.
- [x] 2.2 Count the retargets of each full pass in `lexical_repair_finished`.

## 3. Proof

- [x] 3.1 Add replay cases for an event that arrives after the replay reads the page and for a page that changes after every retarget.
- [x] 3.2 Watch the late-event case fail on the unchanged code, the bound case fail against an unbounded build, and the unreported-edit census case fail against a build that blesses unproven paths.
- [x] 3.3 Measure repair passes by result and stage at the rehearsal's write rate before and after the change.
