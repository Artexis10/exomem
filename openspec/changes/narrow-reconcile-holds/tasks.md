## 1. Measure

- [ ] 1.1 Profile a cold and a warm write-mode reconcile on a disposable copy of a real-sized vault, and record each phase's wall time and boundary hold.
- [ ] 1.2 From the profile, list the phases that write canonical state and the derived phases whose coordinator already serializes them against ordinary writes.

## 2. Implement

- [ ] 2.1 Compute the census once per write-mode run.
- [ ] 2.2 Give each canonical phase its own boundary hold, with the input re-check from design D2 and a skip record in the report and the job receipt.
- [ ] 2.3 Run the derived phases from 1.2 outside the canonical boundary.
- [ ] 2.4 Drop reconcile from `WHOLE_RUN_MODES` in `maintenance_jobs.py`, so that a reconcile job no longer takes the wide guard for the whole leaf.

## 3. Verify

- [ ] 3.1 Prove that a foreground write commits between two reconcile phases, and that a phase whose inputs moved is skipped and reported.
- [ ] 3.2 Repeat the profile from 1.1 and record the longest hold before and after.
- [ ] 3.3 Run Ruff, the privacy gate, `openspec validate --all --strict` and the full suite, then obtain an independent review and merge.
