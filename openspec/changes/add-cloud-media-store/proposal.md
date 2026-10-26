## Why

A Cloud cell keeps every original file, media included, on its one block volume. Recordings, video, scans and photos are most of a media-heavy vault's bytes, and they grow by tens of gigabytes a year for one active owner. Notes, extracted text and indexes stay small.

On today's network volumes this costs money but works, because a volume grows online. It does not work on the planned node-local storage (`move-cloud-cells-to-local-storage`):

- Each node reserves snapshot space of twice its largest cell per concurrent backup (design D6).
- A cell is capped at 20 GiB.
- The restore-time budget assumes cells of about 10 GiB (D4).

One 50 GiB media cell would reserve 200 GiB of a node pool of about 476 GiB. Per gigabyte, node-local NVMe costs about twenty times what object storage costs.

Originals must still stay byte-identical as evidence. The owner must be able to play them back, re-process them, export them and download what they uploaded.

## What Changes

- **Encrypted media store, per cell.** Every original that a governed sidecar binds by SHA-256 is written through to a per-cell object store. The cell encrypts it before it leaves. Object names are keyed digests, so the provider cannot match a stored object to a known file by name; object sizes stay visible.
- **The volume becomes a verified cache.** An original stays at its vault path until the cell needs the space. The largest, longest-idle originals leave the volume first, and only after their stored copy is confirmed. Notes, sidecars, extracted text, embeddings and datasets never leave the volume.
- **One more sidecar state.** A sidecar gains a governed stored-object reference that pins an exact stored version. A missing original with a reference is *evicted*, never deleted. Every reader and every deletion path resolves an original through one state function.
- **A media edge.** A new in-cluster service moves ciphertext between cells and the media bucket. It holds the only bucket credential, and that credential cannot destroy versions. It verifies a per-cell credential that cellctl signs, and it accepts the credential only while cellctl lists it as active. A selected cell gains one egress edge, to the edge's fixed address.
- **Separate bucket, retention by reachability.** Media sits in its own bucket, apart from backups. A stored version is destroyed only when no retained backup snapshot can still reference it, as proven by a reference manifest that each backup writes. Cell deletion revokes the edge credential first, and its proof covers the media bucket and the media key.
- **Export without the key in an egress pod.** A packing step that reaches only the edge decrypts evicted originals. A separate step without the key uploads the archive.
- **Downloads and playback.** An owner opens an original through a link that works only in a browser signed in to Exomem. Local agents use `exomem cloud download` over OAuth. No tool result carries a bearer link.
- **Storage quota.** The included media allowance is a configured value in the one limits source; the first value is 100 GB. Admission checks it before any byte arrives. The 20 GiB volume cap stays and stops being the media limit.
- **Default off, soft fail.** The store, the edge and eviction are off by default. A cell is enabled from a canary list or by a fleet switch. With the store unreachable, a cell keeps every original on its volume, queues offload work and evicts nothing.

No server-side model is added. Extraction keeps running inside the cell, before any eviction, under `add-cloud-multimodal-processing`.

## Capabilities

### New Capabilities

- `cloud-media-store`: the write-through invariant, original states and eviction, encryption and key custody, the media edge, bucket retention, downloads, quota admission, deletion proof and rollout switches.

### Modified Capabilities

- `automatic-media-processing`: an evicted original keeps its sidecar, transcript and derived rows, and re-processing hydrates it first.
- `hosted-vault-portability`: deletion fan-out treats eviction as a state, not a deletion, and export writes evicted originals byte-identical.
- `cloud-operator-access`: privacy copy discloses the media store and who holds the media key.
- `cloud-cell`: not canonical yet. The runtime egress rule gains the media edge peer through dated in-place amendments in `adopt-exomem-cloud-plain-cells` and `move-cloud-cells-to-local-storage`. This change carries no delta for it.

## Impact

- **Exomem (`src/exomem`):** the sidecar descriptor and `original_state`, hydrate and evict, the offload worker on the existing durable media job ledger, the reconciler, governance companions, deletion fan-out, export and download, and a filesystem-backed store for local tests.
- **cellctl and chart:** media key bundle minting and wrapping, the per-cell edge credential, NetworkPolicy and admission rules, the edge Deployment and Service, quota metering and deletion proof.
- **Terraform:** a media bucket with versioning, no version-deleting lifecycle, and its edge key.
- **Substrate (its own change):** the signed-in download link, the tus upload proxy and the split import admission. This change fixes the contract they build to.
- **Storage plan:** phase 7 of `move-cloud-cells-to-local-storage` depends on this change for any media-heavy cell. Phases 1–6 do not.
- **Privacy:** the operator holds the media key, as it holds the volume and backup keys. On the media-storage path, the provider and the media edge see only ciphertext. Playback through the gateway and the export archive keep their existing, disclosed exposure.
