## Context

The cell image and cellctl image are consumed by digest: cellctl rolls out the `cell_image` setting, the platform chart pins `cellctl.image`, and the ValidatingAdmissionPolicy admits any `<cell repository>@sha256:<64 hex>` reference. The release attests each image with `actions/attest` and retains its signed index receipt as a release asset.

Measured on Hetzner cx33 and cax21 (2026-10-10): the arm64 cloud stage builds in 70 s; an amd64 restic binary in an arm64 image exits 126; bge-m3 int8 vectors of the same chunk have cosine 0.9685–0.987 between architectures and 1.000000 within one.

## Goals / Non-Goals

**Goals:** one release runs on x86 and Arm64; verification stays as strict on amd64 as today; an Arm64 cluster can be built with the existing Terraform and Ansible.

**Non-Goals:** mixed-architecture clusters, Arm64 agent types, reviving retired Hosted image roles, an Arm64 CUDA image, and any product code change for vector portability.

## Decisions

### Pin and attest the index digest

`docker/build-push-action` adds a provenance manifest to every pushed image, so the published `cloud` tag and `exomem-cellctl:0.95.0` are already OCI image indexes, and their attestation referrer tag names the index digest. A multi-platform index adds one platform manifest to the same kind of object. The release keeps pinning, attesting and retaining the index digest in the signed release receipt; Kubernetes pulls the matching platform manifest, which the index binds by content address. No verifier in the repository checks a platform-manifest digest, so no per-platform attestation is added.

### Native runners, joined by digest

The current release plan owns five image roles: lean, ml, cuda, cloud and cellctl. Only a role with `publish: true` builds. Native Cloud and cellctl builds run upstream on `ubuntu-latest` and `ubuntu-24.04-arm`. Each runner builds both missing roles and pushes only by digest, with architecture-specific caches and BuildKit provenance `mode=min,version=v0.2`. Each leg retains its successful child digests as Actions artifacts. A role failure does not suppress the other role.

One serialized `publish-image` job retains cross-release ownership of discovery and floating tags. It rechecks original release and producer evidence before using new native children. Authentic historical receipts remain valid, including single-platform receipts; recovery never rebuilds their images. For a missing role, the existing recovery helper joins a complete native pair with Buildx, validates exactly two runtime platforms and the provenance descriptors, and hashes the dry-run index bytes. It publishes that index directly to its digest destination, checks the exact bytes by digest, and verifies BuildKit target provenance before attesting.

The publisher attests only the joined index and retains the existing receipt filenames and artifact prefixes. `finish-images` alone uploads release receipts and writes immutable, discovery and latest aliases after checking every role for conflicts. Its finalizer runs after role failures, completes unaffected roles, and reports missing evidence as an incomplete release. A missing native artifact cannot become successful absence.

A dry run builds both native platforms and the other three roles. It performs no registry login, push, attestation, receipt upload or alias write.

### CUDA stays amd64

The CUDA image targets NVIDIA x86 hosts. The retired Hosted runtime and provisioner are outside this change and stay retired.

### One cluster, one architecture

Images run on both architectures, but a cell's vectors do not move between them unchanged. The fleet server type admits `cx33` or a Hetzner CAX type (the family prefix is the architecture check); agents keep the x86 allow-list. Two checks enforce one architecture per cluster: Terraform refuses agents beside a CAX fleet server, and the K3s role refuses an agent whose architecture differs from the server's. The Exomem images are not the whole cluster: the platform chart also runs the Substrate gateway, published for `linux/amd64` only at the 2026-10-15 inspection, so the runbook checks every image the cluster runs for the target platform before any node changes.

To rebuild a moved cell's vectors, the operator stops the cell, moves the recall sidecar files (`.embeddings.*` in the cell's derived state) aside on its volume, and starts it. The cell's own initial build recreates the index and serves during the build. Doctor's `embeddings.reembed` and `embeddings.sidecar` checks show when it is done. `maintain_memory --mode fix --rebuild-embeddings` is not used: fix mode also rewrites notes, and it reports success when the rebuild fails.

## Risks / Trade-offs

- An arm64 runner outage blocks Cloud image publication for a release. The finalizer publishes unaffected roles and reports the missing role as incomplete. A retry recovers authentic receipts before rebuilding anything.
- The runbook's move, doctor reader and delete ran against the amd64 cloud image under Docker, not on a real moved cell, and its `kubectl` steps have not run against a cluster.
