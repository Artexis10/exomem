## 1. Early checks

- [ ] 1.1 Render every cell StatefulSet and Job kind from `cellctl` and list each command, argument vector and environment name. Evidence: the closed sets for D3, checked against the rendered manifests.
- [ ] 1.2 Check that a ValidatingAdmissionPolicy expression can compare environment values with chart-rendered literals for every container and init container, null-safe. Evidence: the K3s Live job admits the rendered set and refuses each forged write.
- [ ] 1.3 List every caller of the bearer in the gateway and the cell. Evidence: the dual-accept plan covers each one.

## 2. Cell checks in report mode

- [ ] 2.1 Make the gateway proof mandatory behind a report-mode switch, and log the credential that each request used.
- [ ] 2.2 Add the grant verifier and the URL destination check to `cell-export`, `cell-import` and `cell-restore`, in report mode.
- [ ] 2.3 Substrate (separate repository): add the grant key and sign export, import and restore grants on the request rows.

## 3. Command pin

- [ ] 3.1 Add `exomem cell-backup` and `exomem cell-restore`, run restic with argument vectors, and render backup and restore Jobs through them.
- [ ] 3.2 Add the admission rules for the closed pod template, the trust anchors, the cell ID and the minimum release. Add forged-write cases in the K3s Live job for each field that D3 names. Evidence: each forged write is denied, and every rendered kind is admitted.
- [ ] 3.3 Encrypt owner exports to the grant's recipient, and serve the cell's import recipient on a gateway-authenticated route. Substrate (separate repository): the browser-side key pair, decryption after download, and recipients in the grants.

## 4. Enforcement

- [ ] 4.1 Switch the proof, grant and destination checks to refuse, after a release with no would-be refusals from legitimate traffic.

## 5. Backup custody

- [ ] 5.1 Generate the backup password in `cell-init`, write the escrow copy, and read the password in `cell-backup`.
- [ ] 5.2 Move existing cells one at a time, owner first, into new repositories with `exomem cell-backup-migrate` (new master key, `restic copy`), together with `lock-cloud-backups-against-deletion` task 3.1. Destroy each platform-wrapped password only after `restic check` passes and the snapshot lists match. Evidence: the old platform password opens no snapshot in the new repository, and a rehearsal cell restores from escrow after its volume is deleted.
- [ ] 5.3 Deliver signed mode statements, with their own audience, on the tenant-lock key channel, keep the highest generation on the volume, and refuse a downgrade. Evidence: a forged or replayed standard-mode statement to a locked rehearsal cell produces no operator escrow copy.
- [ ] 5.4 Start a new repository when a cell leaves tenant-locked mode. Evidence: the operator's escrow copy opens no snapshot from the locked period.

## 6. Key custody

- [ ] 6.1 Add the key custodian in its own namespace, with the chart check that keeps it out of every namespace `cellctl` can write. Evidence: the K3s Live job shows that `cellctl` cannot write that namespace, and that the custodian creates only the per-cell shape.
- [ ] 6.2 Add the key watcher outside the cluster, with a list-only key and pinned bucket settings. Evidence: a disposable rogue key and a changed default retention on a disposable bucket each raise an alert.
- [ ] 6.3 Move per-cell key creation and deletion to the custodian, give `cellctl` a list-only key, and delete its parent key. Evidence: the provider's key list shows no key in `cellctl` that can create keys.

## 7. Retirements and alerts

- [ ] 7.1 Retire the bearer key from the gateway, `cellctl` and the chart.
- [ ] 7.2 Add the attack-signal evaluator to the access record pipeline, once `make-cloud-cells-operator-resistant` task 2 is in force.

## 8. Egress (later phase)

- [ ] 8.1 Add the platform egress proxy and narrow cell Job egress to it.
