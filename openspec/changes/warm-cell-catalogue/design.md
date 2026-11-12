# Design

## Context

See proposal.md for motivation. The load-bearing facts from the code as it stands:

- The retrieval catalogue is one SQLite sidecar at `<vault state dir>/.lexical.sqlite`, on the cell's volume. `catalog_semantic_identity(vault_root)` hashes the catalogue schema version, parser version, semantic-authoring contract and the vault's note-type registry, so the identity a target image computes on a vault is exactly the identity the upgraded pod will compute.
- `rebuild_atomic` already builds a detached sibling under a cross-process publication lock, captures each scope's freshness checkpoint, replays the delta onto the build, and publishes with a single `os.replace`. It already declines when the identity changes mid-build.
- The serving pod reads the live sidecar through open connections and pinned transactions; a file renamed underneath it does not disturb it — but a re-opened mismatched file reads as stale, and the stale path schedules a repair that would republish under the *old* identity.
- Cell volumes are `ReadWriteOnce`: several pods on the same node may share one, and the existing restore/backup Jobs already mount the cell volume beside the serving pod.
- The cell namespace `ResourceQuota` gives the serving pod plus a bounded number of small Job slots (1 GiB limit, 100m/256Mi requests each).
- Tenant-locked cells (`make-cloud-cells-operator-resistant`) wrap the cell root key under the tenant's key. Only a handover bound to the *replacement instance* moves content access without the tenant.

## Goals / Non-Goals

**Goals:**
- On a catalogue-identity release, a standard-mode cell's stop excludes the catalogue rebuild.
- The serving pod's catalogue reads, its repair worker and the live sidecar see zero effect from the build.
- Adoption is atomic and cannot adopt a torn or wrong-identity file.
- A failed, timed-out or skipped build never blocks the upgrade.

**Non-Goals:**
- Changing `catalog_semantic_identity` or the catalogue schema.
- Warm-building tenant-locked cells (decided below: they keep the cold rebuild).
- Faster rebuilds, smaller catalogues, or changes to the live sidecar's publish path.
- Image preparation, which `automate-cloud-releases` D9's pull-only prepare Job already covers.

## Decisions

### D1 — Staged sibling, adopted by rename at boot

The build Job writes a *staged sibling*: `<state dir>/.lexical-staged-<identity-prefix>.sqlite`, beside the live sidecar. It builds into a `.building` temp name and renames to the staged name only after the catalogue is complete, folded to one file and its `meta` rows (identity and each scope's checkpoint) are written — the same discipline `rebuild_atomic` uses to publish, with the destination changed from the live path to the staged name.

The booting cell scans for staged siblings, opens each candidate, and adopts the one whose *stored* `catalog_identity` equals its own computed identity by `os.replace` onto the live path. Validation reuses the store's existing current-checks (schema currency, meta identity, checkpoint presence). The freshness lag the staged checkpoint carries is then caught up by the ordinary delta path: `rebuild_atomic`'s replay machinery and the lag-repair seam already handle a catalogue behind its vault.

Chosen over two alternatives:

- **Publish into the live path at build time** (reuse `rebuild_atomic` wholesale). Between the build and the pod swap, the serving pod's next connection would open a file whose identity differs from its own. Its reads go stale *before the upgrade even starts*, and its repair worker would republish under the old identity — stomping the staged build and flapping the live file. Staging never touches the live path, so the serving pod is provably undisturbed.
- **Build inside the serving pod** (an in-process or exec'd warm). The serving pod runs the *old* image, which cannot compute the new identity's parser or contract components — it could only rebuild its own identity. The build must run target code, so it must be a second process, and on the cell's node so the RWO volume attaches.

Filename keys the sweep and the Job's idempotence check only; adoption always re-reads the stored identity, so a misnamed file can never be adopted.

### D2 — One warm-build slot in the namespace quota, sized like the serving pod

The build is the same workload the cold rebuild runs at boot — a full-corpus parse — so the Job gets the serving pod's resource *limits* (today 2 CPU / 3 GiB, read from the same values) while keeping Job-sized *requests* so it stays schedulable. The quota gains exactly one slot: `pods +1`, `requests.cpu/memory` headroom for the Job's requests, `limits.cpu/memory` headroom for a second serving-shaped limit. Requests headroom is small; limits headroom costs nothing while the Job does not exist.

Chosen over squeezing the build into the ordinary 1 GiB Job slot, which would throttle or OOM the one workload whose duration is the point of the change. The alternative — raising Job headroom for every Job — would grow every Job's ceiling for one Job's need.

Node capacity is the real constraint, not quota. Builds run inside the cell's upgrade window (its measured quiet hour), at most one per cell attempt, and `upgradeConcurrencyPerNode` already bounds how many attempts a node runs at once. A node that still cannot fit the Job sees it pend; the attempt's build timeout then proceeds cold — correct, if unlucky.

### D3 — Tenant-locked cells keep the cold rebuild

No warm build runs for a tenant-locked cell. The build needs to read vault content; under the custody contract the only mechanism that moves content access without the tenant is a one-use handover bound to the *replacement instance's* ephemeral key, one per cell at a time. A build Job is not the replacement, and inventing a second handover consumer adds custody surface — an ephemeral content key leaving the serving pod for a second workload — for the rarest case in the fleet (locked cells × the ~6% of releases that change the identity).

The trade-off is explicit: a tenant-locked cell keeps the ~8-minute rebuild inside its stop on catalogue releases, exactly as today. The gateway hold budget is already sized for that stop class, and the runbook documents it.

### D4 — The build runs inside the attempt, after the backup, bounded by a build timeout

The attempt sequence gains a step: backup → catalogue-build Job → stop → recreate → adopt. Building inside the attempt rather than earlier in the window keeps the staged checkpoint's lag at roughly build-length, so adoption's delta catch-up stays small. The attempt waits on the Job's terminal state up to a build timeout (sized above the measured rebuild, initially 15 minutes); on success, failure or timeout it proceeds — the worst case is today's cold rebuild.

Running the build earlier (when the cell first becomes due) was considered and rejected for now: it saves nothing user-visible — the cell is already serving — while growing the delta the adoption must replay and adding bookkeeping for builds detached from attempts.

### D5 — Single-flight and idempotent by construction

`cellctl` runs at most one attempt per cell, and the Job declines when a staged sibling already carries its identity or a `.building` file is younger than the build timeout — so a retried attempt or a rescheduled Job never duplicates work or races a live build. The rebuild temp-lock convention (`lexical-rebuild-*.lock` in the vault's private lock directory, on the shared volume) gives cross-process single-flight for free if the running cell ever starts a same-identity build of its own.

### D6 — Sweep on two sides, one rule

`cellctl` reconcile deletes staged siblings whose identity belongs to no current or pending target, and stale `.building` temps older than the build timeout. The booting cell applies the same rule with the knowledge it has (its own computed identity plus the live sidecar's). Two sweepers are safe because the rule is monotone: both only ever delete things no current path can adopt.

## Risks / Trade-offs

- [Build extends the attempt by up to ~8–15 min inside the window] → acceptable: the cell serves throughout; the rollout slot is held longer but windows are per-cell and the quiet hour is the point. Bounded by the build timeout; a timeout costs at most build-timeout + cold rebuild.
- [Two catalogue copies on the cell volume during the build] → the namespace's storage quota already carries a staging bound (`staging_bound_gib`); the catalogue is a fraction of the vault and the sweep bounds retention to pending targets.
- [CPU contention with the serving pod during the build] → builds run in the measured quiet hour; the Job's limits cap it at serving-pod size; a contended cell may respond slower for the build's duration but never stops early.
- [A late vault write burst makes the staged catalogue lag more than the delta can cheaply replay] → adoption still succeeds and the ordinary reconcile escalates to a full rebuild only when the delta is unprovable — the cold-rebuild worst case, no worse.
- [A catalogue-identity release that also changes the adoption code path itself] → adoption validates before swap and falls back to rebuild on any failure; the cell image ships adoption *before* cellctl ships the Job, so no Job can produce a sibling no released pod can adopt.

## Migration Plan

1. Ship the cell-image side first, behind no flag: staged-sibling build entrypoint, boot-time adoption, and the boot sweep. Without the Job it is inert.
2. Ship `cellctl` behind chart flag `cellctl.cataloguePrewarm` (default off): Job render, attempt step, quota slot, skip rules, reconcile sweep.
3. Prove on the rehearsal cluster with a fabricated catalogue-identity release: staged sibling appears, new pod adopts, stop stays in the non-identity band.
4. Enable the flag on the platform; record the first catalogue-identity rollout's stop times for the `automate-cloud-releases` measurement task.

Rollback: set the flag off — no new Jobs run; staged files already on volumes are inert (old pods ignore them, new pods sweep or adopt-or-discard). Reverting the cell image is also safe: the staged naming is inert to pods that never learned it.

## Open Questions

- The concrete build-timeout and Job request values land after the rehearsal measures a real build; the spec holds only the shape (bounded profile, timeout above the measured rebuild).
- Whether a later custody change lets a build Job receive a scoped handover — deliberately deferred; the skip is designed to be lifted without touching adoption.
