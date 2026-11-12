# Tasks

## 1. Cell image: staged build and adoption

- [ ] 1.1 Add the staged-sibling build path to `lexstore`: build a detached catalogue under a `.building` temp name, write its identity and scope checkpoints, fold to one file, rename to `.lexical-staged-<identity-prefix>.sqlite`, and self-gate on the live sidecar's stored identity, an existing staged sibling, or a `.building` temp younger than the build timeout. Verify: unit tests cover each self-gate exit, the identity-keyed naming, and a torn `.building` left by a killed build producing no adoptable sibling.
- [ ] 1.2 Add the cell CLI verb the Job runs (build the staged sibling for the cell's vault and exit, reporting built / already-staged / identity-match on its outcome line). Verify: the verb exits 0 on each self-gate path and writes a valid staged sibling on a vault that needs one.
- [ ] 1.3 Add boot-time adoption before any rebuild: scan staged siblings, read each stored identity, adopt the one matching the computed identity by atomic rename after the store's current-checks pass, then let the ordinary delta path reconcile the staged checkpoint's lag. Verify: tests cover adopt-on-match (no rebuild runs), discard-and-rebuild on a sibling that fails validation, and rebuild with no sibling; a seeded lag proves the delta path, not a full rebuild.
- [ ] 1.4 Add the boot sweep of staged siblings matching neither the live sidecar's identity nor the booting code's identity. Verify: a test shows a superseded target's sibling removed at boot while the current and staged-for-self siblings survive.

## 2. cellctl: the build Job and the attempt step

- [ ] 2.1 Render the catalogue-build Job in `manifests.py`: the target image, the cell's node via `nodeName`, the cell volume mounted read-write, the runtime security context, no-egress policy metadata, and the D2 resource profile (Job-sized requests, serving-sized limits). Verify: manifest tests pin the mount, node placement, image, security context and resource profile.
- [ ] 2.2 Admit the build Job in the cellctl scope `ValidatingAdmissionPolicy` (naming, nodeName, the cell-volume mount, the no-egress set) and in the isolation policy's parameter shape. Verify: chart-render tests assert the policy admits the rendered Job the way the prepare-Job tests do.
- [ ] 2.3 Grow the cell namespace `ResourceQuota` by the D2 warm-build slot: `pods +1` and the Job profile's requests and limits headroom. Verify: manifest tests pin the new hard limits, and a quota test shows the Job admitted beside the serving pod plus the ordinary Job headroom.
- [ ] 2.4 Run the build inside the attempt between the backup and the stop, wait on the Job's terminal state up to the build timeout, record its outcome on the attempt, and proceed on success, failure or timeout. Verify: reconcile tests cover each terminal path and the recorded outcome.
- [ ] 2.5 Skip the build for tenant-locked and non-running cells. Verify: a reconcile test shows no Job created for either state on a catalogue-identity target.
- [ ] 2.6 Sweep dead staged siblings and `.building` temps older than the build timeout during reconcile, keyed on the pending targets and live identities. Verify: a reconcile test removes a rolled-back target's sibling and a stale temp while keeping the pending target's.

## 3. Rehearsal and rollout

- [ ] 3.1 Rehearse a fabricated catalogue-identity release on the disposable cluster: the build Job runs beside the serving cell, the new pod adopts the sibling, and the measured stop stays inside the non-identity band. Verify: the rehearsal log shows the staged sibling, the adoption, and the stop time; record it in the ledger and here.
- [ ] 3.2 Ship behind `cellctl.cataloguePrewarm`, runbook updated with the flag, the build timeout, the locked-cell exception and the sweep behaviour. Verify: the runbook's upgrade section names the step and its skip rules, and the flag defaults off until the rehearsal passes.
- [ ] 3.3 Measure the first real catalogue-identity rollout's per-cell stops and feed the medians into `automate-cloud-releases` task 2.2. Verify: the measured figures replace the design's estimated saving line.
