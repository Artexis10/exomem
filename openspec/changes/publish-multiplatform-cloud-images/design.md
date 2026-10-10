## Context

The cell image and cellctl image are consumed by digest: cellctl rolls out the `cell_image` setting, the platform chart pins `cellctl.image`, and the ValidatingAdmissionPolicy admits any `<cell repository>@sha256:<64 hex>` reference. The release attests each image with `actions/attest` and writes its digest into the release notes.

Measured on Hetzner cx33 and cax21 (2026-10-10): the arm64 cloud stage builds in 70 s; an amd64 restic binary in an arm64 image exits 126; bge-m3 int8 vectors of the same chunk have cosine 0.9685–0.987 between architectures and 1.000000 within one.

## Goals / Non-Goals

**Goals:** one release runs on x86 and Arm64; verification stays as strict on amd64 as today; an Arm64 cluster can be built with the existing Terraform and Ansible.

**Non-Goals:** mixed-architecture clusters, Arm64 agent types, an Arm64 hosted runtime, provisioner or CUDA image, and any product code change for vector portability.

## Decisions

### Pin and attest the index digest

`docker/build-push-action` adds a provenance manifest to every pushed image, so the published `cloud` tag and `exomem-cellctl:0.95.0` are already OCI image indexes, and their attestation referrer tag names the index digest. A multi-platform index adds one platform manifest to the same kind of object. The release keeps pinning, attesting and recording the index digest; Kubernetes pulls the matching platform manifest, which the index binds by content address. No verifier in the repository checks a platform-manifest digest, so no per-platform attestation is added.

### Native runners, joined by digest

Each platform builds on its own runner (`ubuntu-latest`, `ubuntu-24.04-arm`) and pushes by digest only. A join job creates the index with `docker buildx imagetools create`, reads it back through the commit tag, and requires its platforms to be exactly `linux/amd64,linux/arm64` before it attests. Standard runners, the arm64 one included, are free for this public repository. The amd64 cloud and cellctl builds took 1.7–2.6 minutes of the last three releases' `publish-image` job; the two legs run in parallel, so the extra cost is about one more leg plus a one-minute join job, and the Cloud images no longer wait behind the 16–26 minute CUDA build. QEMU would add the emulated model fetch and load gates of the cloud stage to the serial `publish-image` job on every release.

### Hosted and CUDA images stay amd64

The hosted runtime and provisioner belong to the legacy hosted platform, whose candidate verification and admission run on x86 nodes only, and the provisioner's postgres base is pinned to its amd64 manifest. The CUDA image targets NVIDIA x86 hosts, and its build is the slowest in the release.

### One cluster, one architecture

Images run on both architectures, but a cell's vectors do not move between them unchanged. The fleet server type admits `cx33` or a Hetzner CAX type (the family prefix is the architecture check); agents keep the x86 allow-list. Two checks enforce one architecture per cluster: Terraform refuses agents beside a CAX fleet server, and the K3s role refuses an agent whose architecture differs from the server's. The Exomem images are not the whole cluster: the platform chart also runs the Substrate gateway, published for `linux/amd64` only today, so the runbook checks every image the cluster runs for the target platform before any node changes.

To rebuild a moved cell's vectors, the operator stops the cell, moves the recall sidecar files (`.embeddings.*` in the cell's derived state) aside on its volume, and starts it. The cell's own initial build recreates the index and serves during the build. Doctor's `embeddings.reembed` and `embeddings.sidecar` checks show when it is done. `maintain_memory --mode fix --rebuild-embeddings` is not used: fix mode also rewrites notes, and it reports success when the rebuild fails.

## Risks / Trade-offs

- An arm64 runner outage blocks Cloud image publication for a release. Mitigation: rerun the failed leg; the hosted and OSS images publish independently.
- The runbook's move, doctor reader and delete ran against the amd64 cloud image under Docker, not on a real moved cell, and its `kubectl` steps have not run against a cluster.
