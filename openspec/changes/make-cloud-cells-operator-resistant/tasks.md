## 1. Per-cell key hierarchy

- [ ] 1.1 Early check: on a disposable cluster with the pinned hcloud CSI driver, a StorageClass that names a per-namespace node-publish Secret through the provisioner's templates creates, mounts and remounts an encrypted volume. Record the LUKS version that the driver formats. Evidence: the drill output and `cryptsetup luksDump` header version, recorded in design D1.
- [ ] 1.2 Mint a cell root key per cell in cellctl. Wrap it under the platform master key, with the cell ID, purpose and version as additional data. Derive or wrap the volume passphrase, the restic password and, once `add-cloud-media-store` lands, the media key bundle under it. Evidence: unit tests show that two cells' keys differ, that a wrong cell ID fails to unwrap, and that no content key is wrapped directly by the master key.
- [ ] 1.3 Render each new cell's volume Secret in its own namespace, and point the cell StorageClass at it. Evidence: a rehearsal cell's volume opens with its own passphrase and not with the shared one.
- [ ] 1.4 Migrate existing cells one at a time, owner first, through the relocation path: stop under a hold, fresh backup, new volume under the cell's own passphrase, restore, verify, start. Delete the old volume after a soak. Remove the shared passphrase once no volume uses it. Evidence: per cell, the shared passphrase opens none of its volumes, recall, governance status and a governed write pass, and the measured downtime is recorded per size class.
- [ ] 1.5 At cell deletion, destroy every live copy of the cell root key, its content keys and any infrastructure key. Record `keys_expire_at` from the longest platform backup retention, and report the keys destroyed only after it. Update `docs/runbooks/cloud-cell-deletion.md` with the date. Evidence: a rehearsal deletion shows no live copy at once, and its status names the platform backups that still hold copies until `keys_expire_at`.
- [ ] 1.6 Report a cell on storage without a per-cell volume key as not meeting the requirement. Record in `move-cloud-cells-to-local-storage` that its cutover must first add a per-volume LUKS2 layer under the cell's volume passphrase, and name that integration's maintained basis or custom part and its owner. Evidence: the status field on a local-storage rehearsal cell, and that change's updated design.
- [ ] 1.7 Inventory every content-bearing scratch path of a cell (the runtime's `TMPDIR`, media job scratch, import and export staging, and caches that hold content). Move each onto the cell's encrypted volume or into a memory-backed emptyDir within the memory limit, and report a cell whose scratch does not comply. Stage an owner export one part at a time, not as a whole-vault archive. Disable core dumps for cell processes, keep node swap off or encrypted, and restart a crash-locked cell in a new pod. Evidence: a canary written through capture, media processing and an owner export appears on no node path outside the cell's volume, and the memory and latency effect is recorded.

## 2. Access record

- [ ] 2.1 Choose the shipper (Vector or fluent-bit) by fit with K3s audit files and journald, and record the choice in design D4. Create the B2 bucket with Object Lock compliance-mode default retention of at least one year, and a write-only key for the nodes. Evidence: a delete and a retention shortening with the owner key are both refused.
- [ ] 2.2 Pin the audit policy so that connect subresources, Secret access, RBAC and admission changes and certificate requests stay at Metadata or above. Ship them and the node session events (SSH login, key or certificate identity, sudo, session end) within five minutes. Strip query parameters from request URIs, and check every shipped field, including StatefulSet bodies, for Secret values and content. Evidence:
  - a rendered-policy test;
  - a rehearsal break-glass exec and Secret read found in the bucket within five minutes, without the exec command line;
  - a field check of shipped StatefulSet bodies.
- [ ] 2.3 Write a heartbeat at a configured interval, and alert the operator on a missing one. Evidence: stopping the shipper for longer than the interval produces a gap and an alert, and no service waits on the upload.

## 3. Tenant access log

- [ ] 3.1 Derive each cell's access log from the record into the control database. Use cellctl's placement history to attribute node sessions, and include image or configuration changes to cellctl. Evidence: a rehearsal break-glass exec appears in that cell's log within ten minutes, and another cell's log does not show it.
- [ ] 3.2 Serve the log from the control plane (separate repository), including for locked and stopped cells, with every field rendered as text and the received-events limit stated. Evidence: the rendered view on a stopped rehearsal cell, checked in a browser.

## 4. Access tiers and presence check

- [ ] 4.1 Early check: list which platform workload images change through image-only patches. Choose the verifier (Sigstore policy-controller or Kyverno) that accepts only GitHub artifact attestations from this repository's release workflow on a release ref, per image family. Write the image-only admission rule for the deploy identity. Evidence: admission tests that
  - permit an attested digest;
  - deny an unattested pushed digest and another family's attestation;
  - deny volume, command, service-account, RBAC, admission and Secret changes.
- [ ] 4.2 Early check: find the FIDO2 path from the operator's workstation, including under WSL, for OpenSSH `-sk` keys that need a touch on every login. Evidence: a login that succeeds with a touch and fails without the authenticator, and a second login that needs a second touch.
- [ ] 4.3 Move the everyday identity's kubeconfig to the operator's machine, and rewrite the routine procedures to use it with no node login. Evidence: every runbook step marked routine runs from the operator machine. A search of the runbooks finds no node login in a routine step.
- [ ] 4.4 Make node SSH accept only presence-checked `-sk` keys, or certificates that certify one. Issue break-glass only inside such a session. Evidence: a login without the authenticator is refused, and a break-glass issue from a non-admin session fails.
- [ ] 4.5 Inventory every admin-tier credential with its permissions and its side of the D6 boundary:
  - operator and recovery credentials (SSH keys, the operator's provider API token, Terraform state and backend credentials, snapshot restore credentials, escrowed server token and CA material, the escrow identity);
  - the release publisher's run-scoped token;
  - service credentials inside the runtime (the CSI controller's provider token, the server's K3s token, cellctl's wrapping keys).

  Check that SOPS accepts a FIDO2 age plugin identity. Move the operator and recovery credentials into presence-gated custody, and remove every admin credential's plaintext from routine-agent environments and from secret stores that the routine or deploy identities can read. Evidence: the inventory, recorded in design D6; automation with routine credentials finds no plaintext copy; decrypting an operator credential fails without a touch; a release still publishes and the CSI driver still provisions.
- [ ] 4.6 Protect the release ref:
  - add a tag ruleset whose only bypass actor for `v*` tags is the release App, never GitHub Actions;
  - move the App's private key into a protected environment that admits only `main`;
  - make the release workflow refuse a tag whose commit `main` does not contain, as a mistake guard;
  - give the GitHub credentials that agents hold no repository administration permission.

  Evidence:
  - a workflow pushed on a branch cannot read the App key or create a `v*` tag;
  - an agent credential cannot create a `v*` tag or change a ruleset or environment;
  - a normal release from `main` still publishes and attests.

## 5. Tenant lock

- [ ] 5.1 Early check: choose the tenant-lock key channel. The candidates are a node-local unlock agent with an inner LUKS2 layer, and an in-pod FUSE layer. Compare their privileges, SQLite write latency on a real cell, and maintenance. Measure WebAuthn PRF support in the browsers and password managers that tenants use, and decide whether passkey wrapping ships with the recovery code or after it. Evidence: both measurements and the support matrix, with dates and sources, recorded in design D3.
- [ ] 5.2 Implement the cellctl, gateway and runtime halves:
  - storage of the tenant's wraps;
  - the chosen key channel;
  - unlock and handover bound to cell, operation, one-use attempt and expiry;
  - conversion with new keys and `lock_complete_at`;
  - the gateway's `CELL_LOCKED` answer from recorded state, and `locked` job status;
  - restore and relocation waiting for unlock;
  - return to standard mode.

  Evidence: rehearsal tests that cover each scenario in `cloud-cell-key-custody`. After an unlock and a handover, a search of the cluster datastore, a datastore snapshot taken during the unlock, the control database and the logs finds no content key or platform-unwrappable root key, and the infrastructure key found there opens only inner-layer ciphertext. A content canary written while unlocked appears on no node path outside the inner layer or memory after a crash.
- [ ] 5.3 Implement the control plane's flow (separate repository): the opt-in warning, recovery code generation and saved-code check, passkey wrapping, unlock and leaving the lock. Evidence: an end-to-end rehearsal of enabling the lock, crashing, unlocking with the recovery code and upgrading without a second unlock.
- [ ] 5.4 Reconcile sleep with `add-cloud-cell-sleep`, whichever change becomes canonical first. A tenant-locked cell is set never to sleep, capacity counts it as awake, and `CELL_LOCKED` stays distinct from `CELL_WAKING`. Evidence: strict OpenSpec validation of the reconciled requirements, and a rehearsal in which an idle tenant-locked cell stays awake.
- [ ] 5.5 When `add-cloud-media-store`'s privacy-copy requirement is canonical, change it to say that the media key bundle is under the cell root key, and is tenant-held in tenant-locked mode. Evidence: strict OpenSpec validation of that update.

## 6. Claims and copy

- [ ] 6.1 Update the privacy copy (separate repository) and `docs/ARCHITECTURE.md` and `docs/deployment.md` for each property as it comes into force, with the per-mode disclosures. Evidence: the copy review in `cloud-operator-access` passes, and the copy test rejects a "cannot access" claim, a "complete record" claim and a claim whose mechanism is not yet in force.

## 7. Confidential-cell evidence (operator-rented host)

- [ ] 7.1 On a Scaleway Elastic Metal EM-I120E rented by the hour, check that the host exposes SEV-SNP. Boot an SNP guest, and verify its attestation report against the AMD key distribution service with the reported TCB. Time the recall path on the same host with SNP on and off. Check whether AMD-SB-3034's fix raises the attested TCB. Evidence: the command output and timings, recorded in design D8 to replace its unverified Scaleway entry. The host is deleted afterwards.
