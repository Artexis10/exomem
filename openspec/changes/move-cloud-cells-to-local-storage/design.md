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
- **Ratio pinned:** D6 computes the pool as published free bytes plus the node's volume sizes. That holds only at ratio 1.0, where TopoLVM publishes the pool less every virtual size. At any other ratio the published bytes are scaled, and slots would be miscounted. The chart's values schema therefore accepts no other ratio.
- **ext4, pinned:** a clone of a dirty filesystem must mount read-only on the same node. XFS needs `nouuid` for that and refuses some dirty logs read-only. ext4's default `auto_da_alloc` also makes write-to-temp-then-rename survive a power cut in practice (D9).
- **Metadata:** the pool uses 64 KiB chunks, set when it is created, because a chunk size never changes afterwards and LVM's own policy would choose larger chunks for a large pool. Its metadata volume holds 64 bytes per chunk, which is LVM's own default size for that chunk size. It is computed from the pool's size and capped at LVM's 15.81 GiB limit. The volume group keeps twice the metadata size free, so the metadata and its spare can grow. An alert at 80% data or metadata use is task 5.6.
- **Capacity and resize:** TopoLVM publishes free capacity per node and supports online resize, so the import runbook's `storage_gib` increase still works.
- **Operational weight:** one pinned subchart. It runs no webhook here: its pod-mutating webhook stays disabled, so it needs no certificate.
- **Node agent confined:** the node agent and lvmd run privileged, only on nodes labelled `node-restriction.kubernetes.io/exomem-local-storage=true`, which the k3s role sets from the server when such an agent joins, and a kubelet cannot set on itself. Policy admits every container of those two DaemonSets only with the pinned image reference. The TopoLVM controller and the snapshot controller, whose tokens no policy confines, run on the server node only. The chart lets the node agent write every Node and LogicalVolume; two admission policies confine each agent's token to its own node's LogicalVolumes, and to its own Node's capacity annotations and TopoLVM finalizer.

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
- **Snapshot controller:** k3s bundles neither the snapshot CRDs nor a controller (spike 1.1), so the chart ships both, from the pinned external-snapshotter release.
- **Placement:** a clone of a TopoLVM snapshot lives in the source's volume group, so the Job must run on the cell's node. The clone claim uses its own Immediate-binding class. In spike 1.1 TopoLVM gave such a clone's PV a nodeAffinity for the source's node, so the Job follows its PV with no selector, and the resource policy keeps its ban on arbitrary scheduling fields. A WaitForFirstConsumer clone has no PV until a pod is scheduled, so it would leave the node choice to the scheduler. The spike cluster had one node; the drill (4.1) has two and confirms the Job lands on the cell's node.
- **Throughput:** backup concurrency becomes a per-node setting, default 2. In spike 1.1 a whole cycle took 12.5–16.6 s per cell, including a full first backup of 1.04 GiB:
  - snapshot ready: 1.4–2.2 s;
  - clone bound, mounted and Job started: 9–12.7 s;
  - restic: 2–4 s;
  - cleanup: 1.6–2.2 s.
  The cost is per cell, not per byte, so one node can back up about 200 lightly changed cells an hour, one at a time. The repository sat on the runner's disk, so upload to B2 is not in these numbers; the drill measures it. A second slot keeps one slow cell from delaying the rest.
- **Alerting:** cellctl raises one platform alert while any serving cell's last successful backup is older than two hours. It posts the same content-free transition as the hosted scheduler evaluator to the existing alert receiver, which emails only on a change of state. The Role `cellctl-alert-delivery` lets cellctl read that receiver's URL from its one Secret. The stale cells go to cellctl's log by id.
- **Cells on Hetzner volumes:** those volumes can't take CSI snapshots, and cells stay on them until their own migration, which waits on the hardware purchase. A cell whose class has no snapshot support therefore keeps the nightly stopped backup, its window, concurrency 1 and its prune, and its backup-age alert fires at 26 hours.

**Targets:**

| Failure | RPO | RTO |
|---|---|---|
| Disk | 0 (RAID1) | — |
| Node loss | ≤ 1 h | Fleet ≤ 4 h, including replacement capacity |
| Per cell | — | Measured and recorded by the drill |

### D4. Node-loss recovery is an operator-triggered, fenced relocation

cellctl gains a relocation path for a cell whose node is gone. It runs only after the operator confirms the old node's stop, by the same rule as node removal: a reachable host shows no cell process, mount or open storage mapping, or the operator confirms the server is destroyed. The operator records that with Kubernetes' out-of-service taint, which cellctl honours only while the node is not Ready. A node gone from the API counts as lost too, once cellctl has seen it absent for five minutes: only node removal deletes a Node, after the same confirmation. The wait is for a `kubectl delete node` on a healthy agent, whose kubelet registers it again within seconds; that must relocate nothing. Five minutes is several fast passes and small against the 4 h fleet RTO. The clock lives in cellctl's memory, so a restart restarts it, which only delays. Then cellctl:

1. waits for Kubernetes to delete the cell's pod, which the out-of-service taint makes it do;
2. sets the old PV to Retain;
3. deletes the claim and recreates it on available capacity;
4. runs the restore Job from the cell's last backup snapshot;
5. records the new volume identity;
6. starts the cell only after the restore Job succeeds.

Setting Retain before the claim delete means deleting a claim never deletes data. cellctl's ClusterRole gains `patch` on PVs for this, and admission confines it to setting the reclaim policy to Retain on PVs claimed from a cell namespace, so the new verb cannot touch platform volumes or any other field.

TopoLVM's controller would delete every claim on a deleted Node when it finalizes that Node. That deletes the claims before cellctl can set Retain, and leaves no claim to show the cell was lost, so the platform skips that step (`controller.nodeFinalize.skipped`). A Node gone from the API therefore still leaves its PVs pinned to it, and cellctl relocates their cells.

**When a lost node's volume counts as gone:** the retained PV and its logical volume count as absent for a deleted cell's deletion proof only once their node has been gone from the API for the same five minutes. A stopped node still in the API counts as live, because it could rejoin with the volume. The proof follows every PV the cell's namespace still claims, not only the row's recorded volume, so a volume released during a relocation is still checked. Two runbook rules keep "gone" true: a stop-confirmed agent never rejoins with its `cells` volume group, and removing a reachable agent ends with its cells device erased. A later cleanup removes the PV objects.

**No fresh claim over a recorded volume:** while a row records a volume, cellctl never creates a fresh claim for it, on any storage class. A missing claim then sets the row to `VOLUME_MISSING`, and the cell stays stopped until an operator acts. The one exception is relocation, which restores into its new claim before the cell starts. An operator who has found a volume lost, for example after an etcd restore, marks the cell's namespace with `exomem.io/volume-lost=<volume id>`; a missing claim with that mark relocates the cell from its backup. The empty-vault guard (D5) stays as the second line: it covers a claim that exists over an empty volume.

**The removal refusal while a cell's volume is on the target:** until the removal playbook relocates cells itself (task 6.4), `remove-agent.yml` refuses while a local cell PV is still pinned to the target and bound. A retained PV left by a finished relocation does not block it. Maintenance holds block a removal only on cells whose volume is on the target.

- **What it prevents:** deleting the Node of an agent that still holds a cell's only current volume. cellctl would then relocate that cell from its last backup and lose up to an hour of its writes, although the node may have been healthy.
- **Cost when it fires wrongly:** the removal waits until the cell is relocated, or its volume released.
- **Who pays:** the operator. Tenants are not affected.

**Recovery order after a node loss:**

1. The control database is unaffected, since it is on its own server.
2. If the server node is lost, restore etcd from B2 first, then reconcile volumes. A snapshot up to 30 minutes old misses volumes created or relocated since, and those hold the only copy of recent writes, so:
   - an Ansible step lists each agent's logical volumes, since cellctl has no host access;
   - a logical volume matching a row's `volume_id` is re-adopted by an operator runbook step. TopoLVM names a logical volume after its LogicalVolume object's UID, which is also the PV's volume handle. Spike 1.1 showed that recreating the object and PV alone gives a new, empty volume under a new ID, and leaves the old one untouched on the host. The step therefore:
     1. creates the LogicalVolume object;
     2. on the host, removes the new empty volume and renames the old one to the new object's volume ID (`lvremove`, `lvrename`);
     3. creates the PV with that handle and binds the claim;
     4. clears the row's `volume_id` by compare-and-set on the old ID, the same step as a migration (D7). cellctl then records the new handle, and its class and claim checks still apply.
     The spike read a marker file back through this path. cellctl gets no PV create: re-adoption is rare and operator-run, and keeping cellctl's PV write to the one Retain patch is worth more than automating it;
   - deleting a LogicalVolume object on a node whose TopoLVM plugin is running destroys the logical volume, even with the object's finalizer removed (spike 1.1). No runbook step deletes one except to destroy its volume;
   - one matching no row is reported and released only by an operator on the host;
   - a row whose volume can't be re-adopted is relocated from backup. The identity check is never skipped to clear a mismatch.
3. Ensure replacement capacity: a surviving agent or a new one.
4. Relocate cells. The owner's cell relocates alone, and the rest wait until its restore has ended. Then the others relocate by `rollout_priority`, at most four at a time. A restore that failed stops counting as in flight, and so does a relocation whose Retain patch or claim delete was refused (`RELOCATION_REFUSED`), so one stuck cell does not hold back the fleet. Like an upgrade, no relocation starts in a pass that could not observe every cell, since the missing one may be the owner's; relocations already started continue.

**Why four at a time fits the 4 h fleet RTO:** a lost RAID1 node holds at most about 43 cells at 10 GiB (D6). Allow an hour for the operator's confirmation and for replacement capacity. Four at a time then needs 11 rounds in three hours, so each round may take about 16 minutes. One at a time would leave about four minutes per cell. Downloading a full 10 GiB vault from B2 can take that long by itself. The drill measures the real per-cell time (task 4.1), and the bound is revisited if it exceeds 16 minutes.

The trigger is deliberately an operator action, not automatic. Node health on a cloud API is eventually consistent, and automatically relocating cells off a node that is only partitioned would restore older data over newer writes. The cost is human latency inside the RTO budget, paid by the affected tenants. The drill measures it.

**The admission allowance to delete `cell-data`:** today admission refuses any cellctl delete of a cell's claim. Relocation needs one.

- **What the refusal prevents:** cellctl destroying a tenant's only copy.
- **Why the allowance is safe:** with Retain set first, a claim delete removes no data. Admission therefore allows the delete only when the bound PV's reclaim policy is Retain.
- **Cost if that check fires wrongly:** a relocation stops until the operator fixes the PV.
- **Who pays:** the operator.

### D5. A cell that has ever been backed up never initialises an empty vault

`cell-init` today creates a fresh vault on any empty volume (`src/exomem/cell_init.py`). After a node loss, a recreated claim would then serve a blank vault as if it were the tenant's.

**The rule:** cellctl writes a `backed_up` key into the cell's Secret once the cell has a recorded backup. `cell-init` reads it. A cell with that key refuses to initialise empty, and writes a value-free reason code to its termination message. cellctl maps that code to the row's `last_error_code` at once, rather than waiting for the init deadline. A key in the Secret changes neither the StatefulSet template nor the render digest, so the first backup restarts no cell. The backup Job holds the same line from the other side: it runs the cell image's own vault check on its source and fails with `BACKUP_SOURCE_NOT_A_VAULT` when there is none, so a cell restarted onto an emptied volume never replaces its last good restore point with an empty one. A check that cannot run fails with `BACKUP_VAULT_CHECK_FAILED` instead, and cellctl logs either code with the cell's id. The operator sees `BACKUP_FAILED` on the row, then cell-init's refusal once the cell leaves serving; the backup-age alert does not fire for it. The product suite runs the check against the real `exomem` package, so a rename of the vault check fails there.

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
- **Observing the pool size:** TopoLVM publishes free capacity, not pool size: its CSIStorageCapacity and node annotation both report the pool less every volume's virtual size (spike 1.1). At ratio 1.0, pool size is therefore that free capacity plus the sizes of the node's LogicalVolume objects. cellctl gains read-only `list` on LogicalVolumes for this.
- **Default cell size:** 4 GiB on the local class. Hetzner volumes stay 10 GiB, the provider's minimum volume size.
  - At ratio 1.0 a local cell charges its full size, so the default sets density. On 2026-10-06 the live cells used 4.28 GiB (the owner's), about 0.01 GiB and about 0 GiB of their 10 GiB volumes.
  - A RAID1 pool of about 476 GiB holds about 43 cells at 10 GiB, while CPU and memory allow about 98.
  - cellctl counts local slots in the local default, which is configuration.
  - A cell's size is its row's `storage_gib`, which Substrate owns (column default 10). New cells take 4 GiB when the cutover (task 7.2) changes Substrate's default for new cells.
  - A migrating cell keeps its row's size unless the operator lowers it before the restore, never below what the cell uses.
  - A larger `storage_gib` expands a cell's claim through TopoLVM's online expansion, and also restarts the cell once. `storage_gib` is part of the render digest, which the pod template carries, so cellctl re-applies the cell as a digest change (D4). D10's automatic growth leaves the digest as it is and does not restart the cell.
- **Snapshot reserve:** 2 × the largest cell's size × the node's backup concurrency. In spike 1.1, a snapshot and its clone of a 4 GiB volume each took the full 4 GiB from published free capacity.
- **Scheduler backstop:** the scheduler refuses a claim larger than a node's published free capacity ("did not have enough free storage"), and the capacity object followed the node within half a second in the spike. A miscount in this term therefore stops a cell from scheduling; it does not overfill a pool.
- **Published slots:** the minimum of the qualified occupancy, CPU, memory and storage terms.
- **No pool, no slots:** a node whose storage driver has not published a pool publishes zero slots. Once the configured domain is local, that is what keeps cells off the control-plane node: it has no cell pool. No separate rule names the server.
- **Configuration only:** the driver name, the topology key and PV nodeAffinity matching become configuration.
- **Attachment term:** TopoLVM creates no VolumeAttachments, and its CSINode entry carries no allocatable count (spike 1.1), so the attachment term doesn't apply to it. That term is deleted when Hetzner volumes are retired.
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

The promise is therefore a consistent state no newer than the snapshot, not the snapshot instant. Spike 1.1 checked it with a snapshot taken while a cell wrote notes through `remember` in a loop:
- No write acknowledged before the snapshot was missing from the restore, and none started after it was present.
- The restored cell was ready in 13 s.
- It answered recall for the seed note and the newest write.
- It reported the same governance-schema status as its source.
- It accepted a governed write.

### D10. A local cell grows online before it fills

A 4 GiB default only holds if a cell that fills grows without an operator. cellctl therefore expands a local cell's claim when its filesystem passes 80% use.

- **Observed use:** the hourly backup Job already mounts the cell's filesystem, as the clone. It reports the filesystem's used and total bytes in its termination message, value-free, and cellctl reads them from the finished Job. This needs no new privilege; kubelet volume statistics would need `nodes/proxy`, which reaches every pod on the node.
- **Step and cap:** each growth adds one default cell size (4 GiB), up to a configured cap per cell. One growth runs per cell per hourly backup, so a cell never grows faster than its use is observed. A cell past 80% use at its cap cannot grow and will fill, so it raises the same alert as a node without room.
- **Room first:** cellctl grows a cell only while its node's published free bytes cover the step and the larger snapshot reserve that the new size implies (D6). Otherwise it raises an alert through the alert receiver, the same path as the backup-age alert, and leaves the size as it is.
- **Recorded on the row:** cellctl records the grown size on the cell's row, in a column it owns, and renders the claim at the larger of that and `storage_gib`. A later pass therefore never renders a smaller claim, which Kubernetes refuses. The column needs a Substrate migration.
- **Capacity:** a grown cell consumes `ceil(size / default)` slots, as any larger cell does (D6), so admission sees the growth on the next capacity pass.
- **Not shrunk:** a cell never shrinks automatically. Kubernetes cannot shrink a claim, and a smaller size needs a backup and a restore.

### Archive order

The `cloud-cell` and `cloud-service-resource-policy` requirements modified here still live only in the deltas of `adopt-exomem-cloud-plain-cells` and `add-cloud-service-resource-policy`. This change archives after both. If an archive tool refuses that order, these deltas fold into the earlier change before it archives.

## Risks / Trade-offs

- **Node loss now costs up to an hour of writes,** where it cost none before. This is accepted for the cost and density gain. RAID1 covers the more common disk failure with no loss.
- **The snapshot path adds moving parts:** a snapshot controller, clones, quota headroom and thin-pool metadata. Spike 1.1 exercised them on a one-node rehearsal cluster (run 37382385459), and the drill repeats them across two agents before any production use. If thin-snapshot accounting misbehaves at ratio 1.0, the fallback is the stopped backup at reduced frequency, never an overcommitted pool.
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
