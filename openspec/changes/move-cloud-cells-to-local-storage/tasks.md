## 1. Rehearsal spike (no hardware)

- [ ] 1.1 Run TopoLVM on a loop-device volume group in the cloud rehearsal cluster, with a thin device class at overprovision ratio 1.0 and ext4. Record:
  - whether k3s bundles a snapshot controller;
  - the snapshot and clone round trip, including a snapshot taken while a cell is writing, restored and checked with recall, governance status and a governed write;
  - how a snapshot and its clone count against the pool at ratio 1.0;
  - the pool size and free capacity TopoLVM publishes per node;
  - whether CSINode carries an allocatable count;
  - whether the clone's PV is pinned to the source's node, so a Job reading it schedules there without a selector;
  - thin-pool metadata use across a day of hourly snapshot churn;
  - incremental backup time per cell, which sets per-node backup concurrency;
  - that an existing logical volume can be re-adopted by recreating its LogicalVolume object and PV.
  - Evidence: the rehearsal log.
- [ ] 1.2 Check that the node plugin's privileged container and hostPaths can run under a named exception without widening `infra/policy/kubernetes.rego` for any other workload. Evidence: a conftest run.

## 2. cellctl and runtime (red-first)

- [ ] 2.1 Make the storage classes, driver and topology key configuration rather than constants. Accept a cell PV on any configured cell class in the identity check, and match PVs to nodes for local volumes. Evidence: unit tests, including an unmigrated cell on the old class while the local class is configured.
- [ ] 2.2 Add the storage-capacity term:
  - pool size minus twice the largest cell per concurrent backup, over the default size;
  - larger cells charge more;
  - no published pool means zero;
  - only the configured domain publishes slots, and every non-deleted cell counts against them.
  - Evidence: unit tests on recorded observations, including a pass during a backup.
- [ ] 2.3 Render the hourly online backup as a non-stopping hold:
  - snapshot, read-only clone, restic on the clone, cleanup whatever the outcome;
  - one hold per cell at a time;
  - prune only on the first backup after 02:00 UTC, and never on the pre-upgrade backup;
  - per-node backup concurrency;
  - a quota of two claims, twice the storage, and the serving pod plus one backup Job's CPU and memory;
  - an own-node selector on the Job only if 1.1 shows the clone is not pinned;
  - the stopped pre-upgrade backup unchanged;
  - a cell on a class without snapshot support keeps the nightly stopped backup, window, concurrency 1 and prune.
  - Evidence: unit tests for both classes, and a rehearsal backup of a serving cell.
- [ ] 2.4 Add operator-triggered relocation:
  - require the same stop confirmation as node removal;
  - force-delete the pod, set the old PV to Retain, then delete and recreate the claim;
  - restore the last backup, record the new volume identity, start only after the restore succeeds.
  - Evidence: unit tests and a rehearsal relocation.
- [ ] 2.5 Make the deletion proof per storage driver: PV, backend volume, snapshots and clones absent. A failed listing is not absence; a volume on a node confirmed destroyed is. Evidence: unit tests.
- [ ] 2.6 Add the empty-vault guard:
  - cellctl writes a `backed_up` key into the cell's Secret after the first recorded backup;
  - `cell-init` refuses an empty initialisation when the key is present, and writes a value-free code to its termination message;
  - cellctl records that code on the row at once.
  - Evidence: tests for a backed-up cell, a new cell, and a first backup that leaves the render digest unchanged.
- [ ] 2.7 Make the JSON-state writers fsync the temp file before renaming it, and write the writer-lease commit counter by rename: `prominence.py`, `envelope.py`, `mode.py`, `dreamer.py`, `writer_lease.py`. Evidence: unit tests that a write goes through the durable path.
- [ ] 2.8 Alert when a running cell's last successful backup is older than two hours, or 26 hours for a cell on the nightly backup. Evidence: an alert-rule test.
- [ ] 2.9 Add the post-etcd-restore reconciliation:
  - an Ansible step on each agent lists logical volumes;
  - an operator runbook step re-adopts each one that matches a row's `volume_id` by recreating its LogicalVolume object and PV, and cellctl's identity check confirms the match (cellctl gains no PV create);
  - volumes that match no row are reported and released only by an operator step on the host;
  - a row whose volume cannot be re-adopted is relocated from backup, and the identity check is never skipped.
  - Evidence: unit tests on a recorded mismatch, and a rehearsal etcd restore from an older snapshot.

## 3. Chart, admission, policy

- [ ] 3.1 Add TopoLVM, the snapshot controller if k3s lacks one, the local StorageClass and the VolumeSnapshotClass to the platform chart, alongside the existing class.
- [ ] 3.2 Extend admission:
  - cell claims on any configured cell class;
  - clone sources limited to the same cell's own snapshots;
  - cellctl's delete of `cell-data` only when the bound PV's reclaim policy is Retain;
  - cellctl's PV patch only to set the reclaim policy to Retain, only on PVs claimed from a cell namespace, with the matching ClusterRole verb;
  - snapshot RBAC.
  - Evidence: admission tests that admit the own-cell clone, the Retain patch and the Retain delete, and refuse a foreign snapshot, an unconfigured class, a delete under reclaim Delete, a patch of a non-cell PV and a patch of any other PV field.
- [ ] 3.3 Add the named rego exception for the TopoLVM node plugin. Evidence: conftest.
- [ ] 3.4 Update the capacity collector for pool-based capacity, without `hcloudServerId` for nodes that have none. Evidence: collector tests.

## 4. Node-loss drill on disposable infrastructure (no hardware)

- [ ] 4.1 In the rehearsal cluster:
  - seed a cell and let hourly backups run;
  - write once more;
  - destroy its agent;
  - recover onto another agent through relocation.
  - Record the measured recovery point and recovery time, and accept with recall, governance status and a governed write.
- [ ] 4.2 Prove the empty-vault guard: deliberately start the backed-up cell on an empty claim before any restore, and record that it refuses, stays not ready and reports its reason on the row.
- [ ] 4.3 Interrupt a relocation's restore and record that the cell does not start and the retained volume is untouched.

## 5. Dedicated host bootstrap (needs the purchased server)

- [ ] 5.1 Install Tang on the server node, and escrow its keys through the secret-destination contract.
- [ ] 5.2 Write the dedicated-host Ansible role: md RAID1, LUKS, Clevis bound to Tang, the `cells` volume group, and the recovery passphrase escrowed through the secret-destination contract.
- [ ] 5.3 Make the k3s agent unit wait for the unlock. Evidence: `lsblk` and `cryptsetup status` output, plus a reboot that unlocks unattended and starts K3s afterwards.
- [ ] 5.4 Add the manual unlock and the Tang rebind to the operator runbook, and exercise the manual unlock once.
- [ ] 5.5 Alert when a dedicated host's cell array is degraded, through the existing alert receiver. Today `site.yml` only reports it, and the host has no mail transport for `mdmonitor`. Evidence: an alert from a failed member on a disposable host.
- [ ] 5.6 Alert when a thin pool's data or metadata use passes 80%, from TopoLVM's thin-pool metrics, through the existing alert receiver. Evidence: an alert-rule test.
- [ ] 5.7 Write and rehearse a rotation of the K3s agent token (`k3s_agent_token`) for the pinned K3s version. A dedicated host's disk that leaves the provider's custody unwiped exposes the token. The procedure owns the token's activation: it escrows the new version, then selects it in `infra/contracts/active-ansible-selection-v1.json` in a reviewed commit, because escrowing alone activates nothing. Evidence: every agent rejoins with the new token, and the old token is refused.

## 6. Private-link join (needs the purchased server)

- [ ] 6.1 Add the private-link subnet to Terraform foundation (vSwitch for Hetzner), or the WireGuard configuration for another provider.
- [ ] 6.2 Join the dedicated agent over the private link with the existing hardening, the CA-pinned agent token and the inter-node firewall. The k3s role waits until the agent's storage driver publishes its pool. Evidence: the join playbook output and published slots.
- [ ] 6.3 Add the optional Terraform Cloud recovery agent, with its volume group on one Cloud volume under the same LUKS and Clevis layout, created and removed by variable. Evidence: plan/apply/destroy on a disposable stack.
- [ ] 6.4 Implement relocation in the agent-removal playbook for cells whose volumes the remaining nodes cannot reach: a fresh stopped backup, a restore, start after the restore succeeds, acceptance. Evidence: a removal of a disposable agent holding a seeded cell.
- [ ] 6.5 Set the cluster's flannel MTU so that VXLAN fits the private link before the first dedicated join. Cloud nodes run flannel at 1400 over the 1450-byte Cloud network, so their VXLAN packets are 1450 bytes (1400 plus 50 bytes of overhead). A vSwitch carries at most 1400, and a default WireGuard interface 1420. Evidence: pod-to-pod traffic at full MTU between a Cloud node and the dedicated agent.
- [ ] 6.6 Before the first production dedicated join, run the release gate on the full inventory with `--limit` set to the server, so that no agent needs to be reachable and nothing is pruned. A fleet with a WireGuard host would then need every peer's WireGuard public key carried in the inventory, because only a run on that peer reads its key today. Evidence: the gate's two passes on the server, with the agents unreached.

## 7. Cutover and migration (needs the purchased server)

- [ ] 7.1 Agree the owner migration window with the owner.
- [ ] 7.2 Switch the configured cell storage domain to the local class under a capacity closure, with the old class still accepted for existing cells. Evidence: published slots before and after, and the closure reopening.
- [ ] 7.3 Migrate the owner cell:
  - stop it and take the final backup;
  - retain the old PV;
  - restore onto local storage;
  - start after the restore succeeds, and accept it with recall, governance status and a governed write;
  - check large-vault restart latency against the existing gates.
- [ ] 7.4 Migrate the remaining cells one at a time with the same acceptance.
- [ ] 7.5 Update `docs/runbooks/cloud-operator-import.md`, which still cites the nightly 02:00–05:00 UTC backup window.

## 8. Retire Hetzner volumes

- [ ] 8.1 After a 7-day soak with hourly backups succeeding, delete the retained Hetzner volumes. Evidence: provider listing.
- [ ] 8.2 Remove the Hetzner StorageClass from the configured classes and admission, the attachment capacity term, the Hetzner deletion proof, and the CSI driver if nothing else uses it. Evidence: a green full suite.

## 9. Production drill and closure

- [ ] 9.1 Create a Cloud recovery agent with the dedicated-node label and taint, select a seeded test cell onto it, destroy the agent, and relocate the cell. Record the recovery point and recovery time.
- [ ] 9.2 Sync these deltas into canonical specs after `adopt-exomem-cloud-plain-cells` and `add-cloud-service-resource-policy` archive, then archive this change.
