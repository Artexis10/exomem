# Hetzner CSI provenance and LUKS contract

The platform pins the official `hcloud-csi` chart at `2.21.1`. The chart was
resolved from `https://charts.hetzner.cloud` into `Chart.lock`; its controller
and node driver both use the immutable multi-platform image index
`docker.io/hetznercloud/hcloud-csi-driver:v2.21.1@sha256:79b979d2fc7b46fdddab19e619c65faa201d0d76080765f0ec4b1969e0abe33f`.

The matching upstream source is tag `v2.21.1`, commit
`1dd5776c2810f80f038454c9333a3814a2319b1b`. Its Kubernetes guide documents
node-publish Secret references on a StorageClass, and `internal/driver/node.go`
reads the exact `encryption-passphrase` key from `NodePublishVolume` Secrets.
The mount implementation and integration tests identify initialized encrypted
devices as `crypto_LUKS`. These are the interfaces used by
`exomem-hcloud-encrypted-retain`; changing the chart, image, Secret key, or K3s
minor version requires a fresh real-volume create/mount/remount test.

Source evidence:

- <https://github.com/hetznercloud/csi-driver/blob/v2.21.1/docs/kubernetes/getting-started.md#volumes-encrypted-with-luks>
- <https://github.com/hetznercloud/csi-driver/blob/v2.21.1/internal/driver/node.go>
- <https://github.com/hetznercloud/csi-driver/blob/v2.21.1/internal/volumes/mount.go>
- <https://github.com/hetznercloud/csi-driver/blob/v2.21.1/test/integration/volumes_test.go>

Static validation proves the pinned chart renders on the K3s `1.35` Kubernetes
API surface and that the StorageClass passes the supported node-publish Secret
parameters. The real-account encrypted-volume drill remains a deployment gate;
static compatibility is not treated as proof that Hetzner can attach a volume.
