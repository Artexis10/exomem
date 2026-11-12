# Proposal

## Why

A release that changes a Cloud cell's catalogue identity adds a full rebuild to its upgrade stop. The estimate of about eight minutes remains unverified; the frequency of these releases is also unverified. `automate-cloud-releases` task 5.3 requires decisions on cross-process adoption, a second pod's quota and key handover before implementation.

## What Changes

- **An optional build Job, default off.** For each rollout candidate, `cellctl` schedules the target image beside the serving pod when the upgrade window opens. The build Job builds only when the catalogue identity changes. It writes only staged copies, `.building` families and coordination locks on the shared volume.
- **Build activity defers the idle stop.** A running build Job counts as activity until the final force minutes, which default to 10. A build Job waiting for node capacity does not defer the attempt. Attempt deadlines stay unchanged; cancellation ends the build Job's pods before the stop.
- **Adoption at boot.** The cell validates any matching staged copy through a read-only connection. It installs that staged copy over the complete live SQLite set under the publication lock, then uses the normal restart reconcile. Without a valid staged copy, the replacement rebuilds cold.
- **Cleanup by the volume's users.** The build Job removes other staged copies at startup. The booting cell removes every staged copy it did not adopt. Locks protect builds in progress.
- **Quota and node capacity.** One extra pod slot carries the build's requests and limits. Requests cover its measured or estimated peak; a separate per-node cap bounds concurrent builds.
- **Backups exclude temporary catalogues.** Restic excludes staged copies and `.building` families by their fixed filename patterns, including SQLite sidecars.
- **Tenant-locked cells keep the cold rebuild.** The optional build does not join the custody key channel. Tenant-locked and non-running cells skip it.

## Capabilities

### New Capabilities

- `cloud-cell-catalogue-prewarm`: scheduling before attempts, build activity in the due rule, staged copies, backup exclusions, cleanup, quota, node capacity and skip rules.

### Modified Capabilities

- `hosted-tenant-cell`: safe adoption at boot, restart reconcile and cleanup of every unadopted staged copy.

## Impact

- `infra/cellctl` and the platform chart: build scheduling, the due rule, build Job cancellation, backup exclusions, affinity, admission, quota and a per-node build cap.
- `src/exomem`: a staged-build entrypoint, private-state descriptors, boot adoption and cleanup of staged copies and `.building` families.
- `openspec/specs/hosted-tenant-cell`: added adoption and cleanup requirements.
- Out of scope: catalogue identity changes, custody channel changes and changes to ordinary live publication.
