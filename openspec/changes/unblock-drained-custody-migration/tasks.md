## 1. Repair

- [x] 1.1 Reproduce the production refusal as a failing test: a fenced, never-enrolled generation requeued after its attestation window closed must migrate.
- [x] 1.2 Accept a closed window in the migration's inspect and prepare phases, in the schema-successor repair, in enrollment, and in enrollment's own read-back of the window it preserves.
- [x] 1.3 Keep every other proof refusing: expired signing key, future-dated control, wrong custody revision, generation not fully drained, generation still serving.
- [x] 1.4 Restate the two suites that asserted the old refusal so each still proves the half that remains true.

## 2. Verification

- [x] 2.1 Scoped suites green: governance migration coordinator, governance migration membership, governance provision membership, governance live readiness, governance target coordinator, authorization membership.
- [x] 2.2 Wider provisioner suites green: live provider, governance effect guards, rollforward live, provision live, target recovery, operation recovery, Helm guarded transition.
- [x] 2.3 Author-independent review of the relaxation: no path lets a serving or unauthenticated generation through. Forged bundles, serving and partially-drained generations, double enrollment and activation-target substitution were all constructed and all refused. Its three requested changes are delivered: the coordinator's issue-time guard and the custody-revision binding are now pinned by tests that fail when each guard is deleted, and enrollment bounds the issue time itself rather than trusting its caller.
- [ ] 2.4 Full applicable suite run at the delivery boundary.

## 3. Runtime Job window

- [x] 3.1 Reproduce the requeued failure: the target-image migration Job refuses inspect and prepare under a closed window, which the coordinator suite's Job double did not model.
- [x] 3.2 Make the Job double refuse a closed window the way the runtime Job does, and restate the tests that passed only against the double.
- [x] 3.3 Reissue a drained, never-enrolled source window before any inspect or prepare Job, never once a plan exists, refusing a signing key that ends before a Job could finish.
- [x] 3.4 Run the reissued bytes through the real runtime Job custody reader for inspect and prepare.
- [ ] 3.5 Replay the closed-window case in the K3s governance drill.

## 4. Delivery

- [ ] 4.1 Publish the repaired provisioner through the governed release workflow and select it in the deployment lock pair.
- [ ] 4.2 Recover the stranded 2026-09-13 cell through the existing same-operation governance requeue and record the evidence.
