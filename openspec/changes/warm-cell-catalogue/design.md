# Design

## Context

See proposal.md for motivation. These current contracts constrain the design:

- `catalog_semantic_identity(vault_root)` includes the schema, parser, authoring contract and vault note-type registry. `cellctl` cannot compute this identity from an image alone.
- `rebuild_atomic` already builds detached catalogues and publishes under a cross-process lock. Its publication helpers treat the main file, `-wal` and `-shm` as one set.
- Freshness event histories belong to one process. `checkpoint_state` excludes that local history; `_reconcile_live` and `_heal_delta` handle ordinary restart drift.
- `decide.py` stops the cell before backup. Its backup deadline is 15 minutes; its stall bound is 30 minutes. Both start at the attempt's hold.
- RWO volumes permit pods on the same node. This Job introduces the first read-write mount of the live claim beside a serving pod. Hourly backups read a snapshot clone; upgrade backups wait for the serving pod to release the claim.
- Attempts are fleet-serial today. `automate-cloud-releases` task 1.9 has not added per-node attempt concurrency.
- Admission forbids `nodeName` and `priorityClassName`. Export pack Jobs already use required pod affinity for shared placement.

## Goals / Non-Goals

**Goals:**

- Move the full parse outside the stop when a valid staged catalogue is ready.
- Preserve serving reads, normal attempt timing and cold-rebuild fallback.
- Adopt without mixing SQLite generations or accepting an invalid catalogue.

**Non-Goals:**

- Change catalogue identity, the custody channel or ordinary live publication.
- Accelerate the rebuild itself or warm-build tenant-locked cells.

## Decisions

### D1 — Adopt a staged catalogue over the whole live SQLite set

The Job builds a detached catalogue beside `<state dir>/.lexical.sqlite`, using a unique `.building` temporary file. It writes the identity and scope checkpoints, folds the catalogue to one file, then renames it to `.lexical-staged-<identity-prefix>.sqlite`. The filename supports discovery; adoption trusts only the stored identity.

Before opening the live catalogue, the booting cell validates a candidate through an observational read-only connection. Validation checks schema currency, stored identity and checkpoint presence without enabling WAL. Under the publication lock, adoption reuses `_live_set_disposition`, `_quiesce_live_wal` and `_publish_over_quarantined_set`.

Healthy live sets fold before replacement. Missing mains with orphan sidecars and proven-corrupt sets use whole-set quarantine. Failed installation restores the prior set or keeps it isolated. An ordinary busy set makes adoption decline. A bare main-file rename is unsafe because a killed pod can leave a WAL that attaches to the replacement.

After adoption, `checkpoint_state` compares projected state across processes. `_reconcile_live` uses the ordinary restart walk and `_heal_delta` for changed rows. No process replays the Job's event history. Unprovable drift can still cause a full rebuild, including a content change that preserves its timestamp. Building earlier increases this catch-up work, but the same restart path handles it correctly.

Reuse the existing SQLite publication and restart paths instead of adding a store or delta protocol. The custom additions are staged-file ownership, boot selection and the Job entrypoint. They require lifecycle and boundary tests. Publishing directly to the live path would expose the new identity to old code and trigger its repair worker. Building inside the serving pod cannot run the target parser.

### D2 — One quota slot with requests that cover the build peak

The Job uses serving-sized limits from the existing resource values, currently 2 CPU and 3 GiB. Requests cover the measured build peak. Until rehearsal supplies that measurement, use those limits as the conservative estimate for requests. Small ordinary Job requests would admit a build whose real memory use could evict a serving neighbour. The namespace quota adds exactly one pod plus this profile's requests and limits, preserving ordinary Job headroom.

Required pod affinity pins the Job to its serving pod through `kubernetes.io/hostname`, following the export pack pattern. A local PV also constrains the node. Keep `nodeName` forbidden so the scheduler checks capacity. Keep `priorityClassName` forbidden: capacity requests avoid granting cellctl control over pod preemption. Insufficient capacity leaves the build pending until cancellation or timeout; the upgrade proceeds cold.

Detached builds need their own cap despite today's fleet-serial attempts. Allow at most one build per node until task 1.9 accounts for builds alongside attempts. A cell also gets at most one build Job. Raising every ordinary Job's ceiling would add unrelated capacity; the dedicated slot bounds this workload alone.

### D3 — Tenant-locked cells keep the cold rebuild

Skip every tenant-locked cell, including an unlocked tenant-locked cell. Custody binds replacement handover to the replacement instance's ephemeral key. It also allows backup and media Jobs to use the key channel. The optional build does not join that channel because another content-key consumer adds custody work for a latency optimization. This conservative skip changes no correctness; the replacement keeps today's cold rebuild. Non-running cells also skip the build.

### D4 — Build before the attempt; cancel before the stop

For each rollout candidate, schedule the target Job when its upgrade window opens, subject to D2 and D3. The Job computes the target identity and self-gates, so scheduling does not require cellctl to read the catalogue. The feature defaults off. Canary and disabled-window rules still determine when attempts are due; an immediate attempt can have no warm-build opportunity.

When the cell becomes due, start its attempt under the existing eligibility rules and clocks. Do not wait for a build result. Delete any unfinished build Job and confirm its pods have ended before stopping the serving pod. Only success observed before the attempt starts makes staged adoption eligible for that attempt. Pass that eligibility with the pinned attempt to the replacement, so a rename during cancellation cannot make a late build adoptable. Record the build outcome with the attempt.

The existing sequence remains stop → backup → recreate → adopt or rebuild. A timeout, pause, target change or closed window deletes an unfinished Job; cancellation never leaves a builder beside the replacement. A missing or ineligible staged file means a cold rebuild. Building inside the attempt would consume its backup and stall deadlines. Building earlier accepts restart catch-up instead of delaying the idle or forced stop.

### D5 — Locks prove liveness; file age does not

Use the existing interprocess lock primitive for one stable staged-build lock per cell. Hold it across startup cleanup, self-gates, building and staged publication; a contender declines without waiting. Keep its coordination file between contenders. This serializes builders without holding the live publication lock during the parse.

`_RebuildTempLock` protects each unique temporary file from cleanup; it does not serialize builders. The Job uses the serving pod's uid and `TMPDIR=/data/scratch/tmp`, so both resolve locks on the shared volume. Cleanup checks `_RebuildTempLock.held()`, every SQLite family member's age and open connections, following the existing orphan sweep. WAL writes need not update the main file's timestamp. Age alone never proves that a build died.

### D6 — Cleanup belongs to the builder and booting cell

Under the staged-build lock, a starting Job removes every completed staged copy except a valid copy of its own computed identity. It then exits successfully if that copy exists or the live identity matches. It removes abandoned `.building` families only through D5's liveness checks.

At boot, the cell removes every staged copy it did not adopt, including copies matching its own or the old live identity. It applies D5 to unfinished families. `cellctl` neither reads identities nor sweeps files: it has no exec or filesystem channel. Job cancellation before the stop prevents a late publisher from leaving a duplicate after this cleanup.

The Job may change only staged families and their coordination locks on the shared volume. It must not initialize or migrate another store through target-image startup. Identity and schema reads must remain observational. This restriction covers all state, not only the live catalogue.

## Risks / Trade-offs

- [Catch-up grows between build and boot] → reuse restart reconcile; accept a cold rebuild when the drift cannot be proved.
- [Build memory could evict a neighbour] → request the estimated peak and cap builds per node; never bypass scheduling.
- [CPU contention slows serving reads] → use the upgrade window and bounded resources; measure contention in rehearsal.
- [Image pull and startup consume the pre-attempt window] → skip unfinished builds without delaying attempts. Fleet-serial attempts still bound rollout throughput.
- [Staged files consume volume space] → retain only the current builder's completed copy, then remove every unadopted copy at boot.
- [Target startup changes another store] → use a narrow entrypoint and prove the rest of the shared volume stays unchanged.

## Migration Plan

1. Ship the cell entrypoint, adoption and cleanup first; without scheduled Jobs, the build path stays inert.
2. Ship scheduling, cancellation, admission and quota behind `cellctl.cataloguePrewarm`, default off; existing upgrades still rebuild cold.
3. Rehearse an identity change and cancellation on a disposable cluster; record adoption, unchanged state, resource peaks and stop times.
4. Enable the feature through the ordinary platform release; record real stop times for the parent change's measurement task.

Rollback disables the flag and cancels unfinished Jobs. Old images ignore staged files; new images adopt eligible copies or discard them. No catalogue schema or custody change needs reversal.

## Open Questions

- Rehearsal replaces the conservative resource estimate and sets the bounded Job timeout. Neither value changes the attempt deadlines or scheduling decision.
