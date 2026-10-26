## 1. Per-cell key hierarchy

- [ ] 1.1 Early check: on a disposable cluster with the pinned hcloud CSI driver, a StorageClass that names a per-namespace node-publish Secret through the provisioner's templates creates, mounts and remounts an encrypted volume. Record whether deleting the Secret after publish leaves the mounted volume serving, and what a later remount does. Evidence: the drill output, recorded in design D1 and D3.
- [ ] 1.2 Mint a cell root key per cell in cellctl. Wrap it under the platform master key, with the cell ID, purpose and version as additional data. Derive or wrap the volume passphrase, the restic password and, once `add-cloud-media-store` lands, the media key bundle under it. Evidence: unit tests show that two cells' keys differ, that a wrong cell ID fails to unwrap, and that no data key is wrapped directly by the master key.
- [ ] 1.3 Render each new cell's volume Secret in its own namespace, and point the cell StorageClass at it. Evidence: a rehearsal cell's volume opens with its own passphrase and not with the shared one.
- [ ] 1.4 Migrate existing cells one at a time, owner first: fresh backup, keyslot swap, online `cryptsetup reencrypt`, verify, report migrated. Remove the shared passphrase once no volume uses it. Evidence: per cell, the old passphrase fails to open the volume, and recall, governance status and a governed write pass after migration.
- [ ] 1.5 Destroy every stored copy of the cell root key and its data keys at cell deletion, and report deleted only after their absence is observed. Evidence: a rehearsal deletion after which a restore of the retained backup fails with every key that the platform holds.
- [ ] 1.6 Report a cell on storage without a per-cell volume key as not meeting the requirement. Record in `move-cloud-cells-to-local-storage` that its cutover must first adopt per-volume LUKS2 under the cell's volume passphrase. Evidence: the status field on a local-storage rehearsal cell, and that change's updated design.

## 2. Locked access record

- [ ] 2.1 Choose the shipper (Vector or fluent-bit) by fit with K3s audit files and journald, and record the choice in design D4. Create the B2 bucket with Object Lock compliance-mode default retention of at least one year, and a write-only key for the nodes. Evidence: a delete and a retention shortening with the owner key are both refused.
- [ ] 2.2 Pin the audit policy so that connect subresources, Secret access, RBAC and admission changes and certificate requests stay at Metadata or above. Ship them and the node session events (SSH login, certificate serial, sudo, session end) within five minutes. Evidence: a rendered-policy test, and a rehearsal break-glass exec and Secret read found in the bucket within five minutes.
- [ ] 2.3 Write a heartbeat at a configured interval, and alert the operator on a missing one. Evidence: stopping the shipper for longer than the interval produces a gap and an alert.

## 3. Tenant access log

- [ ] 3.1 Derive each cell's access log from the record: connect and Secret events in its namespace, node sessions on its node, break-glass credentials issued while it ran, and gaps. Serve it on a content-free cell endpoint. Evidence: a rehearsal break-glass exec appears in that cell's log within ten minutes, and another cell's log does not show it.
- [ ] 3.2 Show the log in the control plane's cell settings (separate repository). Evidence: the rendered view on a rehearsal cell, checked in a browser.

## 4. Access split and presence check

- [ ] 4.1 Early check: list which platform workload images change through image-only patches, and whether the platform Helm release can run without reading Secrets. Write the admission rule that permits the deploy identity only image changes to release-registry digests. Evidence: admission tests that permit an image-only digest change and deny volume, command, service-account, RBAC, admission and Secret changes.
- [ ] 4.2 Early check: find the FIDO2 authenticator path that works from the operator's workstation, including under WSL, for OpenSSH `-sk` keys or a touch-gated certificate CA. Evidence: a login that succeeds with a touch, and fails without the authenticator.
- [ ] 4.3 Move the everyday identity's kubeconfig to the operator's machine, and rewrite the routine procedures to use it with no node login. Evidence: every runbook step marked routine runs from the operator machine. A search of the runbooks finds no node login in a routine step.
- [ ] 4.4 Make node SSH accept only presence-checked keys or certificates. Issue break-glass only from such a session. Evidence: a login without the authenticator is refused, and a break-glass issue from a non-admin session fails.

## 5. Tenant lock

- [ ] 5.1 Early check: measure WebAuthn PRF support in the browsers and password managers that tenants use. Decide whether passkey wrapping ships with the recovery code or after it. Evidence: a recorded support matrix with dates and sources.
- [ ] 5.2 Implement the cellctl and runtime halves:
  - storage of the wrapped key;
  - the ephemeral-key unlock and a transient mount Secret that is deleted after publish;
  - the one-use handover on planned restarts;
  - the `CELL_LOCKED` answer and `locked` job status;
  - restore and relocation waiting for unlock;
  - sleep exclusion;
  - return to standard mode.

  Evidence: rehearsal tests that cover each scenario in `cloud-cell-key-custody`. After enabling the lock, a search of the control database and cluster finds no copy of the cell root key that the platform can unwrap.
- [ ] 5.3 Implement the control plane's flow (separate repository): the opt-in warning, recovery code generation and saved-code check, passkey wrapping, unlock and leaving the lock. Evidence: an end-to-end rehearsal of enabling the lock, crashing, unlocking with the recovery code and upgrading without a second unlock.
- [ ] 5.4 When `add-cloud-media-store`'s privacy-copy requirement is canonical, change it to say that the media key bundle is under the cell root key, and is tenant-held in tenant-locked mode. Evidence: strict OpenSpec validation of that update.

## 6. Claims and copy

- [ ] 6.1 Update the privacy copy (separate repository) and `docs/ARCHITECTURE.md` and `docs/deployment.md` for each property as it comes into force, with the per-mode disclosures. Evidence: the copy review in `cloud-operator-access` passes, and the copy test rejects a "cannot access" claim and a claim whose mechanism is not yet in force.

## 7. Confidential-cell evidence (operator-rented host)

- [ ] 7.1 On a Scaleway Elastic Metal EM-I120E rented by the hour, check that the host exposes SEV-SNP. Boot an SNP guest, and verify its attestation report against the AMD key distribution service with the reported TCB. Time the recall path on the same host with SNP on and off. Check whether AMD-SB-3034's fix raises the attested TCB. Evidence: the command output and timings, recorded in design D8, which replace its unverified rows. The host is deleted afterwards.
