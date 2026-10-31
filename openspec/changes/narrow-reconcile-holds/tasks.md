## 1. Measure

- [ ] 1.1 Profile each phase of a cold and a warm reconcile on a disposable copy of a real-sized vault.
- [ ] 1.2 From the profile, group the phases into holds under design D1, or stop and report.

## 2. Implement

- [ ] 2.1 Add a reconcile narrowing row in `writer_lease`, honouring `EXOMEM_WIDE_MUTATION_BOUNDARY`.
- [ ] 2.2 Remove reconcile from `WHOLE_RUN_MODES` and keep the graph join outside every hold.
- [ ] 2.3 Give each group from 1.2 its own hold, with a bounded busy wait and stop checks.
- [ ] 2.4 Re-read each phase's inputs inside its hold under design D2.
- [ ] 2.5 Apply only the purge entries that the plan and the fresh read share, and report the rest.

## 3. Verify

- [ ] 3.1 Prove that a foreground write commits between two holds of a CLI reconcile and of a job.
- [ ] 3.2 Prove that a purge entry that moved is reported and left unchanged.
- [ ] 3.3 Repeat the profile from 1.1 and record the longest hold before and after.
- [ ] 3.4 Run Ruff, the privacy gate, `openspec validate --all --strict` and the full suite.
- [ ] 3.5 Obtain an independent review, then merge.
