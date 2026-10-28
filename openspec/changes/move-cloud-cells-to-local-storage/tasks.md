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
- [ ] 2.2 Requalify the storage-capacity term under D12:
  - physical pool size minus the effective D12 reserve and ordinary physical charges not already represented by counted cell commitments, over the default size;
  - pool size and reserved bytes from physical provider inventory, including unclaimed volumes and partial operations;
  - larger cells charge more;
  - no published pool means zero;
  - only the configured domain publishes slots, and every non-deleted cell counts against them.
  - Evidence: unit tests on recorded observations, including a pass during a backup.
- [x] 2.3 Render the hourly online backup as a non-stopping hold:
  - snapshot, read-only clone, restic on the clone, cleanup whatever the outcome;
  - one hold per cell at a time;
  - prune only on the first backup after 02:00 UTC, and never on the pre-upgrade backup;
  - per-node backup concurrency, default 2;
  - a quota of two claims, twice the storage, and the serving pod plus one backup Job's CPU and memory;
  - the clone claim on an Immediate-binding clone class, so the Job follows the clone's PV to the cell's node with no selector;
  - the stopped pre-upgrade backup unchanged;
  - a cell on a class without snapshot support keeps the nightly stopped backup, window, concurrency 1 and prune.
  - Evidence: unit tests for both classes, and a rehearsal backup of a serving cell.
  - Done: Cloud rehearsal run 37436989618 (`local-storage-drill` mode, commit 3070083fa). Three snapshot backups ran while the cell served: same pod, 0 restarts, 0 not-ready polls. Each Job and clone ran on the cell's node with no selector, each hold took about 33 s, and the snapshot, clone and Job were gone when the hold ended.
- [x] 2.4 Add operator-triggered relocation:
  - require the same stop confirmation as node removal;
  - force-delete the pod, set the old PV to Retain, then delete and recreate the claim;
  - restore the last backup, record the new volume identity, start only after the restore succeeds.
  - Evidence: unit tests and a rehearsal relocation.
  - Done: Cloud rehearsal run 37436989618 (`local-storage-drill` mode, commit 3070083fa). After its agent was destroyed, the cell relocated onto the other agent: old PV set to Retain, restore finished before the pod started, row records the new volume, acceptance with recall and a governed write.
- [x] 2.5 Make the deletion proof per storage driver: PV, backend volume, snapshots and clones absent. A failed listing is not absence; a volume on a node confirmed destroyed is. Evidence: unit tests.
- [x] 2.6 Add the empty-vault guard:
  - cellctl writes a `backed_up` key into the cell's Secret after the first recorded backup;
  - `cell-init` refuses an empty initialisation when the key is present, and writes a value-free code to its termination message;
  - cellctl records that code on the row at once.
  - Evidence: tests for a backed-up cell, a new cell, and a first backup that leaves the render digest unchanged.
- [x] 2.7 Make the JSON-state writers fsync the temp file before renaming it, and write the writer-lease commit counter by rename: `prominence.py`, `envelope.py`, `mode.py`, `dreamer.py`, `writer_lease.py`. Evidence: unit tests that a write goes through the durable path.
- [x] 2.8 Alert when a running cell's last successful backup is older than two hours, or 26 hours for a cell on the nightly backup. Evidence: an alert-rule test.
- [x] 2.9 Add the post-etcd-restore reconciliation:
  - an Ansible step on each agent lists logical volumes;
  - an operator runbook step re-adopts each one that matches a row's `volume_id` (design D4): it recreates the LogicalVolume object, renames the old volume to the new volume ID in place of the new empty one, creates the PV, and clears the row's `volume_id` by compare-and-set so cellctl records the new one (cellctl gains no PV create);
  - no step deletes a LogicalVolume object except to destroy its volume;
  - volumes that match no row are reported and released only by an operator step on the host;
  - a row whose volume cannot be re-adopted is relocated from backup, and the identity check is never skipped.
  - Evidence: unit tests on a recorded mismatch, and a rehearsal etcd restore from an older snapshot.
  - Done: Cloud rehearsal run 37436989618 (`local-storage-drill` mode, commit 3070083fa). The server was reset from an older local etcd snapshot; `cellctl.readopt` reported the newer cell for re-adoption, the runbook steps re-adopted its volume, and it served with its data and a matching identity. Not exercised: the escrowed-key S3 snapshot listing.

- [ ] 2.10 Requalify local online growth under D10 and D12:
  - the hourly backup Job reports the filesystem's used and total bytes in its termination message, and cellctl records them;
  - past 80% use, cellctl grows the claim by one default cell size, up to the configured cap, at most once per backup;
  - only through the provider transaction that retains the prospective reserve; otherwise an alert and a retained target;
  - the grown size is recorded in a cellctl-owned row column (Substrate migration) and the claim renders at the larger of it and `storage_gib`.
  - Evidence: unit tests for growth, the cap, the pool refusal with its alert, and a rehearsal growth of a serving cell.
  - Done: Cloud rehearsal run 37436989618 (`local-storage-drill` mode, commit 3070083fa). At 85% use a serving cell grew from 4 to 8 GiB online: same pod, 0 restarts, filesystem 4143677440 to 8369172480 bytes, quota applied at 16Gi with no refusal, row `grown_storage_gib` 8, next clone 8Gi, published slots matching D6. At the cap the verdict was `at-cap` and cellctl raised `storage-growth-blocked` (no receiver in the rehearsal).

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

- [x] 4.1 In the rehearsal cluster, with TopoLVM installed from the platform chart's values:
  - seed a cell and let hourly backups run;
  - write once more;
  - destroy its agent;
  - recover onto another agent through relocation.
  - Record that each hourly backup Job ran on its cell's node with no selector, and the upload time to the bucket.
  - Record the measured recovery point and recovery time, and accept with recall, governance status and a governed write.
  - Done: Cloud rehearsal run 37436989618 (`local-storage-drill` mode, commit 3070083fa). Recovery time 115.2 s from the agent's loss to serving on the other agent; recovery point 42.9 s of writes (the last write lost, all backed-up writes kept). Run 37428273291 measured 112.5 s and 38.2 s. Backup Jobs ran on the cell's node with no selector. Upload took 2–4 s to an S3 double on the same runner, so B2 upload time is still to measure (task 9.1). Acceptance: recall and a governed write passed; governance status read the same before and after (`GOVERNANCE_SCHEMA_UNAVAILABLE`, as the rehearsal has no custody configured).
- [x] 4.2 Prove the empty-vault guard: deliberately start the backed-up cell on an empty claim before any restore, and record that it refuses, stays not ready and reports its reason on the row.
  - Done: Cloud rehearsal run 37436989618 (`local-storage-drill` mode, commit 3070083fa). The backed-up cell on an emptied volume refused with `CELL_INIT_EMPTY_VOLUME_REFUSED` on its row, stayed not ready for the whole minute after, and kept its volume.
- [x] 4.3 Interrupt a relocation's restore and record that the cell does not start and the retained volume is untouched.
  - Done: Cloud rehearsal run 37436989618 (`local-storage-drill` mode, commit 3070083fa). With the restore interrupted, the row read `RESTORE_FAILED`, the StatefulSet stayed at 0 replicas with no cell pod, and the retained PV was unchanged (Retain, Released, its LogicalVolume present at the same size). The retried restore served with the backed-up writes.

## 5. Dedicated host bootstrap

Source and disposable rehearsals can finish before purchase. Production installation and acceptance wait for the purchased host and their operator window.

- [ ] 5.1 Install Tang on the server node, and escrow its keys through the secret-destination contract.
- [x] 5.2 Write the dedicated-host Ansible role: md RAID1, LUKS, Clevis bound to Tang, the `cells` volume group, and the recovery passphrase escrowed through the secret-destination contract.
  - Done in #1595 (`infra/ansible/roles/dedicated_host`). Evidence: the dedicated-host bootstrap workflow, run 37402002035 at a96ff3ee5, built the role on loop devices: RAID1, LUKS with the escrowed passphrase as its first key, Clevis pinned to Tang's signing thumbprint and proven to unlock, a refused unpinned Tang, and the `cells` thin pool. A first run on purchased hardware stays with 5.3.
- [ ] 5.3 Make the k3s agent unit wait for the unlock. Evidence: `lsblk` and `cryptsetup status` output, plus a reboot that unlocks unattended and starts K3s afterwards.
- [ ] 5.4 Add the manual unlock and the Tang rebind to the operator runbook, and exercise the manual unlock once.
  - The runbook (`docs/runbooks/cloud-dedicated-host.md`, "Unlock by hand" and "Tang lost or replaced") is written and checked against the role. The exercise is pending: it waits on the purchased host.
- [ ] 5.5 Alert when a dedicated host's cell array is degraded, through the existing alert receiver. Today `site.yml` only reports it, and the host has no mail transport for `mdmonitor`. Evidence: an alert from a failed member on a disposable host.
  - Source delivered in [#1785](https://github.com/Artexis10/exomem/pull/1785): node-exporter's mdadm collector as a DaemonSet on the nodes TopoLVM's node plugin runs on, read by cellctl, with alert-rule tests against node-exporter's real output. The proof, a failed member on the purchased host before cutover, is pending.
- [x] 5.6 Alert when a thin pool's data or metadata use passes 80%, from TopoLVM's thin-pool metrics, through the existing alert receiver. Evidence: an alert-rule test.
  - Done. Evidence: `infra/cellctl/tests/test_local_storage.py`: `test_a_thin_pool_at_80_percent_of_its_data_or_its_metadata_fires`, `test_thin_pools_just_under_80_percent_resolve_the_alert`, `test_a_thin_pool_that_cannot_be_read_sends_nothing_and_never_resolves`, `test_a_full_thin_pool_fires_even_while_another_node_cannot_be_read` and `test_a_degraded_array_and_a_full_pool_each_fire_once_and_resolve_once`. The test page is built from the series names TopoLVM 0.41.1 documents; its first live read comes with the first dedicated host.
- [x] 5.7 Write and rehearse a rotation of the K3s agent token (`k3s_agent_token`) for the pinned K3s version. A dedicated host's disk that leaves the provider's custody unwiped exposes the token. The procedure owns the token's activation: it escrows the new version, then selects it in `infra/contracts/active-ansible-selection-v1.json` in a reviewed commit, because escrowing alone activates nothing. Evidence: every agent rejoins with the new token, and the old token is refused.
  - Built: the `Rotate the agent token` procedure in `docs/runbooks/cloud-dedicated-host.md`, the role's re-save helper `exomem-k3s-resave-bootstrap`, and the `k3s-link` rehearsal mode. Cloud k3s-link rehearsal run 38904329729 (commit c4481124b, PR merge commit 4bb82fa67) passed all seven checks: the second restart without the re-save stopped on the reconcile fatal, the old token got 401 and the new one 200. The rehearsal's MTU selection changed after that run; Cloud k3s-link rehearsal run 38934995581 on main at 9b3c25244 passed all seven checks again. Production activation remains in 5.7a.
- [ ] 5.7a Rotate the production agent token in an operator window using the reviewed procedure. Verify agent rejoin and refusal of the old token.
- [x] 5.8 Write and rehearse a rotation of the cluster's client CA for a lost disk. The disk holds the agent's kubelet client certificate and node password; K3s keeps accepting the certificate after an agent-token rotation and serves new kube-proxy and k3s-controller client certificates to it, so expiry closes nothing. Evidence: a certificate from before the rotation is refused.
  - Built: the `Rotate the client CA` procedure in `docs/runbooks/cloud-dedicated-host.md`, after the agent-token rotation, and the role's helper `exomem-k3s-rotate-client-ca`. The helper generates a new self-signed client CA on the server, checks the pair and loads it with `k3s certificate rotate-ca --force`. K3s's cross-signed rotation keeps the old CA trusted, and with it the old certificates. Only the client CA changes, so agents keep their token and CA hash and restart without a rejoin. The node password needs no change: K3s checks it only after a valid token or certificate. The `k3s-link` rehearsal mode gained four 5.8 checks. A copied kubelet client certificate and node password get 200 from the API server and the supervisor before the rotation, and 401 after it. Both nodes are Ready again, the agent's new certificate gets 200, and a new pod schedules on the agent. Cloud k3s-link rehearsal run 38941177006 (commit 18c8dd81a, PR merge commit e04b4effe) passed all eleven checks; each 401 for the copied certificate read `certificate signed by unknown authority`, and the 200 controls passed. Production activation remains in 5.8a.
- [ ] 5.8a After 5.7a, rotate the production client CA in an operator window. Verify old-certificate refusal and new-certificate access with every node Ready.

## 6. Private-link join (needs the purchased server)

- [ ] 6.1 Add the private-link subnet to Terraform foundation (vSwitch for Hetzner), or the WireGuard configuration for another provider.
- [ ] 6.2 Join the dedicated agent over the private link with the existing hardening, the CA-pinned agent token and the inter-node firewall. The k3s role waits until the agent's storage driver publishes its pool. Evidence: the join playbook output and published slots.
- [ ] 6.3 Add the optional Terraform Cloud recovery agent, with its volume group on one Cloud volume under the same LUKS and Clevis layout, created and removed by variable. Evidence: plan/apply/destroy on a disposable stack.
  - The agent publishes a pool but has no md array, so it must be excluded from the `cell-array-degraded` alert's expected nodes (`reconcile._host_alerts`), or carry an array. Otherwise that alert stays unknown and never resolves.
- [ ] 6.4 Implement relocation in the agent-removal playbook for cells whose volumes the remaining nodes cannot reach: a fresh stopped backup, a restore, start after the restore succeeds, acceptance. Evidence: a removal of a disposable agent holding a seeded cell.
  - Built: the "Remove the host" runbook cordons the agent and moves and accepts each cell through cellctl's planned move (design D7) before `remove-agent.yml` runs. The playbook refuses unless each cell still on the agent fits a remaining node's pool with the D6 snapshot reserve kept, cordons the agent, and moves each live local cell still on it as a fallback (`roles/k3s/tasks/remove_move.yml`), with contract tests on a stand-in kubectl. Pending: the rehearsal removal of a disposable agent holding a seeded cell.
- [ ] 6.5 Set the cluster's flannel MTU so that VXLAN fits the private link before the first dedicated join. Cloud nodes run flannel at 1400 over the 1450-byte Cloud network, so their VXLAN packets are 1450 bytes (1400 plus 50 bytes of overhead). A vSwitch carries at most 1400, and a default WireGuard interface 1420. Evidence: pod-to-pod traffic at full MTU between a Cloud node and the dedicated agent.
  - Source and rehearsal complete: an opt-in `k3s_flannel_mtu` in the k3s role, unset by default so a routine run restarts nothing; with it set, the flannel configuration, `cluster-cidr`, the check that recreates `flannel.1` whenever it runs at another MTU, and the assertion of 1350. The `Roll out the flannel MTU` runbook steps make the rollout a reviewed commit that sets 1400. Cloud k3s-link rehearsal run 38904329729 (commit c4481124b, PR merge commit 4bb82fa67) passed the three 6.5 checks. The rehearsal's MTU selection changed after that run; Cloud k3s-link rehearsal run 38934995581 on main at 9b3c25244 passed the three 6.5 checks again. Pending: the rollout commit and window, and full-MTU traffic to a joined dedicated agent.
- [ ] 6.6 Before the first production dedicated join, run the release gate on the full inventory with `--limit` set to the server, so that no agent needs to be reachable and nothing is pruned. A fleet with a WireGuard host would then need every peer's WireGuard public key carried in the inventory, because only a run on that peer reads its key today. Evidence: the gate's two passes on the server, with the agents unreached.
  - Built: an optional `wireguard_public_key` per WireGuard host in the generator, which emits `k3s_wireguard_public_key`; the host's check of its record; and the runbook step that records it. Role tests cover a server-limited run. Pending: the gate's two passes on the production server.

## 7. Cutover and migration (needs the purchased server)

- [ ] 7.1 Agree the owner migration window with the owner.
- [ ] 7.2 Switch the configured cell storage domain to the local class under a capacity closure, with the old class still accepted for existing cells. Change Substrate's `storage_gib` default for new cells to 4 GiB (design D6). Evidence: published slots before and after, and the closure reopening.
- [ ] 7.3 Migrate the owner cell:
  - stop it and take the final backup;
  - retain the old PV;
  - restore onto local storage;
  - start after the restore succeeds, and accept it with recall, governance status and a governed write;
  - check large-vault restart latency against the existing gates.
  - Built: cellctl's planned move (design D7) and the procedure in `docs/runbooks/cloud-dedicated-host.md` ("Move a cell to a new claim"). Pending before the first production mark: a rehearsed move of a Hetzner-class cell to the local class, and a rehearsed rollback ("Roll a moved cell back"); neither has run. Then the owner's migration on production, after 7.1 and 7.2.
- [ ] 7.4 Migrate the remaining cells one at a time with the same acceptance.
  - Built: the same move and procedure as 7.3. Pending: each remaining cell's migration on production, after 7.3.
- [x] 7.5 Update `docs/runbooks/cloud-operator-import.md`, which still cites the nightly 02:00–05:00 UTC backup window.
  - Done: the runbook names the hourly backup for a cell on local storage and keeps the window for a cell on a Hetzner volume.
- [ ] 7.6 Before the cutover (7.2), pin the Hetzner CSI controller to the server node, as the TopoLVM and snapshot controllers are: its token can attach any volume to any node. This moves a live production pod, so it ships on its own. Evidence: the controller pod's node, and a volume attach after the move.
- [x] 7.7 Before local storage is enabled in production (6.2, 7.2), rehearse the coexistence configuration end to end: TopoLVM installed, `cellStorage.local.enabled` true, the domain still the Hetzner class, Hetzner-class cells provisioned, backed up and deleted. Neither the P3 rehearsal (local storage off, as production today) nor the drill (local domain) covers it. Evidence: a full P3 run in that configuration.
  - Shipped proof: [#1620](https://github.com/Artexis10/exomem/pull/1620), merge `a9dbf068cf18a067f88ba844dc084536a1cc6acb`.
    Strict [full-coexistence P3 37736543082](https://github.com/Artexis10/exomem/actions/runs/37736543082) passed all 12 steps and all gating targets at exact head `8f2c74a6ecf9a2aa26082a9436e8c23d68f91233`.
    Real TopoLVM and snapshot controllers became ready; the local domain published no pool, and the serving claim selected `exomem-cloud-encrypted`.
    The upgrade took 49.2858 seconds, including the stopped D6 backup; target readiness, hold removal and recall passed.
    Diagnostic logs took 1.4798 seconds before the corrected timer; the release request took 0.0362 seconds inside it.
    Earlier strict attempts retained their failures at 62.3219 and 67.7132 seconds; the unchanged limit remains below 60 seconds.
    Non-gating capture p95 was 1.1452 seconds against its 1.0-second target; that target remained unmet.
    Hetzner CSI and object storage retain P3 doubles. This proof authorizes no local production cutover or hardware adoption.

## 8. Retire Hetzner volumes

- [ ] 8.1 After a 7-day soak with hourly backups succeeding, delete the retained Hetzner volumes. Evidence: provider listing.
- [ ] 8.2 Remove the Hetzner StorageClass from the configured classes and admission, the attachment capacity term, the Hetzner deletion proof, and the CSI driver if nothing else uses it. Evidence: a green full suite.

## 9. Production drill and closure

- [ ] 9.1 Create a Cloud recovery agent with the dedicated-node label and taint, select a seeded test cell onto it, destroy the agent, and relocate the cell. Record the recovery point and recovery time.
  - Exclude the agent from the `cell-array-degraded` alert's expected nodes first (task 6.3), or the alert stays unknown while it exists.
- [ ] 9.2 Sync these deltas into canonical specs after `adopt-exomem-cloud-plain-cells` and `add-cloud-service-resource-policy` archive, then archive this change.

## 10. Authoritative reservations before local import growth

The earlier rehearsals prove their recorded workflows. They do not prove concurrent allocation, provider death or delayed CSI cancellation. Tasks 2.2 and 2.10 reopen that qualification; production tasks retain their existing authority boundaries.

- [x] 10.1 Recheck the D12 contract independently, including cancellation before Begin, source incarnation and concurrency reduction.
- [ ] 10.2 Add provider transactions, physical full-size accounting and atomic bounded family metadata to the pinned source overlay. Verify competing allocations and recovery of every partial command boundary.
- [ ] 10.3 Carry immutable family identity through the existing LogicalVolume protocol and snapshot Secret transport. Verify old queued requests cannot use a later family's reservation.
- [ ] 10.4 Integrate Begin and acknowledged closure with cellctl holds, provider budget publication and prospective local claim growth. Keep local import limits NULL until qualification passes.
- [ ] 10.5 Extend the disposable storage drill with the qualified nsenter/LVM process chain. Verify daemon death, early cancellation, stale tokens, source recreation, orphan charges, concurrency reduction and two complete online-backup cycles.
- [ ] 10.6 Review the integrated batch, pass CI, merge and qualify its built images through the ordinary release. Keep production activation separate.
