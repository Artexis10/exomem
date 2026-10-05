## Context

Cells today mount PVC `cell-data` on the StorageClass `exomem-cloud-encrypted`. That class uses `csi.hetzner.cloud` with reclaim Delete, WaitForFirstConsumer and LUKS through one shared node-publish secret. cellctl renders the class in `infra/cellctl/src/cellctl/manifests.py`, and the chart's admission policy pins it.

Several parts of the platform assume an attachable Hetzner volume:

- **Identity:** the PV's storage class plus its claimRef, and the CSI volume handle recorded as `volume_id` (`decide.py`, `k8s_client.py`). A PV on any other class is an identity conflict, which marks the cell `failed`.
- **Deletion:** proves the Hetzner volume is absent (`storage/hetzner.py`).
- **Capacity:** counts CSINode attachment limits (`capacity.py`).
- **Node removal:** forces volumes off a stopped node.
- **Quota:** the cell's ResourceQuota equals the serving pod's footprint, so a maintenance Job may only overlap a pod that is stopping (`manifests.py`).

Backups are restic Jobs to B2, with a per-cell key and prefix:

- Nightly in a 02–05 UTC window, as a stopping hold: the cell is **stopped** for the copy (`adopt-exomem-cloud-plain-cells` D8). Fleet-wide backup concurrency is 1, with a 900 s Job deadline.
- Before every image change, the backup is followed by restore-based rollback (D6). That backup also runs `forget --prune`.
- A scratch-namespace restore was exercised on 2026-09-28.
- No node-loss recovery has ever been rehearsed.

Production is one Hetzner Cloud cx33. It runs the single embedded-etcd k3s server, the platform pods and three cells. etcd snapshots go to B2 every 30 minutes, and the control database runs on its own server.

## Goals / Non-Goals

**Goals**

- Cells on node-local disk with per-cell size isolation.
- Encryption at rest equivalent to today's, on every node that holds cells.
- Online hourly backups: RPO ≤ 1 h for node loss.
- A rehearsed node-loss recovery with fleet RTO ≤ 4 h.
- Capacity counted from real local storage.
- Dedicated agents joining over a private link.
- A reversible per-cell migration with the owner first.
- No admission or release gated on the hardware purchase.

**Non-Goals**

- Changing the restic repository layout, the per-cell key model, tenant authentication or the control database.
- Per-tenant encryption keys.
- Replicated block storage (Longhorn, DRBD, Ceph). Cross-node replication would cost more than the tenants it protects at this scale, and it turns a disk-loss problem into a distributed-systems problem.
- Choosing or buying hardware.

## Decisions

### D1. TopoLVM with a thin device class, overprovision ratio 1.0, ext4

Each cell gets one logical volume with its own ext4 filesystem, provisioned by TopoLVM (`topolvm.io`, CSI).

- **Isolation:** a cell that fills its volume fails its own writes and cannot touch a neighbour or the node's root disk.
- **Thin pool:** this gives CSI snapshots and clones, which D3 needs.
- **No overcommit:** with ratio 1.0, the virtual size of every volume, snapshot and clone can never exceed the pool, so one tenant's backup can't take the whole pool down. A snapshot and its clone are each full-size thin volumes for this accounting.
- **ext4, pinned:** a clone of a dirty filesystem must mount read-only on the same node. XFS needs `nouuid` for that and refuses some dirty logs read-only. ext4's default `auto_da_alloc` also makes write-to-temp-then-rename survive a power cut in practice (D9).
- **Metadata:** the thin pool's metadata volume is sized for hourly snapshot churn (spike 1.1 measures it), with monitoring on its use.
- **Capacity and resize:** TopoLVM publishes free capacity per node and supports online resize, so the import runbook's `storage_gib` increase still works.
- **Operational weight:** one pinned subchart. Its webhook uses the cert-manager already installed.

Rejected alternatives:

- **k3s local-path:** a host directory with no size limit, so one tenant could fill the node.
- **OpenEBS LVM LocalPV:** no restore of a snapshot into a new claim, which is the step D3 needs.
- **Static local PVs:** manual capacity bookkeeping.
- **ZFS:** an out-of-tree kernel module to keep in step with every kernel update.

### D2. Encryption at the node: LUKS on md RAID1, Clevis/Tang unlock, escrowed keys

- **Layout:** the two NVMe disks form an md RAID1. LUKS sits on top and holds the `cells` volume group.
- **Posture:** this matches today's, where every Cloud Volume uses the same LUKS secret and tenants are separated by namespace and volume, not by key. Per-volume LUKS has no off-the-shelf LVM provisioner, and per-tenant keys are a non-goal.
- **Unlock:** unattended through Clevis against a Tang server on the control-plane node, reached over the private link.
  - A pulled disk, or one the provider swaps out, stays unreadable on its own.
  - Unlock depends on the control plane, which costs nothing: cells cannot serve without the control plane anyway.
- **Escrow:** two things are escrowed through the existing SOPS secret-destination contract:
  - each agent's recovery passphrase, held in a second keyslot;
  - the Tang server's keys, which live on the server's disk outside etcd. Without them, losing the server means unlocking every rebooting agent by hand until a new Tang is bound.
- **Manual unlock:** the runbook carries the passphrase unlock and the Clevis rebind for a replaced Tang.
- **Boot order:** the k3s agent unit waits for the unlock and the volume group, so TopoLVM never sees a missing group.
- **Every cell node:** the Cloud recovery agent (D8) uses the same LUKS and Clevis layout on its Cloud Volume.

### D3. Hourly online backups from a snapshot clone, as a non-stopping hold

Each hourly backup is a new hold kind, `snapshot-backup`, that does not stop the cell:

1. snapshot the cell's volume (CSI VolumeSnapshot);
2. create a read-only clone claim from the snapshot;
3. run the existing restic backup Job against the clone, with the same per-cell key, prefix and B2 bucket;
4. delete the clone and the snapshot.

- **Holds:** a cell carries at most one hold at a time, as today. The hourly hold runs between other holds and never alongside one, so it cannot meet the pre-upgrade backup's restic lock. Desired-state changes during an hourly hold apply at once, because the cell is not stopped; the clone and snapshot are cleaned up whatever happens to the cell.
- **Consistency:** a block snapshot is a power-loss image (D9).
- **Rejected:**
  - Stopping cells hourly, because restart cost on large vaults is too high.
  - A live file copy, which D8 already rejected as inconsistent.
- **Retention:** `--keep-hourly 24 --keep-daily 7 --keep-weekly 4`. `forget --prune` runs only on the first backup after 02:00 UTC each day, and no longer on the pre-upgrade backup, because prune is the expensive call on B2 and needs restic's exclusive lock.
- **Scope:** the backup keeps vault and custody in full, indexes included. Rebuilding embeddings for a large vault would dominate recovery time.
- **Unchanged:** the stopped pre-upgrade backup and restore-based rollback (D6), because their semantics depend on an exact pre-upgrade state.
- **Quota:** the cell's ResourceQuota allows two claims, twice the storage, and the serving pod plus one backup Job's CPU and memory requests and CPU limit. The resource-policy envelope charges that overlap.
- **Placement:** a clone of a TopoLVM snapshot lives in the source's volume group, so the Job must run on the cell's node. Spike 1.1 checks whether the provisioner pins the clone's node, so that the scheduler follows its PV. If it does not, the Job gets an own-node selector derived from the cell's PV, as a named exception to the resource policy's ban on arbitrary scheduling fields.
- **Throughput:** backup concurrency becomes a per-node setting. Spike 1.1 measures incremental backup time per cell, which sets how many cells one node can back up within the hour.
- **Alerting:** the existing alerting raises when any running cell's last successful backup is older than two hours.
- **Cells on Hetzner volumes:** those volumes can't take CSI snapshots, and cells stay on them until their own migration, which waits on the hardware purchase. A cell whose class has no snapshot support therefore keeps the nightly stopped backup, its window, concurrency 1 and its prune, and its backup-age alert fires at 26 hours.

**Targets:**

| Failure | RPO | RTO |
|---|---|---|
| Disk | 0 (RAID1) | — |
| Node loss | ≤ 1 h | Fleet ≤ 4 h, including replacement capacity |
| Per cell | — | Measured and recorded by the drill |

### D4. Node-loss recovery is an operator-triggered, fenced relocation

cellctl gains a relocation path for a cell whose node is gone. It runs only after the operator confirms the old node's stop, by the same rule as node removal: a reachable host shows no cell process, mount or open storage mapping, or the operator confirms the server is destroyed. Then it:

1. force-deletes the cell's pod on the dead node;
2. sets the old PV to Retain;
3. deletes the claim and recreates it on available capacity;
4. runs the restore Job from the cell's last backup snapshot;
5. records the new volume identity;
6. starts the cell only after the restore Job succeeds.

Setting Retain before the claim delete means deleting a claim never deletes data. cellctl's ClusterRole gains `patch` on PVs for this, and admission confines it to setting the reclaim policy to Retain on PVs claimed from a cell namespace, so the new verb cannot touch platform volumes or any other field. The retained PV and its logical volume count as absent for the deletion proof once their node is confirmed destroyed. A later cleanup removes the PV object.

**Recovery order after a node loss:**

1. The control database is unaffected, since it is on its own server.
2. If the server node is lost, restore etcd from B2 first, then reconcile volumes. A snapshot up to 30 minutes old misses volumes created or relocated since, and those hold the only copy of recent writes, so:
   - an Ansible step lists each agent's logical volumes, since cellctl has no host access;
   - a logical volume matching a row's `volume_id` is re-adopted by an operator runbook step that recreates its TopoLVM LogicalVolume object and its PV, after which cellctl's identity check confirms the match. cellctl gets no PV create: re-adoption is rare and operator-run, and keeping cellctl's PV write to the one Retain patch is worth more than automating it;
   - one matching no row is reported and released only by an operator on the host;
   - a row whose volume can't be re-adopted is relocated from backup. The identity check is never skipped to clear a mismatch.
3. Ensure replacement capacity: a surviving agent or a new one.
4. Relocate cells: the owner cell first, then in `rollout_priority` order.

The trigger is deliberately an operator action, not automatic. Node health on a cloud API is eventually consistent, and automatically relocating cells off a node that is only partitioned would restore older data over newer writes. The cost is human latency inside the RTO budget, paid by the affected tenants. The drill measures it.

**The admission allowance to delete `cell-data`:** today admission refuses any cellctl delete of a cell's claim. Relocation needs one.

- **What the refusal prevents:** cellctl destroying a tenant's only copy.
- **Why the allowance is safe:** with Retain set first, a claim delete removes no data. Admission therefore allows the delete only when the bound PV's reclaim policy is Retain.
- **Cost if that check fires wrongly:** a relocation stops until the operator fixes the PV.
- **Who pays:** the operator.

### D5. A cell that has ever been backed up never initialises an empty vault

`cell-init` today creates a fresh vault on any empty volume (`src/exomem/cell_init.py`). After a node loss, a recreated claim would then serve a blank vault as if it were the tenant's.

**The rule:** cellctl writes a `backed_up` key into the cell's Secret once the cell has a recorded backup. `cell-init` reads it. A cell with that key refuses to initialise empty, and writes a value-free reason code to its termination message. cellctl maps that code to the row's `last_error_code` at once, rather than waiting for the init deadline. A key in the Secret changes neither the StatefulSet template nor the render digest, so the first backup restarts no cell.

**Why the control is justified:**

- **Prevents:** a tenant silently seeing and writing into an empty vault while their data sits in backups. Recovering from that later means merging two histories.
- **Cost when it fires wrongly:** one cell stays down until an operator restores it, for example after a legitimate empty restart of a never-written cell. Since the guard only applies after a recorded backup, that case means data existed.
- **Who pays:** that tenant, and the operator.
- **Fail-closed is right here:** an unexpected empty volume is a data-integrity fault, not an eventually-consistent state.

A partly restored vault is a different hazard: an interrupted restore can leave a recognisable but incomplete vault. The relocation path covers it by starting the cell only after the restore Job succeeds, as the upgrade path already does.

### D6. Capacity counts local storage from the pool size

For a local-storage node, the storage term is:

`floor((pool size − snapshot reserve) / default cell size)`

and a cell larger than the default consumes `ceil(size / default)` slots of it.

- **Pool size, not free bytes:** free bytes fall while a backup's snapshot and clone exist, so a term built on them would dip on every backup.
- **Snapshot reserve:** 2 × the largest cell's size × the node's backup concurrency, because each concurrent backup holds a full-size snapshot and a full-size clone.
- **Published slots:** the minimum of the qualified occupancy, CPU, memory and storage terms.
- **No pool, no slots:** a node whose storage driver has not published a pool publishes zero slots. Once the configured domain is local, that is what keeps cells off the control-plane node: it has no cell pool. No separate rule names the server.
- **Configuration only:** the driver name, the topology key and PV nodeAffinity matching become configuration.
- **Attachment term:** TopoLVM creates no VolumeAttachments, so the attachment term doesn't apply to it. That term is deleted when Hetzner volumes are retired.
- **Observed, not configured:** the pool size comes from what TopoLVM publishes, so the D9 rule from `adopt-exomem-cloud-plain-cells` (observed, not configured) holds.

### D7. One domain publishes capacity; two classes coexist only while cells migrate

The Hetzner class stays the configured cell storage domain until the dedicated agent has joined and passed the drill. Until then the server keeps publishing its slots exactly as today, so admission never waits on the hardware purchase.

**Cutover (phase 7):** the configured domain switches to the local class under a short capacity closure. The resource policy already defers invitations during a closure without losing them. From then on:

- only local-storage nodes publish slots;
- cells still on Hetzner volumes count against those slots, which slightly under-admits until they move;
- new cells, relocations and restores use only the local class.

**Coexisting classes:** a cell keeps the class of the PV it is bound to until it migrates.

- cellctl's identity check accepts a cell PV on any configured cell class. A class outside that list is still an identity conflict.
- Admission admits a cell claim on any configured cell class, so a migration rollback can rebind a retained Hetzner PV.
- The Hetzner class leaves both lists in task 8.2.

**Migration, per cell, owner first:**

1. stop the cell and take the final stopped backup;
2. set the old PV's reclaim policy to Retain as the rollback copy;
3. clear the recorded `volume_id`;
4. create the claim on the local class and restore;
5. start after the restore succeeds, and accept with recall, governance status and a governed write.

The retained Hetzner volumes are deleted after a 7-day soak.

### D8. Dedicated agents join over a private link; a Cloud recovery agent stays available

**Join:** a dedicated agent joins over a private link with the existing agent hardening, the CA-pinned agent token and the inter-node firewall. The link depends on the provider:

| Server | Link |
|---|---|
| Hetzner dedicated | vSwitch coupled to the Cloud network (MTU 1400), on a subnet Terraform adds |
| Other provider | WireGuard over the public network |

The choice is made when the hardware is bought.

**Bootstrap:** a new Ansible role builds RAID1, LUKS, Clevis and the volume group. The provider's own install path (installimage, or the provider API) lays the base OS. Tang runs on the server node.

**Join completes on published capacity:** the k3s role waits until the new agent's storage driver publishes its pool before reporting the join done.

**Recovery agent:** Terraform keeps an optional Cloud recovery agent whose volume group sits on **one** large Cloud Volume, under the same LUKS and Clevis layout as a dedicated agent. It escapes the 16-volume limit and is billed only while it exists, so recovering from a lost dedicated node never waits on a hardware order. It also serves as the node the production drill destroys. For the drill it carries the existing dedicated-node label and taint, so it publishes no general slots and only the selected test cell lands on it.

### D9. Crash consistency of a snapshot

A block snapshot is the state a power cut would leave.

- **SQLite:** databases run in WAL mode with synchronous NORMAL or FULL. A snapshot restores a consistent database that may lack the last commits a client saw under NORMAL.
- **Write leases** expire after their 30 s TTL, so a restored cell never inherits a live lease.
- **JSON state:** several files are written to a temp file and renamed without an fsync (`prominence.py`, `envelope.py`, `mode.py`, `dreamer.py`), and the writer-lease commit counter is overwritten in place. `read_config` falls back to `{}` on a truncated file, which would silently reset a tenant's settings. ext4's `auto_da_alloc` flushes a rename-over-existing in practice. This change still makes those writers fsync the temp file before the rename, and write the counter by rename, so the guarantee doesn't rest on a mount default.

The promise is therefore a consistent state no newer than the snapshot, not the snapshot instant. Spike 1.1 checks it with a round trip of a snapshot taken mid-write.

### Archive order

The `cloud-cell` and `cloud-service-resource-policy` requirements modified here still live only in the deltas of `adopt-exomem-cloud-plain-cells` and `add-cloud-service-resource-policy`. This change archives after both. If an archive tool refuses that order, these deltas fold into the earlier change before it archives.

## Risks / Trade-offs

- **Node loss now costs up to an hour of writes,** where it cost none before. This is accepted for the cost and density gain. RAID1 covers the more common disk failure with no loss.
- **The snapshot path adds moving parts:** a snapshot controller, clones, quota headroom and thin-pool metadata. The rehearsal spike proves them before any production use. If thin-snapshot accounting misbehaves at ratio 1.0, the fallback is the stopped backup at reduced frequency, never an overcommitted pool.
- **Unlock depends on Tang on the server node.** If the server is down, a rebooting agent waits. Cells could not serve without the server anyway, and the escrowed passphrase and Tang keys allow a manual unlock or a rebind.
- **Single-node blast radius:** one dedicated node holds many tenants. Recovery time therefore grows with cells per node, which the drill measures. Scaling adds nodes rather than bigger nodes.
- **Under-admission during migration:** cells still on Hetzner volumes count against local slots until they move. It lasts days and costs a few slots.

## Migration Plan

Phases 1–4 need no hardware:
1. rehearsal spike;
2. cellctl and runtime changes;
3. chart and admission;
4. disposable node-loss drill.

Phases 5–7 need the purchased dedicated server:

5. bootstrap;
6. private-link join;
7. cutover and per-cell migration, owner first.

Phase 8 retires the Hetzner volumes after the soak. Phase 9 repeats the drill on production by destroying a Cloud recovery agent that holds a seeded test cell.

**Rollback per cell, until the soak ends:** stop the cell, rebind its retained Hetzner PV, and start. Rebinding (clearing the PV's claimRef) is an operator step, not a cellctl write.

## Open Questions

None blocking the specification.

The private-link type follows the hardware purchase. The owner migration window is agreed with the owner before task 7.1.
