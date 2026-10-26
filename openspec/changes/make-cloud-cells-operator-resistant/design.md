## Context

A Cloud cell is one pod with one encrypted volume on a node that the operator runs. Its runtime embeds, indexes, links and recalls the vault in plaintext memory. That server-side work is the product, so strict end-to-end encryption does not fit: an encrypted sync bucket gives a connector nothing to query.

Current key custody and access paths (from `origin/main`):

| Item | Where it lives | Who can read it |
|---|---|---|
| Volume passphrase | One Secret for every Hetzner volume, through the StorageClass node-publish reference | cluster-admin, node root, anyone who restores a datastore snapshot |
| Backup data keys | Per cell, wrapped under one backup master key in the control database; restic passwords rendered into each cell's Secret | cluster-admin, node root, anyone with a database backup and the master key |
| Cell bearer root | One Secret, read by cellctl and the gateway | cluster-admin, node root |
| Everyday read-only kubeconfig | On the server node, in root's home | anyone who logs in to the node as an administrator |
| Cluster audit log | On the server node, seven-day rotation | root can rewrite it |
| Admin capabilities outside SSH | Provider API token, K3s server token and CA material, snapshot restore credentials, Terraform state, SOPS/age escrow | whoever holds each credential |
| Dedicated-host disks (planned) | LUKS2 under the volume group, unlocked by Tang on the server node | root on the server |

The everyday operator identity has no exec and no Secret reads. But reaching it requires a node login as an administrator with passwordless sudo. The admission rule that denies exec in cell namespaces "guards against accidents, not intent". Automated agents now run many operator procedures with the operator's credentials, so a boundary that relies on good intent is too weak.

## Goals / Non-Goals

**Goals**
- Keep routine and deploy credentials, and the automation that holds them, away from every path to tenant content.
- Make content access deliberate, need the operator's physical presence, and leave a retained, tenant-visible record of what the record received.
- Let a tenant opt in to a lock that leaves no platform-held copy of their keys, once platform backup retention has passed.
- Make a deleted cell's keys expire everywhere by a stated date.
- Add no material infrastructure cost on the current hosting.
- Record the hardware step, and the condition that starts it.

**Non-Goals**
- Operator-blind cells now. Without hardware memory encryption, root on an unlocked cell's node can still read it.
- A complete, tamper-proof record of what root does on a node. Root controls the record's sources (D4).
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
| Per-cell keys (D1) | One leaked content key exposing every cell; a deleted cell's keys surviving past platform backup retention (D2) | Anyone holding the platform master key in standard mode |
| Tenant lock (D3) | Reading a locked, stopped or deleted cell; decrypting its backups or stored media offline, once earlier platform copies have expired; compelled disclosure of stored data | Root on the node while the cell is unlocked, through the decrypted mount, a memory read, or code that captures the key |
| Access record (D4) | Erasing events after the record received them; an unnoticed shipper outage | Root on a node omitting, altering or forging events before they leave it while heartbeats continue; reads inside a session that the record shows only as a session |
| Tenant access log (D5) | Access that the record received but the tenant never hears about | Events the record never received |
| Access tiers and presence check (D6) | Adversary 1 reaching any content path or admin credential; a deploy of an image that the release workflow did not build from `main` | Adversary 1 merging code to `main` through a pull request and the required checks, then releasing it. That path is public and permanent, but on this repository no second person reviews it. Also adversary 2 |
| Truthful claims (D7) | Users trusting a property that is not in force | — |

**Core truth.** Without hardware memory encryption, whoever controls the node that runs an unlocked cell can read it. This change keeps automation and routine credentials away from that path. It makes the act need the operator's presence, records what passes through the cluster API and node logins, and ends stored platform copies of a locked cell's keys. Only a confidential cell (D8) removes the live path.

## Decisions

### D1. One cell root key per cell; every content key hangs under it

A **content key** is a key that decrypts the cell's content without any other tenant-held key. A per-cell **infrastructure key** decrypts only the ciphertext of a tenant-held inner layer, so it alone opens no content. Only a tenant-locked cell has an infrastructure key (D3).

cellctl mints a 256-bit cell root key for each cell. Every content key is derived from it or wrapped by it:

- the volume passphrase, in standard mode;
- the inner-layer key, in tenant-locked mode;
- the restic repository password;
- the media key bundle of `add-cloud-media-store`;
- any later key that decrypts content.

Wrapping uses AES-256-GCM, with the cell ID, purpose and version as additional data. This is the shape that backup keys already use. A tenant-locked cell's outer volume passphrase is an infrastructure key; cellctl keeps it per cell, wrapped under the platform master key, as in standard mode.

**Standard mode.** cellctl stores the cell root key wrapped under the platform master key in the control database. The operator can therefore unwrap it, and the privacy copy says so (D7). Per-cell keys still bring three gains:
- one leaked volume passphrase or restic password exposes one cell;
- deletion expires a key that only one cell used (D2);
- the tenant lock (D3) and a later TEE (D8) re-root one key per cell, not every content key.

**Hetzner volumes.** The StorageClass names the volume Secret through the external provisioner's templated node-publish parameters, so each cell's volume reads a Secret in its own namespace. The pinned hcloud CSI driver reads `encryption-passphrase` from the NodePublish Secret (`infra/helm/platform/HCLOUD_CSI_PROVENANCE.md`). A real-volume create, mount and remount drill must pass before any cell moves, as that file requires.

**Existing volumes.** A LUKS keyslot change alone keeps a volume's master key. The pinned driver formats volumes as LUKS1 (`internal/volumes/cryptsetup.go`, tag `v2.21.1`), and cryptsetup re-encrypts LUKS1 only offline. Migration therefore reuses the existing relocation path of `move-cloud-cells-to-local-storage` (its D4):
1. stop the cell under a hold and take a fresh backup;
2. create a new volume under the cell's own passphrase, which gives it a new LUKS master key;
3. restore into it, verify recall, governance status and a governed write;
4. start the cell, and delete the old volume after a soak.

The downtime is one backup plus one restore, measured per size class in task 1.4. A cell counts as migrated once it runs on the new volume.

**Local storage.** `move-cloud-cells-to-local-storage` encrypts at the node level and calls per-tenant keys a non-goal (its D2). That non-goal is superseded. TopoLVM has no per-volume encryption, so a per-volume LUKS2 layer under the cell's passphrase needs its own integration. Task 1.6 names that integration, its maintained basis or custom part, and its owner before the cutover. Until it ships, a cell on local storage does not meet the per-cell key requirement, and the privacy copy may not claim it for that cell.

**Content scratch.** Today a cell also writes content outside its volume. The runtime's `TMPDIR`, media job scratch (video frames, OCR images, transcripts) and import and export staging sit on disk-backed emptyDirs on the node, and Hetzner Cloud node disks carry no encryption. Every content-bearing scratch path therefore moves onto the cell's own encrypted volume, inside the inner layer when the cell is tenant-locked, or into a memory-backed emptyDir within the cell's memory limit. The same rule covers core dumps and swap: cell processes write no core dumps, and node swap is off or encrypted. A memory-backed emptyDir survives a container crash, so a crash-locked cell restarts in a new pod. Until all of this holds for a cell, the per-cell encryption claim and the tenant-lock claim are not claimable for it. Task 1.7 inventories the paths and moves them.

### D2. Deleting a cell destroys its live keys; platform backup copies expire by a stated date

Cell deletion destroys every live copy of the cell root key, of the content keys under it and of any infrastructure key: control-database rows, cell Secrets and the tenant's wraps.

Platform backups still hold earlier copies:
- control-database backups hold the platform-wrapped cell root key;
- cluster datastore snapshots hold the cell's Secrets;
- the backup bucket holds hidden object versions until its lifecycle deletes them.

These expire with their retention, and the platform master key alone cannot revoke them. The deletion record therefore carries `keys_expire_at`: the deletion time plus the longest of those retentions. cellctl reports the keys destroyed only after that time has passed. Until then, status says which platform backups still hold copies. The deletion runbook (`docs/runbooks/cloud-cell-deletion.md`) already says that snapshots retain deleted Secrets; it gains the date.

A shorter window needs master-key epoch rotation: re-wrap every live root key under a new master key, then destroy the old key and every escrowed copy. That is deferred to its own change, because it touches every cell and the escrow procedure.

### D3. The tenant lock is opt-in and leaves no platform copy of its keys

**The invariant.** For a tenant-locked cell, no copy of its cell root key or of any content key (D1) is written to:
- the cluster datastore, which includes every Secret;
- the control database, except the tenant's own wraps;
- a log;
- any platform backup.

The CSI node-publish Secret therefore cannot carry a content key. A Secret deleted right after the mount still reaches datastore history and snapshots. The outer volume passphrase is an infrastructure key: it stays in the node-publish Secret as in standard mode, because it alone yields only the inner layer's ciphertext. The cell's content scratch lives inside the inner layer or in memory (D1, "Content scratch").

**The key channel** is chosen by task 5.1 from two candidates. Both keep the outer CSI LUKS layer under the cell's per-cell infrastructure key, and add an inner layer under a content key that only the tenant's wrap releases:

1. A **node-local unlock agent** receives the key wrapped to its own ephemeral key, over a channel outside the Kubernetes API. It opens an inner LUKS2 layer on the cell's volume and keeps the key only in kernel dm-crypt state and its own memory, until the open completes.
2. An **in-pod FUSE encryption layer** (for example, gocryptfs) holds the key in the runtime's memory.

The criteria are the privileges each needs, the SQLite write latency measured on a real cell, and maintenance cost. cellctl does not connect to cells (the `cloud-cell` contract), so the key reaches a cell through the gateway's existing cell route or through the node agent. Backups and media jobs of an unlocked tenant-locked cell get their content keys through the same channel, never a Secret; task 5.1 covers that path too.

**Enabling the lock**
- The tenant's client generates a recovery code. A passkey adds a second wrap where the platform supports the WebAuthn PRF extension. Each derives a key-encryption key through HKDF, and the cell root key is wrapped under each.
- The tenant confirms that they saved the recovery code. Only then does cellctl destroy the platform-wrapped copy.
- The recovery code is mandatory, because PRF support across browsers and password managers is uneven (task 5.1).
- A new cell can start tenant-locked.
- **An existing cell converts with new keys.** It gets a new cell root key, new content keys and a new infrastructure key. Its data moves into the new inner layer through the restore path, a new backup repository starts, and stored media is re-encrypted under the new media key. The old repository and old media objects are deleted after the new ones verify.

  Earlier keys still sit in platform backups (D2). So does the platform-wrapped copy that every cell holds until its tenant confirms the recovery code, a new cell included. Status therefore shows `lock_complete_at` for every tenant-locked cell: the time the last platform copy was destroyed, plus the longest platform backup retention. The privacy copy for that cell states the date.

**Unlocking.** The control plane serves the tenant's client the wrapped cell root key. The client unwraps it locally and wraps it to the chosen channel's ephemeral key. That ephemeral key is bound to the cell, the operation, a one-use attempt ID and an expiry. Because TLS ends on the operator's node, this flow protects the key against storage, logs and automation. It does not protect against root on that node, and the threat model says so.

**Planned restarts** (upgrade, rollout, a hold that stops the cell) hand the key from the running instance to its replacement through the same channel. A handover:
- is one per cell at a time;
- expires within a configured bound;
- is bound to the replacement instance's ephemeral key;
- is never written to durable state.

A handover that expires or fails leaves the cell locked, not unlocked.

**Unplanned stops:** a crash, an eviction or a node failure locks the cell. While a cell is locked:
- the gateway answers its requests from cellctl's recorded state, which works with no runtime, with a coded `CELL_LOCKED` state that names the unlock route. `CELL_LOCKED` is distinct from the retryable `CELL_WAKING` of `add-cloud-cell-sleep`;
- backups, indexing and media jobs do not run, and report `locked`, not `failed`;
- content-free health, resource and lifecycle status keep working;
- a restore or relocation waits for the tenant's unlock;
- a lost recovery code and a lost passkey mean lost data, and the product says so before the tenant enables the lock.

**Sleep.** A tenant-locked cell never sleeps. Sleep drops the in-memory key, and the sleep contract promises automatic wakes, backups of sleeping cells and upgrades while asleep, none of which a locked cell can honour. Whichever change becomes canonical first, `add-cloud-cell-sleep` treats tenant-locked cells as set never to sleep (task 5.4). Each tenant-locked cell therefore holds its idle memory permanently, and capacity counts it as awake.

**Leaving the lock.** While unlocked, the tenant may return to standard mode. cellctl then stores the platform-wrapped copy again.

The unlock user interface lives in the control plane, in a separate repository. This repository owns the cellctl, gateway and runtime halves and the wire contract.

### D4. The access record retains what its sources submit

A maintained log shipper, Vector or fluent-bit, chosen in task 2.1, ships these events off the server node within five minutes:
- K3s audit events for exec, attach, port-forward, proxy and ephemeral containers;
- Secret get, list and watch;
- every create, update or delete of RBAC objects, admission policies and their bindings;
- certificate signing requests.

The same shipper sends node session events: SSH logins with the key or certificate identity, sudo, and the end of each session.

**Content.** Request URIs keep their path but lose their query parameters, because an exec request's query carries the command line, which can name tenant content. Task 2.2 checks every shipped field, including the StatefulSet bodies that the audit policy records, for Secret values and content. That check is older debt, which off-node retention makes urgent.

**Destination.** One B2 bucket whose default retention uses Object Lock in compliance mode. B2's documentation (read 2026-10-26) says that no user, Backblaze support included, can delete or shorten a locked object version before its retention ends. Account closure and non-payment were not checked. Retention is a configured value with a floor of one year. The nodes' credential may write but not delete.

**Heartbeat.** The shipper writes a heartbeat object at a configured interval. A missing heartbeat is a gap, and the tenant access log shows it.

**What the record proves.** It proves that a received event exists, when it was received, and when the shipper went silent. It does not prove completeness. Root on a node controls the audit log, the session log and the shipper. It can omit, alter or forge events before they leave the node while heartbeats continue. The node's write credential can also add fabricated objects. The record shows a node session, not every read made inside it. The privacy copy carries this limit beside the claim (D7).

The audit policy needs one addition. Connect-subresource and Secret events stay at the Metadata level or above, whatever later policy edits do, and a test of the rendered policy pins this.

### D5. The tenant access log shows what the record received

cellctl derives each cell's access log from the record into the control database, and the control plane serves it. The log is therefore available while the cell is locked, stopped or deleted until its retention ends. Each entry is one received event or one gap:

- a connect or Secret event in the cell's namespace;
- a node session on the node that hosted the cell at that time, from cellctl's placement history;
- a break-glass credential issued while the cell ran;
- an image or configuration change to cellctl, which derives this log;
- a gap in the heartbeat.

Entries appear within ten minutes of receipt. An entry shows time, identity tier (routine, deploy, admin, or service for a credential that a running service holds, such as cellctl's or the CSI driver's), action, target and, where given, the stated reason. It shows no content, and renders every field as text. The view states the D4 limit: it lists what the record received.

### D6. Three access tiers; admin capabilities need the operator's presence

1. **Routine.** The everyday read-only identity (`exomem-operator-read`) works from the operator's own machine over the private network. Its kubeconfig lives there, not on any node. Routine procedures need no node login. Agents keep pod status, events, endpoints, policies, resource use and content-free logs.
2. **Deploy.** A new identity can only change container images:
   - of cells, through cellctl release attempts;
   - of named platform workloads, through an image-only patch.

   An admission rule compares the old and new objects and permits only image-field changes. A maintained verifier (Sigstore policy-controller or Kyverno, chosen in task 4.1) accepts an image only when it carries a GitHub artifact attestation. The attestation must be signed by this repository's release workflow on a release ref, for that workload's image family. The release workflow already produces these attestations for the Cloud and cellctl images. Registry write access alone cannot produce one, so a pushed image fails.

   A **release ref** is a `v*` tag that only the release App can create, move or delete, on a commit that `main` contains. Today neither holds. Tags carry no rule, and the App's private key is a repository-level Actions secret that any workflow on any branch can read. A holder of repository write could therefore push a branch workflow that mints the App's token, tag an unreviewed commit, and dispatch the release workflow on that tag. The dispatched run executes the tagged commit's own workflow file and attests with the genuine release identity. Task 4.6 closes the path:
   - a tag ruleset restricts creation, updates and deletion of `v*` tags, and admits only the release App as the bypass actor, never GitHub Actions, whose token every workflow holds. Restricting updates matters: otherwise a writer could move an old tag to its own commit;
   - the App's private key moves into a protected environment whose deployment policy admits only `main`, so only workflows running on `main` can mint its token;
   - the release workflow also refuses a tag whose commit `main` does not contain. That check guards against mistakes only, because an attacker's dispatched run would execute the attacker's workflow file.

   `main` already accepts changes only through its required checks. Repository administration (rulesets, branch protection, environments, Actions secrets) is admin-tier, so the GitHub credentials that agents hold carry no administration permission.

   The identity has no exec, attach, port-forward, proxy or ephemeral containers. It cannot read Secrets, or change RBAC, admission policy, volumes or Secrets. A full chart upgrade installs RBAC and admission policy, so no scoped identity can run it; it is an admin action.
3. **Admin.** Every credential that can yield one of the following is admin-tier:
   - node root;
   - cluster administration;
   - the cluster datastore or its snapshots;
   - provider control of a node (API token, rescue, console);
   - escrow decryption;
   - registry write.

   That covers:
   - node SSH keys;
   - the break-glass identity;
   - the K3s server token and CA material;
   - snapshot restore credentials;
   - the provider API token;
   - Terraform state and its backend credentials;
   - the SOPS/age escrow identity;
   - registry and package write tokens;
   - any machine-accessible secret store entry that holds one of these.

   **Custody of the admin tier.** The presence check covers the operator and recovery credentials that sit outside the running services: SSH keys, the operator's provider API token, Terraform state and its backend credentials, snapshot restore credentials, escrowed copies of the K3s server token and CA material, and the escrow identity.
   - Node SSH accepts only OpenSSH `-sk` keys whose every login needs a touch. A certificate, if used, certifies an `-sk` key, so its lifetime never replaces the touch.
   - Break-glass is minted only inside such a session.
   - The other operator and recovery credentials are encrypted at rest to a presence-gated age identity, for example a FIDO2 age plugin, which needs a touch for each decryption.
   - The provider account itself sits behind the operator's own login with a hardware second factor.
   - No routine-agent environment and no secret store that the routine or deploy identities can read holds an admin credential in plaintext.

   **Two bounded exemptions.** Some admin capabilities must run unattended, so the rule names them instead of breaking them:
   - **The release publisher.** The repository's release workflow pushes images with its run-scoped token, which carries package write. It runs only on release refs, the token expires with the run, and the release attestation records every image it publishes. That provenance is exactly what the deploy tier checks.
   - **Service credentials inside the runtime.** Running services keep the credentials they need: the CSI controller's provider token, the K3s server token on the server node, and cellctl's wrapping keys. Each stays in the runtime, reachable only through node root or cluster administration, which are themselves admin-tier.

   Task 4.5 inventories every admin credential, its permissions, and which side of this boundary it sits on, and proves the move.

   **Limits.** The provider account owner can always regain control of the servers through the provider console. That path is the operator's own login, outside the reach of agents, and the design states it rather than claim to gate it. Passwordless sudo stays, but only behind a presence-checked login.

This is the one mechanism that stops adversary 1 before the fact. A presence check needs a human by design: its purpose is to keep automation that holds the operator's credentials away from content. A wrongly firing check costs the operator a touch for each admin session, Terraform run or escrow decryption. A missing or incompatible authenticator blocks administration until it is available, and the operator pays that delay. A skipped check would cost an unrecorded read of a tenant's vault. The exemptions keep releases and running services out of that cost.

The remaining agent path is code. An agent that can merge to `main` can ship code that leaks content in the next release. That path leaves a public pull request, commit and release, and the required checks run on it, but on this solo-maintained repository no second person reviews it. It is a stated limit, disclosed beside the claims (D7).

### D7. Claims follow the mechanisms in force, per mode

The privacy copy may claim a property only after the requirement that provides it runs in production for the cell in question.

**Standard mode**, once D1, D4, D5 and D6 run:
- routine and deploy credentials cannot reach your content, and nobody sees it by accident;
- reaching content needs the operator's physical presence;
- access through the cluster API and node logins is recorded off the server, where received events cannot be erased, and is shown to you;
- your cell's data is encrypted under keys for your cell alone;
- deleting your cell destroys those keys, and copies in platform backups expire by the date shown.

**Tenant-locked mode** adds, from `lock_complete_at`: stored data and backups cannot be decrypted without your key.

Disclosures, beside the claims:
- In standard mode, the operator holds your cell's keys on the serving infrastructure and could deliberately read content.
- Root on a node can suppress or forge events before they leave it, so the record shows what it received, not everything that happened.
- In tenant-locked mode, while your cell is unlocked, a deliberate attack on the node that runs it can still reach content.
- A lost recovery code and passkey means lost data.
- The edge and gateway see requests and responses in transit.
- Code merged to the repository can reach a release, so an agent or attacker with merge rights could ship code that reads content; that path is public and permanent.
- An owner export's archive sits in platform storage, readable by the operator, until the export expires (its configured retention, 24 hours by default) or a newer export replaces it.

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

**Availability, read from provider pages on 2026-10-26** (sources below):

| Option | What the provider states | Price excl. VAT |
|---|---|---|
| Hetzner dedicated (EPYC 9454P: AX162; Dell DX182 with iDRAC9 Enterprise) | No SEV-SNP or TDX statement. Customer iDRAC on Dell models; a KVM console on others. Whether SNP can be enabled is unverified. | AX162 €614/mo + €304 setup; DX182 €514–724/mo + €359 setup |
| Hetzner Cloud | No SEV-SNP or TDX found | — |
| Scaleway Elastic Metal | SEV-SNP "supported on … All new AMD-based Elastic Metal offers" (docs reviewed 2026-09-11); no customer BIOS changes | EM-I120E (EPYC 8024P, 64 GB) €134.99/mo or €0.37/h |
| IONOS Confidential VM | SEV-SNP on EPYC Turin, Frankfurt; customer-run attestation | €0.047 per core-hour + €0.005 per GB-hour |
| OVHcloud Scale 2026 | "supports AMD SEV technology"; SNP not named | not stated |
| GCP, Azure, AWS | SNP in EU regions (GCP N2D, Azure DCasv5, AWS eu-west-1 shared or Dedicated Hosts) | Azure DC4as_v5 West Europe $0.208/h; others not read |

**Cost estimate, not measured.** A confidential VM per cell at about 3 GiB puts about 20 cells on a 64 GB host. On the EM-I120E that is about €7 per cell per month. For comparison, the repository's Terraform prices the CX33 server type at €8.99 a month (`infra/terraform/foundation/outputs.tf`). That excludes volumes and object storage, so it is not a full per-cell cost.

**Limits to state:**
- A software-only malicious host breaks SEV-SNP on unpatched firmware (Fabricked, USENIX Security 2026; AMD-SB-3034). Key release must therefore require a minimum TCB. Whether AMD's fix raises the attested TCB is unverified.
- A low-cost memory interposer with host control breaks TDX and weakens SEV-SNP integrity (DDRop, CCS 2026). It needs physical access.

**Trigger.** A follow-up change specifies confidential cells when either condition holds:
- a confidential host costs no more per cell than the standard fleet at the cell count then served;
- a provider offers SEV-SNP or TDX at a comparable per-cell price.

Task 7 checks the Scaleway row on a rented host. The other rows stay as read, with their dates.

**Sources (read 2026-10-26):**
- Hetzner:
  - <https://www.hetzner.com/dedicated-rootserver/ax162/>
  - <https://www.hetzner.com/dedicated-rootserver/dx182/>
  - <https://www.hetzner.com/dedicated-rootserver/matrix-dell/>
  - <https://docs.hetzner.com/robot/dedicated-server/server-lines/dx-server/>
  - <https://docs.hetzner.com/robot/dedicated-server/maintainance/kvm-console/>
- Scaleway:
  - <https://www.scaleway.com/en/docs/elastic-metal/reference-content/elastic-metal-security-features/>
  - <https://www.scaleway.com/en/pricing/elastic-metal/>
- IONOS:
  - <https://cloud.ionos.de/compute/confidential-compute>
  - <https://docs.ionos.com/cloud/release-notes/september-2026>
- OVHcloud: <https://corporate.ovhcloud.com/en/newsroom/news/ovhcloud-bare-metal-2026-amd/>
- GCP: <https://docs.cloud.google.com/confidential-computing/confidential-vm/docs/supported-configurations>
- AWS: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/sev-snp.html>
- Azure: the Retail Prices API, `prices.azure.com`, for `Standard_DC4as_v5` in `westeurope`
- Fabricked: <https://www.shwetashinde.org/publications/fabricked_usenix26.pdf>
- DDRop: <https://research.google/pubs/ddrop-generic-memory-interposer-attacks-on-confidential-vms-by-dropping-ddr5-writes/>
- B2 Object Lock: <https://www.backblaze.com/docs/cloud-storage-object-lock>

## Reuse

| Need | Maintained option used | Custom part, and why |
|---|---|---|
| Per-volume encryption | hcloud CSI LUKS with templated node-publish Secrets | None |
| Migration to per-cell keys | The existing relocation path (backup, new volume, restore) | None |
| Retained record | B2 Object Lock in compliance mode; Vector or fluent-bit | The heartbeat writer, a few lines of configuration |
| Presence check | OpenSSH `-sk` keys; a FIDO2 age plugin for escrowed admin credentials | None |
| Image provenance | GitHub artifact attestations from the existing release workflow; Sigstore policy-controller or Kyverno | The admission policy |
| Image-only deploys | ValidatingAdmissionPolicy with CEL | The rule itself |
| Tenant lock | WebAuthn PRF, HKDF, AES-GCM; LUKS2 or gocryptfs for the inner layer | The unlock and handover channel, because no maintained component binds a user-held key to a volume without the Kubernetes API |
| Access log | — | Its derivation from the record, which is cell-specific |
| Confidential cells (parked) | Kata Confidential Containers, snphost and snpguest, the AMD key distribution service | Deferred |

## Risks / Trade-offs

- **Lock-out.** A tenant-locked cell stops serving after a crash until its tenant returns. Mitigation: the lock is opt-in, the warning comes first, planned restarts hand over, and the gateway answers with a coded route.
- **Lost keys.** A lost recovery code and passkey lose the vault. Mitigation: the recovery code is mandatory, and the client verifies that it was saved before the platform copy is destroyed.
- **Locked cells cost density.** A tenant-locked cell never sleeps, so it holds its idle memory permanently.
- **Retention windows.** Deletion and lock conversion complete only after platform backup retention. Mitigation: the date is shown; master-key epoch rotation can shorten it later.
- **Shipper outage.** The record shows a gap that is not an attack. Mitigation: a gap is labelled as a gap, never as an access, and an operator alert fires on a missing heartbeat. A late upload never blocks service.
- **Presence friction.** Full chart upgrades, break-glass, Terraform runs and escrow decryption need the operator. Mitigation: attested image rollouts are touch-free, and routine checks need no node login.
- **Migration downtime.** Each existing cell stops for one backup and one restore. Mitigation: one cell at a time, owner first, with the old volume kept until the soak ends.

## Migration Plan

1. Run the early checks: templated Secrets, the driver's LUKS format, image provenance admission, the authenticator path, the tenant-lock key channel and PRF coverage.
2. Ship per-cell keys. New cells get them first. Existing cells migrate one at a time, owner first, through the relocation path. Remove the shared passphrase once no volume uses it.
3. Ship the access record, the tenant access log and the access tiers together, because the standard-mode claim needs all three. Move the admin credentials into presence-gated custody in the same batch.
4. Ship the tenant lock.
5. Change the privacy copy for each property as it comes into force.

**Rollback.**
- Before the shared passphrase is removed, a migrated cell can return to its retained old volume.
- After old volumes and keys are destroyed, no rollback restores them. That is the custody promise; restores come from the cell's own backups.
- The record and the access tiers are additive until node SSH stops accepting older keys.
- The tenant lock is per cell, and a tenant can leave it while unlocked.

## Open Questions

- Which component images can change through image-only patches, and which verifier enforces release attestations (task 4.1).
- Which FIDO2 authenticator works from the operator's workstation, and whether SOPS accepts a FIDO2 age plugin identity (tasks 4.2 and 4.5).
- Which key channel the tenant lock uses: a node-local unlock agent or an in-pod FUSE layer (task 5.1).
- Whether AMD's Fabricked fix raises the attested TCB (task 7).
