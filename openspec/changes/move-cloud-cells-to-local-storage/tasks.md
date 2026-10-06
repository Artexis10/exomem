## 1. Rehearsal spike (no hardware)

- [x] 1.1 Run TopoLVM on a loop-device volume group in the cloud rehearsal cluster, with a thin device class at overprovision ratio 1.0 and ext4. Record:
  - whether k3s bundles a snapshot controller;
  - the snapshot and clone round trip, including a snapshot taken while a cell is writing, restored and checked with recall, governance status and a governed write;
  - how a snapshot and its clone count against the pool at ratio 1.0;
  - the pool size and free capacity TopoLVM publishes per node;
  - whether CSINode carries an allocatable count;
  - whether the clone's PV is pinned to the source's node, so a Job reading it schedules there without a selector;
  - thin-pool metadata use across a day of hourly snapshot churn;
  - incremental backup time per cell, which sets per-node backup concurrency;
  - that an existing logical volume can be re-adopted by recreating its LogicalVolume object and PV.
  - Evidence: Cloud rehearsal run 37382385459 (`storage-spike` mode, commit 4cb5301d1); results are in design D1, D3, D4, D6 and D9. The spike's code and its separate TopoLVM values were removed once it had run; they remain in that commit.
- [x] 1.2 Check that the node plugin's privileged container and hostPaths can run under a named exception without widening `infra/policy/kubernetes.rego` for any other workload. Evidence: `conftest test` of the rendered TopoLVM 17.2.0 chart passes 231 checks with the exception, and fails on its two DaemonSets' privileged containers and hostPaths without it; `conftest verify` passes the 5 policy tests, which deny an unnamed privileged pod, the same name in another namespace, another hostPath and a second privileged container.

## 2. cellctl and runtime (red-first)

- [x] 2.1 Make the storage classes, driver and topology key configuration rather than constants. Accept a cell PV on any configured cell class in the identity check, and match PVs to nodes for local volumes. Evidence: unit tests, including an unmigrated cell on the old class while the local class is configured.
- [x] 2.2 Add the storage-capacity term:
  - pool size minus twice the largest cell per concurrent backup, over the default size;
  - pool size observed as published free capacity plus the node's LogicalVolume sizes, with read-only `list` on LogicalVolumes;
  - larger cells charge more;
  - no published pool means zero;
  - only the configured domain publishes slots, and every non-deleted cell counts against them.
  - Evidence: unit tests on recorded observations, including a pass during a backup.
- [ ] 2.3 Render the hourly online backup as a non-stopping hold:
  - snapshot, read-only clone, restic on the clone, cleanup whatever the outcome;
  - one hold per cell at a time;
  - prune only on the first backup after 02:00 UTC, and never on the pre-upgrade backup;
  - per-node backup concurrency, default 2;
  - a quota of two claims, twice the storage, and the serving pod plus one backup Job's CPU and memory;
  - the clone claim on an Immediate-binding clone class, so the Job follows the clone's PV to the cell's node with no selector;
  - the stopped pre-upgrade backup unchanged;
  - a cell on a class without snapshot support keeps the nightly stopped backup, window, concurrency 1 and prune.
  - Evidence: unit tests for both classes, and a rehearsal backup of a serving cell.
- [ ] 2.4 Add operator-triggered relocation:
  - require the same stop confirmation as node removal;
  - force-delete the pod, set the old PV to Retain, then delete and recreate the claim;
  - restore the last backup, record the new volume identity, start only after the restore succeeds.
  - Evidence: unit tests and a rehearsal relocation.
- [x] 2.5 Make the deletion proof per storage driver: PV, backend volume, snapshots and clones absent. A failed listing is not absence; a volume on a node confirmed destroyed is. Evidence: unit tests.
- [x] 2.6 Add the empty-vault guard:
  - cellctl writes a `backed_up` key into the cell's Secret after the first recorded backup;
  - `cell-init` refuses an empty initialisation when the key is present, and writes a value-free code to its termination message;
  - cellctl records that code on the row at once.
  - Evidence: tests for a backed-up cell, a new cell, and a first backup that leaves the render digest unchanged.
- [x] 2.7 Make the JSON-state writers fsync the temp file before renaming it, and write the writer-lease commit counter by rename: `prominence.py`, `envelope.py`, `mode.py`, `dreamer.py`, `writer_lease.py`. Evidence: unit tests that a write goes through the durable path.
- [x] 2.8 Alert when a running cell's last successful backup is older than two hours, or 26 hours for a cell on the nightly backup. Evidence: an alert-rule test.
- [ ] 2.9 Add the post-etcd-restore reconciliation:
  - an Ansible step on each agent lists logical volumes;
  - an operator runbook step re-adopts each one that matches a row's `volume_id` (design D4): it recreates the LogicalVolume object, renames the old volume to the new volume ID in place of the new empty one, creates the PV, and clears the row's `volume_id` by compare-and-set so cellctl records the new one (cellctl gains no PV create);
  - no step deletes a LogicalVolume object except to destroy its volume;
  - volumes that match no row are reported and released only by an operator step on the host;
  - a row whose volume cannot be re-adopted is relocated from backup, and the identity check is never skipped.
  - Evidence: unit tests on a recorded mismatch, and a rehearsal etcd restore from an older snapshot.

- [ ] 2.10 Grow a local cell online before it fills (design D10), red-first:
  - the hourly backup Job reports the filesystem's used and total bytes in its termination message, and cellctl records them;
  - past 80% use, cellctl grows the claim by one default cell size, up to the configured cap, at most once per backup;
  - only while the node's published free bytes cover the step and the larger snapshot reserve; otherwise an alert through the alert receiver;
  - the grown size is recorded in a cellctl-owned row column (Substrate migration) and the claim renders at the larger of it and `storage_gib`.
  - Evidence: unit tests for growth, the cap, the pool refusal with its alert, and a rehearsal growth of a serving cell.

## 3. Chart, admission, policy

- [x] 3.1 Add TopoLVM, the snapshot CRDs and controller (k3s bundles neither), the local StorageClass, the Immediate-binding clone class and the VolumeSnapshotClass to the platform chart, alongside the existing class.
- [x] 3.2 Extend admission:
  - cell claims on any configured cell class;
  - clone sources limited to the same cell's own snapshots;
  - cellctl's delete of `cell-data` only when the bound PV's reclaim policy is Retain;
  - cellctl's PV patch only to set the reclaim policy to Retain, only on PVs claimed from a cell namespace, with the matching ClusterRole verb;
  - snapshot RBAC.
  - Evidence: admission tests that admit the own-cell clone, the Retain patch and the Retain delete, and refuse a foreign snapshot, an unconfigured class, a delete under reclaim Delete, a patch of a non-cell PV and a patch of any other PV field.
- [x] 3.3 Add the named rego exception for the TopoLVM node plugin. Evidence: conftest.
- [x] ~~3.4 Update the capacity collector for pool-based capacity, without `hcloudServerId` for nodes that have none. Evidence: collector tests.~~
  - Dropped: the capacity collector is legacy hosted tooling, and the chart suspends it whenever legacy hosted is paused (`capacity-collector.yaml`), which cellctl being enabled implies.

## 4. Node-loss drill on disposable infrastructure (no hardware)

- [ ] 4.1 In the rehearsal cluster, with TopoLVM installed from the platform chart's values:
  - seed a cell and let hourly backups run;
  - write once more;
  - destroy its agent;
  - recover onto another agent through relocation.
  - Record that each hourly backup Job ran on its cell's node with no selector, and the upload time to the bucket.
  - Record the measured recovery point and recovery time, and accept with recall, governance status and a governed write.
- [ ] 4.2 Prove the empty-vault guard: deliberately start the backed-up cell on an empty claim before any restore, and record that it refuses, stays not ready and reports its reason on the row.
- [ ] 4.3 Interrupt a relocation's restore and record that the cell does not start and the retained volume is untouched.

## 5. Dedicated host bootstrap (needs the purchased server)

- [ ] 5.1 Install Tang on the server node, and escrow its keys through the secret-destination contract.
- [ ] 5.2 Write the dedicated-host Ansible role: md RAID1, LUKS, Clevis bound to Tang, the `cells` volume group, and the recovery passphrase escrowed through the secret-destination contract.
- [ ] 5.3 Make the k3s agent unit wait for the unlock. Evidence: `lsblk` and `cryptsetup status` output, plus a reboot that unlocks unattended and starts K3s afterwards.
- [ ] 5.4 Add the manual unlock and the Tang rebind to the operator runbook, and exercise the manual unlock once.

## 6. Private-link join (needs the purchased server)

- [ ] 6.1 Add the private-link subnet to Terraform foundation (vSwitch for Hetzner), or the WireGuard configuration for another provider.
- [ ] 6.2 Join the dedicated agent over the private link with the existing hardening, the CA-pinned agent token and the inter-node firewall. The k3s role waits until the agent's storage driver publishes its pool. Evidence: the join playbook output and published slots.
- [ ] 6.3 Add the optional Terraform Cloud recovery agent, with its volume group on one Cloud volume under the same LUKS and Clevis layout, created and removed by variable. Evidence: plan/apply/destroy on a disposable stack.
- [ ] 6.4 Implement relocation in the agent-removal playbook for cells whose volumes the remaining nodes cannot reach: a fresh stopped backup, a restore, start after the restore succeeds, acceptance. Evidence: a removal of a disposable agent holding a seeded cell.

## 7. Cutover and migration (needs the purchased server)

- [ ] 7.1 Agree the owner migration window with the owner.
- [ ] 7.2 Switch the configured cell storage domain to the local class under a capacity closure, with the old class still accepted for existing cells. Change Substrate's `storage_gib` default for new cells to 4 GiB (design D6). Evidence: published slots before and after, and the closure reopening.
- [ ] 7.3 Migrate the owner cell:
  - stop it and take the final backup;
  - retain the old PV;
  - restore onto local storage;
  - start after the restore succeeds, and accept it with recall, governance status and a governed write;
  - check large-vault restart latency against the existing gates.
- [ ] 7.4 Migrate the remaining cells one at a time with the same acceptance.
- [ ] 7.5 Update `docs/runbooks/cloud-operator-import.md`, which still cites the nightly 02:00–05:00 UTC backup window.
- [ ] 7.6 Before the cutover (7.2), pin the Hetzner CSI controller to the server node, as the TopoLVM and snapshot controllers are: its token can attach any volume to any node. This moves a live production pod, so it ships on its own. Evidence: the controller pod's node, and a volume attach after the move.

## 8. Retire Hetzner volumes

- [ ] 8.1 After a 7-day soak with hourly backups succeeding, delete the retained Hetzner volumes. Evidence: provider listing.
- [ ] 8.2 Remove the Hetzner StorageClass from the configured classes and admission, the attachment capacity term, the Hetzner deletion proof, and the CSI driver if nothing else uses it. Evidence: a green full suite.

## 9. Production drill and closure

- [ ] 9.1 Create a Cloud recovery agent with the dedicated-node label and taint, select a seeded test cell onto it, destroy the agent, and relocate the cell. Record the recovery point and recovery time.
- [ ] 9.2 Sync these deltas into canonical specs after `adopt-exomem-cloud-plain-cells` and `add-cloud-service-resource-policy` archive, then archive this change.
