# Tasks

## 1. Cell image: staged build and adoption

- [ ] 1.1 Add the detached staged-build path and its private-state ownership descriptors. Cover `.lexical-staged-*`, `.building` families and coordination locks in `reserved_paths`. Use D5's shared uid and TMPDIR; keep the stable lock file between contenders. Verify: concurrent builders serialize, self-gates avoid duplicate work, and lock-aware cleanup preserves active WAL families despite old main-file timestamps. Killed builds leave no torn adoptable copy.
- [ ] 1.2 Add the narrow build CLI entrypoint without ordinary runtime initialization. Verify: each self-gate exits successfully, target code builds a valid staged copy, and all other shared-volume files remain byte-identical.
- [ ] 1.3 Add read-only validation and boot adoption through the existing publication helpers for the whole live SQLite set. Use `checkpoint_state`, `_reconcile_live` and `_heal_delta` for restart catch-up, never the Job's process-local delta history. Verify: matching copies avoid a full parse, ordinary edits heal, invalid copies rebuild cold, and a killed pod's WAL cannot replace adopted rows. Failed installation never mixes generations.
- [ ] 1.4 Add the boot cleanup of every unadopted completed staged copy. Verify: copies matching the live, computed or superseded identity all disappear unless adopted; active temporary files retain their lock protection.

## 2. cellctl: the build Job before the attempt

- [ ] 2.1 Render the target-image Job with required pod affinity to the serving pod, using the export pack pattern. Use the live claim, runtime security context, shared uid and TMPDIR, no-egress metadata and D2's peak-sized requests. Verify: manifest tests cover placement, target, resources and shared locks without `nodeName` or `priorityClassName`.
- [ ] 2.2 Admit the Job's exact name, affinity, mount and isolation metadata without relaxing other placement restrictions. Verify: chart tests admit the rendered Job and reject arbitrary affinity, node assignment and priority classes.
- [ ] 2.3 Add one namespace quota slot and enforce D2's per-node build cap separately from attempt concurrency. Verify: quota admits the full build profile beside existing headroom; a second builder cannot occupy the same node.
- [ ] 2.4 Schedule builds for eligible candidates when their windows open, outside attempts. Cancel unfinished Jobs before due stops, on timeout, pause, target change or window closure. Pass observed-success eligibility with the pinned attempt to boot and record the outcome. Verify: attempts never wait for build completion, cancellation ends Job pods before stopping, and a cancellation-time rename cannot become adoptable. Existing backup and stall clocks remain unchanged.
- [ ] 2.5 Skip tenant-locked and non-running cells; document the default-off flag, scheduling, cancellation, custody exception and cleanup in the runbook. Verify: both skip states create no Job, including an unlocked tenant-locked cell, and the runbook describes the rendered defaults.

## 3. Rehearsal and rollout

- [ ] 3.1 Rehearse an identity change, an unfinished build and insufficient node capacity on a disposable cluster. Verify: logs show adoption, restart catch-up, unchanged unrelated state, cancellation before the stop and cold fallback. Record resource peaks, the Job timeout and stop times in the delivery evidence.
- [ ] 3.2 Release the cell image before cellctl, keeping `cellctl.cataloguePrewarm` off until rehearsal passes. Verify: disabling the flag cancels unfinished Jobs and ordinary upgrades retain their cold-rebuild path.
- [ ] 3.3 Measure real per-cell stops after enabling the feature and update `automate-cloud-releases` task 2.2. Verify: cited rollout evidence replaces the unverified duration estimate.
