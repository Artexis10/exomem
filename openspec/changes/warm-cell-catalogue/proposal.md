# Proposal

## Why

A Cloud cell whose release changes the retrieval catalogue's identity rebuilds the catalogue on first start, which adds about 8 minutes to the upgrade stop on a production cell. 10 of 156 releases since 2026-09-10 changed the identity, so the worst stops in the fleet come from exactly these releases — while every other release now stops for about a minute. The upgrade's stop is where `automate-cloud-releases` design D9 parked this cost, pending a decision on cross-process adoption, a second pod's quota and the key handover for tenant-locked cells.

## What Changes

- **A catalogue warm-build Job.** Before a cell's upgrade attempt on a catalogue-identity release, a Job running the target image builds the cell's catalogue on the cell's volume, as a detached sibling keyed by the target's catalogue identity. The Job never publishes into the live sidecar, so the serving pod's reads and its repair worker are untouched.
- **Adoption at boot.** A cell that finds a staged sibling whose `catalog_identity` equals the identity its own code computes adopts it by rename instead of rebuilding, then reconciles the freshness lag between the staged checkpoint and the live vault with the ordinary delta path.
- **A staged-sibling sweep.** Cells and `cellctl` drop staged catalogues whose identity no current or pending target still wants, so a dead build cannot accumulate on the volume.
- **Quota headroom for the builder.** The cell namespace's `ResourceQuota` gains one warm-build slot — one extra pod, plus the requests and limits the build's resource profile needs — so the Job is admitted beside the serving pod without raising it to a second full serving quota permanently.
- **Tenant-locked cells keep the cold rebuild.** A warm-build Job cannot read a locked cell's content, and the custody contract binds a key handover to the replacement instance only. A locked cell skips the warm build and pays the rebuild inside its stop, which is already the price of a catalogue release under the lock.

## Capabilities

### New Capabilities
- `cloud-cell-catalogue-prewarm`: the staged catalogue build and its lifecycle — how `cellctl` schedules the build for a catalogue-identity release, what the Job may touch on the cell volume, how the staged sibling is named and swept, the quota slot it runs under, and which cells it skips.

### Modified Capabilities
- `hosted-tenant-cell`: a hosted cell at boot adopts a staged sibling catalogue whose stored identity equals its own computed identity, in place of a rebuild, and sweeps staged siblings no target still wants.

## Impact

- `infra/cellctl`: prepare-Job rendering for catalogue targets, an attempt step that runs the build before the cell stops, the `ResourceQuota` warm-build slot, and staged-sibling cleanup in reconcile.
- `src/exomem` (cell image): a build-sibling entrypoint for the Job, boot-time adoption in `lexstore`, and the staged-sibling sweep.
- `openspec/specs/hosted-tenant-cell`: one added requirement.
- Out of scope: catalogue-identity changes to `catalog_semantic_identity` itself, tenant-locked key handover beyond the replacement instance, and any change to the live sidecar's publish path.
