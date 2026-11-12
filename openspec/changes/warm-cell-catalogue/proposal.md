# Proposal

## Why

A release that changes a Cloud cell's catalogue identity adds a full rebuild to its upgrade stop. The estimate of about eight minutes remains unverified; the frequency of these releases is also unverified. `automate-cloud-releases` task 5.3 requires decisions on cross-process adoption, a second pod's quota and key handover before implementation.

## What Changes

- **An optional catalogue-build Job, default off.** For each rollout candidate, `cellctl` schedules the target image beside the serving pod when the upgrade window opens. The Job builds only when the catalogue identity changes. It writes only staged files and coordination locks on the shared volume.
- **No build wait inside the attempt.** The attempt starts when due and deletes any unfinished build Job before stopping the cell. A failed, unfinished or skipped build leaves the replacement to rebuild cold.
- **Adoption at boot.** The cell validates a matching staged catalogue through a read-only connection. It installs that file over the complete live SQLite set under the publication lock, then uses the normal restart reconcile.
- **Cleanup by the volume's users.** The build Job removes other staged copies at startup. The booting cell removes every staged copy it did not adopt. Locks protect builds in progress.
- **Quota and node capacity.** One extra pod slot carries the build's requests and limits. Requests cover its measured or estimated peak; a separate per-node cap bounds concurrent builds.
- **Tenant-locked cells keep the cold rebuild.** The optional build does not join the custody key channel. Tenant-locked and non-running cells skip it.

## Capabilities

### New Capabilities

- `cloud-cell-catalogue-prewarm`: scheduling before attempts, staged files, cleanup, quota, node capacity and skip rules.

### Modified Capabilities

- `hosted-tenant-cell`: safe adoption at boot, restart reconcile and cleanup of every unadopted staged copy.

## Impact

- `infra/cellctl` and the platform chart: build scheduling, Job cancellation, pod affinity, admission rules, a quota slot and a per-node build cap.
- `src/exomem`: a staged-build entrypoint, private-state descriptors, boot adoption and staged-file cleanup.
- `openspec/specs/hosted-tenant-cell`: added adoption and cleanup requirements.
- Out of scope: catalogue identity changes, custody channel changes and changes to ordinary live publication.
