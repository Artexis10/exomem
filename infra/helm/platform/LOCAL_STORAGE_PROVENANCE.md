# Local cell storage provenance

`cellStorage.local.enabled` installs what cells on local disks need
(`move-cloud-cells-to-local-storage`). The values ship off.

- **TopoLVM:** the official chart `17.2.0` from `https://topolvm.github.io/topolvm`,
  resolved into `Chart.lock`. Its default image reference is already
  `ghcr.io/topolvm/topolvm-with-sidecar:0.41.1@sha256:70548dbe0c6addcccf79a557f29e95db2e6dc2cba2102988c91f30086004d0fc`,
  which also carries the CSI sidecars. `infra/policy/kubernetes.rego` names its two privileged
  DaemonSets at this chart version and release name.
- **Snapshot CRDs:** `files/external-snapshotter/` holds the three
  `snapshot.storage.k8s.io` CRDs from
  [kubernetes-csi/external-snapshotter](https://github.com/kubernetes-csi/external-snapshotter)
  tag `v8.6.0` (commit `78e32cd84e0abec2621924a30e38c755f93e180a`, `client/config/crd/`),
  byte for byte. The group-snapshot CRDs are left out: the controller only watches them when its
  `CSIVolumeGroupSnapshot` gate is on, and it is off by default.
- **Snapshot controller:** `registry.k8s.io/sig-storage/snapshot-controller:v8.6.0`, pinned by its
  image index digest `sha256:81e79f205083f105e573ba2ebfe5964ede7f69a154e8627f53ebbbccd3ed9f70`.
  Its RBAC is the release's `deploy/kubernetes/snapshot-controller/rbac-snapshot-controller.yaml`
  without the group-snapshot rules. The tag's own deployment manifest still names image `v8.5.0`,
  which is what the storage spike (task 1.1) ran, so this image is not yet rehearsed.

`files/external-snapshotter/SHA256SUMS.txt` records the upstream digests of the three CRDs, and
`infra/scripts/validate.sh` checks the copies against it.

To bump any of them:

1. Copy the three CRDs from the new tag's `client/config/crd/`.
2. Regenerate `SHA256SUMS.txt` from that upstream directory.
3. Update the controller image digest from the registry.
4. Run the rehearsal's storage spike. Expect the snapshot and clone round trip to pass.
