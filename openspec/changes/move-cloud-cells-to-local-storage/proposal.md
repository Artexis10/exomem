## Why

Every Exomem Cloud cell stores its data on its own 10 GiB Hetzner Cloud Volume. That shape has three costs that grow with customers:

- **Money.** A volume costs about €0.57 a month per cell. That is a quarter to a third of what one customer can cost to serve within the target margin.
- **Density.** A Cloud server takes at most 16 volumes, so a Cloud node can never hold more than 16 cells, however much memory it has.
- **Hardware choice.** Cloud Volumes attach only to Hetzner Cloud servers. The capacity Exomem needs next is monthly dedicated hardware: 64 GB, 16 threads, mirrored NVMe. Those machines have no Cloud Volumes at all.

Moving cells to node-local disk removes all three. It also removes the main durability property the volumes gave us: a lost server lost no data, because its volumes survived it. Today a cell is backed up only nightly, with the cell stopped, so on local disk a lost node would lose up to a day of every tenant's writes. This change therefore ships local storage together with hourly online backups and a rehearsed node-loss recovery, and moves no tenant until the drill passes.

## What Changes

- **Per-cell local volumes.** Cells store their data on a fixed-size logical volume on the node's local disk, provisioned through TopoLVM (a CSI driver):
  - One cell cannot read or fill another's volume, or the node's root disk.
  - The thin pool is never overcommitted.
- **Encryption.** Local cell storage is encrypted at rest at the node level: LUKS under the volume group on mirrored disks. Unlock is unattended against a key server on the control-plane node, and the recovery key is escrowed.
- **Online backups.** Cells are backed up hourly without being stopped:
  - The backup takes a crash-consistent volume snapshot, clones it read-only, and runs the existing per-cell restic backup against the clone, as a hold that does not stop the cell.
  - An alert fires when a cell's last good backup is over two hours old.
  - The stopped pre-upgrade backup and its restore-based rollback are unchanged.
  - Targets: RPO ≤ 1 h for node loss, and fleet RTO ≤ 4 h.
- **Node-loss recovery is an operation with a drill.** Once the operator confirms a lost node is stopped, its cells are recreated on surviving capacity from their latest backups, owner first. The old volume is kept until its node is confirmed destroyed. The drill runs on disposable infrastructure before any tenant moves, and again on production on a sacrificial recovery agent holding a seeded test cell.
- **No silent empty vault.** A cell that has ever been backed up refuses to initialise an empty vault on a fresh volume. It stays down for an operator restore rather than serving a blank vault in place of the tenant's.
- **Capacity from local storage.** Capacity counts a node's local storage pool, less a reserve for backup snapshots, instead of its volume-attachment limit. Removing a node checks that every cell still fits and relocates cells by backup and restore.
- **Dedicated nodes.**
  - A dedicated server joins the cluster as an agent over a private link, with encrypted mirrored local storage.
  - The control-plane node stays the server and ingress node. Once the cells move, it has no cell storage pool and so hosts none; until then it keeps serving and admitting cells as today.
  - An on-demand Cloud recovery agent can be created from Terraform, with its encrypted volume group on one large Cloud Volume, billed only while it exists.
- **Retiring Cloud Volumes.** Existing cells migrate one at a time, owner first, with both storage classes accepted until the last one moves. Each old volume is retained as the rollback copy until a soak ends. The Hetzner volume path is then retired.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `cloud-cell`: per-cell storage isolation, backup cadence and consistency, deletion proof, capacity, node-loss recovery, and the empty-vault guard.
- `cloud-node-pool`: agents publish local storage capacity; removal fits by bytes and relocates by backup/restore; a dedicated agent joins over a private link with encrypted local storage.
- `cloud-service-resource-policy`: the shared envelope's storage term and dedicated-cell relocation no longer assume an attachable volume.

## Impact

- **cellctl:** storage classes from configuration, the hourly snapshot-backup hold, the storage capacity term, fenced relocation, per-driver deletion proof, post-etcd-restore reconciliation.
- **Runtime:** the `cell-init` empty-vault guard, and fsync before rename in the JSON-state writers.
- **Platform chart:** TopoLVM, snapshot controller, the new StorageClass, admission rules for the clone source and the relocation delete, RBAC.
- **Policy:** a named exception for TopoLVM's node plugin.
- **Ansible:** the k3s role (driver per host, capacity wait); a new dedicated-host role for RAID1, LUKS, Clevis, the volume group and the private link; Tang on the server.
- **Terraform:** a private-link subnet and the optional recovery agent.
- **Elsewhere:** the capacity collector, and the cloud rehearsal (TopoLVM on a loop-device volume group).

**Sequencing:**
- This change archives after `adopt-exomem-cloud-plain-cells` and `add-cloud-service-resource-policy`, whose requirements it modifies.
- Phases that need a purchased dedicated server are marked as such in the tasks. The purchase itself is an owner decision outside this change.

**Not changed:** tenant authentication, the backup format and keys, per-cell restic repositories, the control database, and the cell resource limits.
