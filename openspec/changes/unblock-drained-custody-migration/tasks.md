## 1. Repair

- [x] 1.1 Reproduce the production refusal as a failing test: a fenced, never-enrolled generation requeued after its attestation window closed must migrate.
- [x] 1.2 Accept a closed window in the migration's inspect and prepare phases, in the schema-successor repair, in enrollment, and in enrollment's own read-back of the window it preserves.
- [x] 1.3 Keep every other proof refusing: expired signing key, future-dated control, wrong custody revision, generation not fully drained, generation still serving.
- [x] 1.4 Restate the two suites that asserted the old refusal so each still proves the half that remains true.

## 2. Verification

- [x] 2.1 Scoped suites green: governance migration coordinator, governance migration membership, governance provision membership, governance live readiness, governance target coordinator, authorization membership.
- [x] 2.2 Wider provisioner suites green: live provider, governance effect guards, rollforward live, provision live, target recovery, operation recovery, Helm guarded transition.
- [ ] 2.3 Author-independent review of the relaxation, specifically that no remaining path lets a serving or unauthenticated generation through.
- [ ] 2.4 Full applicable suite run at the delivery boundary.

## 3. Delivery

- [ ] 3.1 Publish the repaired provisioner through the governed release workflow and select it in the deployment lock pair.
- [ ] 3.2 Recover the stranded 2026-09-13 cell through the existing same-operation governance requeue and record the evidence.
