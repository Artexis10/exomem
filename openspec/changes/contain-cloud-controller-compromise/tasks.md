## 1. Key custody (first)

- [ ] 1.1 The operator creates the cell-data member account in the provider's account group and stores its key-creating key and the watcher's list key in BWS. Evidence: the group lists both accounts, and the new account holds no bucket except cell backups.
- [ ] 1.2 Add the key custodian in its own namespace, on a node without cells, behind a default-deny NetworkPolicy, writing only `cell-object-store-key`. Add the chart check that keeps it out of every namespace `cellctl` can write. Evidence: the K3s Live job shows that `cellctl` cannot write that namespace or read that Secret, and that the custodian creates only the per-cell shape.
- [ ] 1.3 Add the key watcher as a scheduled CI job, with key IDs from Terraform outputs, `readBucketRetentions`, `readBucketEncryption`, `readBucketReplications`, a heartbeat and its own receiver credential. Evidence: on a disposable bucket, a rogue key, a cloned key, a changed default retention and a deleted watcher key each raise an alert.
- [ ] 1.4 After `lock-cloud-backups-against-deletion` task 2.1 creates the locked bucket in the cell-data account, move per-cell key creation for new cells to the custodian, pointed at that bucket; give `cellctl` a staging-bucket key and a list-only backup key, and delete its parent key. Hide only on a signed deletion record. Evidence: the provider's key list shows no key in the cluster that can create keys in either account.
- [ ] 1.5 After step 1.4, rotate every secret that the Terraform state holds: the Cloudflare tunnel token, the Access service-token secret, the durability keys and both state backend keys (foundation and durability). Evidence: the old tunnel token no longer connects, Substrate reaches the control plane with the new Access secret, and the key watcher's first clean run lists no key outside the pinned set.

## 2. Early checks

- [x] 2.1 Render every cell StatefulSet and Job kind from `cellctl` and list each command, argument vector and environment name. Evidence: the closed sets for D3, checked against the rendered manifests.
- [ ] 2.2 Check that a ValidatingAdmissionPolicy expression can compare environment values with chart-rendered literals for every container and init container, null-safe. Evidence: the K3s Live job admits the rendered set and refuses each forged write.
- [ ] 2.3 List every caller of the bearer in the gateway and the cell. Evidence: the dual-accept plan covers each one.

## 3. Cell checks in report mode

- [ ] 3.1 Make the gateway proof mandatory behind a report-mode switch, and log the credential that each request used.
- [ ] 3.2 Add the grant verifier and the URL destination check to `cell-export`, `cell-import` and `cell-restore`, in report mode.
- [ ] 3.3 Substrate (separate repository): add the grant key and sign export, import and restore grants on the request rows.

## 4. Command pin

- [ ] 4.1 Add `exomem cell-backup` and `exomem cell-restore`, run restic with argument vectors, and render backup and restore Jobs through them.
- [ ] 4.2 Add the admission rules for the closed pod template, the trust anchors, the cell ID and the minimum release. Add forged-write cases in the K3s Live job for each field that D3 names. Evidence: each forged write is denied, and every rendered kind is admitted.
- [ ] 4.3 Encrypt owner exports to the grant's recipient, and serve the cell's import recipient on a gateway-authenticated route. Substrate (separate repository): the browser-side key pair, decryption after download, and recipients in the grants.

## 5. Enforcement

- [ ] 5.1 Switch the proof, grant and destination checks to refuse, after a release with no would-be refusals from legitimate traffic.

## 6. Backup custody

- [ ] 6.1 Generate the backup password in `cell-init`, write the escrow copy, and read the password in `cell-backup`.
- [ ] 6.2 Move existing cells one at a time, owner first, into new repositories with `exomem cell-backup-migrate` (new master key, `restic copy`), together with `lock-cloud-backups-against-deletion` task 3.1. Destroy each platform-wrapped password only after `restic check --read-data` passes and every old snapshot appears by original ID. Evidence: the old platform password opens no snapshot in the new repository, and a rehearsal cell restores from escrow after its volume is deleted.
- [ ] 6.3 Deliver signed mode statements, with their own audience, on the tenant-lock key channel, keep the highest generation on the volume, and refuse a downgrade. Evidence: a forged or replayed standard-mode statement to a locked rehearsal cell produces no operator escrow copy.
- [ ] 6.4 When a cell leaves tenant-locked mode, start an empty repository at the next generation, erase the locked-period password before the root key is re-wrapped, and hide the locked-period repository. Evidence: the operator's escrow copy opens no snapshot from the locked period.

## 7. Retirements and alerts

- [ ] 7.1 Retire the bearer key from the gateway, `cellctl` and the chart.
- [ ] 7.2 Add the attack-signal evaluator to the access record pipeline, once `make-cloud-cells-operator-resistant` task 2 is in force.

## 8. Egress (later phase)

- [ ] 8.1 Add the platform egress proxy and narrow cell Job egress to it.
