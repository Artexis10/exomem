## Why

Exomem Cloud must be able to run on any provider's x86 or Arm64 machines. Today each release publishes the Cloud cell and cellctl images for `linux/amd64` only, the cell image always fetches the amd64 restic binary, the K3s role refuses an Arm64 node, and Terraform pins the fleet server to `cx33`. Production stays on x86; this change makes an Arm64 cluster possible without changing what x86 runs.

## What Changes

- Each release publishes the Cloud cell image and the cellctl image for `linux/amd64` and `linux/arm64` as one image index per tag. Each platform builds natively on its own runner, and a join job creates the index.
- The release pins, attests and records the index digest. This is the digest kind the release already attests: today's single-platform images are already image indexes, because each holds a provenance manifest.
- Every binary that a Dockerfile downloads (restic in the cell image, Helm in the provisioner image) is selected by target architecture and checked against a sha256 pinned for that architecture. An unknown architecture fails the build. The restic stage runs the binary, so a native build fails on a binary for another architecture.
- A path-filtered pull-request workflow builds both images natively on amd64 and arm64 and runs restic, the CLI version and a cell save-and-find on each.
- The K3s role pins the K3s binary per architecture and accepts `aarch64`. Terraform accepts a Hetzner CAX (Arm64) fleet server alongside `cx33`. One cluster runs one architecture; agent types stay x86.
- The node replacement runbook adds the architecture change, with a vector index rebuild for each moved cell: vectors differ between architectures.
- The hosted and CUDA images stay `linux/amd64` only.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `container-runtime-profiles`: Publish the Cloud cell and cellctl images as one multi-platform index per tag, pinned and attested by the index digest.
- `cloud-node-pool`: A cluster runs on one CPU architecture, x86 or Arm64; the K3s binary is pinned per architecture.

## Impact

`.github/workflows/release-please.yml` (Cloud and cellctl publication), a new `.github/workflows/cloud-images.yml`, the root `Dockerfile` restic stage, `infra/provisioner/Dockerfile`, the K3s Ansible role, the Terraform foundation server type, and the node runbooks. The amd64 build inputs, the tags, the release-note lines and the attestation subject kind are unchanged. Cell admission already accepts any digest-pinned reference from the cell repository, so no admission or cellctl change is needed. The hosted runtime, provisioner and CUDA publication paths are unchanged.
