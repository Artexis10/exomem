## Context

A Cloud cell is one pod with one encrypted volume on a node that the operator runs. Its runtime embeds, indexes, links and recalls the vault in plaintext memory. That server-side work is the product, so strict end-to-end encryption does not fit: an encrypted sync bucket gives a connector nothing to query.

Current key custody and access paths (from `origin/main`):

| Item | Where it lives | Who can read it |
|---|---|---|
| Volume passphrase | One Secret for every Hetzner volume, through the StorageClass node-publish reference | cluster-admin, node root |
| Backup data keys | Per cell, wrapped under one backup master key in the control database; restic passwords rendered into each cell's Secret | cluster-admin, node root |
| Cell bearer root | One Secret, read by cellctl and the gateway | cluster-admin, node root |
| Everyday read-only kubeconfig | On the server node, in root's home | anyone who logs in to the node as an administrator |
| Cluster audit log | On the server node, seven-day rotation | root can rewrite it |
| Dedicated-host disks (planned) | LUKS2 under the volume group, unlocked by Tang on the server node | root on the server |

The everyday operator identity has no exec and no Secret reads. But reaching it requires a node login as an administrator with passwordless sudo. The admission rule that denies exec in cell namespaces "guards against accidents, not intent". Automated agents now run many operator procedures with the operator's credentials, so a boundary that relies on good intent is too weak.

## Goals / Non-Goals

**Goals**
- Make every path to tenant content deliberate, recorded where the operator cannot erase it, and visible to the tenant.
- Let a tenant opt in to a lock that leaves the operator no stored key for their cell.
- Keep agents productive on routine work (health, resources, errors, content-free logs, image rollouts) without any path to content.
- Destroy a deleted cell's keys, so its backups and stored media become unreadable.
- Add no material infrastructure cost on the current hosting.
- Record the hardware step, and the condition that starts it.

**Non-Goals**
- Operator-blind cells now. Without hardware memory encryption, root on an unlocked cell's node can still read it (see the threat model).
- End-to-end encryption.
- Protection against the AI client the tenant connects. It receives whatever recall returns.
- A move to a shell-less node operating system. It adds friction for the operator but no blindness, because the operator still holds its API.

## Threat model

Adversaries, in the order this change ranks them:

1. **An agent or attacker holding the operator's routine credentials.** It wants content and may run any procedure that those credentials allow. It cannot touch the operator's authenticator.
2. **The operator acting deliberately**, with every credential and their authenticator.
3. **A provider insider** with access to volumes, snapshots, backups or disks.
4. **Compelled disclosure** of stored data.

Already handled elsewhere: other tenants (namespace, volume and network isolation) and the public edge (`cloud-operator-access`).

Out of scope, and stated as limits:
- the tenant's own devices;
- the AI client;
- a malicious release that passes review and is published, because it is public and permanent;
- hardware attacks on a future TEE.

What each mechanism stops:

| Mechanism | Stops | Does not stop |
|---|---|---|
| Per-cell keys (D1) | One leaked data key exposing every cell; keys outliving a deleted cell (D2) | Anyone holding the platform master key in standard mode |
| Tenant lock (D3) | Reading a locked, stopped or deleted cell; decrypting its backups or stored media offline; compelled disclosure of stored data | Root on the node while the cell is unlocked, through the decrypted mount, a memory read, or code that captures the key |
| Locked access record (D4) | Silent use of exec, Secrets, break-glass or node shells; erasing the record afterwards | Root disabling the shipper first, which leaves a visible gap; reads that never pass through the API server or a logged session |
| Tenant access log (D5) | Access that the tenant never hears about | A deliberate change to the log's derivation, which is itself recorded |
| Access split and presence check (D6) | Agent 1 reaching any content path; a deploy that opens a shell or reads a Secret | Agent 1 merging and releasing code that leaks content, which needs review and is public; adversary 2 |
| Truthful claims (D7) | Users trusting a property that is not in force | — |

**Core truth.** Without hardware memory encryption, whoever controls the node that runs an unlocked cell can read it. This change makes that act deliberate, recorded and visible, and removes every stored-key path. Only a confidential cell (D8) removes the live path.

## Decisions

### D1. One cell root key per cell; every data key hangs under it

cellctl mints a 256-bit cell root key for each cell. Every key that encrypts that cell's data is derived from it or wrapped by it:

- the volume passphrase;
- the restic repository password;
- the media key bundle of `add-cloud-media-store`;
- any later data key.

Wrapping uses AES-256-GCM, with the cell ID, purpose and version as additional data. This is the shape that backup keys already use.

**Standard mode.** cellctl stores the cell root key wrapped under the platform master key in the control database. The operator can therefore unwrap it, and the privacy copy says so (D7). Per-cell keys still bring three gains:
- one leaked volume passphrase or restic password exposes one cell;
- deletion destroys a key that only one cell used (D2);
- the tenant lock (D3) and a later TEE (D8) re-root one key per cell, not every data key.

**Hetzner volumes.** The StorageClass names the volume Secret through the external provisioner's templated node-publish parameters, so each cell's volume reads a Secret in its own namespace. The pinned hcloud CSI driver reads `encryption-passphrase` from the NodePublish Secret (`infra/helm/platform/HCLOUD_CSI_PROVENANCE.md`). A real-volume create, mount and remount drill must pass before any cell moves, as that file requires.

**Existing volumes.** A LUKS keyslot change alone keeps the volume's master key, so anyone who read the old shared passphrase together with a header copy could still decrypt. Migration therefore swaps the keyslot and then runs `cryptsetup reencrypt` online, which replaces the master key. A cell counts as migrated only after re-encryption completes.

**Local storage.** `move-cloud-cells-to-local-storage` encrypts at the node level and calls per-tenant keys a non-goal (its D2). That non-goal is superseded: before its cutover, each local logical volume carries its own LUKS2 layer under the cell's volume passphrase. Until that ships, a cell on local storage does not meet the per-cell key requirement, and the privacy copy may not claim it for that cell.

### D2. Deleting a cell destroys its keys

Cell deletion destroys every stored copy of the cell root key and every data key under it: control-database rows, cell Secrets and the unlock wrap. cellctl reports the cell deleted only after it observes their absence.

Backup and media objects that a provider still retains then decrypt with no key that anyone holds. This holds whatever the provider's own deletion delay or versioning does. Media objects keep the deletion path of `add-cloud-media-store` D9 as well.

### D3. The tenant lock is opt-in and leaves no stored platform key

**Enabling the lock**
- The tenant's client generates a recovery code. A passkey adds a second wrap where the platform supports the WebAuthn PRF extension. Each one derives a key-encryption key through HKDF, and the cell root key is wrapped under each.
- The tenant confirms that they saved the recovery code. Only then does cellctl destroy the platform-wrapped copy.
- The recovery code is mandatory, because PRF support across browsers and password managers is uneven. Coverage is checked early (task 5.1).

**Unlocking**
- The control plane serves the tenant's client the wrapped cell root key. The client unwraps it locally, then wraps it again to an ephemeral public key that cellctl generates for this unlock.
- cellctl decrypts it and renders the volume passphrase into a transient Secret. It starts the cell and deletes the Secret once the volume is published.
- The runtime receives the cell root key and derives the backup and media keys in memory.
- No Secret that holds a data key outlives the mount it served.
- Because TLS ends on the operator's node, this flow protects the key against logs and casual capture, not against root on that node. The threat model says so.

**Planned restarts** (upgrade, rollout, a hold that stops the cell):
- cellctl asks the running cell for a one-use handover. The cell returns the volume passphrase and cell root key, wrapped to cellctl's ephemeral key for this handover.
- cellctl then repeats the unlock path for the new pod.
- Nothing reaches durable storage.

**Unplanned stops:** a crash, an eviction or a node failure locks the cell. While a cell is locked:
- MCP and HTTP requests answer with a coded `CELL_LOCKED` state that names the unlock route;
- backups, indexing and media jobs do not run, and report `locked`, not `failed`;
- content-free health, resource and lifecycle status keep working;
- a restore or relocation waits for the tenant's unlock;
- a lost recovery code and a lost passkey mean lost data, and the product says so before the tenant enables the lock.

**Sleep.** If `add-cloud-cell-sleep` ships, a tenant-locked cell never sleeps by default, because sleep drops the in-memory key. The tenant may opt in to sleeping and waking locked. cellctl never holds sleeping cells' keys in its own memory, because one process would then hold every locked tenant's key.

**Leaving the lock.** While unlocked, the tenant may return to standard mode. cellctl then stores the platform-wrapped copy again.

The unlock user interface lives in the control plane, in a separate repository. This repository owns the cellctl and runtime halves and the wire contract.

### D4. The locked access record

A maintained log shipper, Vector or fluent-bit, chosen in task 2.1, ships these events off the server node within five minutes:
- K3s audit events for exec, attach, port-forward, proxy and ephemeral containers;
- Secret get, list and watch;
- every create, update or delete of RBAC objects, admission policies and their bindings;
- certificate signing requests.

The same shipper sends node session events: SSH logins and certificate serials, sudo, and the end of each session.

- **Destination.** One B2 bucket whose default retention uses Object Lock in compliance mode. In that mode nobody can delete or shorten a locked object version before its retention ends, Backblaze support and the account owner included (B2 Object Lock documentation, read 2026-10-26). Retention is a configured value with a floor of one year.
- **Writer credential.** It may write but not delete, and it is the only credential on the nodes for this bucket.
- **Heartbeat.** The shipper writes a heartbeat object at a configured interval. A missing heartbeat is a gap, and the tenant access log shows gaps (D5).
- **Content.** The record never contains content. The audit policy keeps request and response bodies only for namespaces, PersistentVolumeClaims and StatefulSets, as today.

The audit policy needs one addition. Connect-subresource and Secret events stay at the Metadata level or above, whatever later policy edits do, and a test of the rendered policy pins this.

### D5. The tenant access log

cellctl derives each cell's access log from the locked record. Each entry is one event or one gap:

- a connect or Secret event in the cell's namespace;
- a node session on the node that hosts the cell;
- a break-glass certificate issued while the cell ran;
- a gap in the heartbeat.

The cell serves the log in its settings view and on a content-free endpoint, within ten minutes of the event. The log shows time, identity class (routine, deploy or admin), action, target and, where given, the stated reason. It never shows content.

cellctl derives the log, and the operator runs cellctl. So a deliberate operator could change the derivation, but that change is a recorded deploy or admin action.

### D6. Three access tiers; content paths need the operator's presence

1. **Routine.** The everyday read-only identity (`exomem-operator-read`) works from the operator's own machine over the private network. Its kubeconfig lives there, not on any node. Routine procedures need no node login. Agents keep pod status, events, endpoints, policies, resource use and content-free logs.
2. **Deploy.** A new identity can only change container images:
   - of cells, through cellctl release attempts;
   - of named platform workloads, through an image-only patch.

   An admission rule compares the old and new objects and permits a change only to container image fields. Each image must be a digest in the release registry, a format check. The identity has no exec, attach, port-forward, proxy or ephemeral containers. It cannot read Secrets, or change RBAC, admission policy, volumes or Secrets.

   A full chart upgrade installs RBAC and admission policy, so no scoped identity can run it. It is an admin action. Which component images change through image-only patches, and whether the Helm release can avoid reading Secrets, is checked early (task 4.1).
3. **Admin.** Node shells, the break-glass identity, secret application and full chart upgrades need a **presence check**: a touch on a FIDO2 authenticator that the operator holds. Two shapes are possible:
   - node SSH accepts only OpenSSH `-sk` keys or short-lived user certificates signed by a CA key that needs that touch;
   - the break-glass certificate is minted only from such a session.

   Passwordless sudo stays, but only behind that login. Which authenticator works from the operator's workstation, including under WSL, is checked early (task 4.2).

This is the one mechanism that stops adversary 1 before the fact. A presence check needs a human by design: its purpose is to keep automation that holds the operator's credentials from reaching content. A wrongly firing check costs the operator one touch. A skipped check would cost an unrecorded read of a tenant's vault.

The remaining agent path is code. An agent that can merge and release could ship code that leaks content. That path needs review, leaves a public release, and is a stated limit.

### D7. Claims follow the mechanisms in force, per mode

The privacy copy may claim a property only after the requirement that provides it runs in production for the cell in question.

- **Standard mode**, once D1, D4, D5 and D6 run: nobody but the operator can reach the cell; never by accident or silently; every access path is recorded where the operator cannot erase it and is shown to the tenant; data at rest and in backups is encrypted under keys for that cell alone, and deleting the cell destroys them.
- **Tenant-locked mode** adds: stored data and backups cannot be decrypted without the tenant's key.

Disclosures:
- In standard mode, the operator holds the cell's keys on the serving infrastructure and could deliberately read content.
- In tenant-locked mode, while the cell is unlocked, a deliberate attack on the node that runs it can still reach content.
- A lost recovery code and passkey means lost data.
- The edge and gateway see requests and responses in transit.

No copy claims that the operator is technically unable to access tenant data while cells run without hardware memory encryption and attestation.

### D8. Confidential cells are parked, with a trigger

**Direction.**
- Each cell runs in its own SEV-SNP confidential VM, either Kata Confidential Containers or a per-cell VM.
- The image is measured and reproducible, with no shell and no exec into the guest.
- The cell root key is released only after an attestation report matches a published measurement, at or above a minimum TCB. The key is then sealed to that measurement for restarts.
- TLS terminates inside the VM.
- The tenant lock carries over. With attestation verified by the tenant's client, the operator then holds no live path either.

**Open questions for the follow-up change:**
- the gateway's role once TLS passes through to the cell;
- key handover across image upgrades, which change the measurement;
- whether the tenant's client or an operator-run broker verifies attestation;
- per-cell memory, because confidential VMs cannot share model weights.

**Availability, read from provider pages on 2026-10-26:**

| Option | What the provider states | Price excl. VAT |
|---|---|---|
| Hetzner dedicated (EPYC 9454P: AX162; Dell DX182 with iDRAC9 Enterprise) | No SEV-SNP or TDX statement. Customer iDRAC on Dell models; a KVM console on others. Whether SNP can be enabled is unverified. | AX162 €614/mo + €304 setup; DX182 €514–724/mo + €359 setup |
| Hetzner Cloud | No SEV-SNP or TDX | — |
| Scaleway Elastic Metal | SEV-SNP "supported on … All new AMD-based Elastic Metal offers" (docs reviewed 2026-09-11); no customer BIOS changes | EM-I120E (EPYC 8024P, 64 GB) €134.99/mo or €0.37/h |
| IONOS Confidential VM | SEV-SNP on EPYC Turin, Frankfurt; customer-run attestation | €0.047 per core-hour + €0.005 per GB-hour |
| OVHcloud Scale 2026 | "supports AMD SEV technology"; SNP not named | not stated |
| GCP, Azure, AWS | SNP in EU regions (GCP N2D, Azure DCasv5, AWS eu-west-1 shared or Dedicated Hosts) | Azure DC4as_v5 West Europe $0.208/h; others not read |

**Cost estimate, not measured.** A confidential VM per cell at about 3 GiB puts about 20 cells on a 64 GB host. On the EM-I120E that is about €7 per cell per month. For comparison, the repository's Terraform prices the current server type, which hosts many cells, at €8.99 a month.

**Limits to state:**
- A software-only malicious host breaks SEV-SNP on unpatched firmware (Fabricked, USENIX Security 2026; AMD-SB-3034). Key release must therefore require a minimum TCB. Whether AMD's fix raises the attested TCB is unverified.
- A low-cost memory interposer with host control breaks TDX and weakens SEV-SNP integrity (DDRop, CCS 2026). It needs physical access.

**Trigger.** A follow-up change specifies confidential cells when either condition holds:
- a confidential host costs no more per cell than the standard fleet at the cell count then served;
- a provider offers SEV-SNP or TDX at a comparable per-cell price.

Task 7 replaces the unverified availability rows with evidence from a rented host.

## Reuse

| Need | Maintained option used | Custom part, and why |
|---|---|---|
| Per-volume encryption | hcloud CSI LUKS with templated node-publish Secrets; LUKS2 and `cryptsetup reencrypt` | None |
| Append-only record | B2 Object Lock in compliance mode; Vector or fluent-bit | The heartbeat writer, a few lines of configuration |
| Presence check | OpenSSH `-sk` keys and user certificates | None |
| Image-only deploys | ValidatingAdmissionPolicy with CEL | The rule itself |
| Tenant lock | WebAuthn PRF, HKDF, AES-GCM, age-style recipient wrapping | The unlock and handover flow, because no maintained component binds a user-held key to a CSI volume mount |
| Access log | — | Its derivation from the record, which is cell-specific |
| Confidential cells (parked) | Kata Confidential Containers, snphost and snpguest, the AMD key distribution service | Deferred |

## Risks / Trade-offs

- **Lock-out.** A tenant-locked cell stops serving after a crash until its tenant returns. Mitigation: the lock is opt-in, the warning comes first, planned restarts hand over, and the locked state answers with a coded route.
- **Lost keys.** A lost recovery code and passkey lose the vault. Mitigation: the recovery code is mandatory, and the client verifies that it was saved before the platform copy is destroyed.
- **Shipper outage.** The record shows a gap that is not an attack. Mitigation: a gap is labelled as a gap, never as an access, and an operator alert fires on a missing heartbeat.
- **Presence friction.** Full chart upgrades and break-glass need the operator. Mitigation: image rollouts are touch-free, and routine checks need no node login.
- **Re-encryption load.** `cryptsetup reencrypt` rewrites each volume once. Mitigation: one cell at a time, behind a hold, after a fresh backup.

## Migration Plan

1. Run the early checks: templated Secrets, delete after mount, image-only admission, the authenticator path and PRF coverage.
2. Ship per-cell keys. New cells get them first. Existing cells migrate one at a time, owner first: backup, keyslot swap, re-encryption, verify, then remove the shared passphrase once no volume uses it.
3. Ship the locked record, the tenant access log and the access split together, because the claim needs all three.
4. Ship the tenant lock.
5. Change the privacy copy for each property as it comes into force.

**Rollback.** Per-cell keys and the record are additive until the shared passphrase is removed. The tenant lock is per cell and reversible while unlocked.

## Open Questions

- Which component images can change through image-only patches, and whether the Helm release can avoid reading Secrets (task 4.1).
- Which FIDO2 authenticator works from the operator's workstation (task 4.2).
- Whether AMD's Fabricked fix raises the attested TCB (task 7).
